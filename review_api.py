"""Settings > Review and everything behind it, kept in its own module so app.py only needs two lines:

    import review_api
    review_api.register(app, require_role, run_upload_job, get_batch_result, match_company)

Routes (all admin-only):
  /api/review/summary                     counts for the tab badges
  /api/review/drafts[...]                 uploads (imported immediately; approve confirms, reject deletes)
  /api/review/conflicts[...]              AI vs stored-value disagreements
  /api/review/issues[...]                 extraction steps that raised (they used to be swallowed silently)
  /api/manual/<company>/<year>[...]       fill in what no reader could extract but the report contains
  /api/tabs/...                           per-tab status and cross-tab reconciliation (tab_modules.py)
  /api/system/schema-check | schema-fix   model columns the live database is missing
"""
import io
import json
import re
import threading
import uuid
from datetime import datetime

from flask import jsonify, request
from sqlalchemy import inspect, text

import review_preview
import tab_modules
from models import (db, Company, FinancialPeriod, FinancialStatement, SourceDocument, PrincipalRisk,
                     ManagementGuidance, MarketDataSnapshot, DirectorRemunerationRow, Committee, CommitteeMember)
from models_ai import UploadDraft, ExtractionIssue, AIExtractionItem
from models_survey import SurveyCompanyData, SurveyDirector, SurveyDirectorBenefit, SurveyBenefitCategory, EvidenceConflict

_app = None
_match_company = None


# ------------------------------------------------------------------ review gate helpers
def record_issue(company_id, fiscal_year, filename, step, exc):
    """Log an extraction step that raised, instead of letting it vanish."""
    try:
        db.session.rollback()
        db.session.add(ExtractionIssue(company_id=company_id, fiscal_year=fiscal_year, filename=filename,
                                       step=step, error=f"{type(exc).__name__}: {exc}"[:1500]))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _run_preview(flask_app, draft_id):
    with flask_app.app_context():
        d = db.session.get(UploadDraft, draft_id)
        if d is None or d.pdf_bytes is None:
            return
        try:
            pv = review_preview.build_preview(d.pdf_bytes, d.filename)
            det = (pv.get('company') or {}).get('detected')
            info = {'detected': det, 'matched': None, 'will_create': False}
            if d.forced_company_id:
                c = db.session.get(Company, d.forced_company_id)
                info.update({'matched': {'id': c.id, 'name': c.name, 'score': 1.0} if c else None, 'forced': True})
            elif det:
                matched, score = _match_company(det)
                companies = Company.query.all()
                cid = None
                if matched:
                    cid = next((c.id for c in companies if (c.ticker or '').lower() == matched['ticker'].lower()
                                or c.name.strip().lower() == matched['name'].strip().lower()), None)
                if not cid:
                    cid = next((c.id for c in companies if c.name.strip().lower() == det.strip().lower()), None)
                if cid:
                    info['matched'] = {'id': cid, 'name': db.session.get(Company, cid).name, 'score': round(score, 2)}
                else:
                    info['will_create'] = True
                    info['new_name'] = (matched or {}).get('name') or det
            pv['company'] = info
            d.preview_json = json.dumps(pv)
            d.status = 'ready'
        except Exception as e:
            flask_app.logger.exception('Upload preview failed')
            d.status, d.error = 'failed', str(e)[:500]
        db.session.commit()


def gate_payloads(flask_app, file_payloads, forced_company_id):
    """Create a Settings > Review > Uploads record for every PDF, but no longer hold anything back:
    ALL files (PDF and otherwise) go on to the normal import immediately, in the same batch. Returns
    (file_payloads, draft_ids) where draft_ids is {filename: draft_id} for the PDFs a draft was
    created for. The caller (upload_documents_batch/_run_upload_batch_job in app.py) links each
    draft to the document it produced once the import finishes - see finish_gated_drafts below.
    A duplicate re-upload of a PDF still mid-review reuses its existing draft rather than creating
    a second one, same as before."""
    import hashlib
    if _gate_off():
        return file_payloads, {}
    draft_ids = {}
    for filename, data in file_payloads:
        if data is None or not data[:5] == b'%PDF-':
            continue
        sha = hashlib.sha256(data).hexdigest()
        dup = UploadDraft.query.filter(UploadDraft.sha256 == sha, UploadDraft.status.in_(('processing', 'ready'))).first()
        if dup is None:
            dup = UploadDraft(filename=filename, sha256=sha, size=len(data), pdf_bytes=data,
                              forced_company_id=forced_company_id, status='processing')
            db.session.add(dup)
            db.session.commit()
            if flask_app.config.get('REVIEW_SYNC_PREVIEW'):
                _run_preview(flask_app, dup.id)
            else:
                threading.Thread(target=_run_preview, args=(flask_app, dup.id), daemon=True).start()
        draft_ids[filename] = dup.id
    return file_payloads, draft_ids


def finish_gated_drafts(draft_ids, results):
    """Call once a batch's real import has finished (results is the same {filename, ok, ...} list
    upload_documents_batch returns to the client). Matches each result back to the draft
    gate_payloads() created for it and records the outcome: on success, status 'ready' plus the
    source_document_id the import produced, so Settings > Review > Uploads can find and (if
    needed) delete that document's data later; on failure, status 'failed' with the error. Either
    way pdf_bytes is cleared - the data now lives in the normal tables (or nowhere, if it failed),
    not in this draft."""
    if not draft_ids:
        return
    for r in results:
        draft_id = draft_ids.get(r.get('filename'))
        if draft_id is None:
            continue
        d = db.session.get(UploadDraft, draft_id)
        if d is None:
            continue
        if r.get('ok'):
            d.status = 'ready'
            d.source_document_id = r.get('source_document_id')
            d.result_json = json.dumps({k: v for k, v in r.items() if k != 'filename'})
        else:
            d.status = 'failed'
            d.error = (r.get('error') or 'import failed')[:500]
        d.pdf_bytes = None
    db.session.commit()


def _delete_document_data(source_document_id):
    """Delete every row a source document contributed - financial statements (and, via cascade,
    their line items), principal risks, management guidance, market data, director pay rows,
    committees (and their members), named directors and their benefit rows - then the
    SourceDocument row itself. A FinancialPeriod left with no statements afterward (this document
    was its only source) is removed too, same convention Cleanup_empty_periods.py already uses.
    This is the actual mechanism behind a reviewer's reject: the report disappears from the
    Survey Report page because its saved data is gone, not because of a hidden flag somewhere."""
    if not source_document_id:
        return
    period_ids = set()
    for fs in FinancialStatement.query.filter_by(source_document_id=source_document_id).all():
        period_ids.add(fs.period_id)
        db.session.delete(fs)                       # cascades to its FinancialLineItem rows
    for c in Committee.query.filter_by(source_document_id=source_document_id).all():
        CommitteeMember.query.filter_by(committee_id=c.id).delete(synchronize_session=False)
        db.session.delete(c)
    PrincipalRisk.query.filter_by(source_document_id=source_document_id).delete(synchronize_session=False)
    ManagementGuidance.query.filter_by(source_document_id=source_document_id).delete(synchronize_session=False)
    MarketDataSnapshot.query.filter_by(source_document_id=source_document_id).delete(synchronize_session=False)
    DirectorRemunerationRow.query.filter_by(source_document_id=source_document_id).delete(synchronize_session=False)
    SurveyDirector.query.filter_by(source_document_id=source_document_id).delete(synchronize_session=False)
    SurveyDirectorBenefit.query.filter_by(source_document_id=source_document_id).delete(synchronize_session=False)
    SurveyBenefitCategory.query.filter_by(source_document_id=source_document_id).delete(synchronize_session=False)
    db.session.flush()
    for period_id in period_ids:
        if FinancialStatement.query.filter_by(period_id=period_id).count() == 0:
            period = db.session.get(FinancialPeriod, period_id)
            if period is not None:
                db.session.delete(period)
    doc = db.session.get(SourceDocument, source_document_id)
    if doc is not None:
        db.session.delete(doc)
    db.session.commit()


def _gate_off():
    import os
    return os.environ.get('FINSIGHT_REVIEW_GATE', '1') == '0'


# ------------------------------------------------------------------------ registration
def register(app, require_role, run_upload_job, get_batch_result, match_company):
    global _app, _match_company
    _app, _match_company = app, match_company
    admin = require_role('admin')

    def route(rule, **kw):
        def deco(fn):
            return app.route(rule, **kw)(admin(fn))
        return deco

    # ---------------------------------------------------------------- summary
    @route('/api/review/summary')
    def review_summary():
        return jsonify({
            'uploads_ready': UploadDraft.query.filter_by(status='ready').count(),
            'uploads_processing': UploadDraft.query.filter_by(status='processing').count(),
            'ai_ready': AIExtractionItem.query.filter_by(status='done').count(),
            'ai_waiting': AIExtractionItem.query.filter(AIExtractionItem.status.in_(('pending', 'failed'))).count(),
            'conflicts_open': EvidenceConflict.query.filter(EvidenceConflict.resolution.is_(None)).count(),
            'issues_open': ExtractionIssue.query.filter_by(resolved=False).count()})

    # ---------------------------------------------------------------- drafts
    @route('/api/review/drafts')
    def review_drafts():
        q = UploadDraft.query
        if request.args.get('status'):
            q = q.filter_by(status=request.args['status'])
        else:
            q = q.filter(UploadDraft.status.in_(('processing', 'ready', 'failed')))
        return jsonify([d.to_dict() for d in q.order_by(UploadDraft.id.desc()).all()])

    @route('/api/review/drafts/<int:draft_id>')
    def review_draft_detail(draft_id):
        return jsonify(UploadDraft.query.get_or_404(draft_id).to_dict(full=True))

    @route('/api/review/drafts/<int:draft_id>/approve', methods=['POST'])
    def review_draft_approve(draft_id):
        """Confirms this upload. If it already has a source_document_id, the real import ran at
        upload time and there is nothing left to do but mark it reviewed. Only an old-style draft
        from before uploads ran immediately (no source_document_id, pdf_bytes still stored) falls
        back to running the import now, for backward compatibility with anything already sitting
        in the queue when this changed."""
        d = UploadDraft.query.get_or_404(draft_id)
        if d.source_document_id:
            d.status, d.reviewed_at = 'approved', datetime.utcnow()
            db.session.commit()
            return jsonify({'draft': d.to_dict(full=True)})
        if d.status not in ('ready', 'failed', 'processing') or d.pdf_bytes is None:
            return jsonify({'error': f'This draft is {d.status}; nothing to approve.'}), 400
        batch_id = f'draft-{d.id}-{uuid.uuid4().hex[:8]}'
        run_upload_job(_app, [(d.filename, d.pdf_bytes)], batch_id, d.forced_company_id, datetime.utcnow())
        outcome = get_batch_result(batch_id) or {}
        result = (outcome.get('results') or [{}])[0]
        db.session.refresh(d)
        if result.get('ok'):
            d.status, d.result_json, d.pdf_bytes, d.error = 'approved', json.dumps(result), None, None
            d.source_document_id = result.get('source_document_id')
            d.reviewed_at = datetime.utcnow()
        else:
            d.status, d.error = 'failed', (result.get('error') or 'import failed')[:500]
        db.session.commit()
        return jsonify({'draft': d.to_dict(full=True), 'result': result}), (200 if result.get('ok') else 422)

    @route('/api/review/drafts/<int:draft_id>/reject', methods=['POST'])
    def review_draft_reject(draft_id):
        """Rejects an already-imported upload: deletes every row its source document contributed
        (see _delete_document_data), which is what removes it from the Survey Report page. For an
        old-style draft that never got a source_document_id (still mid-review, or its import
        failed), there's nothing saved to delete - this just marks it rejected."""
        d = UploadDraft.query.get_or_404(draft_id)
        _delete_document_data(d.source_document_id)
        d.status, d.pdf_bytes, d.reviewed_at = 'rejected', None, datetime.utcnow()
        db.session.commit()
        return jsonify({'draft': d.to_dict()})

    @route('/api/review/drafts/<int:draft_id>/discard', methods=['POST'])
    def review_draft_discard(draft_id):
        d = UploadDraft.query.get_or_404(draft_id)
        d.status, d.pdf_bytes, d.reviewed_at = 'discarded', None, datetime.utcnow()
        db.session.commit()
        return jsonify({'draft': d.to_dict()})

    # ---------------------------------------------------------------- conflicts
    @route('/api/review/conflicts')
    def review_conflicts():
        names = {c.id: c.name for c in Company.query.all()}
        out = []
        for c in EvidenceConflict.query.order_by(EvidenceConflict.resolution.isnot(None), EvidenceConflict.id.desc()).all():
            d = c.to_dict()
            d['company'] = names.get(c.company_id)
            out.append(d)
        return jsonify(out)

    @route('/api/review/conflicts/<int:conflict_id>/resolve', methods=['POST'])
    def review_conflict_resolve(conflict_id):
        c = EvidenceConflict.query.get_or_404(conflict_id)
        choice = (request.get_json(silent=True) or {}).get('resolution')
        if choice not in ('candidate_a', 'candidate_b', 'neither'):
            return jsonify({'error': 'resolution must be candidate_a (keep the stored value), candidate_b (use the other value) or neither'}), 400
        if choice == 'candidate_b':
            row = SurveyCompanyData.query.filter_by(company_id=c.company_id, fiscal_year=c.fiscal_year).first()
            if row is not None and hasattr(row, c.field_name):
                setattr(row, c.field_name, c.candidate_b_value)
                src = dict(row.field_sources or {})
                src[c.field_name] = f"chosen by a reviewer over the stored value: {c.candidate_b_source_text or 'other value'}"[:400]
                row.field_sources = src
        c.resolution, c.resolved_at = choice, datetime.utcnow()
        db.session.commit()
        return jsonify({'conflict': c.to_dict()})

    # ---------------------------------------------------------------- issues
    @route('/api/review/issues')
    def review_issues():
        names = {c.id: c.name for c in Company.query.all()}
        out = []
        for i in ExtractionIssue.query.order_by(ExtractionIssue.resolved, ExtractionIssue.id.desc()).limit(200).all():
            d = i.to_dict()
            d['company'] = names.get(i.company_id)
            out.append(d)
        return jsonify(out)

    @route('/api/review/issues/<int:issue_id>/resolve', methods=['POST'])
    def review_issue_resolve(issue_id):
        i = ExtractionIssue.query.get_or_404(issue_id)
        i.resolved = True
        db.session.commit()
        return jsonify({'issue': i.to_dict()})

    # ---------------------------------------------------------------- tabs
    @route('/api/tabs/status')
    def tabs_status():
        cid = request.args.get('company_id', type=int)
        fy = request.args.get('fiscal_year')
        if not cid or not fy:
            return jsonify({'error': 'company_id and fiscal_year are required'}), 400
        return jsonify({'company_id': cid, 'fiscal_year': fy, 'tabs': tab_modules.tab_status(cid, fy)})

    @route('/api/tabs/<int:company_id>/<fiscal_year>/reconcile', methods=['POST'])
    def tabs_reconcile(company_id, fiscal_year):
        return jsonify(tab_modules.reconcile_tabs(company_id, fiscal_year))

    @route('/api/tabs/reconcile-all', methods=['POST'])
    def tabs_reconcile_all():
        return jsonify(tab_modules.reconcile_all())

    # ---------------------------------------------------------------- manual entry
    def _period_years(company_id):
        years = {p.period_label for p in FinancialPeriod.query.filter_by(company_id=company_id).all()}
        years |= {r.fiscal_year for r in SurveyCompanyData.query.filter_by(company_id=company_id).all()}
        return sorted(years, reverse=True)

    @route('/api/manual/<int:company_id>/years')
    def manual_years(company_id):
        return jsonify({'years': _period_years(company_id)})

    @route('/api/manual/<int:company_id>/<fiscal_year>')
    def manual_snapshot(company_id, fiscal_year):
        Company.query.get_or_404(company_id)
        row = tab_modules.survey_row(company_id, fiscal_year)
        srcs, confs = (row.field_sources or {}, row.field_confidence or {}) if row else ({}, {})
        groups = []
        for mod in tab_modules.TAB_MODULES:
            if mod['metrics']:
                groups.append({'tab': mod['id'], 'label': mod['label'], 'metrics': [
                    {'key': k, 'label': lbl, 'value': getattr(row, k, None) if row else None,
                     'source': srcs.get(k), 'confidence': confs.get(k)} for k, lbl in mod['metrics']]})
        period = FinancialPeriod.query.filter_by(company_id=company_id, period_label=fiscal_year).first()
        pay = DirectorRemunerationRow.query.filter_by(period_id=period.id).order_by(DirectorRemunerationRow.order_index).all() if period else []
        comms = Committee.query.filter_by(period_id=period.id).order_by(Committee.order_index).all() if period else []
        return jsonify({
            'company_id': company_id, 'fiscal_year': fiscal_year, 'unit': row.director_figures_unit if row else None,
            'groups': groups, 'status': tab_modules.tab_status(company_id, fiscal_year),
            'directors': [d.to_dict() for d in SurveyDirector.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).order_by(SurveyDirector.order_index).all()],
            'committees': [{**c.to_dict(), 'members': [m.director_name for m in CommitteeMember.query.filter_by(committee_id=c.id).order_by(CommitteeMember.order_index).all()]} for c in comms],
            'pay_rows': [r.to_dict() for r in pay],
            'ned_benefits': (row.ned_benefits or {}) if row else {}, 'benefit_keys': tab_modules.BENEFIT_KEYS})

    @route('/api/manual/<int:company_id>/<fiscal_year>/metrics', methods=['PUT'])
    def manual_metrics(company_id, fiscal_year):
        """{'fields': {column: number | null}, 'note': str, 'unit': 'units|thousands|millions'}. A value entered
        here is marked reviewed, so neither the rule-based readers nor the AI will ever overwrite it."""
        Company.query.get_or_404(company_id)
        body = request.get_json(silent=True) or {}
        allowed = tab_modules.survey_metric_columns()
        row = tab_modules.survey_row(company_id, fiscal_year, create=True)
        srcs, confs = dict(row.field_sources or {}), dict(row.field_confidence or {})
        status = dict(row.field_review_status or {})
        note = (body.get('note') or '').strip()
        changed = []
        for col, val in (body.get('fields') or {}).items():
            if col not in allowed:
                continue
            if val in (None, ''):
                setattr(row, col, None); srcs.pop(col, None); confs.pop(col, None); status.pop(col, None)
            else:
                try:
                    num = float(str(val).replace(',', ''))
                except ValueError:
                    return jsonify({'error': f'{col}: "{val}" is not a number'}), 400
                setattr(row, col, int(num) if col.endswith('_count') or col in ('board_size', 'board_meetings_per_year', 'committees_per_board') else num)
                srcs[col] = 'entered manually' + (f': {note}' if note else '')
                confs[col] = 1.0
                status[col] = 'corrected'
            changed.append(col)
        if body.get('unit') in ('units', 'thousands', 'millions'):
            row.director_figures_unit = body['unit']
        row.field_sources, row.field_confidence, row.field_review_status = srcs, confs, status
        db.session.commit()
        return jsonify({'changed': changed, 'status': tab_modules.tab_status(company_id, fiscal_year)})

    @route('/api/manual/<int:company_id>/<fiscal_year>/directors', methods=['POST'])
    def manual_director_save(company_id, fiscal_year):
        b = request.get_json(silent=True) or {}
        name = (b.get('director_name') or '').strip()
        if not name:
            return jsonify({'error': 'director_name is required'}), 400
        d = SurveyDirector.query.get(b['id']) if b.get('id') else None
        if d is None:
            n = SurveyDirector.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).count()
            d = SurveyDirector(company_id=company_id, fiscal_year=fiscal_year, order_index=n)
            db.session.add(d)
        d.director_name = name
        d.position = (b.get('position') or None)
        d.role = b.get('role') if b.get('role') in ('executive', 'non_executive', 'unknown') else 'unknown'
        d.gender = b.get('gender') if b.get('gender') in ('Male', 'Female') else None
        d.nationality = (b.get('nationality') or None)
        d.independent = b.get('independent') if isinstance(b.get('independent'), bool) else None
        d.appointed_date = (b.get('appointed_date') or None)
        comm = b.get('committees')
        d.committees = [c.strip() for c in comm.split(',') if c.strip()] if isinstance(comm, str) else (comm or None)
        d.confidence = 1.0
        db.session.commit()
        return jsonify({'director': d.to_dict(), 'status': tab_modules.tab_status(company_id, fiscal_year)})

    @route('/api/manual/directors/<int:director_id>', methods=['DELETE'])
    def manual_director_delete(director_id):
        d = SurveyDirector.query.get_or_404(director_id)
        cid, fy = d.company_id, d.fiscal_year
        db.session.delete(d)
        db.session.commit()
        return jsonify({'status': tab_modules.tab_status(cid, fy)})

    @route('/api/manual/<int:company_id>/<fiscal_year>/committees', methods=['POST'])
    def manual_committee_save(company_id, fiscal_year):
        b = request.get_json(silent=True) or {}
        name = (b.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'name is required'}), 400
        period = tab_modules.ensure_period(company_id, fiscal_year)
        c = Committee.query.get(b['id']) if b.get('id') else None
        if c is None:
            c = Committee(period_id=period.id, source_document_id=None, name=name,
                          order_index=Committee.query.filter_by(period_id=period.id).count())
            db.session.add(c)
            db.session.flush()
        c.name = name
        c.chairperson_name = (b.get('chairperson_name') or None)
        c.meetings_held = int(b['meetings_held']) if str(b.get('meetings_held') or '').strip() != '' else None
        c.attendance_rate = float(b['attendance_rate']) if str(b.get('attendance_rate') or '').strip() != '' else None
        members = b.get('members')
        if isinstance(members, str):
            members = [m.strip() for m in re.split(r"[,\n;]", members) if m.strip()]
        members = members or []
        CommitteeMember.query.filter_by(committee_id=c.id).delete()
        for j, m in enumerate(members):
            db.session.add(CommitteeMember(committee_id=c.id, director_name=m, order_index=j,
                                           role_on_committee='chair' if c.chairperson_name and m.lower() == c.chairperson_name.lower() else 'member'))
        c.member_count = len(members) or None
        c.confidence = 1.0
        db.session.commit()
        return jsonify({'status': tab_modules.tab_status(company_id, fiscal_year)})

    @route('/api/manual/committees/<int:committee_id>', methods=['DELETE'])
    def manual_committee_delete(committee_id):
        c = Committee.query.get_or_404(committee_id)
        period = db.session.get(FinancialPeriod, c.period_id)
        CommitteeMember.query.filter_by(committee_id=c.id).delete()
        db.session.delete(c)
        db.session.commit()
        return jsonify({'status': tab_modules.tab_status(period.company_id, period.period_label)})

    @route('/api/manual/<int:company_id>/<fiscal_year>/pay-rows', methods=['POST'])
    def manual_pay_save(company_id, fiscal_year):
        """A named director's pay as printed. `table_kind='manual'` protects the row from being replaced by a re-import."""
        b = request.get_json(silent=True) or {}
        name = (b.get('director_name') or '').strip()
        if not name:
            return jsonify({'error': 'director_name is required'}), 400
        period = tab_modules.ensure_period(company_id, fiscal_year)
        r = DirectorRemunerationRow.query.get(b['id']) if b.get('id') else None
        if r is None:
            r = DirectorRemunerationRow(period_id=period.id, source_document_id=None, table_kind='manual', is_total_row=False,
                                        director_name=name[:150], order_index=DirectorRemunerationRow.query.filter_by(period_id=period.id).count())
            db.session.add(r)
        r.director_name = name[:150]
        r.role = b.get('role') if b.get('role') in ('executive', 'non_executive') else 'unknown'
        r.is_grand_total = bool(b.get('is_grand_total'))
        try:
            r.total = float(str(b['total']).replace(',', '')) if str(b.get('total') or '').strip() != '' else None
        except ValueError:
            return jsonify({'error': 'total must be a number'}), 400
        comps = b.get('components') or {}
        clean = {}
        for k, v in (comps.items() if isinstance(comps, dict) else []):
            try:
                clean[str(k)[:60]] = float(str(v).replace(',', ''))
            except ValueError:
                continue
        r.components = json.dumps(clean)
        r.page = b.get('page') if isinstance(b.get('page'), int) else None
        r.confidence = 1.0
        db.session.commit()
        return jsonify({'status': tab_modules.tab_status(company_id, fiscal_year)})

    @route('/api/manual/pay-rows/<int:row_id>', methods=['DELETE'])
    def manual_pay_delete(row_id):
        r = DirectorRemunerationRow.query.get_or_404(row_id)
        period = db.session.get(FinancialPeriod, r.period_id)
        db.session.delete(r)
        db.session.commit()
        return jsonify({'status': tab_modules.tab_status(period.company_id, period.period_label)})

    @route('/api/manual/<int:company_id>/<fiscal_year>/benefits', methods=['PUT'])
    def manual_benefits(company_id, fiscal_year):
        """{'ned_benefits': {Key: {'provided': bool, 'detail': str}}} - replaces the NED benefits checklist."""
        b = (request.get_json(silent=True) or {}).get('ned_benefits') or {}
        row = tab_modules.survey_row(company_id, fiscal_year, create=True)
        clean = {k: {'provided': bool(v.get('provided')), 'detail': (v.get('detail') or None)}
                 for k, v in b.items() if k in tab_modules.BENEFIT_KEYS and isinstance(v, dict)}
        row.ned_benefits = clean or None
        srcs, confs = dict(row.field_sources or {}), dict(row.field_confidence or {})
        if clean:
            srcs['ned_benefits'], confs['ned_benefits'] = 'entered manually', 1.0
        else:
            srcs.pop('ned_benefits', None); confs.pop('ned_benefits', None)
        row.field_sources, row.field_confidence = srcs, confs
        db.session.commit()
        return jsonify({'status': tab_modules.tab_status(company_id, fiscal_year)})

    # ---------------------------------------------------------------- schema check
    def _schema_gaps():
        insp = inspect(db.engine)
        existing_tables = set(insp.get_table_names())
        gaps = []
        for table in db.metadata.sorted_tables:
            if table.name not in existing_tables:
                gaps.append({'table': table.name, 'column': None, 'type': None, 'missing_table': True})
                continue
            have = {c['name'] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    gaps.append({'table': table.name, 'column': col.name, 'missing_table': False,
                                 'type': col.type.compile(dialect=db.engine.dialect)})
        return gaps

    @route('/api/system/schema-check')
    def schema_check():
        """Model columns / tables the LIVE database lacks. db.create_all() adds missing tables but never missing
        columns, so a model that advanced past the deployed schema fails on every query or insert that touches the
        new column - which shows up only as an empty tab."""
        gaps = _schema_gaps()
        return jsonify({'ok': not gaps, 'engine': db.engine.dialect.name, 'gaps': gaps})

    @route('/api/system/schema-fix', methods=['POST'])
    def schema_fix():
        """Additive only: CREATE missing tables, ADD missing columns (nullable). Never drops or alters anything."""
        fixed, failed = [], []
        db.create_all()
        for g in _schema_gaps():
            if g['missing_table']:
                continue
            try:
                db.session.execute(text(f'ALTER TABLE "{g["table"]}" ADD COLUMN "{g["column"]}" {g["type"]}'))
                db.session.commit()
                fixed.append(f'{g["table"]}.{g["column"]}')
            except Exception as e:
                db.session.rollback()
                failed.append({'column': f'{g["table"]}.{g["column"]}', 'error': str(e)[:200]})
        return jsonify({'fixed': fixed, 'failed': failed, 'remaining': _schema_gaps()})