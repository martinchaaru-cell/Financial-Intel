"""Extraction report - WHY each survey field was filled, or why it was not.

Three sources feed the report:
  1. The live survey row (SurveyCompanyData): value, source sentence/page, confidence, review state. This exists for every
     file already in the system, so the report works for old uploads straight away.
  2. An import trace (ExtractionTrace), written while a PDF is imported - the only moment the PDF is in memory (the app
     deletes the bytes afterwards). It stores document facts (pages, text layer, two-page spreads, unit), which pages
     mention each field's topic, and what the layout-independent reader decided or rejected.
  3. Upload problems already logged (ExtractionIssue, failed UploadDraft rows).

Scopes: one company + year ("file"), one company (all years), one year (all companies), or everything.
Output: JSON for the page, plus XLSX / CSV downloads.  A PDF can also be "diagnosed" without changing any data
(POST /api/extraction-report/diagnose) to add a trace for a file imported before this module existed.
"""
import csv
import io
import json
import re
import zlib
from datetime import datetime

from flask import Response, jsonify, request

from models import db, Company
from models_survey import SurveyCompanyData
import tab_modules

LOW_CONF = 0.75
_PENDING = {}   # (company_id, fiscal_year) -> decisions made by the dynamic reader during the import in progress


# ============================================================================= model

class ExtractionTrace(db.Model):
    """What was known about one PDF at the moment it was imported (or diagnosed)."""
    __tablename__ = 'extraction_traces'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    fiscal_year = db.Column(db.String(20), index=True)
    source_document_id = db.Column(db.Integer)
    filename = db.Column(db.String(255))
    kind = db.Column(db.String(12), default='import')       # import | diagnose
    doc_json = db.Column(db.Text)                            # document facts
    topics_json = db.Column(db.Text)                         # {field: {'pages': [...], 'snippet': '...'}}
    decisions_json = db.Column(db.Text)                      # dynamic-reader decisions + rejections
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class ExtractionPages(db.Model):
    """The report's own text, page by page (zlib-compressed JSON).  Stored at import / diagnose time because the PDF itself
    is never kept.  It is what lets the report explain - months later, without the annual report to hand - why each field
    passed (the value really is on page N beside the right wording) or failed (what the report does say about the topic)."""
    __tablename__ = 'extraction_pages'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, index=True)
    fiscal_year = db.Column(db.String(20), index=True)
    filename = db.Column(db.String(255))
    n_pages = db.Column(db.Integer)
    pages_z = db.Column(db.LargeBinary)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


def store_pages(company_id, fiscal_year, filename, texts):
    """Replace the stored text for this company + year.  Never raises into the import."""
    try:
        blob = zlib.compress(json.dumps({str(n): t for n, t in texts}, ensure_ascii=False).encode('utf-8'), 6)
        ExtractionPages.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).delete(synchronize_session=False)
        db.session.add(ExtractionPages(company_id=company_id, fiscal_year=fiscal_year, filename=filename,
                                       n_pages=len(texts), pages_z=blob))
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        return False


def load_pages(company_id, fiscal_year):
    """[(page_no, text)] for this company + year, or None when no text was ever stored for it."""
    rec = ExtractionPages.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).order_by(
        ExtractionPages.created_at.desc()).first()
    if rec is None or not rec.pages_z:
        return None
    try:
        d = json.loads(zlib.decompress(rec.pages_z).decode('utf-8'))
        return sorted(((int(k), v) for k, v in d.items()), key=lambda x: x[0])
    except Exception:
        return None


def pages_stored_map():
    """{(company_id, fiscal_year): (n_pages, stored_at_iso)} without reading any blob."""
    out = {}
    for cid, fy, n, ts in db.session.query(ExtractionPages.company_id, ExtractionPages.fiscal_year,
                                           ExtractionPages.n_pages, ExtractionPages.created_at).all():
        out[(cid, fy)] = (n, ts.isoformat() if ts else None)
    return out


# ============================================================================= what each field looks like in a report

_P = {
    'turnover': r'revenue from contracts|total revenue|net sales|turnover|gross (?:earned|written) premium|total income',
    'net_profit': r'profit for the (?:year|period)|profit after tax|net profit|net income|loss for the (?:year|period)',
    'market_cap': r'market capitali[sz]ation|market cap\b',
    'board_size': r'board (?:of directors )?(?:comprises|consists|is (?:made up|composed)|currently)|\b(?:five|six|seven|eight|nine|ten|eleven|twelve|thirteen|\d{1,2}) (?:\(\d+\) )?directors',
    'board_meetings_per_year': r'board (?:met|convened|held)|board meetings?|meetings? of the board',
    'committees_per_board': r'(?:board|standing) committees?',
    'committee_meetings_per_year': r'committee meetings?|committee (?:met|held)',
    'directors_female': r'\bfemale\b|\bwomen\b|gender (?:diversity|balance|split)',
    'directors_male': r'\bmale\b|gender (?:diversity|balance|split)',
    'executive_directors_count': r'executive directors?',
    'non_executive_directors_count': r'non[- ]executive directors?',
    'independent_neds_count': r'independent non[- ]executive|independent directors?',
    'non_independent_neds_count': r'non[- ]independent',
    'neds_kenyan_count': r'nationality|\bkenyan\b',
    'neds_non_kenyan_count': r'nationality|non[- ]kenyan|foreign',
    'avg_age_executive_directors': r'\bage\b|aged \d|\(\d{2}\)|years old',
    'avg_age_non_executive_directors': r'\bage\b|aged \d|\(\d{2}\)|years old',
    'avg_age_independent_neds': r'\bage\b|aged \d|\(\d{2}\)|years old',
    'avg_age_non_independent_neds': r'\bage\b|aged \d|\(\d{2}\)|years old',
    'chairperson_annual_retainer': r'chair(?:man|person)?.{0,60}(?:annual|retainer|fees?)|directors.? fees?',
    'other_ned_annual_retainer': r'annual (?:retainer|fees?)|retainer|directors.? fees?',
    'chairperson_meeting_allowance': r'sitting allowance|per meeting|meeting allowance|attendance allowance',
    'other_ned_meeting_allowance': r'sitting allowance|per meeting|meeting allowance|attendance allowance',
    'executive_director_annual_retainer': r'executive directors?.{0,80}(?:fees?|retainer)',
    'executive_director_meeting_allowance': r'executive directors?.{0,80}(?:sitting|allowance)',
    'committee_chair_annual_retainer': r'committee chair\w*.{0,80}(?:fee|retainer|allowance)',
    'committee_member_annual_retainer': r'committee members?.{0,80}(?:fee|retainer|allowance)',
    'committee_chair_meeting_allowance': r'committee chair\w*.{0,80}(?:sitting|per meeting|allowance)',
    'committee_member_meeting_allowance': r'committee members?.{0,80}(?:sitting|per meeting|allowance)',
    'ceo_monthly_salary': r'\bsalary\b|base pay|basic pay',
    'ceo_monthly_allowances': r'allowances?',
    'ceo_monthly_incentive_bonus': r'bonus|incentive',
    'ceo_monthly_deferred_incentive': r'deferred|long[- ]term incentive|LTIP|restricted share',
    'ceo_monthly_non_cash_benefits': r'non[- ]cash|benefits? in kind|company car|medical',
    'ceo_monthly_pension': r'pension|retirement benefit|provident',
    'ceo_monthly_gratuity': r'gratuity|end of service|severance',
    'ceo_monthly_share_value': r'share[- ]based|share options?|share awards?',
    'ceo_monthly_cost_of_employment': r'total (?:remuneration|compensation)|single figure|total cost',
    'ceo_annual_salary_as_stated': r'\bsalary\b|base pay|basic pay',
    'ceo_monthly_conversion_basis': r'\bsalary\b|base pay',
}
# pages that only count as evidence when they look like the right kind of page
_NEEDS = {
    'turnover': r'statement of (?:profit|comprehensive)|income statement', 'net_profit': r'statement of (?:profit|comprehensive)|income statement',
}
_CEO_NEEDS = r'chief executive|managing director|\bCEO\b|executive director'
_MONEY = r'remunerat|fees|allowance|salary|retainer'
_GOV = r'directors?|board'
_PAGE_FILTER = {}
for _k in _P:
    if _k.startswith('ceo_'):
        _PAGE_FILTER[_k] = [_CEO_NEEDS, r'remunerat|salary|emolument']
    elif 'retainer' in _k or 'allowance' in _k:
        _PAGE_FILTER[_k] = [_MONEY, _GOV]
    elif _k.startswith(('board_', 'committee', 'directors_', 'executive_', 'non_', 'independent', 'neds_', 'avg_age')):
        _PAGE_FILTER[_k] = [_GOV]
for _k, _v in _NEEDS.items():
    _PAGE_FILTER[_k] = [_v]

_TOPIC_NAME = {k: v for k, v in tab_modules.all_metric_labels().items()}
_CORE_KEYS = set(tab_modules.core_metric_labels())      # what the report scores (same as the data-quality score)

# why a field might be absent even when the topic is present, per field family
_HINTS = {
    'market_cap': "It is only read from a 'share information' table that prints 'Market capitalisation (Ksh billion)'; "
                  "a report that gives no such table (or only a share price) leaves it blank.",
    'turnover': "Comes from the parsed financial statements; if the income statement was not parsed (units in thousands, "
                "two-page spreads, or a layout the statement reader does not recognise) it stays empty.",
    'net_profit': "Comes from the parsed financial statements; if the income statement was not parsed it stays empty.",
}


# ============================================================================= analysing a PDF

def _page_texts(pdf_bytes):
    from pdf_parse import _pypdf_or_pdfplumber_page_texts
    return _pypdf_or_pdfplumber_page_texts(pdf_bytes)


def _page_shapes(pdf_bytes):
    try:
        from pypdf import PdfReader
        out = []
        for p in PdfReader(io.BytesIO(pdf_bytes)).pages:
            box = p.mediabox
            out.append((float(box.width), float(box.height)))
        return out
    except Exception:
        return []


def analyze_pdf(pdf_bytes):
    """Pure function (no database): document facts + which pages mention each survey field's topic."""
    pages = _page_texts(pdf_bytes)
    texts = [(n, t or '') for n, t in pages]
    out = analyze_texts(texts, _page_shapes(pdf_bytes))
    out['texts'] = texts
    return out


def analyze_texts(texts, shapes=None):
    """The analysis itself, on already-extracted page text."""
    n_pages = len(texts)
    chars = [len(t) for _, t in texts]
    thin = sum(1 for c in chars if c < 120)
    shapes = shapes or []
    spreads = sum(1 for w, h in shapes if h and w / h > 1.3)
    unit = None
    try:
        from pdf_parse import detect_statement_unit
        unit = detect_statement_unit(texts)
    except Exception:
        pass
    doc = {
        'pages': n_pages, 'text_chars': sum(chars),
        'text_pages_pct': round(100 * (n_pages - thin) / n_pages) if n_pages else 0,
        'thin_pages': thin, 'wide_pages': spreads,
        'looks_like_spreads': bool(n_pages and spreads / n_pages > 0.5),
        'statement_unit': unit if isinstance(unit, str) else (unit or None),
    }
    low = [(n, t, t.lower()) for n, t in texts]
    topics = {}
    for col, pat in _P.items():
        rx = re.compile(pat, re.I)
        filt = [re.compile(f, re.I) for f in _PAGE_FILTER.get(col, [])]
        scored = []
        for n, t, tl in low:
            if filt and not all(f.search(t) for f in filt):
                continue
            hits = list(rx.finditer(t))
            if hits:
                scored.append((len(hits), n, hits[0], t))
        if not scored:
            continue
        if col in _NEEDS:      # prefer the statement page itself over notes that merely mention the words
            scored = [((x[0] + 100) if re.search(r'statements? of (?:profit or loss|comprehensive income)|income statement',
                                                  x[3][:700], re.I) else x[0], x[1], x[2], x[3]) for x in scored]
        scored.sort(key=lambda x: (-x[0], x[1]))
        top = scored[0]
        m, t = top[2], top[3]
        s0, e0 = max(0, m.start() - 70), min(len(t), m.end() + 90)
        snippets = []
        for x in scored[:3]:
            mm, tt = x[2], x[3]
            a0, b0 = max(0, mm.start() - 70), min(len(tt), mm.end() + 90)
            snippets.append({'page': x[1], 'snippet': re.sub(r'\s+', ' ', tt[a0:b0]).strip()})
        topics[col] = {'pages': [x[1] for x in scored[:6]], 'snippet': re.sub(r'\s+', ' ', t[s0:e0]).strip(),
                       'snippets': snippets}
    return {'doc': doc, 'topics': topics}


# ============================================================================= recording

def stash_decisions(company_id, fiscal_year, decisions):
    """Called by the dynamic reader's writer (app._apply_dynamic_survey) during an import."""
    _PENDING[(company_id, fiscal_year)] = decisions


def record_import(company_id, fiscal_year, filename, source_document_id, pdf_bytes, kind='import'):
    """Store the trace for this PDF. Never raises into the import."""
    try:
        a = analyze_pdf(pdf_bytes)
        store_pages(company_id, fiscal_year, filename, a.pop('texts', []) or [])
        dec = _PENDING.pop((company_id, fiscal_year), None) or {}
        db.session.add(ExtractionTrace(
            company_id=company_id, fiscal_year=fiscal_year, source_document_id=source_document_id, filename=filename,
            kind=kind, doc_json=json.dumps(a['doc']), topics_json=json.dumps(a['topics']), decisions_json=json.dumps(dec)))
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        return False


# ============================================================================= evidence (needs the stored report text)

EVIDENCE_MAX_FILES = 8          # bigger scopes load evidence per file on demand (see /api/extraction-report/explain)
_WORDS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve',
          'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen', 'twenty']


def _value_variants(v):
    """The ways a saved value could be printed in a report (counts as digits or words; money in full / thousands /
    millions / billions).  Values are stored in full, but filings print them in their own unit."""
    if isinstance(v, bool):
        return []
    if isinstance(v, str):
        return [v.strip()] if len(v.strip()) >= 3 else []
    try:
        x = float(v)
    except (TypeError, ValueError):
        return []
    out = []

    def add(a):
        if a and a not in out:
            out.append(a)
    for scale in (1, 1e3, 1e6, 1e9):
        y = x / scale
        if x != 0 and abs(y) < 0.05:
            continue
        if abs(y - round(y)) < 1e-9:
            i = int(round(y))
            if scale == 1 and abs(i) < 1000:
                add(str(i))
            elif len(str(abs(i))) >= 3 or scale == 1:
                add(f'{i:,}')
                add(str(i))
        elif scale == 1 and abs(x) < 1000 or len(f'{y:.1f}') >= 3:
            for d in (1, 2):
                t = f'{y:,.{d}f}'
                add(t)
                if t.endswith('0'):
                    add(t.rstrip('0').rstrip('.'))
    if x == int(x) and 0 <= int(x) <= 20:
        add(_WORDS[int(x)])
    return out


def _cited_pages(*sources):
    pages = set()
    for s in sources:
        for m in re.finditer(r'\bpages?\s+(\d+)(?:\s*[-\u2013]\s*(\d+))?', str(s or ''), re.I):
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else a
            pages.update(range(a, min(b, a + 6) + 1))
    return pages


def _find_value_evidence(col, v, texts, cited):
    """Where in the stored report text the saved value shows up next to wording for this field.
    Returns [{'page', 'snippet', 'kind'}] best first ('beside_topic' beats 'on_cited_page')."""
    cands = _value_variants(v)
    if not cands:
        return []
    topic = re.compile(_P[col], re.I) if col in _P else None
    hits = []
    for n, t in texts:
        if not t:
            continue
        for c in cands:
            for m in re.finditer(r'(?<![\d.,])' + re.escape(c) + r'(?!\d)(?!,\d)(?!\.\d)', t, re.I):
                win = t[max(0, m.start() - 220): m.end() + 220]
                near = bool(topic and topic.search(win))
                if not near and n not in cited:
                    continue
                a0, b0 = max(0, m.start() - 90), min(len(t), m.end() + 90)
                hits.append((0 if near else 1, 0 if n in cited else 1, n, re.sub(r'\s+', ' ', t[a0:b0]).strip(),
                             'beside_topic' if near else 'on_cited_page'))
                break
    hits.sort(key=lambda h: (h[0], h[1], h[2]))
    seen, out = set(), []
    for _, _, n, snip, kind in hits:
        if n in seen:
            continue
        seen.add(n)
        out.append({'page': n, 'snippet': snip, 'kind': kind})
        if len(out) >= 2:
            break
    return out


_ACTIONS = {
    'not_in_text': 'If the report discloses it in a chart or image, type it in Report Manual Entry; otherwise it is most likely not disclosed.',
    'topic_found': 'Open the page(s) shown above and type the value in Report Manual Entry, or run AI Backfilling for this company.',
    'rejected': 'Check the rejected value above; if it is right, type it in Report Manual Entry.',
    'scanned': 'This is an image-only PDF - it needs OCR before any reader can use it.',
    'no_trace': 'Run Diagnose once for this file (Extraction report page) - the text is then stored and you never need the PDF again.',
}


def _enrich_item(item, col, raw_value, texts, trace, src, corr_by):
    """Adds `evidence` + a plain-language cross-check (passed) or action (failed) to a report row."""
    item['evidence'], item['verified'] = [], None
    if item['status'] == 'missing':
        t = ((trace or {}).get('topics') or {}).get(col)
        if t:
            item['evidence'] = t.get('snippets') or [{'page': (t.get('pages') or [None])[0], 'snippet': t.get('snippet', ''),
                                                      'kind': 'topic'}]
            for e in item['evidence']:
                e.setdefault('kind', 'topic')
        item['action'] = _ACTIONS.get(item['reason_code'], '')
        return
    if texts is None:
        item['reason'] += (' Cross-check: the report text for this file is not stored yet, so this value cannot be checked '
                           'against the report here - run Diagnose once for this file.')
        return
    if item.get('stage', '').startswith('Manual') and not _cited_pages(src):
        item['verified'] = None
        item['reason'] += ' Cross-check: typed by a person, so there is no machine source to compare.'
        return
    ev = _find_value_evidence(col, raw_value, texts, _cited_pages(src))
    item['evidence'] = ev
    if ev:
        item['verified'] = True
        e0 = ev[0]
        where = 'beside wording for this field' if e0['kind'] == 'beside_topic' else 'on the page the reader cited'
        item['reason'] += f" Cross-check passed: the value appears in the report text on page {e0['page']}, {where}."
    else:
        item['verified'] = False
        item['reason'] += (' Cross-check: the value does not appear word-for-word in the report text, so it was derived '
                           '(counted from a table, converted to full amounts or averaged) - it rests on the source shown below.')


def _load_pages_for(rows_key, with_ev, stored):
    return load_pages(*rows_key) if with_ev and rows_key in stored else None


# ============================================================================= building the report

_STAGES = [('from the document scan', 'Layout-independent reader'), ('from the board table', 'Board reader'),
           ('from the attendance table', 'Board reader'), ('from the director roster', 'Board reader'),
           ('from the director pay table', 'Pay-table reader'), ('from the remuneration policy text', 'Remuneration policy reader'),
           ('from the remuneration report', 'Remuneration policy reader'), ('stated in the report', 'Board reader'),
           ('from parsed financial statements', 'Financial statements parser'), ('from AI extraction', 'AI extraction'),
           ('entered manually: form upload', 'Manual entry form'), ('entered manually', 'Manual entry (typed)')]


def _stage(src):
    s = (src or '').strip()
    for prefix, label in _STAGES:
        if s.startswith(prefix):
            return label
    return 'Survey file / other' if s else 'Unknown'


def _fmt(v):
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return v


def _section_of():
    out = {}
    for m in tab_modules.TAB_MODULES:
        for k, _ in list(m['metrics']) + list(m.get('text_metrics', [])):
            out[k] = m['label']
    return out


def _diagnose_missing(col, label, trace, dyn_rej):
    """(reason_code, explanation) for a field with no value."""
    if dyn_rej:
        r = dyn_rej[0]
        where = f" on page {r['page']}" if r.get('page') else ''
        return 'rejected', f"A reader found {r.get('value')!r}{where} but did not keep it: {r['reason']}."
    if trace is None:
        return 'no_trace', ("No import trace exists for this file (it was imported before the report existed). Re-upload the PDF, or use "
                            "'Diagnose a PDF' on this page, to capture why. " + _HINTS.get(col, ''))
    doc = trace['doc']
    if doc.get('text_pages_pct', 100) < 30:
        return 'scanned', (f"Only {doc.get('text_pages_pct')}% of pages have a text layer - this looks like a scan / image PDF, so "
                           f"no reader can see the text. OCR is needed.")
    t = trace['topics'].get(col)
    if not t:
        return 'not_in_text', (f"The report text never mentions '{label}'-type wording, so it is most likely not disclosed "
                               f"(or is shown only inside a chart / image). " + _HINTS.get(col, ''))
    pages = ', '.join(str(p) for p in t['pages'][:5])
    extra = ''
    if doc.get('looks_like_spreads'):
        extra += (' The PDF is laid out as two-page spreads, so text from the left and right pages is interleaved '
                  'line by line - the usual reason sentence and table readers miss values.')
    if col == 'market_cap' or col in ('turnover', 'net_profit'):
        extra += ' ' + _HINTS[col]
    return 'topic_found', (f"The topic appears on page(s) {pages} (e.g. \u201c{t['snippet'][:150]}\u201d) but no reader "
                           f"produced a value from it.{extra}")


def _load_traces():
    """Latest trace per (company_id, fiscal_year) -> parsed dict."""
    latest = {}
    for t in ExtractionTrace.query.order_by(ExtractionTrace.created_at.asc()).all():
        latest[(t.company_id, t.fiscal_year)] = {
            'id': t.id, 'kind': t.kind, 'filename': t.filename, 'created_at': t.created_at.isoformat() if t.created_at else None,
            'doc': json.loads(t.doc_json or '{}'), 'topics': json.loads(t.topics_json or '{}'),
            'decisions': json.loads(t.decisions_json or '{}')}
    return latest


_TABLE_NAMES = {'register': "Directors' register", 'pay_rows': 'Director pay rows', 'committees': 'Committees',
                'ned_benefits': 'NED benefits'}
_TABLE_TOPIC = {'register': 'board_size', 'pay_rows': 'chairperson_annual_retainer', 'committees': 'committees_per_board',
                'ned_benefits': 'other_ned_annual_retainer'}


def _table_items(company_id, fiscal_year, trace, fields):
    """Everything on the Manual Entry form that is NOT one of the 40 survey fields: empty tables, register / committee /
    pay rows with blank cells, NED benefits, unresolved conflicts.  Built from the SAME function the form uses
    (manual_entry_form.collect), so the report and the form can never disagree about what is open.
    Also returns a parity check: survey fields the form lists as missing but this report does not (and the reverse)."""
    try:
        import manual_entry_form as mef
        d = mef.collect(company_id, fiscal_year)
    except Exception:
        return [], None

    def why(table):
        if trace is None:
            return 'No import trace for this file, so the cause is unknown.'
        if trace['doc'].get('text_pages_pct', 100) < 30:
            return 'The PDF has almost no text layer (scan / image), so no reader could see the table.'
        t = trace['topics'].get(_TABLE_TOPIC[table])
        spreads = ' The PDF is laid out as two-page spreads, which interleaves table columns.' if trace['doc'].get('looks_like_spreads') else ''
        if t:
            return f"Related wording appears on page(s) {', '.join(str(p) for p in t['pages'][:5])} but no reader built rows from it.{spreads}"
        return 'The report text has no matching wording, so it is most likely not disclosed.'

    items = []
    empty = set(d['missing_lists'])
    for key in ('register', 'pay_rows', 'committees', 'ned_benefits'):
        if key in empty:
            items.append({'section': _TABLE_NAMES[key], 'item': 'Table is empty', 'status': 'missing', 'reason': why(key)})
    for g in d['director_gaps']:
        items.append({'section': _TABLE_NAMES['register'], 'item': g['name'] or 'Director', 'status': 'missing',
                      'reason': 'Blank: ' + ', '.join(g['missing'])})
    for g in d['committee_gaps']:
        items.append({'section': _TABLE_NAMES['committees'], 'item': g['name'] or 'Committee', 'status': 'missing',
                      'reason': 'Blank: ' + ', '.join(g['missing'])})
    for g in d['pay_gaps']:
        items.append({'section': _TABLE_NAMES['pay_rows'], 'item': g['name'] or 'Director', 'status': 'missing',
                      'reason': 'Row has no total'})
    for c in d['conflicts']:
        items.append({'section': 'Conflicting readings', 'item': c['label'], 'status': 'to_confirm',
                      'reason': f"Reading A {c['a']} (p.{c['a_page'] or '?'}) vs reading B {c['b']} (p.{c['b_page'] or '?'}); no one has chosen."})
    form_missing = {k for _, ms in d['missing_metrics'] for k, _ in ms if k in _CORE_KEYS}
    report_missing = {f['field'] for f in fields if f['status'] == 'missing'}
    return items, {'form_only': sorted(form_missing - report_missing), 'report_only': sorted(report_missing - form_missing),
                   'agree': form_missing == report_missing}


def build_report(company_id=None, fiscal_year=None, evidence=None):
    labels = tab_modules.core_metric_labels()        # the 40 scored fields; extended fields have no reader yet
    section_of = _section_of()
    q = SurveyCompanyData.query
    if company_id:
        q = q.filter_by(company_id=company_id)
    if fiscal_year:
        q = q.filter_by(fiscal_year=fiscal_year)
    rows = q.all()
    companies = {c.id: c for c in Company.query.all()}
    traces = _load_traces()
    stored = pages_stored_map()
    with_ev = (len(rows) <= EVIDENCE_MAX_FILES) if evidence is None else bool(evidence)
    files = []
    for r in sorted(rows, key=lambda r: (companies[r.company_id].name if r.company_id in companies else '', r.fiscal_year or '')):
        c = companies.get(r.company_id)
        trace = traces.get((r.company_id, r.fiscal_year))
        texts = _load_pages_for((r.company_id, r.fiscal_year), with_ev, stored)
        dec = (trace or {}).get('decisions') or {}
        rej_by = {}
        for x in dec.get('rejections', []):
            rej_by.setdefault(x['field'], []).append(x)
        kept_by = {x['field']: x for x in dec.get('decisions', [])}
        conf = r.field_confidence or {}
        srcs = r.field_sources or {}
        review = r.field_review_status or {}
        corr = r.field_review_corrections or {}
        fields = []
        for col, label in labels.items():
            v = getattr(r, col, None)
            has = v is not None and str(v).strip() != ''
            cf = conf.get(col)
            src = srcs.get(col)
            item = {'field': col, 'label': label, 'section': section_of.get(col, ''), 'value': _fmt(v) if has else None,
                    'confidence': cf, 'source': src, 'stage': _stage(src) if has else None,
                    'review': review.get(col), 'corrected_from': (corr.get(col) or {}).get('extracted_value') if col in corr else None}
            if has:
                human = _stage(src).startswith('Manual') or review.get(col) in ('approved', 'corrected')
                if cf is not None and cf < LOW_CONF:
                    item['status'] = 'low_confidence'
                    item['reason_code'] = 'low_confidence'
                    why = [x['reason'] for x in rej_by.get(col, []) if 'lowered' in x['reason'] or 'dropped' in x['reason']]
                    item['reason'] = (f"Value found but confidence is {cf} (< {LOW_CONF}). "
                                      + (why[0] + '.' if why else 'Sentence-derived or cross-check disagreed; a person should confirm it.'))
                else:
                    item['status'] = 'filled'
                    item['reason_code'] = 'filled_manual' if human else 'filled'
                    item['reason'] = ('Entered / confirmed by a person.' if human else f"Read by {item['stage']}.")
                k = kept_by.get(col)
                if k and k.get('decision') != 'written':
                    item['reason'] += f" (Layout-independent reader also had {k.get('value')!r}: {k.get('reason')}.)"
            else:
                item['status'] = 'missing'
                code, why = _diagnose_missing(col, label, trace, rej_by.get(col))
                item['reason_code'], item['reason'] = code, why
                k = kept_by.get(col)
            if with_ev:
                _enrich_item(item, col, v if has else None, texts, trace, src, corr)
            fields.append(item)
        tables, parity = _table_items(r.company_id, r.fiscal_year, trace, fields)
        n = len(fields)
        filled = sum(1 for f in fields if f['status'] == 'filled')
        low = sum(1 for f in fields if f['status'] == 'low_confidence')
        miss = n - filled - low
        files.append({
            'company_id': r.company_id, 'company': c.name if c else f'#{r.company_id}', 'ticker': c.ticker if c else None,
            'fiscal_year': r.fiscal_year, 'source_filename': r.source_filename,
            'uploaded_at': r.uploaded_at.isoformat() if r.uploaded_at else None,
            'trace': ({'created_at': trace['created_at'], 'kind': trace['kind'], 'filename': trace['filename'], 'doc': trace['doc']}
                      if trace else None),
            'text_stored': (r.company_id, r.fiscal_year) in stored,
            'text_pages': (stored.get((r.company_id, r.fiscal_year)) or (None, None))[0],
            'text_stored_at': (stored.get((r.company_id, r.fiscal_year)) or (None, None))[1],
            'evidence_loaded': with_ev,
            'counts': {'filled': filled, 'low_confidence': low, 'missing': miss, 'total': n},
            'score': round(100 * filled / n) if n else 0, 'fields': fields, 'tables': tables, 'parity': parity})

    # companies / years with no survey row at all
    years = sorted({r.fiscal_year for r in SurveyCompanyData.query.all() if r.fiscal_year})
    want_years = [fiscal_year] if fiscal_year else years
    have = {(f['company_id'], f['fiscal_year']) for f in files}
    no_row = []
    for c in sorted(companies.values(), key=lambda c: c.name or ''):
        if company_id and c.id != company_id:
            continue
        for y in want_years:
            if (c.id, y) not in have and not SurveyCompanyData.query.filter_by(company_id=c.id, fiscal_year=y).first():
                no_row.append({'company_id': c.id, 'company': c.name, 'ticker': c.ticker, 'fiscal_year': y,
                               'reason': 'No survey row for this company and year - no report was imported, or the import '
                                         'failed before writing (see Upload problems).'})

    # roll-ups
    by_reason, by_field = {}, {}
    for f in files:
        for it in f['fields']:
            bf = by_field.setdefault(it['field'], {'field': it['field'], 'label': it['label'], 'section': it['section'],
                                                   'filled': 0, 'low_confidence': 0, 'missing': 0, 'reasons': {}})
            bf[it['status']] += 1
            if it['status'] != 'filled':
                by_reason[it['reason_code']] = by_reason.get(it['reason_code'], 0) + 1
                bf['reasons'][it['reason_code']] = bf['reasons'].get(it['reason_code'], 0) + 1
    by_field_list = sorted(by_field.values(), key=lambda b: -(b['missing'] + b['low_confidence']))
    for b in by_field_list:
        tot = b['filled'] + b['low_confidence'] + b['missing']
        b['fail_pct'] = round(100 * (b['missing'] + b['low_confidence']) / tot) if tot else 0
        b['top_reason'] = max(b['reasons'], key=b['reasons'].get) if b['reasons'] else None

    issues, drafts = [], []
    try:
        from models_ai import ExtractionIssue, UploadDraft
        iq = ExtractionIssue.query
        if company_id:
            iq = iq.filter_by(company_id=company_id)
        if fiscal_year:
            iq = iq.filter_by(fiscal_year=fiscal_year)
        issues = [i.to_dict() for i in iq.order_by(ExtractionIssue.created_at.desc()).limit(500).all()]
        for d in UploadDraft.query.filter(UploadDraft.status.in_(['failed', 'rejected', 'discarded'])).order_by(
                UploadDraft.created_at.desc()).limit(200).all():
            drafts.append({'filename': d.filename, 'status': d.status, 'error': d.error,
                           'created_at': d.created_at.isoformat() if d.created_at else None})
    except Exception:
        pass

    return {'generated_at': datetime.utcnow().isoformat(), 'scope': {'company_id': company_id, 'fiscal_year': fiscal_year},
            'coverage': {'files': len(files), 'with_text': sum(1 for f in files if f['text_stored']),
                         'without_text': sum(1 for f in files if not f['text_stored'])},
            'files': files, 'no_survey_row': no_row,
            'rollups': {'by_reason': by_reason, 'by_field': by_field_list},
            'upload_problems': {'extraction_issues': issues, 'failed_uploads': drafts},
            'reason_labels': REASON_LABELS}


REASON_LABELS = {
    'filled': 'Filled by an automatic reader', 'filled_manual': 'Filled / confirmed by a person',
    'low_confidence': 'Filled but low confidence (needs review)',
    'rejected': 'Found by a reader but rejected', 'topic_found': 'Topic is in the report, no reader parsed a value',
    'not_in_text': 'Not mentioned in the report text (likely not disclosed)', 'scanned': 'Scanned / image PDF (no text layer)',
    'no_trace': 'No import trace (older upload) - cause unknown'}


# ============================================================================= downloads

def _scope_name(rep):
    s = rep['scope']
    bits = ['extraction_report']
    if s['company_id']:
        bits.append(next((f['company'] for f in rep['files']), f"company{s['company_id']}"))
    bits.append(s['fiscal_year'] or ('all_years' if s['company_id'] else 'all_years_all_companies'))
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', '_'.join(bits))


def to_csv(rep):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Company', 'Ticker', 'Fiscal year', 'Source file', 'Section', 'Field', 'Status', 'Value', 'Confidence', 'Filled by',
                'Source (page / sentence)', 'Reason code', 'Why'])
    for f in rep['files']:
        for it in f['fields']:
            w.writerow([f['company'], f['ticker'] or '', f['fiscal_year'], f['source_filename'] or '', it['section'], it['label'],
                        it['status'], '' if it['value'] is None else it['value'], '' if it['confidence'] is None else it['confidence'],
                        it['stage'] or '', it['source'] or '', it['reason_code'], it['reason']])
    for f in rep['files']:
        for t in f.get('tables') or []:
            w.writerow([f['company'], f['ticker'] or '', f['fiscal_year'], f['source_filename'] or '', t['section'], t['item'],
                        t['status'], '', '', '', '', 'table', t['reason']])
    for n in rep['no_survey_row']:
        w.writerow([n['company'], n['ticker'] or '', n['fiscal_year'], '', '', '(whole file)', 'no_survey_row', '', '', '', '',
                    'no_row', n['reason']])
    return '\ufeff' + buf.getvalue()


def to_xlsx(rep):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    head_fill = PatternFill('solid', fgColor='1F2A44')
    head_font = Font(bold=True, color='FFFFFF')
    fills = {'filled': 'DFF5E3', 'low_confidence': 'FFF1CC', 'missing': 'FADBD8'}

    def sheet(ws, header, rows, widths, wrap_cols=(), status_col=None):
        ws.append(header)
        for c in ws[1]:
            c.fill, c.font = head_fill, head_font
        for r in rows:
            ws.append(r)
        for i, wd in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = wd
        for row in ws.iter_rows(min_row=2):
            for c in row:
                if c.column in wrap_cols:
                    c.alignment = Alignment(wrap_text=True, vertical='top')
            if status_col:
                f = fills.get(row[status_col - 1].value)
                if f:
                    row[status_col - 1].fill = PatternFill('solid', fgColor=f)
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions

    ws = wb.active
    ws.title = 'Summary'
    rows = []
    for f in rep['files']:
        c, doc = f['counts'], (f['trace'] or {}).get('doc') or {}
        top = {}
        for it in f['fields']:
            if it['status'] != 'filled':
                top[it['reason_code']] = top.get(it['reason_code'], 0) + 1
        main = max(top, key=top.get) if top else ''
        rows.append([f['company'], f['ticker'] or '', f['fiscal_year'], f['source_filename'] or '', f['score'], c['filled'],
                     c['low_confidence'], c['missing'], REASON_LABELS.get(main, main),
                     doc.get('pages', ''), doc.get('text_pages_pct', ''), 'yes' if doc.get('looks_like_spreads') else ('no' if doc else ''),
                     doc.get('statement_unit') or '', (f['trace'] or {}).get('created_at', 'no trace')])
    for n in rep['no_survey_row']:
        rows.append([n['company'], n['ticker'] or '', n['fiscal_year'], '(no file)', 0, 0, 0, 40, 'No survey row', '', '', '', '', ''])
    sheet(ws, ['Company', 'Ticker', 'FY', 'Source file', 'Score %', 'Filled', 'Low confidence', 'Missing', 'Main failure reason',
               'PDF pages', 'Text pages %', 'Two-page spreads', 'Statement unit', 'Trace captured'], rows,
          [30, 9, 9, 38, 9, 8, 14, 9, 50, 10, 12, 15, 14, 24])

    ws = wb.create_sheet('Fields')
    rows = []
    for f in rep['files']:
        for it in f['fields']:
            rows.append([f['company'], f['fiscal_year'], it['section'], it['label'], it['status'],
                         '' if it['value'] is None else it['value'], '' if it['confidence'] is None else it['confidence'],
                         it['stage'] or '', it['source'] or '', REASON_LABELS.get(it['reason_code'], it['reason_code']), it['reason']])
    sheet(ws, ['Company', 'FY', 'Section', 'Field', 'Status', 'Value', 'Confidence', 'Filled by', 'Source (page / sentence)',
               'Reason', 'Why'], rows, [28, 9, 18, 36, 14, 14, 11, 26, 60, 38, 90], wrap_cols=(9, 11), status_col=5)

    ws = wb.create_sheet('Tables & rows')
    rows = [[f['company'], f['fiscal_year'], t['section'], t['item'], t['status'], t['reason']]
            for f in rep['files'] for t in (f.get('tables') or [])]
    sheet(ws, ['Company', 'FY', 'Section', 'Item', 'Status', 'Detail'], rows, [28, 9, 26, 36, 14, 90], wrap_cols=(6,))

    ws = wb.create_sheet('Weakest fields')
    rows = [[b['label'], b['section'], b['filled'], b['low_confidence'], b['missing'], b['fail_pct'],
             REASON_LABELS.get(b['top_reason'], b['top_reason'] or '')] for b in rep['rollups']['by_field']]
    sheet(ws, ['Field', 'Section', 'Filled', 'Low confidence', 'Missing', 'Fail %', 'Most common reason'], rows,
          [38, 20, 9, 15, 9, 9, 55])

    ws = wb.create_sheet('Failure reasons')
    rows = [[REASON_LABELS.get(k, k), v] for k, v in sorted(rep['rollups']['by_reason'].items(), key=lambda x: -x[1])]
    sheet(ws, ['Reason', 'Fields affected'], rows, [60, 16])

    ws = wb.create_sheet('Upload problems')
    rows = [['Step failed', i.get('filename'), i.get('fiscal_year'), i.get('step'), i.get('error'), i.get('created_at'),
             'resolved' if i.get('resolved') else 'open'] for i in rep['upload_problems']['extraction_issues']]
    rows += [['Upload ' + d['status'], d['filename'], '', '', d['error'] or '', d['created_at'], ''] for d in rep['upload_problems']['failed_uploads']]
    sheet(ws, ['Kind', 'File', 'FY', 'Step', 'Error', 'When', 'State'], rows, [18, 40, 9, 32, 90, 22, 10], wrap_cols=(5,))

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ============================================================================= PDF + ZIP

NAVY_HEX, DIM_HEX, LINE_HEX, HEAD_HEX = '#0F2B5B', '#5A6478', '#C9CFDD', '#EEF1F8'
_STATUS_HEX = {'filled': '#1E8E4E', 'low_confidence': '#B8790C', 'missing': '#C0392B', 'to_confirm': '#B8790C'}


def _safe(name):
    return ''.join(c for c in (name or 'file') if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_') or 'file'


def _pdf_styles():
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    ss = getSampleStyleSheet()
    navy, dim = colors.HexColor(NAVY_HEX), colors.HexColor(DIM_HEX)
    return {
        'title': ParagraphStyle('t', parent=ss['Title'], fontName='Helvetica-Bold', fontSize=18, textColor=navy, alignment=0, spaceAfter=2),
        'sub': ParagraphStyle('s', parent=ss['Normal'], fontName='Helvetica', fontSize=9.5, textColor=dim, leading=13),
        'h2': ParagraphStyle('h2', parent=ss['Normal'], fontName='Helvetica-Bold', fontSize=13, textColor=navy, spaceBefore=14, spaceAfter=4),
        'h3': ParagraphStyle('h3', parent=ss['Normal'], fontName='Helvetica-Bold', fontSize=10, textColor=navy, spaceBefore=9, spaceAfter=3),
        'cell': ParagraphStyle('c', parent=ss['Normal'], fontName='Helvetica', fontSize=8, leading=10),
        'head': ParagraphStyle('hd', parent=ss['Normal'], fontName='Helvetica-Bold', fontSize=8, leading=10, textColor=navy),
    }


def _pdf_table(rows, widths, st):
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle
    from xml.sax.saxutils import escape
    data = []
    for i, r in enumerate(rows):
        data.append([c if not isinstance(c, (str, int, float, type(None))) else
                     Paragraph(escape('' if c is None else str(c)), st['head'] if i == 0 else st['cell']) for c in r])
    from reportlab.lib.units import mm
    total = float(sum(widths))
    widths = [w * (172 * mm) / total for w in widths]       # always exactly the printable width
    t = Table(data, colWidths=widths, repeatRows=1, hAlign='LEFT')
    t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor(LINE_HEX)), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                           ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(HEAD_HEX)),
                           ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]))
    return t


def _p(text, style):
    from reportlab.platypus import Paragraph
    from xml.sax.saxutils import escape
    return Paragraph(escape('' if text is None else str(text)), style)


def _doc_facts(f):
    d = (f.get('trace') or {}).get('doc') or {}
    if not d:
        return 'No import trace for this file - causes for missing fields are limited. Re-upload the PDF or use Diagnose a PDF.'
    bits = [f"{d.get('pages', '?')} pages", f"{d.get('text_pages_pct', '?')}% of pages carry text"]
    if d.get('looks_like_spreads'):
        bits.append('two-page spreads')
    if d.get('statement_unit'):
        bits.append(f"statement unit {d['statement_unit']}")
    return ', '.join(bits)


def _file_story(f, st, story):
    """One company + year: counts, missing, low confidence, tables to complete, then every filled field with its source."""
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, Spacer
    c = f['counts']
    story.append(_p(f"{f['company']} - {f['fiscal_year']}", st['h2']))
    story.append(_p(f"{f.get('source_filename') or 'no source file name'}  |  {_doc_facts(f)}", st['sub']))
    story.append(Spacer(1, 5))
    story.append(_pdf_table([['Extraction score', 'Filled', 'Low confidence', 'Missing', 'Table / row gaps'],
                             [f"{f['score']}%", c['filled'], c['low_confidence'], c['missing'], len(f.get('tables') or [])]],
                            [36 * mm, 28 * mm, 34 * mm, 28 * mm, 34 * mm], st))
    miss = [x for x in f['fields'] if x['status'] == 'missing']
    low = [x for x in f['fields'] if x['status'] == 'low_confidence']
    if miss:
        story.append(CondPageBreak(45 * mm))
        story.append(_p(f"Fields not extracted ({len(miss)})", st['h3']))
        story.append(_pdf_table([['Section', 'Field', 'Cause', 'Detail']] +
                                [[x['section'], x['label'], REASON_LABELS.get(x['reason_code'], x['reason_code']), x['reason']] for x in miss],
                                [26 * mm, 36 * mm, 34 * mm, 84 * mm], st))
    if low:
        story.append(CondPageBreak(45 * mm))
        story.append(_p(f"Low-confidence values ({len(low)})", st['h3']))
        story.append(_pdf_table([['Field', 'Value', 'Confidence', 'Source / detail']] +
                                [[x['label'], x['value'], x['confidence'], (x['source'] or '') + ' ' + x['reason']] for x in low],
                                [44 * mm, 26 * mm, 20 * mm, 90 * mm], st))
    if f.get('tables'):
        story.append(CondPageBreak(45 * mm))
        story.append(_p(f"Tables and rows to complete ({len(f['tables'])})", st['h3']))
        story.append(_pdf_table([['Section', 'Item', 'Status', 'Detail']] +
                                [[x['section'], x['item'], x['status'].replace('_', ' '), x['reason']] for x in f['tables']],
                                [34 * mm, 40 * mm, 20 * mm, 86 * mm], st))
    filled = [x for x in f['fields'] if x['status'] == 'filled']
    if filled:
        story.append(CondPageBreak(45 * mm))
        story.append(_p(f"Fields extracted ({len(filled)})", st['h3']))
        story.append(_pdf_table([['Section', 'Field', 'Value', 'Read by', 'Source']] +
                                [[x['section'], x['label'], x['value'], x['stage'], x['source'] or ''] for x in filled],
                                [26 * mm, 40 * mm, 24 * mm, 32 * mm, 58 * mm], st))
    par = f.get('parity')
    if par and not par['agree']:
        story.append(CondPageBreak(45 * mm))
        story.append(_p('Difference from the Manual Entry form', st['h3']))
        story.append(_p('Listed as missing on the form but not here: ' + (', '.join(par['form_only']) or 'none')
                        + '.  Missing here but not on the form: ' + (', '.join(par['report_only']) or 'none') + '.', st['cell']))


def _build_pdf(story_fn, title):
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate
    buf, st = BytesIO(), _pdf_styles()
    story = []
    story_fn(story, st)

    def foot(canvas, d):
        canvas.saveState()
        canvas.setFont('Helvetica', 7.5)
        canvas.setFillColor(colors.HexColor(DIM_HEX))
        canvas.drawString(15 * mm, 9 * mm, f'FinSight - {title}')
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f'Page {d.page}')
        canvas.restoreState()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=16 * mm,
                      title=title).build(story, onFirstPage=foot, onLaterPages=foot)
    buf.seek(0)
    return buf


def company_pdf(company_id, fiscal_year=None, rep=None):
    """Text extraction report for one company: every year on file, or just `fiscal_year`.  -> (BytesIO, filename)"""
    from reportlab.platypus import PageBreak
    rep = rep or build_report(company_id, fiscal_year)
    files = [f for f in rep['files'] if f['company_id'] == company_id]
    if not files:
        return None, None
    name, fy_label = files[0]['company'], fiscal_year or 'All_years'
    gen = datetime.now().strftime('%d %b %Y')

    def story_fn(story, st):
        story.append(_p('Text Extraction Report', st['title']))
        story.append(_p(f"{name}  |  {'all years' if not fiscal_year else fiscal_year}  |  generated {gen}", st['sub']))
        for i, f in enumerate(files):
            if i:
                story.append(PageBreak())
            _file_story(f, st, story)
    return _build_pdf(story_fn, f'Text Extraction Report - {name}'), f'Text_Extraction_Report_{_safe(name)}_{_safe(fy_label)}.pdf'


def _file_main_reason(f):
    top = {}
    for it in f['fields']:
        if it['status'] != 'filled':
            top[it['reason_code']] = top.get(it['reason_code'], 0) + 1
    return REASON_LABELS.get(max(top, key=top.get), '') if top else ''


def _summary_rows(rep):
    rows = []
    for f in sorted(rep['files'], key=lambda f: (-f['counts']['missing'], f['company'], f['fiscal_year'] or '')):
        c, par = f['counts'], f.get('parity') or {}
        rows.append({'company': f['company'], 'ticker': f['ticker'] or '', 'fy': f['fiscal_year'], 'source': f.get('source_filename') or '',
                     'score': f['score'], 'filled': c['filled'], 'low': c['low_confidence'], 'missing': c['missing'],
                     'tables': len(f.get('tables') or []), 'reason': _file_main_reason(f),
                     'trace': 'yes' if f.get('trace') else 'no',
                     'matches_form': 'n/a' if not par else ('yes' if par.get('agree') else 'no')})
    return rows


def summary_pdf(rep, files_by_name=None):
    from reportlab.lib.units import mm
    from reportlab.platypus import Spacer
    rows = _summary_rows(rep)
    scope = rep['scope']['fiscal_year'] or 'all years'
    gen = datetime.now().strftime('%d %b %Y')
    n = sum(r['filled'] + r['low'] + r['missing'] for r in rows)
    fi = sum(r['filled'] for r in rows)

    def story_fn(story, st):
        story.append(_p('Text Extraction Coverage Summary', st['title']))
        story.append(_p(f'{len(rows)} file{"" if len(rows) == 1 else "s"}  |  {scope}  |  generated {gen}', st['sub']))
        story.append(Spacer(1, 6))
        story.append(_pdf_table([['Files', 'Fields filled', 'Low confidence', 'Missing', 'Company-years with no file'],
                                 [len(rows), f"{round(100 * fi / n) if n else 0}%", sum(r['low'] for r in rows),
                                  sum(r['missing'] for r in rows), len(rep['no_survey_row'])]],
                                [30 * mm, 34 * mm, 34 * mm, 28 * mm, 54 * mm], st))
        story.append(_p('Per file, most missing first', st['h3']))
        story.append(_pdf_table([['Company', 'FY', 'Score', 'Filled', 'Low', 'Missing', 'Gaps', 'Trace', 'Form match', 'Main cause']] +
                                [[r['company'], r['fy'], f"{r['score']}%", r['filled'], r['low'], r['missing'], r['tables'], r['trace'],
                                  r['matches_form'], r['reason']] for r in rows],
                                [30 * mm, 18 * mm, 15 * mm, 15 * mm, 12 * mm, 17 * mm, 14 * mm, 14 * mm, 16 * mm, 29 * mm], st))
        weak = rep['rollups']['by_field'][:12]
        if weak:
            story.append(_p('Weakest fields across this scope', st['h3']))
            story.append(_pdf_table([['Field', 'Section', 'Filled', 'Low', 'Missing', 'Most common cause']] +
                                    [[b['label'], b['section'], b['filled'], b['low_confidence'], b['missing'],
                                      REASON_LABELS.get(b['top_reason'], b['top_reason'] or '')] for b in weak],
                                    [48 * mm, 30 * mm, 14 * mm, 12 * mm, 16 * mm, 60 * mm], st))
        if rep['no_survey_row']:
            story.append(_p('Company-years with no file', st['h3']))
            story.append(_p(' - '.join(f"{x['company']} {x['fiscal_year']}" for x in rep['no_survey_row'][:80]), st['cell']))
    return _build_pdf(story_fn, 'Text Extraction Coverage Summary')


def _summary_csv(rep):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Company', 'Ticker', 'Fiscal year', 'Source file', 'Score %', 'Filled', 'Low confidence', 'Missing', 'Table / row gaps',
                'Main cause', 'Import trace', 'Missing list matches Manual Entry form'])
    for r in _summary_rows(rep):
        w.writerow([r['company'], r['ticker'], r['fy'], r['source'], r['score'], r['filled'], r['low'], r['missing'], r['tables'],
                    r['reason'], r['trace'], r['matches_form']])
    return '\ufeff' + buf.getvalue()


def build_zip(fiscal_year=None):
    """One text extraction PDF per company (the selected year, or every year), ranked by missing fields, plus a summary PDF + CSV.
    -> (BytesIO, filename)"""
    import zipfile
    rep = build_report(None, fiscal_year)
    by_co = {}
    for f in rep['files']:
        by_co.setdefault(f['company_id'], []).append(f)
    order = sorted(by_co, key=lambda cid: (-sum(f['counts']['missing'] for f in by_co[cid]), by_co[cid][0]['company']))
    width = max(2, len(str(len(order))))
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('00_Extraction_Coverage_Summary.pdf', summary_pdf(rep).getvalue())
        z.writestr('00_Extraction_Coverage_Summary.csv', _summary_csv(rep))
        for i, cid in enumerate(order, 1):
            sub = dict(rep, files=by_co[cid])
            buf, name = company_pdf(cid, fiscal_year, rep=sub)
            if buf:
                z.writestr(f'{i:0{width}d}_{name}', buf.getvalue())
    out.seek(0)
    tag = _safe(fiscal_year) if fiscal_year else 'All_years'
    return out, f'Text_Extraction_Reports_{tag}_{datetime.now().strftime("%Y%m%d")}.zip'


# ============================================================================= bulk diagnose (already-uploaded files)

_FN_RX = re.compile(r'^(?:[a-z]{2}[-_])?([a-z0-9&.]+?)[-_](20\d\d)(?:[-_]|\b)', re.I)


def _slug(x):
    return re.sub(r'[^a-z0-9]+', '', (x or '').lower())


def match_filename(filename, companies=None):
    """('ke-kcb-2025-ar-00.pdf') -> (company or None, 'FY2025' or None, note).  Ticker first, then company name.
    The year is the one in the file name; when the company has no survey row for that FY but has one for the next year
    (a financial year that ends in the following calendar year), that row's FY is used and the note says so."""
    base = (filename or '').rsplit('/', 1)[-1]
    stem = re.sub(r'\.pdf$', '', base, flags=re.I)
    m = _FN_RX.match(stem)
    ym = re.search(r'(20\d\d)', stem)
    year = m.group(2) if m else (ym.group(1) if ym else None)
    token = _slug(m.group(1)) if m else ''
    companies = companies if companies is not None else Company.query.all()
    co = None
    if token:
        co = next((c for c in companies if _slug(c.ticker) == token), None)
        if not co:
            cands = [c for c in companies if _slug(c.ticker) and (_slug(c.ticker).startswith(token) or token.startswith(_slug(c.ticker)))
                     and min(len(_slug(c.ticker)), len(token)) >= 3]
            co = cands[0] if len(cands) == 1 else None
        if not co:
            cands = [c for c in companies if len(token) >= 4 and token in _slug(c.name)]
            co = cands[0] if len(cands) == 1 else None
    if not co:
        sl = _slug(stem)
        cands = [c for c in companies if len(_slug(c.name)) >= 5 and _slug(c.name) in sl]
        co = max(cands, key=lambda c: len(_slug(c.name))) if cands else None
    if not co:
        return None, (f'FY{year}' if year else None), 'Could not tell which company this file belongs to from its name.'
    if not year:
        return co, None, 'Could not find a year in the file name.'
    fy, note = f'FY{year}', ''
    have = {r[0] for r in db.session.query(SurveyCompanyData.fiscal_year).filter_by(company_id=co.id) if r[0]}
    nxt = f'FY{int(year) + 1}'
    if fy not in have and nxt in have:
        fy, note = nxt, f'No {f"FY{year}"} survey row; used {nxt} (financial year ends in {int(year) + 1}).'
    return co, fy, note


def diagnose_one(filename, data, company_id=None, fiscal_year=None):
    """Analyse one PDF and store a trace.  Changes no survey data.  -> result dict for the page."""
    res = {'filename': filename, 'status': 'ok', 'company': None, 'fiscal_year': None, 'note': ''}
    if company_id:
        co, fy, note = Company.query.get(company_id), (fiscal_year or '').strip() or None, ''
    else:
        co, fy, note = match_filename(filename)
        if fiscal_year:
            fy = fiscal_year.strip()
    if not co or not fy:
        res.update(status='unmatched', note=note or 'Choose a company and year.')
        return res
    res.update(company=co.name, company_id=co.id, fiscal_year=fy, note=note)
    if not (data or b'').startswith(b'%PDF'):
        res.update(status='not_pdf', note='Not a PDF.')
        return res
    if not record_import(co.id, fy, filename, None, data, kind='diagnose'):
        res.update(status='unreadable', note='The PDF could not be read (damaged or password protected).')
        return res
    if not SurveyCompanyData.query.filter_by(company_id=co.id, fiscal_year=fy).first():
        res.update(status='stored_no_row', note=(note + ' ' if note else '') + 'Trace stored, but there is no survey row for this company and year yet, so it will show once a report is imported.')
    return res


def diagnose_many(files):
    """files: [(name, bytes)] - PDFs, or zips of PDFs (expanded here)."""
    import zipfile
    out = []
    for name, data in files:
        if (name or '').lower().endswith('.zip') or data[:2] == b'PK':
            try:
                z = zipfile.ZipFile(io.BytesIO(data))
            except Exception:
                out.append({'filename': name, 'status': 'unreadable', 'note': 'Not a valid zip.'})
                continue
            for n in sorted(z.namelist()):
                if n.lower().endswith('.pdf') and not n.startswith('__MACOSX'):
                    out.append(diagnose_one(n.rsplit('/', 1)[-1], z.read(n)))
        else:
            out.append(diagnose_one(name, data))
    return out


# ============================================================================= routes

def _int(v):
    try:
        return int(v) if v not in (None, '') else None
    except ValueError:
        return None


def register(app, require_role):
    gate = require_role('admin', 'user')

    @app.route('/extraction-report')
    def extraction_report_page():
        return Response(PAGE_HTML, mimetype='text/html')

    @app.route('/api/extraction-report/options')
    @gate
    def extraction_report_options():
        years = sorted({r[0] for r in db.session.query(SurveyCompanyData.fiscal_year).distinct() if r[0]}, reverse=True)
        cos = [{'id': c.id, 'name': c.name, 'ticker': c.ticker} for c in Company.query.order_by(Company.name).all()]
        return jsonify({'companies': cos, 'years': years, 'traces': ExtractionTrace.query.count()})

    @app.route('/api/extraction-report')
    @gate
    def extraction_report_json():
        return jsonify(build_report(_int(request.args.get('company_id')), request.args.get('fiscal_year') or None))

    @app.route('/api/extraction-report/explain')
    @gate
    def extraction_report_explain():
        """One company + year with the evidence loaded - the page calls this when a file in a big scope is opened."""
        cid, fy = _int(request.args.get('company_id')), (request.args.get('fiscal_year') or '').strip()
        if not cid or not fy:
            return jsonify({'error': 'company_id and fiscal_year are required.'}), 400
        rep = build_report(cid, fy, evidence=True)
        if not rep['files']:
            return jsonify({'error': 'No survey file on record for that company and year.'}), 404
        return jsonify(rep['files'][0])

    @app.route('/api/extraction-report/download')
    @gate
    def extraction_report_download():
        rep = build_report(_int(request.args.get('company_id')), request.args.get('fiscal_year') or None)
        name = _scope_name(rep)
        if (request.args.get('format') or 'xlsx').lower() == 'csv':
            return Response(to_csv(rep), mimetype='text/csv',
                            headers={'Content-Disposition': f'attachment; filename="{name}.csv"'})
        return Response(to_xlsx(rep), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': f'attachment; filename="{name}.xlsx"'})

    @app.route('/api/extraction-report/company/<int:cid>.pdf')
    @gate
    def extraction_report_company_pdf(cid):
        from flask import send_file
        buf, name = company_pdf(cid, request.args.get('fiscal_year') or None)
        if not buf:
            return jsonify({'error': 'No survey file on record for this company and year yet, so there is nothing to report.'}), 404
        return send_file(buf, as_attachment=True, download_name=name, mimetype='application/pdf')

    @app.route('/api/extraction-report/zip')
    @gate
    def extraction_report_zip():
        from flask import send_file
        buf, name = build_zip(request.args.get('fiscal_year') or None)
        return send_file(buf, as_attachment=True, download_name=name, mimetype='application/zip')

    @app.route('/api/extraction-report/diagnose-bulk', methods=['POST'])
    @gate
    def extraction_report_diagnose_bulk():
        """One or more annual-report PDFs (or a zip of them). Company + year are read from each file name
        (ke-<ticker>-<year>-ar-00.pdf) unless company_id / fiscal_year are sent. Stores traces only; changes no survey data."""
        files = [(f.filename, f.read()) for f in request.files.getlist('files') if f and f.filename]
        if not files:
            return jsonify({'error': 'Choose one or more annual-report PDFs (or a zip of them).'}), 400
        if len(files) == 1 and not str(files[0][0]).lower().endswith('.zip') and request.form.get('company_id'):
            res = [diagnose_one(files[0][0], files[0][1], _int(request.form.get('company_id')), request.form.get('fiscal_year'))]
        else:
            res = diagnose_many(files)
        return jsonify({'results': res, 'traces': ExtractionTrace.query.count()})

    @app.route('/api/extraction-report/diagnose', methods=['POST'])
    @gate
    def extraction_report_diagnose():
        """Analyse a PDF for one company + year and store a trace. Changes no survey data."""
        f = request.files.get('file')
        cid, fy = _int(request.form.get('company_id')), (request.form.get('fiscal_year') or '').strip()
        if not f or not cid or not fy:
            return jsonify({'error': 'Choose a company, a fiscal year (e.g. FY2026) and a PDF.'}), 400
        data = f.read()
        if not record_import(cid, fy, f.filename, None, data, kind='diagnose'):
            return jsonify({'error': 'Could not read that PDF.'}), 400
        return jsonify({'ok': True, 'report': build_report(cid, fy)})


# ============================================================================= the page

PAGE_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Extraction report</title><style>
:root{--bg:#0b1220;--card:#121b2f;--line:#24324f;--tx:#e6ecf7;--mut:#8da0c4;--ok:#34d399;--warn:#fbbf24;--bad:#f87171;--acc:#60a5fa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif;padding:18px 22px}
h1{margin:0 0 2px;font-size:22px}.sub{color:var(--mut);margin-bottom:14px}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:end;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:14px}
label{display:flex;flex-direction:column;font-size:12px;color:var(--mut);gap:4px}
select,input[type=file],button{background:#0e1627;color:var(--tx);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit}
button{cursor:pointer}button.p{background:#1d4ed8;border-color:#2563eb}button:hover{filter:brightness(1.15)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.c{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}.c b{display:block;font-size:24px}.c span{color:var(--mut);font-size:12px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:14px}
.panel h2{font-size:15px;margin:0 0 8px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px}.s-filled{color:var(--ok)}.s-low_confidence{color:var(--warn)}.s-missing{color:var(--bad)}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid var(--line);font-size:12px}
details{border:1px solid var(--line);border-radius:10px;margin-bottom:8px;background:#0e1627}summary{cursor:pointer;padding:9px 12px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.why{color:var(--mut);max-width:760px}.ev{margin-top:4px;padding:4px 8px;border-left:3px solid var(--line);font-size:12px;color:var(--tx)}.act{color:var(--warn);margin-top:4px}.mut{color:var(--mut)}.sc{margin-left:auto;font-weight:700}.err{color:var(--bad)}
.tools{display:flex;gap:8px;padding:6px 12px}.small{font-size:12px}
</style></head><body>
<h1>Extraction report</h1><div class="sub">Why each survey field was filled, left at low confidence, or not filled - for any file already in the system.</div>
<div class="bar">
 <label>Company<select id="co"><option value="">All companies</option></select></label>
 <label>Fiscal year<select id="fy"><option value="">All years</option></select></label>
 <button class="p" onclick="load()">Show report</button>
 <button onclick="dl('xlsx')">Download Excel</button><button onclick="dl('csv')">Download CSV</button>
 <span class="mut small" id="tr"></span>
</div>
<div class="panel"><h2>Diagnose already-uploaded reports in bulk (changes no data)</h2>
 <div class="mut small" style="margin-bottom:8px">Select all the annual-report PDFs at once (or a zip). Company and year are read from each file name, e.g. ke-kcb-2025-ar-00.pdf. Each file gets its reasons stored, so the report, the PDF and the zip show real causes for it.</div>
 <div class="bar" style="border:0;padding:0;margin:0"><label>PDFs or zip<input type="file" id="bfiles" accept="application/pdf,.zip" multiple></label><button class="p" onclick="bulk()">Diagnose all</button><span id="bmsg" class="mut small"></span></div>
 <table id="btab" style="margin-top:8px"></table></div>
<div class="panel"><h2>Diagnose a single PDF (adds the reasons for a file imported before this report existed; changes no data)</h2>
 <div class="bar" style="border:0;padding:0;margin:0"><label>Company<select id="dco"></select></label>
 <label>Fiscal year<input id="dfy" placeholder="FY2026" style="background:#0e1627;color:var(--tx);border:1px solid var(--line);border-radius:8px;padding:8px 10px;width:110px"></label>
 <label>PDF<input type="file" id="dfile" accept="application/pdf"></label><button onclick="diag()">Diagnose</button><span id="dmsg" class="mut small"></span></div></div>
<div id="out"></div>
<script>
const $=id=>document.getElementById(id);let R=null;
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(u,o){const r=await fetch(u,o);if(!r.ok){let m='Request failed';try{m=(await r.json()).error||m}catch(e){}throw new Error(m)}return r.json()}
async function init(){try{const o=await api('/api/extraction-report/options');
 for(const c of o.companies){$('co').add(new Option(c.name+(c.ticker?' ('+c.ticker+')':''),c.id));$('dco').add(new Option(c.name,c.id))}
 for(const y of o.years)$('fy').add(new Option(y,y));$('tr').textContent=o.traces+' import trace(s) stored';load()}catch(e){$('out').innerHTML='<div class="panel err">'+esc(e.message)+'</div>'}}
const qs=()=>{const p=new URLSearchParams();if($('co').value)p.set('company_id',$('co').value);if($('fy').value)p.set('fiscal_year',$('fy').value);return p.toString()};
function dl(f){location.href='/api/extraction-report/download?format='+f+'&'+qs()}
async function load(){$('out').innerHTML='<div class="panel mut">Loading\u2026</div>';try{R=await api('/api/extraction-report?'+qs());draw()}catch(e){$('out').innerHTML='<div class="panel err">'+esc(e.message)+'</div>'}}
async function diag(){const f=$('dfile').files[0];if(!f||!$('dco').value||!$('dfy').value){$('dmsg').textContent='Pick a company, year and PDF.';return}
 const fd=new FormData();fd.append('file',f);fd.append('company_id',$('dco').value);fd.append('fiscal_year',$('dfy').value.trim());$('dmsg').textContent='Analysing\u2026';
 try{const r=await api('/api/extraction-report/diagnose',{method:'POST',body:fd});$('dmsg').textContent='Done.';$('co').value=$('dco').value;$('fy').value=$('dfy').value.trim();R=r.report;draw()}catch(e){$('dmsg').textContent=e.message}}
async function bulk(){const fs=[...$('bfiles').files];if(!fs.length){$('bmsg').textContent='Choose files first.';return}
 const rows=[];let done=0;const col={ok:'s-filled',stored_no_row:'s-low_confidence'};
 const show=()=>{$('btab').innerHTML='<tr><th>File</th><th>Company</th><th>Year</th><th>Result</th></tr>'+rows.map(r=>'<tr><td>'+esc(r.filename)+'</td><td>'+esc(r.company||'')+'</td><td>'+esc(r.fiscal_year||'')+'</td><td class=\"'+(col[r.status]||'s-missing')+'\">'+esc(r.status.replace(/_/g,' '))+(r.note?' <span class=\"mut\">'+esc(r.note)+'</span>':'')+'</td></tr>').join('')};
 for(const f of fs){$('bmsg').textContent='Analysing '+(done+1)+' of '+fs.length+'\u2026';const fd=new FormData();fd.append('files',f);
  try{const r=await api('/api/extraction-report/diagnose-bulk',{method:'POST',body:fd});rows.push(...r.results)}catch(e){rows.push({filename:f.name,status:'failed',note:e.message})}
  done++;show()}
 const ok=rows.filter(r=>r.status==='ok'||r.status==='stored_no_row').length;$('bmsg').textContent='Done: '+ok+' of '+rows.length+' stored.';load()}
function draw(){let n=0,fi=0,lo=0,mi=0;for(const f of R.files){fi+=f.counts.filled;lo+=f.counts.low_confidence;mi+=f.counts.missing;n+=f.counts.total}
 const L=R.reason_labels;let h='<div class="cards"><div class="c"><b>'+R.files.length+'</b><span>files in scope</span></div><div class="c"><b>'+(n?Math.round(100*fi/n):0)+'%</b><span>fields filled</span></div>'+
 '<div class="c"><b class="s-low_confidence">'+lo+'</b><span>low confidence</span></div><div class="c"><b class="s-missing">'+mi+'</b><span>missing</span></div><div class="c"><b>'+R.no_survey_row.length+'</b><span>company-years with no file</span></div></div>';
 const br=Object.entries(R.rollups.by_reason).sort((a,b)=>b[1]-a[1]);
 if(R.coverage&&R.coverage.without_text)h+='<div class="panel"><b class="s-low_confidence">'+R.coverage.without_text+' of '+R.coverage.files+' file(s) have no stored report text.</b> <span class="mut">Run \u201cDiagnose already-uploaded reports in bulk\u201d once for them; the text is kept from then on, so the explanations never need the annual report again. Files uploaded from now on store it automatically.</span></div>';
 h+='<div class="panel"><h2>Why fields fail (across this scope)</h2><table><tr><th>Reason</th><th>Fields</th></tr>'+br.map(([k,v])=>'<tr><td>'+esc(L[k]||k)+'</td><td>'+v+'</td></tr>').join('')+'</table></div>';
 h+='<div class="panel"><h2>Weakest fields</h2><table><tr><th>Field</th><th>Section</th><th>Filled</th><th>Low conf.</th><th>Missing</th><th>Most common reason</th></tr>'+
 R.rollups.by_field.slice(0,15).map(b=>'<tr><td>'+esc(b.label)+'</td><td>'+esc(b.section)+'</td><td>'+b.filled+'</td><td>'+b.low_confidence+'</td><td>'+b.missing+'</td><td>'+esc(L[b.top_reason]||b.top_reason||'')+'</td></tr>').join('')+'</table></div>';
 h+='<div class="panel"><h2>Files</h2><div class="tools small mut">Filter fields: <select id="flt" onchange="draw2()"><option value="">all</option><option value="missing">missing</option><option value="low_confidence">low confidence</option><option value="filled">filled</option></select></div><div id="files"></div></div>';
 const up=R.upload_problems;if(up.extraction_issues.length||up.failed_uploads.length){h+='<div class="panel"><h2>Upload problems</h2><table><tr><th>When</th><th>File</th><th>Step</th><th>Error</th></tr>'+
  up.extraction_issues.slice(0,40).map(i=>'<tr><td>'+esc((i.created_at||'').slice(0,16))+'</td><td>'+esc(i.filename)+' '+esc(i.fiscal_year||'')+'</td><td>'+esc(i.step)+'</td><td class="err">'+esc(i.error)+'</td></tr>').join('')+
  up.failed_uploads.slice(0,40).map(d=>'<tr><td>'+esc((d.created_at||'').slice(0,16))+'</td><td>'+esc(d.filename)+'</td><td>upload '+esc(d.status)+'</td><td class="err">'+esc(d.error||'')+'</td></tr>').join('')+'</table></div>'}
 if(R.no_survey_row.length&&R.no_survey_row.length<=60){h+='<div class="panel"><h2>Company-years with no file</h2><div class="mut small">'+R.no_survey_row.map(x=>esc(x.company)+' '+esc(x.fiscal_year)).join(' \u00b7 ')+'</div></div>'}
 $('out').innerHTML=h;draw2()}
function evHtml(x){let h='';
 if(x.verified===true)h+='<div class="small s-filled">\u2714 Cross-checked in the report text</div>';
 else if(x.verified===false)h+='<div class="small s-low_confidence">Not found word-for-word in the report text (derived value)</div>';
 for(const e of (x.evidence||[]))h+='<div class="ev"><b>p.'+esc(e.page)+'</b> \u201c'+esc(e.snippet)+'\u201d</div>';
 if(x.action)h+='<div class="small act">What to do: '+esc(x.action)+'</div>';return h}
function fileBody(f){const flt=($('flt')||{}).value||'';const d=(f.trace||{}).doc||{};
 const facts=f.trace?('<div class="small mut" style="padding:0 12px 6px">PDF: '+(d.pages||'?')+' pages, '+(d.text_pages_pct??'?')+'% with text'+(d.looks_like_spreads?', <b class="s-low_confidence">two-page spreads</b>':'')+(d.statement_unit?', unit '+esc(d.statement_unit):'')+' \u00b7 trace '+esc((f.trace.created_at||'').slice(0,16))+' ('+esc(f.trace.kind)+')'+
  (f.text_stored?' \u00b7 <b class="s-filled">report text stored ('+f.text_pages+' pages) - no need to re-upload</b>':' \u00b7 <b class="s-low_confidence">report text not stored - run Diagnose once for this file</b>')+'</div>'):
  '<div class="small mut" style="padding:0 12px 6px">No import trace for this file - causes for missing fields are limited. Use \u201cDiagnose a PDF\u201d above (the text is stored, so it is needed only once).</div>';
 const rows=f.fields.filter(x=>!flt||x.status===flt).map(x=>'<tr><td>'+esc(x.section)+'</td><td>'+esc(x.label)+'</td><td class="s-'+x.status+'">'+x.status.replace('_',' ')+'</td><td>'+esc(x.value==null?'':x.value)+(x.confidence!=null?' <span class="mut">('+x.confidence+')</span>':'')+'</td><td class="why">'+esc(x.reason)+evHtml(x)+(x.source?'<div class="small">Source: '+esc(x.source)+'</div>':'')+'</td></tr>').join('');
 return facts+'<div style="padding:0 12px 12px;overflow:auto"><table><tr><th>Section</th><th>Field</th><th>Status</th><th>Value</th><th>Why it passed / failed</th></tr>'+rows+'</table></div>'}
async function openFile(i,el){const f=R.files[i];if(!el.open||f.evidence_loaded)return;
 const box=$('fb'+i);box.insertAdjacentHTML('afterbegin','<div class="small mut" style="padding:6px 12px">Loading the evidence\u2026</div>');
 try{const r=await api('/api/extraction-report/explain?company_id='+f.company_id+'&fiscal_year='+encodeURIComponent(f.fiscal_year));r.evidence_loaded=true;R.files[i]=r;box.innerHTML=fileBody(r)}catch(e){box.innerHTML='<div class="small err" style="padding:6px 12px">'+esc(e.message)+'</div>'+fileBody(f)}}
function draw2(){$('files').innerHTML=R.files.map((f,i)=>'<details ontoggle="openFile('+i+',this)"><summary><b>'+esc(f.company)+'</b> '+esc(f.fiscal_year)+' <span class="mut small">'+esc(f.source_filename||'')+'</span><span class="pill s-filled">'+f.counts.filled+' filled</span><span class="pill s-low_confidence">'+f.counts.low_confidence+' low</span><span class="pill s-missing">'+f.counts.missing+' missing</span>'+(f.text_stored?'<span class="pill s-filled">text stored</span>':'<span class="pill s-missing">text not stored</span>')+'<span class="sc">'+f.score+'%</span></summary><div id="fb'+i+'">'+fileBody(f)+'</div></details>').join('')||'<div class="mut">No files in this scope.</div>'}

init();
</script></body></html>"""