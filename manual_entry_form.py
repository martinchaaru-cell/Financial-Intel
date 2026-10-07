"""Downloadable Manual Entry forms (PDF).

One form per company + fiscal year, listing only what still needs a person:
  A. MISSING       - metrics no reader could extract, plus empty tables (register / pay rows / committees / benefits)
  B. TO CONFIRM    - stored values with low confidence (< 0.75, the same threshold the Data Quality tab uses)
                     and any unresolved evidence conflicts
  C. Register / committee rows that exist but have blank cells (gender, nationality, chair, ...)

build_company_form(company_id, fiscal_year=None) -> (BytesIO, filename)
build_all_forms_zip(fiscal_year=None)            -> (BytesIO, filename)   one PDF per company inside

The PDFs are FILLABLE: every blank is a real form field (type into it with Adobe Reader, Preview, Chrome, Edge, ...),
and the file carries a hidden `meta` field naming the company and year. Upload the filled file(s) - or a zip of them -
on the Manual Entry page (manual_entry_upload.py reads the fields back). A printed-and-handwritten or scanned form
cannot be read back; type into the PDF, or enter those values on the page by hand.

Field names (parsed by manual_entry_upload.py - keep the two in sync):
  meta                      hidden JSON {"v":1,"cid":..,"fy":..,"name":..}
  metric|<column>           a missing metric        metricnote|<column>  its source page / note
  confirm|<column>          corrected value for a low-confidence metric      confirmok|<column>  "stored value is right"
  conflict|<field>          the correct figure for an unresolved conflict
  dir|<id>|<attr>           blank cell of an existing register row (attr: role, independent, gender, nationality, appointed)
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
from models import Company, FinancialPeriod, DirectorRemunerationRow, Committee, CommitteeMember
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

    def __init__(self, name, kind='text', options=None, height=14):
        super().__init__()
        self.name, self.kind, self.options, self.height = name, kind, options or [], height
        self.width = 60

    def wrap(self, availWidth, availHeight):
        self.width = max(availWidth, 10)
        return (self.width if self.kind != 'check' else 12), self.height

    def draw(self):
        af, val = self.canv.acroForm, Field.prefill.get(self.name, '')
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

    return {'quality': data_quality.company_quality(row), 'status': status, 'missing_metrics': missing_metrics, 'missing_lists': missing_lists,
            'confirm': confirm, 'conflicts': conflicts, 'director_gaps': gaps, 'committee_gaps': comm_gaps,
            'pay_gaps': pay_gaps, 'benefits_open': benefits_open, 'unit': row.director_figures_unit if row else None,
            'currency': row.currency if row else None}


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
    S.append(_p(f'{company.name}  |  {fy}  |  generated {generated}', st['sub']))
    unit = d['unit'] or 'as printed in the report'
    S.append(_p('Type into the white boxes (this PDF is fillable), save it, and upload it on the Manual Entry page - '
                f'nothing is stored until an admin approves the preview. Pay figures unit: {unit}. Turnover, net profit and market cap: always in millions of the report currency (e.g. a report showing 41,886,782 in thousands = 41,886.8 million). '
                'Leave a box empty if the report does not state it.', st['note']))

    n_missing = sum(len(ms) for _, ms in d['missing_metrics']) + len(d['missing_lists'])
    n_confirm = len(d['confirm']) + len(d['conflicts'])
    n_gaps = len(d['director_gaps']) + len(d['committee_gaps']) + len(d['pay_gaps'])
    S.append(Spacer(1, 6))
    S.append(_table([['Missing items', 'To confirm', 'Rows with blank cells'], [n_missing, n_confirm, n_gaps]],
                    [58 * mm, 58 * mm, 58 * mm], st))
    S.extend(_quality_block(d['quality'], st))
    if not (n_missing or n_confirm or n_gaps):
        S.append(Spacer(1, 8))
        S.append(_p('Nothing is missing or waiting for confirmation for this company and year.', st['h3']))
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
            S.append(_p('Tick "OK" if the stored value is right, or type the correct value.', st['note']))
            rows = [['Field', 'Stored value', 'Conf.', 'Source', 'OK?', 'Correct value']]
            for c in d['confirm']:
                rows.append([c['label'], _num(c['value']), f"{round(c['confidence'] * 100)}%", (c['source'] or '')[:60],
                             T(f"confirmok|{c['key']}", kind='check'), T(f"confirm|{c['key']}")])
            S.append(_table(rows, [40 * mm, 28 * mm, 13 * mm, 45 * mm, 12 * mm, 42 * mm], st))
        if d['conflicts']:
            S.append(_p('Conflicting readings - type the correct figure', st['h3']))
            rows = [['Field', 'Reading A (page)', 'Reading B (page)', 'Correct value']]
            for c in d['conflicts']:
                rows.append([c['label'], f"{_num(c['a'])} (p.{c['a_page'] or '?'})", f"{_num(c['b'])} (p.{c['b_page'] or '?'})",
                             T(f"conflict|{c['label']}")])
            S.append(_table(rows, [50 * mm, 40 * mm, 40 * mm, 50 * mm], st))

    empty = set(d['missing_lists'])
    if d['director_gaps'] or d['committee_gaps'] or d['pay_gaps'] or empty & {'register', 'committees', 'pay_rows'}:
        S.append(_p('C. Tables to complete', st['h2']))
        reg_head = ['Name', 'Role', 'Independent', 'Gender', 'Nationality', 'Appointed']

        def reg_cells(prefix, missing):
            def cell(attr, kind, opts=None):
                return T(f'{prefix}|{attr}', kind=kind, options=opts) if attr in missing else 'on file'
            return [cell('role', 'choice', ROLE_OPTS), cell('independent', 'choice', YESNO_OPTS),
                    cell('gender', 'choice', GENDER_OPTS), cell('nationality', 'text'), cell('appointed', 'text')]

        if d['director_gaps']:
            S.append(_p("Directors' Register - fill the empty boxes", st['h3']))
            rows = [reg_head]
            for g in d['director_gaps']:
                m = {'Role': 'role', 'Independent': 'independent', 'Gender': 'gender', 'Nationality': 'nationality',
                     'Appointed': 'appointed'}
                rows.append([g['name']] + reg_cells(f"dir|{g['id']}", {m[x] for x in g['missing']}))
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
        S.append(_p('D. NED benefits - choose one per line', st['h2']))
        rows = [['Benefit', 'Status', 'Detail / page']]
        rows += [[k, T(f'ben|{k}', kind='choice', options=BEN_OPTS), T(f'bendetail|{k}')] for k in d['benefits_open']]
        S.append(_table(rows, [55 * mm, 40 * mm, 85 * mm], st))
    return S


def _render(company, fy):
    d = collect(company.id, fy)
    buf = BytesIO()
    meta = json.dumps({'v': 1, 'cid': company.id, 'fy': fy, 'name': company.name})

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


def build_all_forms_zip(fiscal_year=None):
    """One PDF per company, RANKED: file 01 is the company with the most missing survey fields, then descending.
    The rank is the filename prefix (01_, 02_, ...) so any file browser sorts them the same way as the summary
    (00_Data_Quality_Summary.pdf / .csv, written first). Each company uses `fiscal_year` if given and it has data for it,
    else its own latest year. Every form lists ALL of that company's missing fields (section A) - a company with no
    survey data yet gets a form with all of them."""
    entries = []
    for c in Company.query.order_by(Company.name).all():
        years = {p.period_label for p in FinancialPeriod.query.filter_by(company_id=c.id).all()}
        years |= {r.fiscal_year for r in SurveyCompanyData.query.filter_by(company_id=c.id).all()}
        fy = fiscal_year if fiscal_year in years else (latest_year(c.id) or 'No year yet')
        entries.append({'company': c, 'fy': fy, 'q': data_quality.company_quality(tab_modules.survey_row(c.id, fy))})
    entries.sort(key=lambda e: (-e['q']['missing'], e['q']['score'], (e['company'].name or '').lower()))
    width = max(2, len(str(len(entries))))
    summary = []
    for i, e in enumerate(entries, 1):
        c = e['company']
        e['file'] = f'{i:0{width}d}_Manual_Entry_Form_{_safe(c.name)}_{_safe(e["fy"])}.pdf'
        summary.append({'rank': i, 'company': c.name, 'ticker': c.ticker, 'fiscal_year': e['fy'], 'file': e['file'], 'q': e['q']})
    out = BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr(f'{SUMMARY_PREFIX}.pdf', _render_summary(summary).getvalue())
        z.writestr(f'{SUMMARY_PREFIX}.csv', _summary_csv(summary))
        for e in entries:
            z.writestr(e['file'], _render(e['company'], e['fy']).getvalue())
    out.seek(0)
    return out, f'Manual_Entry_Forms_All_Companies_{datetime.now().strftime("%Y%m%d")}.zip'


def _average(summary):
    scored = [x['q']['score'] for x in summary if x['q']['has_data']]
    return round(sum(scored) / len(scored)) if scored else 0


def _render_summary(summary):
    """Read-only overview for the zip: companies RANKED from most missing survey fields to fewest (rank = the number in
    the form's filename), the average data quality, and what each form still needs."""
    st, buf, generated = _styles(), BytesIO(), datetime.now().strftime('%d %b %Y')
    avg = _average(summary)
    scored = sum(1 for x in summary if x['q']['has_data'])
    nfields = len(tab_modules.all_metric_labels())
    S = [_p('Data Quality Summary', st['title']),
         _p(f'{len(summary)} companies  |  generated {generated}  |  ranked from most to fewest missing fields', st['sub']), Spacer(1, 6),
         _table([['Average data quality', 'Companies with survey data', 'Most missing', 'Fewest missing'],
                 [f'{avg}%  ({data_quality.grade(avg)})', f'{scored} of {len(summary)}',
                  _missing_extreme(summary, max), _missing_extreme(summary, min)]], [44 * mm, 42 * mm, 44 * mm, 44 * mm], st),
         Spacer(1, 4),
         _p(f'Missing = survey fields (of {nfields}) with no value on file. Score = complete fields / {nfields}, measured before any '
            'form is applied. The average covers only companies that have survey data on file; a company with none is listed '
            f'at 0% with all {nfields} fields missing but is not averaged in. This page is not a form - do not upload it.', st['note']),
         Spacer(1, 6)]
    rows = [['#', 'Company', 'Year', 'Missing', 'Complete', 'Review', 'Score', 'Form file']]
    for x in summary:
        q = x['q']
        rows.append([x['rank'], x['company'], x['fiscal_year'], q['missing'], q['complete'], q['needs_review'], f"{q['score']}%", x['file']])
    S.append(_table(rows, [8 * mm, 31 * mm, 16 * mm, 16 * mm, 20 * mm, 17 * mm, 14 * mm, 52 * mm], st))
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


def _summary_csv(summary):
    """Same ranking and numbers as the summary PDF, plus the NAMES of each company's missing fields - opens in Excel."""
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(['Rank (most missing first)', 'Company', 'Ticker', 'Fiscal year', 'Missing fields', 'Complete', 'Needs review',
                'Total fields', 'Data quality %', 'Grade', 'Form file', 'Missing field names'])
    for x in summary:
        q = x['q']
        w.writerow([x['rank'], x['company'], x['ticker'] or '', x['fiscal_year'], q['missing'], q['complete'], q['needs_review'],
                    q['total'], q['score'], q['grade'], x['file'], '; '.join(q['missing_fields'])])
    w.writerow([])
    w.writerow(['AVERAGE data quality % (companies with survey data)', '', '', '', '', '', '', '', _average(summary)])
    return buf.getvalue().encode('utf-8-sig')