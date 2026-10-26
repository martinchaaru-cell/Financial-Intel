"""Downloadable Manual Entry forms (PDF).

One form per company + fiscal year covering ALL 40 survey fields, so a person can audit what is on file:
  Checks           - problems found automatically: rules the stored fields break (exec + NED = board size, annual / 12
                     = monthly salary, ...) and signs the file is the wrong company / year / not the annual report
  A. MISSING       - metrics no reader could extract, plus empty tables (register / pay rows / committees / benefits)
  B. TO CONFIRM    - stored values with low confidence (< 0.75, the same threshold the Data Quality tab uses)
                     and any unresolved evidence conflicts
  C. ON FILE       - every other filled field: value + unit, source page, and whether the value is found in the stored
                     report text; tick OK or type the correct value (same confirm|/confirmok| fields as B)
  D. Register (every director, with what is on file) / committee / pay rows with blank cells

build_company_form(company_id, fiscal_year=None) -> (BytesIO, filename)
build_all_forms_zip(fiscal_year=None)            -> (BytesIO, filename)   one PDF per company inside

The PDFs are FILLABLE: every blank is a real form field (type into it with Adobe Reader, Preview, Chrome, Edge, ...),
and the file carries a hidden `meta` field naming the company and year. Upload the filled file(s) - or a zip of them -
on the Manual Entry page (manual_entry_upload.py reads the fields back). A printed-and-handwritten or scanned form
cannot be read back; type into the PDF, or enter those values on the page by hand.

Field names (parsed by manual_entry_upload.py - keep the two in sync):
  meta                      hidden JSON {"v":1,"cid":..,"fy":..,"name":..}
  metric|<column>           a missing metric        metricnote|<column>  its source page / note
  val|<column>              v2: box pre-filled with the stored value (sections B and C) - edited = correction
  confirm|<column>          v1 forms: corrected value for a stored metric    confirmok|<column>  "stored value is right"
  conflict|<field>          the correct figure for an unresolved conflict
  dirname|<id>              v2: a register row's name, pre-filled - emptied = remove (replace mode)
  dir|<id>|<attr>           blank cell of an existing register row (attr: role, independent, gender, nationality, appointed)
  dirfix|<id>|<attr>        filled cell of a register row, pre-filled with the stored value - a changed value is a correction
  newdir|<n>|<attr>         new register row (attr also: name)
  com|<id>|<attr>           blank cell of an existing committee (attr: chair, meetings, members)
  newcom|<n>|<attr>         new committee (attr also: name)
  pay|<id>|<attr>           existing pay row without a total (attr: total, components)
  newpay|<n>|<attr>         new pay row (attr also: name, role)
  ben|<key>  /  bendetail|<key>   NED benefit status (dropdown) and detail
Must be called inside a Flask app context (it queries the database).
"""
import json
import re
import zipfile
from datetime import datetime
from io import BytesIO, StringIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, PageBreak

import csv
import data_quality
import tab_modules
from models import db, Company, FinancialPeriod, DirectorRemunerationRow, Committee, CommitteeMember
from models_survey import SurveyCompanyData, SurveyDirector, EvidenceConflict

NAVY = colors.HexColor('#0F2B5B')
DIM = colors.HexColor('#5A6478')
BORDER = colors.HexColor('#C9CFDD')
HEAD_BG = colors.HexColor('#EEF1F8')
AMBER = colors.HexColor('#B8790C')
LOW_CONF = 0.75
BLANK_ROWS = 4
SUMMARY_PREFIX = '00_Data_Quality_Summary'    # first file in the zip; manual_entry_upload skips it (same prefix there)


def _safe(name):
    return ''.join(c for c in (name or 'company') if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_') or 'company'


def _styles():
    ss = getSampleStyleSheet()
    return {
        'title': ParagraphStyle('t', parent=ss['Title'], fontName='Helvetica-Bold', fontSize=18, textColor=NAVY, alignment=0, spaceAfter=2),
        'sub': ParagraphStyle('s', parent=ss['Normal'], fontName='Helvetica', fontSize=9.5, textColor=DIM, leading=13),
        'h2': ParagraphStyle('h2', parent=ss['Normal'], fontName='Helvetica-Bold', fontSize=12, textColor=NAVY, spaceBefore=12, spaceAfter=4, keepWithNext=1),
        'h3': ParagraphStyle('h3', parent=ss['Normal'], fontName='Helvetica-Bold', fontSize=9.5, textColor=NAVY, spaceBefore=6, spaceAfter=3, keepWithNext=1),
        'cell': ParagraphStyle('c', parent=ss['Normal'], fontName='Helvetica', fontSize=8.5, leading=11),
        'head': ParagraphStyle('hd', parent=ss['Normal'], fontName='Helvetica-Bold', fontSize=8.5, leading=11, textColor=NAVY),
        'note': ParagraphStyle('n', parent=ss['Normal'], fontName='Helvetica-Oblique', fontSize=8, textColor=DIM, leading=11),
    }


class Field(Flowable):
    """One fillable AcroForm field sitting in a table cell."""
    prefill = {}          # name -> value; only used by tests to simulate a filled form

    def __init__(self, name, kind='text', options=None, height=14, value=None):
        super().__init__()
        self.name, self.kind, self.options, self.height = name, kind, options or [], height
        self.value = value
        self.width = 60

    def wrap(self, availWidth, availHeight):
        self.width = max(availWidth, 10)
        return (self.width if self.kind != 'check' else 12), self.height

    def draw(self):
        af, val = self.canv.acroForm, Field.prefill.get(self.name, self.value if self.value is not None else '')
        if isinstance(val, str):
            import manual_entry_upload as meu
            val = meu.box_safe(val)
        common = dict(name=self.name, x=0, y=0, borderWidth=0.6, borderColor=BORDER, fillColor=colors.white,
                      textColor=colors.black, forceBorder=True, tooltip=self.name, relative=True)
        if self.kind == 'check':
            af.checkbox(size=11, checked=bool(val), buttonStyle='check', **common)
        elif self.kind == 'choice':
            af.choice(width=self.width, height=self.height, value=val or self.options[0], options=self.options,
                      fieldFlags='combo', fontSize=8, **common)
        else:
            af.textfield(width=self.width, height=self.height, value=str(val), fontSize=8, **common)


def _p(text, style):
    return Paragraph(escape(str(text)) if text is not None else '', style)


def _table(rows, widths, st, header=True):
    data = []
    for i, r in enumerate(rows):
        data.append([c if isinstance(c, Flowable) else _p(c, st['head'] if (header and i == 0) else st['cell']) for c in r])
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [('GRID', (0, 0), (-1, -1), 0.5, BORDER), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
             ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4)]
    if header:
        style.append(('BACKGROUND', (0, 0), (-1, 0), HEAD_BG))
    t.setStyle(TableStyle(style))
    return t


def _num(v):
    if v is None:
        return ''
    if isinstance(v, float):
        return f'{v:,.2f}'.rstrip('0').rstrip('.')
    return str(v)


def latest_year(company_id):
    years = {p.period_label for p in FinancialPeriod.query.filter_by(company_id=company_id).all() if p.period_label}
    years |= {r.fiscal_year for r in SurveyCompanyData.query.filter_by(company_id=company_id).all() if r.fiscal_year}
    return sorted(years, reverse=True)[0] if years else None


def collect(company_id, fiscal_year):
    """Everything the form needs, as plain data (also handy for tests)."""
    status = tab_modules.tab_status(company_id, fiscal_year)
    row = tab_modules.survey_row(company_id, fiscal_year)
    srcs = (row.field_sources or {}) if row else {}
    confs = (row.field_confidence or {}) if row else {}
    labels = {k: lbl for m in tab_modules.TAB_MODULES for k, lbl in m['metrics']}       # numeric only (used for "confirm")
    all_labels = tab_modules.all_metric_labels()                                          # numeric + free-text

    missing_metrics, missing_lists = [], []
    for mod in tab_modules.TAB_MODULES:
        st = status.get(mod['id'], {})
        keys = set(k for k, _ in mod['metrics']) | set(k for k, _ in mod.get('text_metrics', []))
        ms = [(k, all_labels[k]) for k in st.get('missing', []) if k in keys]
        if ms:
            missing_metrics.append((mod['label'], ms))
        for k in st.get('missing', []):
            if k not in keys:
                missing_lists.append(k)

    confirm = []
    if row:
        for k, lbl in labels.items():
            v = getattr(row, k, None)
            c = confs.get(k)
            if v is not None and c is not None and c < LOW_CONF:
                confirm.append({'key': k, 'label': lbl, 'value': v, 'confidence': c, 'source': srcs.get(k) or ''})
    conflicts = [{'label': ec.field_name, 'a': ec.candidate_a_value, 'a_page': ec.candidate_a_page,
                  'b': ec.candidate_b_value, 'b_page': ec.candidate_b_page}
                 for ec in EvidenceConflict.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).all()
                 if ec.resolution is None]

    period = FinancialPeriod.query.filter_by(company_id=company_id, period_label=fiscal_year).first()
    directors = SurveyDirector.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).order_by(SurveyDirector.order_index).all()
    gaps = []
    for d in directors:
        miss = []
        if not d.role or d.role == 'unknown':
            miss.append('Role')
        if d.independent is None:
            miss.append('Independent')
        if not d.gender:
            miss.append('Gender')
        if not d.nationality:
            miss.append('Nationality')
        if not d.appointed_date:
            miss.append('Appointed')
        if miss:
            gaps.append({'id': d.id, 'name': d.director_name, 'role': d.role, 'missing': miss})

    comm_gaps = []
    if period:
        for c in Committee.query.filter_by(period_id=period.id).order_by(Committee.order_index).all():
            n_members = CommitteeMember.query.filter_by(committee_id=c.id).count()
            miss = []
            if not c.chairperson_name:
                miss.append('Chair')
            if c.meetings_held is None:
                miss.append('Meetings')
            if not n_members:
                miss.append('Members')
            if miss:
                comm_gaps.append({'id': c.id, 'name': c.name, 'missing': miss})

    pay_gaps = []
    if period:
        for r in DirectorRemunerationRow.query.filter_by(period_id=period.id).all():
            if not r.is_grand_total and not r.is_total_row and r.total is None:
                pay_gaps.append({'id': r.id, 'name': r.director_name})

    ned = (row.ned_benefits or {}) if row else {}
    benefits_open = [k for k in tab_modules.BENEFIT_KEYS if k not in ned]

    texts = None
    if row is not None:
        try:
            import extraction_report as er
            texts = er.load_pages(company_id, fiscal_year)
        except Exception:
            texts = None
    confirm_keys = {c['key'] for c in confirm}
    for c in confirm:
        c['check'] = _report_check(row, c['key'], c['value'], c['source'], texts)
    filled = []
    if row is not None:
        for mod in tab_modules.TAB_MODULES:
            for k, lbl in list(mod['metrics']) + list(mod.get('text_metrics', [])):
                v = getattr(row, k, None)
                if v is None or str(v).strip() == '' or k in confirm_keys:
                    continue
                filled.append({'key': k, 'label': lbl, 'section': mod['label'], 'value': v, 'unit': _unit_of(row, k),
                               'confidence': confs.get(k), 'source': srcs.get(k) or '',
                               'check': _report_check(row, k, v, srcs.get(k), texts)})
    company = db.session.get(Company, company_id)
    checks = (file_checks(company, fiscal_year, row, texts) + cross_year_checks(company_id, fiscal_year, row)
              + consistency_checks(row, directors)) if row is not None else []

    return {'quality': data_quality.company_quality(row), 'status': status, 'missing_metrics': missing_metrics, 'missing_lists': missing_lists,
            'confirm': confirm, 'conflicts': conflicts, 'director_gaps': gaps, 'committee_gaps': comm_gaps,
            'pay_gaps': pay_gaps, 'benefits_open': benefits_open, 'unit': row.director_figures_unit if row else None,
            'currency': row.currency if row else None, 'headline_unit': row.unit if row else None,
            'filled': filled, 'checks': checks, 'directors': directors, 'text_stored': texts is not None,
            'source_filename': row.source_filename if row else None}


# ============================================================================= checks printed on the form
HEADLINE_COLS = ('turnover', 'net_profit', 'market_cap')
AGE_COLS = ('avg_age_executive_directors', 'avg_age_non_executive_directors', 'avg_age_independent_neds',
            'avg_age_non_independent_neds')
FIX_DELETE = ('Remove this year for this company (Settings > Companies, choose the year, Delete), then upload the '
              'right annual report.')
FIX_FIELD = 'Type the correct value in section C (or tick OK if the report really says this).'


def _unit_of(row, col):
    """How the stored value should be read: headline figures use the row's unit, pay figures the pay-table unit."""
    if col in HEADLINE_COLS:
        return f"{row.currency or 'KES'} {row.unit or 'millions'}"
    if col in AGE_COLS:
        return 'years'
    if col.endswith(('_retainer', '_allowance')) or (col.startswith('ceo_') and col not in ('ceo_monthly_conversion_basis',)):
        return f"{row.currency or 'KES'} {row.director_figures_unit or 'as printed'}"
    return ''


def _short_source(src):
    """Source note for the form: page number first (it is what a checker needs), typed entries said plainly."""
    s = re.sub(r'\s+', ' ', str(src or '')).strip()
    if not s:
        return '-'
    if s.lower().startswith('entered manually'):
        return 'Typed by hand' + (' (form upload)' if 'form upload' in s.lower() else '')
    pages = re.findall(r'\bpages?\s+(\d+(?:\s*[-\u2013]\s*\d+)?)', s, re.I)
    s = re.sub(r'\s+in\s+\S+\.pdf\b', '', s)              # the file name is already in the form header
    s = s if len(s) <= 90 else s[:88].rstrip() + '...'
    return (f"p.{', '.join(dict.fromkeys(p.replace(' ', '') for p in pages))} - " if pages else 'page not recorded - ') + s


def _prefill(v):
    import manual_entry_upload as meu
    return meu.prefill_text(v)


def _report_check(row, col, value, source, texts):
    """Compare one stored value with the report's own stored text.
    -> {'status': found|derived|person|no_text, 'page': int|None, 'text': short wording for the form}"""
    review = ((row.field_review_status or {}) if row is not None else {}).get(col)
    try:
        import extraction_report as er
        stage = er._stage(source)
    except Exception:
        er, stage = None, ''
    by_person = stage.startswith('Manual') or review in ('approved', 'corrected')
    if texts is None or er is None:
        return {'status': 'person' if by_person else 'no_text', 'page': None,
                'text': 'Entered / reviewed by a person' if by_person else 'Report text not stored - check by hand'}
    ev = er._find_value_evidence(col, value, texts, er._cited_pages(source))
    if ev:
        return {'status': 'found', 'page': ev[0]['page'], 'text': f"Found on p.{ev[0]['page']}"}
    if by_person:
        return {'status': 'person', 'page': None, 'text': 'Entered / reviewed by a person'}
    return {'status': 'derived', 'page': None,
            'text': 'Not in the report as-is - check'}


_GENERIC = {'plc', 'limited', 'ltd', 'company', 'co', 'group', 'holdings', 'holding', 'the', 'kenya', 'investment',
            'investments', 'and', 'of', 'bank', 'corporation', 'corp', 'inc', 'kenyan', 'east', 'africa', 'african'}


def _core_tokens(name):
    toks = re.findall(r'[a-z0-9]+(?:-[a-z0-9]+)?', (name or '').lower())
    return [t for t in toks if t not in _GENERIC and len(t) >= 3]


def _phrase_hits(tokens, text):
    if not tokens:
        return 0
    pat = r'\b' + r'\W+(?:and\W+|&\W+)?'.join(re.escape(t) for t in tokens) + r'\b'
    return len(re.findall(pat, text))


_MONTHS = r'(?:january|february|march|april|may|june|july|august|september|october|november|december)'
_YEAR_END = re.compile(r'(?:(?:year|period)\s+ended|as\s+at|\bat)\s+(?:\d{1,2}(?:st|nd|rd|th)?\s+)?' + _MONTHS
                       + r'\s+(?:\d{1,2},?\s+)?(20\d\d)', re.I)


def file_checks(company, fiscal_year, row, texts):
    """Is this really the right company's annual report for this year? Uses only the stored report text."""
    out = []
    if texts is None:
        if row is not None and row.source_filename:
            out.append({'severity': 'warning', 'text': f'The report text for {row.source_filename} is not stored, so no value '
                        'on this form could be checked against it automatically.',
                        'fix': "Run Diagnose once with the PDF (Extraction report page), or check section C by hand."})
        return out
    n = len(texts)
    full = '\n'.join(t or '' for _, t in texts).lower()
    head = '\n'.join(t or '' for _, t in texts[:4]).lower()
    own = _core_tokens(company.name if company else '')
    own_hits = _phrase_hits(own, full) if own else None
    if company is not None and company.ticker and own_hits == 0:
        own_hits = len(re.findall(r'\b' + re.escape(company.ticker.lower()) + r'\b', full)) or 0
    if own and own_hits == 0:
        out.append({'severity': 'error', 'text': f'The name "{company.name}" never appears in the stored report text - '
                    'this file may belong to another company.', 'fix': FIX_DELETE})
    if own and (own_hits or 0) < 3:
        best = None
        for c in Company.query.all():
            if company is not None and c.id == company.id:
                continue
            ct = _core_tokens(c.name)
            if not ct or set(ct) <= set(own) or set(own) <= set(ct):
                continue
            h = _phrase_hits(ct, full)
            if h >= 15 and (best is None or h > best[1]):
                best = (c.name, h)
        if best:
            out.append({'severity': 'error', 'text': f'The report mentions "{best[0]}" {best[1]} times but "{company.name}" '
                        f'only {own_hits or 0} - it looks like {best[0]}\'s report.', 'fix': FIX_DELETE})
    digits = ''.join(ch for ch in str(fiscal_year or '') if ch.isdigit())
    if len(digits) == 4:
        years = {}
        for m in _YEAR_END.finditer(full):
            years[m.group(1)] = years.get(m.group(1), 0) + 1
        if years:
            top = max(years, key=years.get)
            mine = years.get(digits, 0)
            if mine == 0:
                out.append({'severity': 'error', 'text': f'The report never gives a {digits} year-end date; it mostly refers to '
                            f'{top} ({years[top]} times) - it is probably recorded under the wrong year.', 'fix': FIX_DELETE})
            elif top != digits and years[top] >= 2 * mine:
                out.append({'severity': 'warning', 'text': f'Year-end dates in the report point mostly to {top} ({years[top]}x) '
                            f'rather than {digits} ({mine}x) - check the report is the {fiscal_year} one.', 'fix': FIX_DELETE})
    if re.search(r'\binterim\b|half[- ]year|six months ended|nine months ended|quarter ended', head) and 'annual report' not in head:
        out.append({'severity': 'error', 'text': 'The first pages read like interim / half-year results, not the annual report.',
                    'fix': FIX_DELETE})
    elif n < 20:
        out.append({'severity': 'warning', 'text': f'The stored report has only {n} pages - it may be an extract or results '
                    'announcement rather than the full annual report.', 'fix': 'Check the file; if it is not the annual report, '
                    + FIX_DELETE[0].lower() + FIX_DELETE[1:]})
    return out


MONEY_COLS = HEADLINE_COLS + tuple(k for m in tab_modules.TAB_MODULES for k, _ in m['metrics']
                                   if k.endswith(('_retainer', '_allowance')) or (k.startswith('ceo_') and k != 'ceo_monthly_conversion_basis'))


def _fy_num(fy):
    d = ''.join(ch for ch in str(fy or '') if ch.isdigit())
    return int(d) if len(d) == 4 else None


def cross_year_checks(company_id, fiscal_year, row):
    """Problems that only show up by comparing this year with the company's other years:
    the same report file saved under two years, one year's figures copied into another, and figures that are really
    the PRIOR year's (a comparative column) - a value that appears in last year's own report cannot be this year's."""
    out = []
    if row is None:
        return out
    lbl = tab_modules.all_metric_labels()
    others = [r for r in SurveyCompanyData.query.filter_by(company_id=company_id).all() if r.fiscal_year != fiscal_year]
    me = (row.source_filename or '').strip().lower()
    yr = _fy_num(fiscal_year)
    body = [k for k in tab_modules.all_metric_labels() if k not in HEADLINE_COLS]
    def real_files(fy):
        """Files this company + year was actually imported / diagnosed from (import traces and stored text) - the
        row's own source_filename can be a stale label (a later year's comparative column creates the row first)."""
        try:
            import extraction_report as er
            names = {(t.filename or '').strip().lower() for t in er.ExtractionTrace.query.filter_by(
                company_id=company_id, fiscal_year=fy).all()}
            names |= {(t.filename or '').strip().lower() for t in er.ExtractionPages.query.filter_by(
                company_id=company_id, fiscal_year=fy).all()}
            return {n for n in names if n}
        except Exception:
            return set()

    for o in others:
        if not me or (o.source_filename or '').strip().lower() != me:
            continue
        # Expected: an annual report's comparative column gives the PRIOR year its turnover / net profit, and that row
        # is labelled with this report's file name. Only board / pay values under the other year are a question, and
        # only when no separate report for that year is on record.
        theirs = [k for k in body if getattr(o, k, None) not in (None, '')]
        own_files = real_files(o.fiscal_year) - {me}
        if not theirs or own_files:
            continue
        same = [k for k in theirs if getattr(row, k, None) == getattr(o, k, None)]
        pay_same = [k for k in same if k in MONEY_COLS and not k.endswith(('_retainer', '_allowance'))]
        if len(same) >= 3:
            out.append({'severity': 'error' if pay_same else 'warning',
                        'text': f'No {o.fiscal_year} report is on record - {o.fiscal_year} is labelled with this year\'s '
                                f'report ({row.source_filename}) and {len(same)} of its board / pay values equal '
                                f"{fiscal_year}\'s ({', '.join(lbl[k] for k in same[:4])}{' ...' if len(same) > 4 else ''}). "
                                + (f'CEO pay is identical too, so {fiscal_year} data was most likely copied into '
                                   f'{o.fiscal_year}.' if pay_same else 'Board counts can genuinely stay the same, but '
                                   f'check where the {o.fiscal_year} values came from.'),
                        'fix': f'Diagnose the {o.fiscal_year} annual report for this company, then download the '
                               f'{o.fiscal_year} forms and correct them; if there is no {o.fiscal_year} report, delete '
                               f'{o.fiscal_year} for this company (Settings > Companies).'})
    # repeated money figures: fee RATES (retainers / allowances) often stay the same for years, so they are not evidence;
    # turnover, profit and CEO pay repeating exactly are.
    strong = HEADLINE_COLS + tuple(k for k in MONEY_COLS if k.startswith('ceo_'))
    for o in others:
        same = [k for k in strong if getattr(row, k, None) not in (None, 0) and getattr(row, k, None) == getattr(o, k, None)]
        if not same:
            continue
        head = [k for k in same if k in HEADLINE_COLS]
        if head or len(same) >= 2:
            out.append({'severity': 'error' if head else 'warning',
                        'text': f"Same figures as {o.fiscal_year}: {', '.join(lbl[k] for k in same[:5])}"
                                f"{' ...' if len(same) > 5 else ''} - "
                                + ('turnover / profit never repeat exactly, so one year was copied or read from the other '
                                   'year\'s column.' if head else 'CEO pay rarely stays exactly the same - check both years.'),
                        'fix': 'Check both years against their own reports and correct the wrong one in section C.'})
    prev = next((o for o in others if yr and _fy_num(o.fiscal_year) == yr - 1), None)
    if prev is not None:
        try:
            import extraction_report as er
            ptexts = er.load_pages(company_id, prev.fiscal_year)
        except Exception:
            ptexts = None
        if ptexts:
            hits = []
            for k in MONEY_COLS:
                v = getattr(row, k, None)
                if v in (None, 0) or v == getattr(prev, k, None):
                    continue
                ev = er._find_value_evidence(k, v, ptexts, set())
                if ev and ev[0]['kind'] == 'beside_topic':
                    hits.append(f"{lbl[k]} (p.{ev[0]['page']})")
            if hits:
                out.append({'severity': 'error', 'text': f"These {fiscal_year} values appear in the {prev.fiscal_year} report, "
                            f"so they are probably {prev.fiscal_year} figures (the comparative column): {', '.join(hits[:5])}"
                            f"{' ...' if len(hits) > 5 else ''}.",
                            'fix': f'Take each value from the {fiscal_year} column of the {fiscal_year} report and type it in section C.'})
    return out


def consistency_checks(row, directors):
    """Arithmetic and common-sense rules between the stored fields (and the directors' register)."""
    out = []
    g = lambda k: getattr(row, k, None)
    lbl = tab_modules.all_metric_labels()

    def add(sev, text, fix=FIX_FIELD):
        out.append({'severity': sev, 'text': text, 'fix': fix})

    def sum_rule(parts, total):
        vals = [g(p) for p in parts]
        t = g(total)
        if t is None or any(v is None for v in vals):
            return
        if abs(sum(vals) - t) > 0.5:
            add('error', f"{' + '.join(lbl[p] for p in parts)} = {_num(sum(vals))}, but {lbl[total]} is {_num(t)}.")

    sum_rule(('executive_directors_count', 'non_executive_directors_count'), 'board_size')
    sum_rule(('independent_neds_count', 'non_independent_neds_count'), 'non_executive_directors_count')
    sum_rule(('directors_female', 'directors_male'), 'board_size')
    sum_rule(('neds_kenyan_count', 'neds_non_kenyan_count'), 'non_executive_directors_count')

    bs = g('board_size')
    if bs is not None and not 3 <= bs <= 25:
        add('error', f'Board size {bs} is outside the usual 3-25 range.')
    for k, lo, hi in (('board_meetings_per_year', 1, 30), ('committees_per_board', 1, 15), ('committee_meetings_per_year', 1, 150)):
        v = g(k)
        if v is not None and not lo <= v <= hi:
            add('warning', f'{lbl[k]} = {_num(v)} is outside the usual {lo}-{hi} range.')
    for k in AGE_COLS:
        v = g(k)
        if v is not None and not 25 <= v <= 95:
            add('error', f'{lbl[k]} = {_num(v)} is not a believable age.')

    named = [d for d in directors]
    if named and bs is not None and len(named) != bs:
        add('warning', f"The directors' register lists {len(named)} people but board size is {bs}.",
            'Check the board page of the report and fix board size or the register.')
    if named:
        if all(d.gender for d in named):
            f = sum(1 for d in named if d.gender == 'Female')
            if g('directors_female') is not None and g('directors_female') != f:
                add('warning', f"The register has {f} women but Female directors is {g('directors_female')}.")
        if g('neds_kenyan_count') is not None and not any(d.nationality for d in named):
            add('warning', 'Kenyan / non-Kenyan NED counts are filled but no director in the register has a nationality - '
                'confirm the counts against the report.', 'Tick OK in section C only if the report states nationalities.')
        roles = [d.role for d in named]
        if g('executive_directors_count') is not None and all(r in ('executive', 'non_executive') for r in roles):
            ex = roles.count('executive')
            if ex != g('executive_directors_count'):
                add('warning', f"The register has {ex} executive directors but Executive directors is {g('executive_directors_count')}.")

    sal, ann = g('ceo_monthly_salary'), g('ceo_annual_salary_as_stated')
    if sal and ann and abs(ann / 12 - sal) > max(1.0, 0.02 * sal):
        add('error', f'CEO annual salary {_num(ann)} / 12 = {_num(ann / 12)}, but CEO monthly salary is {_num(sal)}.')
    comps = [g(k) for k in ('ceo_monthly_salary', 'ceo_monthly_allowances', 'ceo_monthly_incentive_bonus',
                            'ceo_monthly_deferred_incentive', 'ceo_monthly_non_cash_benefits', 'ceo_monthly_pension',
                            'ceo_monthly_gratuity', 'ceo_monthly_share_value')]
    tot = g('ceo_monthly_cost_of_employment')
    parts = sum(v for v in comps if v)
    if tot and parts > tot * 1.02:
        add('error', f'CEO monthly pay components add up to {_num(parts)}, more than the cost of employment {_num(tot)}.')
    for a, b in (('chairperson_annual_retainer', 'other_ned_annual_retainer'),
                 ('chairperson_meeting_allowance', 'other_ned_meeting_allowance'),
                 ('committee_chair_annual_retainer', 'committee_member_annual_retainer'),
                 ('committee_chair_meeting_allowance', 'committee_member_meeting_allowance')):
        if g(a) is not None and g(b) is not None and g(a) < g(b):
            add('warning', f'{lbl[a]} ({_num(g(a))}) is lower than {lbl[b]} ({_num(g(b))}) - usually the chair is paid more.')

    t, p = g('turnover'), g('net_profit')
    if t and p and abs(p) > abs(t):
        add('warning', f'Net profit ({_num(p)}) is larger than turnover ({_num(t)}) - check both were read from the same '
            'statement and unit.')
    try:
        import survey_aggregate as sa
        scale = sa.UNIT_SCALE.get((row.unit or 'millions').lower())
        if scale and (row.currency or 'KES') == 'KES':
            for k in HEADLINE_COLS:
                v = g(k)
                if v is not None and abs(v) * scale / 1e6 > sa.PLAUSIBLE_MAX_MILLIONS[k]:
                    add('error', f'{lbl[k]} {_num(v)} in {row.unit} is far too large for a listed company - the unit is '
                        'probably wrong (a full-shilling figure tagged as millions).',
                        'Fix the unit on the company\'s Survey Report page, or type the value again in section C.')
    except Exception:
        pass
    return out


BLANK = '-'      # first dropdown entry = "not answered" (reportlab cannot draw a combo whose value is empty)
ROLE_OPTS = [BLANK, 'executive', 'non-executive']
YESNO_OPTS = [BLANK, 'Yes', 'No']
GENDER_OPTS = [BLANK, 'Male', 'Female']
BEN_OPTS = [BLANK, 'Provided', 'Not provided']
REG_W = [46 * mm, 27 * mm, 24 * mm, 22 * mm, 30 * mm, 27 * mm]


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont('Helvetica', 7.5)
    canvas.setFillColor(DIM)
    canvas.drawString(15 * mm, 9 * mm, 'FinSight - Manual Entry form. Fill it in, save it, then upload it on the Manual Entry page.')
    canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f'Page {doc.page}')
    canvas.restoreState()


def _quality_block(q, st):
    """'Data quality' panel printed on every form: overall score plus a per-section breakdown (scored BEFORE this form
    is applied - see data_quality.py for the rules, identical to each company's Data Quality tab)."""
    S = [Spacer(1, 8), _p('Data quality', st['h3'])]
    if not q['has_data']:
        S.append(_p(f"No survey data on file yet - 0% of the {q['total']} survey fields are complete.", st['note']))
        return S
    S.append(_table([['Overall score', 'Complete', 'Needs review', 'Missing', 'Fields'],
                     [f"{q['score']}%  ({q['grade']})", q['complete'], q['needs_review'], q['missing'], q['total']]],
                    [46 * mm, 32 * mm, 32 * mm, 32 * mm, 32 * mm], st))
    S.append(Spacer(1, 6))
    rows = [['Section', 'Complete', 'Needs review', 'Missing', 'Score']]
    rows += [[x['section'], f"{x['complete']}/{x['total']}", x['needs_review'], x['missing'], f"{x['score']}%"] for x in q['sections']]
    S.append(_table(rows, [64 * mm, 30 * mm, 30 * mm, 28 * mm, 22 * mm], st))
    S.append(Spacer(1, 3))
    S.append(_p('Score = complete fields / all survey fields, measured before this form is applied. '
                'Complete = has a value with confidence of at least 75%.', st['note']))
    return S


def _story(company, fy, st, d, generated):
    S = []
    T = lambda name, **kw: Field(name, **kw)
    S.append(_p('Manual Entry Form', st['title']))
    src = f"  |  file: {d['source_filename']}" if d.get('source_filename') else ''
    S.append(_p(f'{company.name}  |  {fy}  |  generated {generated}{src}', st['sub']))
    unit = d['unit'] or 'as printed in the report'
    head_unit = f"{d.get('currency') or 'KES'} {d.get('headline_unit') or 'millions'}"
    S.append(_p('Type into the white boxes (this PDF is fillable), save it, and upload it on the Manual Entry page - '
                'tick "Replace the year with the form" there to make this company and year exactly what the form says; '
                f'nothing is stored until an admin approves the preview. Pay figures unit: {unit}. Turnover, net profit and '
                f'market cap for this company and year are held in {head_unit}: type any value or correction in that same unit. '
                'Leave a box empty if the report does not state it.', st['note']))

    n_missing = sum(len(ms) for _, ms in d['missing_metrics']) + len(d['missing_lists'])
    n_confirm = len(d['confirm']) + len(d['conflicts'])
    n_gaps = len(d['director_gaps']) + len(d['committee_gaps']) + len(d['pay_gaps'])
    n_filled = len(d['filled'])
    n_issues = len(d['checks']) + sum(1 for f in d['filled'] if f['check']['status'] in ('derived', 'no_text'))
    S.append(Spacer(1, 6))
    S.append(_table([['Missing', 'To confirm', 'Filled - verify', 'Checks raised', 'Rows with blank cells'],
                     [n_missing, n_confirm, n_filled, len(d['checks']), n_gaps]],
                    [34 * mm, 34 * mm, 38 * mm, 36 * mm, 38 * mm], st))
    S.extend(_quality_block(d['quality'], st))

    if d['checks']:
        S.append(_p('Checks - problems found in what is on file', st['h2']))
        S.append(_p('Errors usually mean a wrong value or a file recorded against the wrong company / year. '
                    'Fix the value in section C, or follow the step shown.', st['note']))
        rows = [['', 'Problem', 'How to fix']]
        for c in sorted(d['checks'], key=lambda c: c['severity'] != 'error'):
            rows.append(['ERROR' if c['severity'] == 'error' else 'Check', c['text'], c['fix']])
        t = _table(rows, [16 * mm, 98 * mm, 66 * mm], st)
        t.setStyle(TableStyle([('TEXTCOLOR', (0, i), (0, i), colors.HexColor('#B42318') if r[0] == 'ERROR' else AMBER)
                               for i, r in enumerate(rows) if i]))
        S.append(t)

    if not (n_missing or n_confirm or n_gaps or n_filled):
        S.append(Spacer(1, 8))
        S.append(_p('Nothing is on file or missing for this company and year.', st['h3']))
        return S

    if d['missing_metrics']:
        S.append(_p('A. Missing values', st['h2']))
        for tab, ms in d['missing_metrics']:
            S.append(_p(tab, st['h3']))
            S.append(_table([['Field', 'Enter value', 'Source page / note']] +
                            [[lbl, T(f'metric|{k}'), T(f'metricnote|{k}')] for k, lbl in ms], [72 * mm, 50 * mm, 58 * mm], st))
    if d['missing_lists']:
        names = {'register': "Directors' Register", 'pay_rows': 'Director pay rows',
                 'committees': 'Committees', 'ned_benefits': 'NED benefits'}
        S.append(_p('Empty tables (nothing on file yet): ' + ', '.join(names.get(k, k) for k in d['missing_lists']), st['h3']))

    if d['confirm'] or d['conflicts']:
        S.append(_p('B. Please confirm (low-confidence or conflicting values)', st['h2']))
        if d['confirm']:
            S.append(_p('The box holds the stored value: tick "OK" if it is right, or overwrite it with the correct value.', st['note']))
            rows = [['Field', 'Stored value', 'Conf.', 'Source / report check', 'OK?', 'Value (edit if wrong)']]
            for c in d['confirm']:
                chk = c.get('check') or {}
                rows.append([c['label'], _num(c['value']), f"{round(c['confidence'] * 100)}%",
                             f"{_short_source(c['source'])}{' | ' + chk['text'] if chk.get('text') else ''}",
                             T(f"confirmok|{c['key']}", kind='check'), T(f"val|{c['key']}", value=_prefill(c['value']))])
            S.append(_table(rows, [40 * mm, 28 * mm, 13 * mm, 45 * mm, 12 * mm, 42 * mm], st))
        if d['conflicts']:
            S.append(_p('Conflicting readings - type the correct figure', st['h3']))
            rows = [['Field', 'Reading A (page)', 'Reading B (page)', 'Correct value']]
            for c in d['conflicts']:
                rows.append([c['label'], f"{_num(c['a'])} (p.{c['a_page'] or '?'})", f"{_num(c['b'])} (p.{c['b_page'] or '?'})",
                             T(f"conflict|{c['label']}")])
            S.append(_table(rows, [50 * mm, 40 * mm, 40 * mm, 50 * mm], st))

    if d['filled']:
        S.append(_p('C. Values on file - check each against the annual report', st['h2']))
        found = sum(1 for f in d['filled'] if f['check']['status'] == 'found')
        S.append(_p(f'{n_filled} filled field(s). {found} were found word-for-word in the stored report text; the rest were '
                    'counted, averaged or converted by a reader, or typed by a person. Open the report at the page shown, then '
                    'tick "OK" if the value is right, or overwrite the box with the correct value (same unit as shown); empty the box '
                    'if the report does not disclose it. Shaded rows were not found '
                    'as printed (counted, averaged or converted by a reader) - check them first.', st['note']))
        by_sec = {}
        for f in d['filled']:
            by_sec.setdefault(f['section'], []).append(f)
        for sec, items in by_sec.items():
            S.append(_p(sec, st['h3']))
            rows = [['Field', 'On file', 'Source / page', 'Report check', 'OK?', 'Value (edit if wrong)']]
            shade = []
            for i, f in enumerate(items, 1):
                val = _num(f['value']) + (f" {f['unit']}" if f['unit'] else '')
                if f['confidence'] is not None:
                    val += f" ({round(f['confidence'] * 100)}%)"
                rows.append([f['label'], val, _short_source(f['source']), f['check']['text'],
                             T(f"confirmok|{f['key']}", kind='check'), T(f"val|{f['key']}", value=_prefill(f['value']))])
                if f['check']['status'] in ('derived', 'no_text'):
                    shade.append(i)
            t = _table(rows, [36 * mm, 30 * mm, 38 * mm, 36 * mm, 10 * mm, 30 * mm], st)
            t.setStyle(TableStyle([('BACKGROUND', (0, i), (3, i), colors.HexColor('#FFF6E0')) for i in shade]))
            S.append(t)

    empty = set(d['missing_lists'])
    if d['directors'] or d['committee_gaps'] or d['pay_gaps'] or empty & {'register', 'committees', 'pay_rows'}:
        S.append(_p('D. Tables to check and complete', st['h2']))
        reg_head = ['Name', 'Role', 'Independent', 'Gender', 'Nationality', 'Appointed']

        def reg_cells(prefix, missing):
            def cell(attr, kind, opts=None):
                return T(f'{prefix}|{attr}', kind=kind, options=opts) if attr in missing else 'on file'
            return [cell('role', 'choice', ROLE_OPTS), cell('independent', 'choice', YESNO_OPTS),
                    cell('gender', 'choice', GENDER_OPTS), cell('nationality', 'text'), cell('appointed', 'text')]

        if d['directors']:
            S.append(_p("Directors' Register - check what is on file, fill the empty boxes", st['h3']))
            S.append(_p('Every box holds what is on file now - change only what is wrong. Empty a name to remove that '
                        'director; use the blank rows at the bottom to add one.', st['note']))
            gap_ids = {g['id']: g for g in d['director_gaps']}
            m = {'Role': 'role', 'Independent': 'independent', 'Gender': 'gender', 'Nationality': 'nationality',
                 'Appointed': 'appointed'}
            rows = [reg_head]
            for dr in d['directors']:
                miss = {m[x] for x in gap_ids[dr.id]['missing']} if dr.id in gap_ids else set()
                shown = {'role': (dr.role or '').replace('_', '-'), 'independent': '' if dr.independent is None else
                         ('Yes' if dr.independent else 'No'), 'gender': dr.gender or '', 'nationality': dr.nationality or '',
                         'appointed': dr.appointed_date or ''}
                cells = reg_cells(f"dir|{dr.id}", miss)
                opts = {'role': ROLE_OPTS, 'independent': YESNO_OPTS, 'gender': GENDER_OPTS}
                out = []
                for c, a in zip(cells, ('role', 'independent', 'gender', 'nationality', 'appointed')):
                    if not isinstance(c, str):
                        out.append(c)                       # blank cell -> empty box (dir|...)
                    elif a in opts and shown[a] in opts[a]:
                        out.append(T(f"dirfix|{dr.id}|{a}", kind='choice', options=opts[a], value=shown[a]))
                    else:                                   # filled cell -> box holding the stored value, change it if wrong
                        out.append(T(f"dirfix|{dr.id}|{a}", value=_prefill(shown[a])))
                cells = out
                rows.append([T(f"dirname|{dr.id}", value=_prefill(dr.director_name))] + cells)
            allm = {'role', 'independent', 'gender', 'nationality', 'appointed'}
            # enough blank rows to bring the register up to a 15-person board (at least 3)
            rows += [[T(f'newdir|{n}|name')] + reg_cells(f'newdir|{n}', allm) for n in range(max(3, 15 - len(d['directors'])))]
            S.append(_table(rows, REG_W, st))
        elif 'register' in empty:
            S.append(_p("Directors' Register - add every director", st['h3']))
            allm = {'role', 'independent', 'gender', 'nationality', 'appointed'}
            S.append(_table([reg_head] + [[T(f'newdir|{n}|name')] + reg_cells(f'newdir|{n}', allm)
                                          for n in range(BLANK_ROWS + 4)], REG_W, st))

        com_head = ['Committee', 'Chair', 'Meetings', 'Members (comma separated)']
        com_w = [46 * mm, 40 * mm, 22 * mm, 72 * mm]
        if d['committee_gaps']:
            S.append(_p('Committees - fill the empty boxes', st['h3']))
            rows = [com_head]
            for g in d['committee_gaps']:
                m = set(g['missing'])
                rows.append([g['name'],
                             T(f"com|{g['id']}|chair") if 'Chair' in m else 'on file',
                             T(f"com|{g['id']}|meetings") if 'Meetings' in m else 'on file',
                             T(f"com|{g['id']}|members") if 'Members' in m else 'on file'])
            S.append(_table(rows, com_w, st))
        elif 'committees' in empty:
            S.append(_p('Committees - add each committee', st['h3']))
            S.append(_table([com_head] + [[T(f'newcom|{n}|name'), T(f'newcom|{n}|chair'), T(f'newcom|{n}|meetings'),
                                            T(f'newcom|{n}|members')] for n in range(BLANK_ROWS)], com_w, st))

        pay_head = ['Name', 'Total', 'Components (e.g. Fees=5000, Allowances=1200)']
        if d['pay_gaps']:
            S.append(_p('Director pay - these rows have no total', st['h3']))
            S.append(_table([pay_head] + [[g['name'], T(f"pay|{g['id']}|total"), T(f"pay|{g['id']}|components")]
                                          for g in d['pay_gaps']], [55 * mm, 35 * mm, 90 * mm], st))
        elif 'pay_rows' in empty:
            S.append(_p('Director pay rows - add each director', st['h3']))
            S.append(_table([['Name', 'Role', 'Total', 'Components (e.g. Fees=5000, Allowances=1200)']] +
                            [[T(f'newpay|{n}|name'), T(f'newpay|{n}|role', kind='choice', options=ROLE_OPTS),
                              T(f'newpay|{n}|total'), T(f'newpay|{n}|components')] for n in range(BLANK_ROWS + 2)],
                            [45 * mm, 28 * mm, 30 * mm, 77 * mm], st))
    if 'ned_benefits' in empty and d['benefits_open']:
        S.append(_p('E. NED benefits - choose one per line', st['h2']))
        rows = [['Benefit', 'Status', 'Detail / page']]
        rows += [[k, T(f'ben|{k}', kind='choice', options=BEN_OPTS), T(f'bendetail|{k}')] for k in d['benefits_open']]
        S.append(_table(rows, [55 * mm, 40 * mm, 85 * mm], st))
    return S


def _render(company, fy, d=None):
    d = d if d is not None else collect(company.id, fy)
    buf = BytesIO()
    meta = json.dumps({'v': 2, 'cid': company.id, 'fy': fy, 'name': company.name})

    def first_page(canvas, doc):
        # Hidden read-only field: tells the upload which company + year this form belongs to.
        canvas.acroForm.textfield(name='meta', value=meta, x=0, y=0, width=1, height=1, borderWidth=0,
                                  annotationFlags='hidden', fieldFlags='readOnly', fontSize=1)
        _footer(canvas, doc)

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm,
                            bottomMargin=16 * mm, title=f'Manual Entry Form - {company.name} - {fy}')
    doc.build(_story(company, fy, _styles(), d, datetime.now().strftime('%d %b %Y')), onFirstPage=first_page, onLaterPages=_footer)
    buf.seek(0)
    return buf


def build_company_form(company_id, fiscal_year=None):
    company = Company.query.get_or_404(company_id)
    fy = fiscal_year or latest_year(company_id) or 'No year yet'
    return _render(company, fy), f'Manual_Entry_Form_{_safe(company.name)}_{_safe(fy)}.pdf'


def _issues(d):
    """Plain-language problem list for the summary: errors / checks first, then values not found in the report."""
    out = [('error' if c['severity'] == 'error' else 'check', c['text'], c['fix']) for c in d['checks']]
    out.sort(key=lambda x: x[0] != 'error')
    nf = [f['label'] for f in d['filled'] if f['check']['status'] == 'derived']
    if nf:
        out.append(('verify', f"{len(nf)} value(s) not word-for-word in the report: {', '.join(nf[:6])}"
                    + (' ...' if len(nf) > 6 else ''), 'Check them in section C of the form.'))
    if d['filled'] and not d['text_stored']:
        out.append(('verify', 'Report text not stored - every value must be checked by hand.', 'Check section C of the form.'))
    return out


def build_all_forms_zip(fiscal_year=None):
    """One PDF per company, RANKED by most problems (errors, then checks, then missing fields).
    With `fiscal_year`, every form in the zip is for that one year; companies with no data for it get a blank form
    (ranked last, listed in the summary) so the year can be typed in. Without it, each company uses its own latest year.
    Every form lists ALL 40 survey fields: missing ones to fill (A), low-confidence ones to confirm (B) and every value
    already on file with its source page and a check against the stored report text (C)."""
    entries, no_year = [], []
    for c in Company.query.order_by(Company.name).all():
        years = {p.period_label for p in FinancialPeriod.query.filter_by(company_id=c.id).all()}
        years |= {r.fiscal_year for r in SurveyCompanyData.query.filter_by(company_id=c.id).all()}
        if fiscal_year:
            fy = fiscal_year
            if fiscal_year not in years:
                no_year.append(c.name)       # still gets a blank form for this year (ranked last) to type it in
        else:
            fy = latest_year(c.id) or 'No year yet'
        d = collect(c.id, fy)
        iss = _issues(d)
        entries.append({'company': c, 'fy': fy, 'd': d, 'q': d['quality'], 'issues': iss, 'has_data': d['quality']['has_data'],
                        'errors': sum(1 for i in iss if i[0] == 'error')})
    entries.sort(key=lambda e: (not e['has_data'], -e['errors'], -len(e['issues']), -e['q']['missing'],
                                (e['company'].name or '').lower()))
    width = max(2, len(str(len(entries))))
    summary = []
    for i, e in enumerate(entries, 1):
        c = e['company']
        e['file'] = f'{i:0{width}d}_Manual_Entry_Form_{_safe(c.name)}_{_safe(e["fy"])}.pdf'
        summary.append({'rank': i, 'company': c.name, 'ticker': c.ticker, 'fiscal_year': e['fy'], 'file': e['file'], 'q': e['q'],
                        'issues': e['issues'], 'errors': e['errors'], 'filled': len(e['d']['filled']),
                        'found': sum(1 for f in e['d']['filled'] if f['check']['status'] == 'found')})
    out = BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr(f'{SUMMARY_PREFIX}.pdf', _render_summary(summary, fiscal_year, no_year).getvalue())
        z.writestr(f'{SUMMARY_PREFIX}.csv', _summary_csv(summary, no_year))
        for e in entries:
            z.writestr(e['file'], _render(e['company'], e['fy'], e['d']).getvalue())
    out.seek(0)
    tag = _safe(fiscal_year) + '_' if fiscal_year else ''
    return out, f'Manual_Entry_Forms_All_Companies_{tag}{datetime.now().strftime("%Y%m%d")}.zip'


def _average(summary):
    scored = [x['q']['score'] for x in summary if x['q']['has_data']]
    return round(sum(scored) / len(scored)) if scored else 0


def _render_summary(summary, fiscal_year=None, no_year=()):
    """Read-only overview for the zip: companies RANKED from most missing survey fields to fewest (rank = the number in
    the form's filename), the average data quality, and what each form still needs."""
    st, buf, generated = _styles(), BytesIO(), datetime.now().strftime('%d %b %Y')
    avg = _average(summary)
    scored = sum(1 for x in summary if x['q']['has_data'])
    nfields = len(tab_modules.all_metric_labels())
    S = [_p('Data Quality Summary', st['title']),
         _p(f"{len(summary)} companies  |  {fiscal_year or 'each company at its latest year'}  |  generated {generated}  |  "
            'ranked by most problems first', st['sub']), Spacer(1, 6),
         _table([['Average data quality', 'Companies with survey data', 'Most missing', 'Fewest missing'],
                 [f'{avg}%  ({data_quality.grade(avg)})', f'{scored} of {len(summary)}',
                  _missing_extreme(summary, max), _missing_extreme(summary, min)]], [44 * mm, 42 * mm, 44 * mm, 44 * mm], st),
         Spacer(1, 4),
         _p(f'Missing = survey fields (of {nfields}) with no value on file. Score = complete fields / {nfields}, measured before any '
            'form is applied. The average covers only companies that have survey data on file; a company with none is listed '
            f'at 0% with all {nfields} fields missing but is not averaged in. This page is not a form - do not upload it.', st['note']),
         Spacer(1, 6)]
    rows = [['#', 'Company', 'Year', 'Errors', 'Checks', 'Filled (found in report)', 'Missing', 'Score']]
    for x in summary:
        q = x['q']
        rows.append([x['rank'], x['company'], x['fiscal_year'], x['errors'], len(x['issues']) - x['errors'],
                     f"{x['filled']} ({x['found']})", q['missing'], f"{q['score']}%"])
    S.append(_table(rows, [8 * mm, 42 * mm, 17 * mm, 15 * mm, 15 * mm, 35 * mm, 18 * mm, 16 * mm], st))
    if no_year:
        S.append(Spacer(1, 4))
        S.append(_p(f"No {fiscal_year} data on file (blank form included, ranked last): " + ', '.join(no_year), st['note']))
    probs = [x for x in summary if x['issues']]
    if probs:
        S.append(_p('Problems by company - and how to fix them', st['h2']))
        S.append(_p('ERROR = a value that breaks a rule, or a file that looks like the wrong company / year / not the annual '
                    'report (those should be deleted and re-uploaded). CHECK = worth a look. VERIFY = compare with the report '
                    'in section C of that company\'s form.', st['note']))
        for x in probs:
            S.append(_p(f"{x['rank']}. {x['company']} - {x['fiscal_year']}  ({x['file']})", st['h3']))
            S.append(_table([['', 'Problem', 'How to fix']] + [[k.upper(), t, f] for k, t, f in x['issues']],
                            [16 * mm, 100 * mm, 64 * mm], st))
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm,
                            bottomMargin=16 * mm, title='Manual Entry - Data Quality Summary')

    def foot(canvas, d):
        canvas.saveState(); canvas.setFont('Helvetica', 7.5); canvas.setFillColor(DIM)
        canvas.drawString(15 * mm, 9 * mm, 'FinSight - Data Quality Summary (read-only; not a form)')
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f'Page {d.page}'); canvas.restoreState()
    doc.build(S, onFirstPage=foot, onLaterPages=foot)
    buf.seek(0)
    return buf


def _missing_extreme(summary, fn):
    scored = [x for x in summary if x['q']['has_data']]
    if not scored:
        return '-'
    x = fn(scored, key=lambda r: r['q']['missing'])
    return f"{x['company']} ({x['q']['missing']})"


def _summary_csv(summary, no_year=()):
    """Same ranking as the summary PDF, one row per company plus one row per problem - opens in Excel."""
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(['Rank', 'Company', 'Ticker', 'Fiscal year', 'Errors', 'Checks', 'Filled fields', 'Found in report text',
                'Missing fields', 'Needs review', 'Data quality %', 'Form file', 'Problem type', 'Problem', 'How to fix',
                'Missing field names'])
    for x in summary:
        q = x['q']
        base = [x['rank'], x['company'], x['ticker'] or '', x['fiscal_year'], x['errors'], len(x['issues']) - x['errors'],
                x['filled'], x['found'], q['missing'], q['needs_review'], q['score'], x['file']]
        if not x['issues']:
            w.writerow(base + ['', '', '', '; '.join(q['missing_fields'])])
        for i, (k, t, f) in enumerate(x['issues']):
            w.writerow(base + [k.upper(), t, f, '; '.join(q['missing_fields']) if i == 0 else ''])
    w.writerow([])
    w.writerow(['AVERAGE data quality % (companies with survey data)', '', '', '', '', '', '', '', '', '', _average(summary)])
    return buf.getvalue().encode('utf-8-sig')