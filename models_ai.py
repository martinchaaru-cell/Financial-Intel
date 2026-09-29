"""Tables for the AI extraction module (ai_extract.py). New tables only - db.create_all()
creates them on startup, so no migration is needed for an existing database."""
from datetime import datetime
import json

from models import db


class AIExtractionJob(db.Model):
    __tablename__ = 'ai_extraction_jobs'
    id = db.Column(db.Integer, primary_key=True)
    mode = db.Column(db.String(10), nullable=False)          # 'batch' | 'direct'
    model = db.Column(db.String(60))
    batch_id = db.Column(db.String(80))
    status = db.Column(db.String(20), default='submitted')   # submitted | ended | failed | canceled
    n_items = db.Column(db.Integer, default=0)
    est_cost_usd = db.Column(db.Float)
    actual_cost_usd = db.Column(db.Float)
    input_tokens = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime)

    def to_dict(self):
        return {'id': self.id, 'mode': self.mode, 'model': self.model, 'batch_id': self.batch_id, 'status': self.status,
                'n_items': self.n_items, 'est_cost_usd': self.est_cost_usd, 'actual_cost_usd': self.actual_cost_usd,
                'input_tokens': self.input_tokens, 'output_tokens': self.output_tokens, 'error': self.error,
                'created_at': self.created_at.isoformat() if self.created_at else None,
                'finished_at': self.finished_at.isoformat() if self.finished_at else None}


class AIExtractionItem(db.Model):
    """One (report, task) unit of work: the relevant pages' text is stored at upload time,
    because the uploaded PDF itself is not kept."""
    __tablename__ = 'ai_extraction_items'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    fiscal_year = db.Column(db.String(20), nullable=False)
    source_document_id = db.Column(db.Integer)
    filename = db.Column(db.String(255))
    task = db.Column(db.String(20), nullable=False)          # board | committees | pay
    pages_json = db.Column(db.Text)                          # [page numbers sent]
    page_text = db.Column(db.Text)                           # {"113": "...layout text..."}
    status = db.Column(db.String(20), default='pending')     # pending | submitted | done | failed | applied | discarded
    job_id = db.Column(db.Integer, db.ForeignKey('ai_extraction_jobs.id'))
    custom_id = db.Column(db.String(80))
    result_json = db.Column(db.Text)
    verification_json = db.Column(db.Text)
    usage_json = db.Column(db.Text)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    applied_at = db.Column(db.DateTime)

    def pages(self):
        return json.loads(self.page_text) if self.page_text else {}

    def _meta(self):
        raw = json.loads(self.pages_json) if self.pages_json else []
        return raw if isinstance(raw, dict) else {'pages': raw, 'gaps': []}

    def gaps(self):
        """What the rule-based readers could NOT fill for this task - the only things the AI is asked for."""
        return self._meta().get('gaps', [])

    def to_dict(self, full=False):
        v = json.loads(self.verification_json) if self.verification_json else None
        d = {'id': self.id, 'company_id': self.company_id, 'fiscal_year': self.fiscal_year, 'filename': self.filename,
             'task': self.task, 'status': self.status, 'job_id': self.job_id,
             'pages': self._meta().get('pages', []), 'gaps': self._meta().get('gaps', []),
             'summary': v['summary'] if v else None, 'error': self.error,
             'applied_at': self.applied_at.isoformat() if self.applied_at else None,
             'chars': len(self.page_text or '')}
        if full:
            d['result'] = json.loads(self.result_json) if self.result_json else None
            d['checks'] = v['checks'] if v else []
        return d


class UploadDraft(db.Model):
    """A PDF that has been uploaded and imported straight into the normal tables (Overview,
    Survey Report, everything else) - the import is no longer held back pending approval. This
    row exists so Settings > Review > Uploads still has something to show and confirm: while the
    real import runs, a quick separate preview pass (_run_preview in review_api.py) fills
    preview_json for early context; once the import finishes, source_document_id links this draft
    to the SourceDocument it produced. Approving is then just a confirmation (no re-import).
    Rejecting deletes every row that source document contributed - see review_api.py's
    _delete_document_data - which is how a bad upload is removed from the Survey Report page.
    pdf_bytes is cleared once the import finishes either way; the real data no longer lives here."""
    __tablename__ = 'upload_drafts'
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    sha256 = db.Column(db.String(64), index=True)
    size = db.Column(db.Integer)
    pdf_bytes = db.Column(db.LargeBinary)
    forced_company_id = db.Column(db.Integer)
    status = db.Column(db.String(20), default='processing')   # processing | ready | approved | rejected | discarded | failed
    source_document_id = db.Column(db.Integer)   # set once the real import finishes; None for an old-style
                                                    # draft uploaded before this field existed, or one still importing
    preview_json = db.Column(db.Text)
    result_json = db.Column(db.Text)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime)

    def to_dict(self, full=False):
        pv = json.loads(self.preview_json) if self.preview_json else None
        d = {'id': self.id, 'filename': self.filename, 'size': self.size, 'status': self.status, 'error': self.error,
             'source_document_id': self.source_document_id,
             'created_at': self.created_at.isoformat() if self.created_at else None,
             'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
             'company': (pv or {}).get('company'), 'period': (pv or {}).get('period'),
             'checks_failed': sum(1 for c in (pv or {}).get('checks', []) if not c.get('ok')),
             'warnings': len((pv or {}).get('warnings', []))}
        if full:
            d['preview'] = pv
            d['result'] = json.loads(self.result_json) if self.result_json else None
        return d


class ExtractionIssue(db.Model):
    """An extraction step that raised. Before this table, such failures were swallowed ("non-fatal") and
    the only symptom was an empty tab. Listed on Settings > Review > Issues."""
    __tablename__ = 'extraction_issues'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer)
    fiscal_year = db.Column(db.String(20))
    filename = db.Column(db.String(255))
    step = db.Column(db.String(80))
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {'id': self.id, 'company_id': self.company_id, 'fiscal_year': self.fiscal_year, 'filename': self.filename,
                'step': self.step, 'error': self.error, 'resolved': bool(self.resolved),
                'created_at': self.created_at.isoformat() if self.created_at else None}