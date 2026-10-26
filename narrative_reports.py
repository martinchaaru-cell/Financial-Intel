"""
Narrative PDF reports for Financial Intel.

Two reports, both written as readable analysis rather than as filled-in forms:

  build_combined_report(overview, rows, trends)
      One PDF for a selected fiscal year that joins the Survey Analytics & Reporting
      findings (the aggregate survey) with the Intelligence Layer findings (rankings,
      governance, sector benchmarks, year-on-year movement).

  build_company_report(payload, overview, rows)
      One PDF for a single company and fiscal year - the same story told for one
      board: who sits on it, how they are paid, and how that compares with the
      market - as shown on the Survey Report page.

House rules (the same discipline as the rest of the survey pipeline):
- Every sentence is generated from stored figures. A sentence whose figure is missing is
  simply not written - nothing is estimated, defaulted to 0 or filled in with filler text.
- An average is only narrated when survey_aggregate decided enough companies supplied it
  (MIN_COMPANIES_FOR_AVERAGE); otherwise the report says there is not enough data.
- Amounts are always written in full (no K/M/B suffixes, no "millions" wording).
- A figure that is mathematically possible but not credible (e.g. an average profit margin of
  1770%) is left out of the narrative and the data-quality note says why.
"""

from io import BytesIO
from datetime import datetime
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                                PageBreak, KeepTogether, CondPageBreak)
from reportlab.graphics.shapes import Drawing, Rect, String, Line, PolyLine, Circle, Wedge

from survey_aggregate import UNIT_SCALE, MIN_COMPANIES_FOR_AVERAGE, QUALITY_FIELDS

APP_NAME = 'Financial Intel'
APP_TAGLINE = 'Financial Intelligence'

NAVY = colors.HexColor('#0B1F44')
NAVY_2 = colors.HexColor('#12326B')
BLUE = colors.HexColor('#2F6BFF')
SKY = colors.HexColor('#8FB4FF')
GREEN = colors.HexColor('#1FA971')
AMBER = colors.HexColor('#C98A0B')
PURPLE = colors.HexColor('#7C5CD6')
RED = colors.HexColor('#D24B4B')
INK = colors.HexColor('#1E2635')
DIM = colors.HexColor('#566074')
FAINT = colors.HexColor('#8B93A8')
PANEL = colors.HexColor('#F3F6FC')
RULE = colors.HexColor('#DCE3F0')

PAGE_W, PAGE_H = A4
LM = RM = 20 * mm
CONTENT_W = PAGE_W - LM - RM


# ----------------------------------------------------------------------------- styles
def _styles():
    s = {}
    s['body'] = ParagraphStyle('body', fontName='Helvetica', fontSize=10, leading=15.5, textColor=INK,
                               alignment=TA_JUSTIFY, spaceAfter=7)
    s['lead'] = ParagraphStyle('lead', parent=s['body'], fontSize=11.5, leading=17.5, textColor=NAVY_2, spaceAfter=9)
    s['h1'] = ParagraphStyle('h1', fontName='Helvetica-Bold', fontSize=19, leading=24, textColor=NAVY, spaceAfter=3)
    s['kicker'] = ParagraphStyle('kicker', fontName='Helvetica-Bold', fontSize=8, leading=11, textColor=BLUE, spaceAfter=2)
    s['h2'] = ParagraphStyle('h2', fontName='Helvetica-Bold', fontSize=12.5, leading=16, textColor=NAVY,
                             spaceBefore=8, spaceAfter=4)
    s['standfirst'] = ParagraphStyle('standfirst', fontName='Helvetica-Oblique', fontSize=10.5, leading=15, textColor=DIM,
                                     spaceAfter=10)
    s['callout'] = ParagraphStyle('callout', fontName='Helvetica-Bold', fontSize=11, leading=16, textColor=NAVY)
    s['caption'] = ParagraphStyle('caption', fontName='Helvetica-Oblique', fontSize=8, leading=11, textColor=FAINT,
                                  spaceBefore=2, spaceAfter=8)
    s['small'] = ParagraphStyle('small', fontName='Helvetica', fontSize=8.5, leading=12, textColor=DIM, spaceAfter=4)
    s['kpi_l'] = ParagraphStyle('kpi_l', fontName='Helvetica', fontSize=7.5, leading=10, textColor=DIM)
    s['kpi_v'] = ParagraphStyle('kpi_v', fontName='Helvetica-Bold', fontSize=11.5, leading=14, textColor=NAVY)
    s['th'] = ParagraphStyle('th', fontName='Helvetica-Bold', fontSize=8, leading=10.5, textColor=colors.white)
    s['td'] = ParagraphStyle('td', fontName='Helvetica', fontSize=8.5, leading=11.5, textColor=INK)
    s['td_r'] = ParagraphStyle('td_r', parent=s['td'], alignment=2)
    s['td_f'] = ParagraphStyle('td_f', parent=s['td'], textColor=FAINT, fontName='Helvetica-Oblique')
    s['th_s'] = ParagraphStyle('th_s', fontName='Helvetica-Bold', fontSize=7, leading=9, textColor=colors.white)
    s['th_sr'] = ParagraphStyle('th_sr', parent=s['th_s'], alignment=2)
    s['td_s'] = ParagraphStyle('td_s', fontName='Helvetica', fontSize=7.4, leading=9.6, textColor=INK)
    s['td_sr'] = ParagraphStyle('td_sr', parent=s['td_s'], alignment=2)
    s['td_sb'] = ParagraphStyle('td_sb', parent=s['td_s'], fontName='Helvetica-Bold')
    s['td_srb'] = ParagraphStyle('td_srb', parent=s['td_sr'], fontName='Helvetica-Bold')
    s['bullet'] = ParagraphStyle('bullet', parent=s['body'], alignment=0, leftIndent=11, bulletIndent=0, spaceAfter=4)
    s['toc'] = ParagraphStyle('toc', fontName='Helvetica', fontSize=10.5, leading=19, textColor=INK, leftIndent=2)
    return s


# ----------------------------------------------------------------------------- text helpers
def _e(v):
    return _xml_escape(str(v)) if v is not None else ''


def _num(value, scale=1):
    """Amount in full: 1,234,567 or 1,234,567.5 - never abbreviated."""
    if value is None:
        return None
    v = round(float(value) * scale, 2)
    out = f"{abs(v):,.2f}".rstrip('0').rstrip('.')
    return ('-' if v < 0 else '') + out


def _money(cur, value, scale=1):
    n = _num(value, scale)
    return None if n is None else f"{cur} {n}"


def _and(items):
    items = [i for i in items if i]
    if not items:
        return ''
    if len(items) == 1:
        return items[0]
    return ', '.join(items[:-1]) + ' and ' + items[-1]


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + 's')


def _pct(part, whole, dp=0):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, dp) if dp else int(round(100.0 * part / whole))


def _avg(m):
    return m.get('average') if m else None


def _cnt(m):
    return (m or {}).get('company_count', 0)


def _trunc(s, n):
    s = str(s or '')
    return s if len(s) <= n else s[:n - 1].rstrip() + '…'


def _median(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    k = len(v) // 2
    return v[k] if len(v) % 2 else (v[k - 1] + v[k]) / 2


def _times(x):
    return f"{x:.2f}".rstrip('0').rstrip('.') + ' times'


def _relative(value, ref):
    """'12% above' / '8% below' / 'in line with' phrase for value vs ref, or None."""
    if value is None or not ref:
        return None
    d = (value - ref) / abs(ref) * 100
    if abs(d) < 3:
        return 'in line with'
    return f"{abs(d):.0f}% {'above' if d > 0 else 'below'}"


# ----------------------------------------------------------------------------- brand
def _draw_logo(c, x, y, size):
    """The app icon (same artwork as the favicon in templates/index.html): dark rounded tile with
    three rising blue bars. x,y = bottom-left."""
    c.saveState()
    c.setFillColor(colors.HexColor('#0A0F1C'))
    c.roundRect(x, y, size, size, size * 0.22, stroke=0, fill=1)
    c.setFillColor(colors.HexColor('#5B9BFF'))
    # favicon is a 100x100 box; bars at x=20/43/66 (w14), bottoms at y=72, tops at 52/34/22
    for bx, top in ((20, 52), (43, 34), (66, 22)):
        c.rect(x + bx / 100 * size, y + (100 - 72) / 100 * size,
               14 / 100 * size, (72 - top) / 100 * size, stroke=0, fill=1)
    c.restoreState()


def _make_page_callbacks(meta, styles):
    def cover(c, doc):
        c.saveState()
        band_h = 112 * mm
        c.setFillColor(NAVY)
        c.rect(0, PAGE_H - band_h, PAGE_W, band_h, stroke=0, fill=1)
        c.setFillColor(NAVY_2)
        c.circle(PAGE_W - 18 * mm, PAGE_H - 30 * mm, 62 * mm, stroke=0, fill=1)
        c.setFillColor(NAVY)
        c.rect(0, PAGE_H - 24 * mm, PAGE_W, 24 * mm, stroke=0, fill=1)   # keep the top clean
        c.setFillColor(BLUE)
        c.rect(0, PAGE_H - band_h - 2.2 * mm, PAGE_W, 2.2 * mm, stroke=0, fill=1)
        _draw_logo(c, LM, PAGE_H - 40 * mm, 16 * mm)
        c.setFillColor(colors.white)
        c.setFont('Helvetica-Bold', 20)
        c.drawString(LM + 21 * mm, PAGE_H - 31.5 * mm, APP_NAME)
        c.setFillColor(SKY)
        c.setFont('Helvetica', 8.5)
        c.drawString(LM + 21 * mm, PAGE_H - 37.5 * mm, APP_TAGLINE.upper())
        c.setFillColor(SKY)
        c.setFont('Helvetica-Bold', 8.5)
        c.drawString(LM, PAGE_H - 64 * mm, meta['kicker'].upper())
        tp = Paragraph(_e(meta['title']), ParagraphStyle('ct', fontName='Helvetica-Bold', fontSize=27, leading=33, textColor=colors.white))
        w, h = tp.wrapOn(c, CONTENT_W - 10 * mm, 60 * mm)
        tp.drawOn(c, LM, PAGE_H - 68 * mm - h)
        sp = Paragraph(_e(meta['subtitle']), ParagraphStyle('cs', fontName='Helvetica', fontSize=11.5, leading=16, textColor=colors.HexColor('#C9D7F5')))
        w2, h2 = sp.wrapOn(c, CONTENT_W - 10 * mm, 30 * mm)
        sp.drawOn(c, LM, PAGE_H - 68 * mm - h - 6 * mm - h2)
        c.setFillColor(FAINT)
        c.setFont('Helvetica', 7.5)
        c.drawString(LM, 12 * mm, f"{APP_NAME}  ·  {APP_TAGLINE}")
        c.drawRightString(PAGE_W - RM, 12 * mm, f"Prepared {meta['generated']}")
        c.restoreState()

    def later(c, doc):
        c.saveState()
        _draw_logo(c, LM, PAGE_H - 17 * mm, 7 * mm)
        c.setFillColor(NAVY)
        c.setFont('Helvetica-Bold', 9.5)
        c.drawString(LM + 9.5 * mm, PAGE_H - 12.4 * mm, APP_NAME)
        c.setFillColor(FAINT)
        c.setFont('Helvetica', 7)
        c.drawString(LM + 9.5 * mm, PAGE_H - 15.8 * mm, APP_TAGLINE.upper())
        c.setFillColor(DIM)
        c.setFont('Helvetica', 8)
        c.drawRightString(PAGE_W - RM, PAGE_H - 13.5 * mm, meta['running'])
        c.setStrokeColor(RULE)
        c.setLineWidth(0.6)
        c.line(LM, PAGE_H - 20 * mm, PAGE_W - RM, PAGE_H - 20 * mm)
        c.setFillColor(BLUE)
        c.rect(LM, PAGE_H - 20.4 * mm, 18 * mm, 0.9 * mm, stroke=0, fill=1)
        c.setStrokeColor(RULE)
        c.line(LM, 15 * mm, PAGE_W - RM, 15 * mm)
        c.setFillColor(FAINT)
        c.setFont('Helvetica', 7.5)
        c.drawString(LM, 10.5 * mm, meta['footer'])
        c.drawRightString(PAGE_W - RM, 10.5 * mm, f"Page {doc.page}")
        c.restoreState()
    return cover, later


# ----------------------------------------------------------------------------- flowable builders
def _section(st, kicker, title, standfirst=None, min_space=70 * mm):
    # One heading per section: the short label (Contents, Summary, Overview, Part One · ...) is
    # the heading. The second, descriptive title that used to follow it ("In this report",
    # "Key findings", "The year at a glance") repeated it, so it is no longer rendered.
    # `title` and `standfirst` stay as parameters so existing call sites keep working.
    return [CondPageBreak(min_space), Spacer(1, 10), Paragraph(_e(kicker), st['h1']), Spacer(1, 6)]


def _callout(st, text, accent=BLUE):
    t = Table([[Paragraph(text, st['callout'])]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), PANEL),
        ('LINEBEFORE', (0, 0), (0, -1), 3, accent),
        ('LEFTPADDING', (0, 0), (-1, -1), 12), ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ('TOPPADDING', (0, 0), (-1, -1), 9), ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
    ]))
    return KeepTogether([Spacer(1, 2), t, Spacer(1, 9)])


def _kpi_strip(st, items):
    """items: [(label, value_html, accent_color)] - up to 4 across, each a card with a coloured top edge."""
    items = [i for i in items if i[1]]
    if not items:
        return []
    per_row = 2 if any(len(re_strip(v)) > 17 for _, v, _ in items) else min(4, len(items))
    gap = 3 * mm
    cw = (CONTENT_W - gap * (per_row - 1)) / per_row
    rows, row = [], []
    for label, val, accent in items:
        cell = Table([[Paragraph(_e(label).upper(), st['kpi_l'])], [Paragraph(val, st['kpi_v'])]], colWidths=[cw])
        cell.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), PANEL), ('LINEABOVE', (0, 0), (-1, 0), 2.2, accent),
            ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, 0), 7), ('BOTTOMPADDING', (0, -1), (-1, -1), 8),
        ]))
        row.append(cell)
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row + [''] * (per_row - len(row)))
    outer = Table(rows, colWidths=[cw + (gap if i < per_row - 1 else 0) for i in range(per_row)])
    outer.setStyle(TableStyle([('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                               ('RIGHTPADDING', (0, 0), (-2, -1), gap), ('TOPPADDING', (0, 0), (-1, -1), 0),
                               ('BOTTOMPADDING', (0, 0), (-1, -1), gap), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    return [outer, Spacer(1, 6)]


def re_strip(html):
    import re
    return re.sub(r'<[^>]+>', '', html or '')


def _table(st, header, rows, widths, right_cols=()):
    data = [[Paragraph(_e(h), st['th']) for h in header]]
    for r in rows:
        data.append([Paragraph(str(c), st['td_r'] if i in right_cols else st['td']) for i, c in enumerate(r)])
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY), ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PANEL]),
        ('LINEBELOW', (0, 0), (-1, -1), 0.3, RULE), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 7),
    ]))
    return t


def _paras(st, texts, style='body'):
    return [Paragraph(t, st[style]) for t in texts if t]


def _bullets(st, texts):
    return [Paragraph(t, st['bullet'], bulletText='•') for t in texts if t]


def _fn(v, dp=None):
    """Plain number for table cells (currency is named in the caption/header): '—' when missing."""
    if v is None:
        return '—'
    if dp is not None:
        return f"{v:,.{dp}f}"
    return _num(v)


def _fp(v, dp=0):
    return '—' if v is None else f"{v:.{dp}f}%"


def _grid(st, header, rows, widths, right_cols=(), bold_rows=(), caption=None, highlight=()):
    """Compact multi-column table for company-by-company data. `bold_rows`/`highlight` are row indexes
    (0-based, excluding the header) to emphasise / shade."""
    data = [[Paragraph(_e(h), st['th_sr'] if i in right_cols else st['th_s']) for i, h in enumerate(header)]]
    for ri, r in enumerate(rows):
        bold = ri in bold_rows
        cells = []
        for i, c in enumerate(r):
            key = ('td_srb' if bold else 'td_sr') if i in right_cols else ('td_sb' if bold else 'td_s')
            cells.append(Paragraph(str(c), st[key]))
        data.append(cells)
    t = Table(data, colWidths=widths, repeatRows=1)
    style = [
        ('BACKGROUND', (0, 0), (-1, 0), NAVY), ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PANEL]),
        ('LINEBELOW', (0, 0), (-1, -1), 0.3, RULE), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 3.4), ('BOTTOMPADDING', (0, 0), (-1, -1), 3.4),
        ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ]
    for ri in highlight:
        style.append(('BACKGROUND', (0, ri + 1), (-1, ri + 1), colors.HexColor('#DCE8FF')))
    t.setStyle(TableStyle(style))
    out = [t]
    if caption:
        out.append(Paragraph(caption, st['caption']))
    else:
        out.append(Spacer(1, 8))
    return out


def _widths(first, n_rest):
    """First column `first` mm, the remaining n_rest columns share the rest of the content width."""
    rest = (CONTENT_W - first * mm) / n_rest
    return [first * mm] + [rest] * n_rest


def _market_row(cur, label, m, scale=1):
    """[label, companies, 25th, median, 75th, average] from a survey_aggregate metric dict."""
    if not m or m.get('company_count', 0) == 0:
        return None
    c = m.get('company_count', 0)
    if m.get('average') is None:
        return [label, str(c), '—', '—', '—', 'Not enough data']
    return [label, str(c), _fn(m.get('p25') * scale if m.get('p25') is not None else None),
            _fn(m.get('p50') * scale if m.get('p50') is not None else None),
            _fn(m.get('p75') * scale if m.get('p75') is not None else None), _fn(m['average'] * scale)]


MARKET_HEADER = ['Pay element', 'Companies', '25th percentile', 'Median', '75th percentile', 'Average']


def _market_table(st, cur, items, caption=None):
    rows = [r for r in (_market_row(cur, lab, m, sc) for lab, m, sc in items) if r]
    if not rows:
        return []
    return _grid(st, MARKET_HEADER, rows, [48 * mm] + [(CONTENT_W - 48 * mm) / 5] * 5, right_cols=(1, 2, 3, 4, 5),
                 caption=caption or f'Amounts in {cur}. Percentiles and averages are published only where at least {MIN_COMPANIES_FOR_AVERAGE} companies disclosed the figure.')


def _skewed(m):
    """True when an average sits far above the median - a sign that one or two filings are pulling it."""
    if not m or m.get('average') is None or not m.get('p50'):
        return False
    return m.get('company_count', 0) >= 3 and m['average'] > 3 * m['p50']


# ----------------------------------------------------------------------------- charts
def _hbar(items, color=BLUE, width=CONTENT_W, label_w=50 * mm, value_w=42 * mm, row_h=7 * mm, fmt=str):
    items = [(l, v) for l, v in items if v is not None]
    if not items:
        return None
    h = len(items) * row_h + 2 * mm
    d = Drawing(width, h)
    mx = max(abs(v) for _, v in items) or 1
    bar_w = width - label_w - value_w
    for i, (lab, v) in enumerate(items):
        y = h - (i + 1) * row_h + 1.2 * mm
        d.add(String(0, y + 1.3 * mm, _trunc(lab, 30), fontName='Helvetica', fontSize=8, fillColor=INK))
        w = max(bar_w * abs(v) / mx, 0.8 * mm)
        d.add(Rect(label_w, y, w, 4.4 * mm, rx=1.2, ry=1.2, fillColor=color, strokeColor=None))
        d.add(String(label_w + w + 2 * mm, y + 1.3 * mm, fmt(v), fontName='Helvetica-Bold', fontSize=7.8, fillColor=NAVY))
    return d


def _donut(parts, center_text, size=34 * mm):
    """parts: [(label, value, color)] -> Drawing with a ring and a legend to the right."""
    total = sum(v for _, v, _ in parts if v) or 0
    if not total:
        return None
    d = Drawing(CONTENT_W, size + 4 * mm)
    cx, cy, r = size / 2 + 2 * mm, size / 2 + 2 * mm, size / 2
    a = 90.0
    for lab, v, col in parts:
        if not v:
            continue
        span = 360.0 * v / total
        d.add(Wedge(cx, cy, r, a - span, a, radius1=r * 0.6, fillColor=col, strokeColor=colors.white, strokeWidth=1))
        a -= span
    d.add(String(cx, cy - 2.5, center_text, fontName='Helvetica-Bold', fontSize=11, fillColor=NAVY, textAnchor='middle'))
    ly = cy + 6 * mm
    for lab, v, col in parts:
        d.add(Rect(size + 14 * mm, ly, 3 * mm, 3 * mm, fillColor=col, strokeColor=None))
        d.add(String(size + 19 * mm, ly + 0.4 * mm, f"{lab}: {v:,} ({int(round(100 * v / total))}%)", fontName='Helvetica', fontSize=8.5, fillColor=INK))
        ly -= 6 * mm
    return d


def _line_chart(years, series, height=46 * mm, fmt=lambda v: f"{v:,.0f}"):
    """series: [(label, [values aligned to years, None allowed], color)]."""
    vals = [v for _, vs, _ in series for v in vs if v is not None]
    if len(years) < 2 or not vals:
        return None
    left, right, bottom, top = 8 * mm, 40 * mm, 10 * mm, 5 * mm
    d = Drawing(CONTENT_W, height)
    pw, ph = CONTENT_W - left - right, height - bottom - top
    lo, hi = min(0, min(vals)), max(vals)
    span = (hi - lo) or 1
    xs = [left + pw * i / (len(years) - 1) for i in range(len(years))]
    d.add(Line(left, bottom, left + pw, bottom, strokeColor=RULE, strokeWidth=0.8))
    for i, y in enumerate(years):
        d.add(String(xs[i], bottom - 7 * mm * 0.9, str(y), fontName='Helvetica', fontSize=7.5, fillColor=DIM, textAnchor='middle'))
    ends = []
    for label, vs, col in series:
        pts = [(xs[i], bottom + ph * (v - lo) / span) for i, v in enumerate(vs) if v is not None]
        if len(pts) >= 2:
            d.add(PolyLine([c for p in pts for c in p], strokeColor=col, strokeWidth=1.8))
        for (px, py) in pts:
            d.add(Circle(px, py, 1.7, fillColor=col, strokeColor=colors.white, strokeWidth=0.6))
        if pts:
            ends.append([pts[-1][0], pts[-1][1], label, col, [v for v in vs if v is not None][-1]])
    ends.sort(key=lambda e: e[1])
    for i in range(1, len(ends)):                      # keep labels at least 17pt apart
        if ends[i][1] - ends[i - 1][1] < 17:
            ends[i][1] = ends[i - 1][1] + 17
    for px, py, label, col, lastv in ends:
        d.add(String(px + 2.5 * mm, py - 1, label, fontName='Helvetica-Bold', fontSize=7.5, fillColor=col))
        d.add(String(px + 2.5 * mm, py - 9, fmt(lastv), fontName='Helvetica', fontSize=7, fillColor=DIM))
    return d


# ----------------------------------------------------------------------------- shared narrative blocks
def _implausible_margin(m):
    return m is not None and abs(m) > 100


def _doc(buf, title):
    return SimpleDocTemplate(buf, pagesize=A4, leftMargin=LM, rightMargin=RM, topMargin=28 * mm, bottomMargin=22 * mm,
                             title=title, author=APP_NAME, subject=APP_TAGLINE)



def _key_findings_combined(st, overview, rows, cur, fy, n, pscale):
    """A short list of the headline findings, every one computed from stored figures. A finding whose
    figures are missing is simply not written."""
    bo, dr = overview['board_overview'], overview['directors_remuneration']
    cr, ceo = overview['committee_remuneration'], overview['ceo_remuneration']
    out = []
    ned, chair = dr['other_ned_annual_retainer'], dr['chairperson_annual_retainer']
    if _avg(ned) is not None:
        t = (f"The median non-executive director retainer is {_e(_money(cur, ned.get('p50')))} a year; the average is "
             f"{_e(_money(cur, _avg(ned)))} across {_cnt(ned)} companies" )
        if _skewed(ned):
            t += ", pulled well above the median by a few large payers, so the median is the better guide to a typical level"
        out.append(t + '.')
    if _avg(chair) is not None and dr.get('chairperson_vs_other_ned_annual_retainer_ratio'):
        out.append(f"Chairpersons earn {_times(dr['chairperson_vs_other_ned_annual_retainer_ratio'])} the retainer of the other non-executives on the same board.")
    ranked_co = sorted([r for r in rows if r.get('total_director_remuneration')], key=lambda r: -r['total_director_remuneration'])
    if ranked_co:
        out.append(f"{_e(ranked_co[0]['name'])} has the highest combined director retainers ({_e(_money(cur, ranked_co[0]['total_director_remuneration']))}); "
                   f"{len(ranked_co)} companies filed enough to be ranked.")
    ind_known = [r for r in rows if (r.get('governance') or {}).get('independence_pct') is not None]
    gen_known = [r for r in rows if (r.get('governance') or {}).get('gender_pct') is not None]
    if ind_known:
        k = sum(1 for r in ind_known if r['governance']['independence_compliant'])
        out.append(f"{k} of {len(ind_known)} boards ({_pct(k, len(ind_known))}%) meet the one-third independent non-executive benchmark.")
    if gen_known:
        k = sum(1 for r in gen_known if r['governance']['gender_compliant'])
        out.append(f"{k} of {len(gen_known)} boards ({_pct(k, len(gen_known))}%) have at least one-third women; "
                   f"{len(gen_known) - k} fall short.")
    sec = sorted([(x['sector'], x['avg_other_ned_annual_retainer']) for x in overview['sector_comparison']
                  if x.get('avg_other_ned_annual_retainer') is not None], key=lambda x: -x[1])
    if len(sec) > 1:
        out.append(f"{_e(sec[0][0])} is the best-paying comparable sector for non-executive directors ({_e(_money(cur, sec[0][1]))}); "
                   f"{_e(sec[-1][0])} pays the least ({_e(_money(cur, sec[-1][1]))}).")
    cost = ceo['ceo_monthly_cost_of_employment']
    if _avg(cost) is not None:
        out.append(f"A chief executive costs a median {_e(_money(cur, cost.get('p50')))} a month "
                   f"({_e(_money(cur, cost.get('p50'), 12))} a year) across {_cnt(cost)} companies.")
    ben = overview.get('ned_benefits_summary', {})
    top = sorted([(k, v['percent_provided']) for k, v in ben.items() if v.get('percent_provided') is not None], key=lambda x: -x[1])
    if top and top[0][1] > 0:
        out.append(f"{_e(_NED_BENEFIT_LABELS.get(top[0][0], top[0][0]))} is among the most common non-executive benefits ({top[0][1]:.0f}% of responding companies).")
    if not out:
        return []
    return _section(st, 'Summary', 'Key findings') + _bullets(st, out)

# ============================================================================ COMBINED REPORT
def build_combined_report(overview, rows, trends):
    """overview = survey_aggregate.build_survey_overview(fy) (has_data True);
    rows = build_company_rows(fy)['rows']; trends = build_historical_trends()."""
    st = _styles()
    fy = overview['fiscal_year']
    cur = overview.get('currency', 'KES')
    n = overview['companies_surveyed']
    pscale = UNIT_SCALE.get(overview.get('unit', 'millions'), 1)
    es, bo = overview['executive_summary'], overview['board_overview']
    dr, cr, ceo = overview['directors_remuneration'], overview['committee_remuneration'], overview['ceo_remuneration']
    generated = datetime.utcnow().strftime('%d %B %Y')
    meta = {
        'kicker': 'Board & Executive Remuneration Report',
        'title': f'Board Remuneration & Governance Report — {fy}',
        'subtitle': f'Survey analytics and intelligence across {n} NSE-listed {_plural(n, "company", "companies")}: how boards are built, how directors are paid, and who leads.',
        'generated': generated,
        'running': f'Board Remuneration & Governance Report · {fy}',
        'footer': f'{APP_NAME} · built from company filings uploaded to the platform',
    }
    cover, later = _make_page_callbacks(meta, st)
    buf = BytesIO()
    doc = _doc(buf, meta['title'])
    S = []

    # ---------------- cover, contents, key findings
    S += [Spacer(1, 98 * mm)]
    S += _kpi_strip(st, [
        ('Reporting year', _e(fy), BLUE), ('Companies surveyed', f"{n}", GREEN),
        ('Coverage', 'NSE listed', AMBER), ('Currency', _e(cur), PURPLE)])
    S += [PageBreak()]
    S += _section(st, 'Contents', 'In this report')
    for line in ['Key findings', 'The year at a glance',
                 'Part One — Survey Analytics: board overview, directors\' remuneration, committee pay, CEO/MD pay, benefits, sectors',
                 'Part Two — Intelligence Layer: company rankings, governance and compliance, historical trends',
                 'Data quality and method', 'Appendix — company-by-company data tables']:
        S.append(Paragraph('•&nbsp;&nbsp;' + _e(line), st['toc']))
    S += _key_findings_combined(st, overview, rows, cur, fy, n, pscale)

    # ---------------- at a glance
    S += _section(st, 'Overview', f'The year at a glance — {fy}')
    glance = []
    avg_t, avg_p, avg_m = _avg(es['avg_turnover']), _avg(es['avg_net_profit']), _avg(es['avg_profit_margin'])
    if avg_t is not None:
        s = f"The typical company in the survey turned over {_e(_money(cur, avg_t, pscale))} in the year"
        if avg_p is not None:
            s += (f", with average net profit of {_e(_money(cur, avg_p, pscale))}" if avg_p >= 0
                  else f", with an average net loss of {_e(_money(cur, -avg_p, pscale))}")
        s += '.'
        if es.get('total_market_cap'):
            s += f" Together the surveyed companies carry a market capitalisation of {_e(_money(cur, es['total_market_cap'], pscale))}."
        glance.append(s)
    elif es.get('total_market_cap'):
        glance.append(f"Together the surveyed companies carry a market capitalisation of {_e(_money(cur, es['total_market_cap'], pscale))}.")
    sz, mt = _avg(bo['avg_board_size']), _avg(bo['avg_board_meetings_per_year'])
    if sz is not None:
        s = f"Boards average {sz:.1f} directors"
        if mt is not None:
            s += f" and meet {mt:.1f} times a year"
        glance.append(s + '.')
    f, m = bo['total_directors_female'], bo['total_directors_male']
    fem = _pct(f, (f or 0) + (m or 0)) if f is not None and m is not None else None
    ned_avg = _avg(dr['other_ned_annual_retainer'])
    chair_ratio = dr.get('chairperson_vs_other_ned_annual_retainer_ratio')
    if ned_avg is not None:
        s = f"A non-executive director is paid an annual retainer of {_e(_money(cur, ned_avg))} on average"
        if chair_ratio:
            s += f", and a chairperson earns {_times(chair_ratio)} that of the other non-executives on the same board"
        glance.append(s + '.')
    if fem is not None:
        glance.append(f"Women hold {fem}% of the directorships for which gender was reported.")
    S += _paras(st, [' '.join(glance[:2]), ' '.join(glance[2:])], 'lead')
    kp = []
    if avg_t is not None:
        kp.append(('Average turnover', _e(_money(cur, avg_t, pscale)), BLUE))
    if ned_avg is not None:
        kp.append(('Average NED retainer', _e(_money(cur, ned_avg)), GREEN))
    if sz is not None:
        kp.append(('Average board size', f"{sz:.1f} directors", AMBER))
    if fem is not None:
        kp.append(('Women on boards', f"{fem}%", PURPLE))
    S += _kpi_strip(st, kp)
    if _implausible_margin(avg_m):
        S += _paras(st, [f"<i>The survey also computes an average profit margin, but the current figure ({avg_m:.1f}%) is not credible "
                         f"and is left out of this narrative; see Data quality and method.</i>"], 'small')

    prow = [[_e(_trunc(r['name'], 30)), _e(_trunc(r.get('sector') or '—', 22)), _fn(r.get('turnover')), _fn(r.get('net_profit')),
             _fp(r.get('profit_margin'), 1) if (r.get('profit_margin') is not None and abs(r['profit_margin']) <= 100) else '—', _fn(r.get('market_cap'))]
            for r in sorted(rows, key=lambda r: -(r.get('turnover') or -1))
            if any(r.get(k) is not None for k in ('turnover', 'net_profit', 'market_cap'))]
    if prow:
        S += [Paragraph('Financial performance by company', st['h2'])]
        S += _grid(st, ['Company', 'Sector', f'Turnover ({cur})', f'Net profit ({cur})', 'Margin', f'Market cap ({cur})'], prow,
                   [40 * mm, 28 * mm, 32 * mm, 28 * mm, 14 * mm, CONTENT_W - 142 * mm], right_cols=(2, 3, 4, 5),
                   caption='Amounts written in full. A dash means the figure was not filed, or was left out because it was not credible for a listed company (see Data quality and method).')

    # ---------------- PART ONE
    S += [PageBreak(), Spacer(1, 40 * mm), Paragraph('PART ONE', st['kicker']),
          Paragraph('Survey Analytics', ParagraphStyle('pt', parent=st['h1'], fontSize=30, leading=36)),
          Spacer(1, 2)]

    # board overview
    S += _section(st, 'Part One · Board Overview', 'Who sits on the boards',
                  'Size, meeting rhythm, gender, independence and age across the surveyed boards.')
    t = []
    if sz is not None:
        s = f"Across the {_cnt(bo['avg_board_size'])} companies that reported board size, the average board has {sz:.1f} directors"
        if mt is not None:
            s += f" and meets {mt:.1f} times a year"
        cm, cmm = _avg(bo['avg_committees_per_board']), _avg(bo['avg_committee_meetings_per_year'])
        if cm is not None:
            s += f", supported by {cm:.1f} board committees"
            if cmm is not None:
                s += f" that meet {cmm:.1f} times a year between them"
        t.append(s + '.')
    ind, nind = bo['total_independent_neds'], bo['total_non_independent_neds']
    ex, ne = bo['total_executive_directors'], bo['total_non_executive_directors']
    comp = []
    if ex is not None and ne is not None:
        comp.append(f"Of the directors whose role was reported, {ne:,} are non-executive and {ex:,} executive")
    if ind is not None and nind is not None and (ind + nind):
        comp.append(f"{ind:,} of {ind + nind:,} non-executives ({_pct(ind, ind + nind)}%) are independent")
    if comp:
        t.append('; '.join(comp) + '.')
    kn, nk = bo['total_neds_kenyan'], bo['total_neds_non_kenyan']
    if kn is not None and nk is not None and (kn + nk):
        t.append(f"Kenyan nationals make up {_pct(kn, kn + nk)}% of the non-executive directors for whom nationality was recorded.")
    ages = [(lab, _avg(bo[k])) for lab, k in [('executive directors', 'avg_age_executive_directors'),
            ('non-executive directors', 'avg_age_non_executive_directors')] if _avg(bo[k]) is not None]
    if ages:
        t.append('On age, ' + _and([f"{lab} average {a:.1f} years" for lab, a in ages]) + '.')
    S += _paras(st, t)
    if fem is not None:
        S += [_callout(st, f"Women hold {fem}% of directorships — {f:,} of {f + m:,}. "
                           + ("That meets" if fem >= 33.3 else "That is below") + " the one-third gender-diversity benchmark used in this report's governance score.",
                       PURPLE if fem < 33.3 else GREEN)]
        d = _donut([('Women', int(f), PURPLE), ('Men', int(m), BLUE)], f"{fem}%")
        if d:
            S += [d, Paragraph('Gender split of directors, all surveyed companies.', st['caption'])]
    tot_rows = []
    for lab, k in [('Executive directors', 'total_executive_directors'), ('Non-executive directors', 'total_non_executive_directors'),
                   ('Independent non-executives', 'total_independent_neds'), ('Non-independent non-executives', 'total_non_independent_neds'),
                   ('Non-executives — Kenyan nationals', 'total_neds_kenyan'), ('Non-executives — other nationalities', 'total_neds_non_kenyan'),
                   ('Women directors', 'total_directors_female'), ('Men directors', 'total_directors_male')]:
        if bo.get(k) is not None:
            tot_rows.append([lab, f"{bo[k]:,}"])
    if tot_rows:
        S += [Paragraph('Board composition across the survey', st['h2'])]
        S += _grid(st, ['Measure', 'Directors'], tot_rows, [110 * mm, CONTENT_W - 110 * mm], right_cols=(1,))
    cad = [_market_row(cur, lab, bo[k]) for lab, k in [
        ('Board size (directors)', 'avg_board_size'), ('Board meetings per year', 'avg_board_meetings_per_year'),
        ('Committees per board', 'avg_committees_per_board'), ('Committee meetings per year', 'avg_committee_meetings_per_year'),
        ('Average age — executive directors', 'avg_age_executive_directors'), ('Average age — non-executive directors', 'avg_age_non_executive_directors'),
        ('Average age — independent non-executives', 'avg_age_independent_neds'), ('Average age — non-independent non-executives', 'avg_age_non_independent_neds')]
        if bo.get(k)]
    cad = [r for r in cad if r]
    if cad:
        S += [Paragraph('Board size, meetings and age', st['h2'])]
        S += _grid(st, ['Measure', 'Companies', '25th percentile', 'Median', '75th percentile', 'Average'],
                   [[r[0], r[1]] + [x if x in ('—', 'Not enough data') else _fn(float(x.replace(',', '')), 1) for x in r[2:]] for r in cad],
                   [58 * mm] + [(CONTENT_W - 58 * mm) / 5] * 5, right_cols=(1, 2, 3, 4, 5))
    prof = []
    for r in sorted(rows, key=lambda r: ((r.get('governance') or {}).get('score') is None, -((r.get('governance') or {}).get('score') or 0), r['name'])):
        if r.get('board_size') is None and r.get('board_meetings_per_year') is None and not (r.get('governance') or {}).get('score') is not None:
            continue
        g = r.get('governance') or {}
        prof.append([_e(_trunc(r['name'], 30)), _fn(r.get('board_size')), _fn(r.get('executive_directors_count')),
                     _fn(r.get('non_executive_directors_count')), _fn(r.get('board_meetings_per_year')), _fn(r.get('committees_per_board')),
                     _fp(g.get('independence_pct')), _fp(g.get('gender_pct')), _fn(g.get('score'))])
    if prof:
        S += [Paragraph('Board profile by company', st['h2'])]
        S += _grid(st, ['Company', 'Board size', 'Exec.', 'Non-exec.', 'Meetings / yr', 'Committees', 'Independent', 'Women', 'Score'],
                   prof, [44 * mm] + [(CONTENT_W - 44 * mm) / 8] * 8, right_cols=tuple(range(1, 9)),
                   caption='Independent = share of non-executives who are independent; Women = share of all directors. Score is the illustrative governance score out of 100.')

    # directors' remuneration
    S += _section(st, "Part One · Directors' Remuneration", 'How directors are paid',
                  'Annual retainers and meeting allowances for chairpersons, non-executive and executive directors.')
    t = []
    p25, p50, p75 = (dr['other_ned_annual_retainer'].get(k) for k in ('p25', 'p50', 'p75'))
    if ned_avg is not None:
        s = (f"The average non-executive director receives an annual retainer of {_e(_money(cur, ned_avg))}, "
             f"based on {_cnt(dr['other_ned_annual_retainer'])} companies")
        if p25 is not None and p75 is not None:
            s += f". The middle half of companies pay between {_e(_money(cur, p25))} and {_e(_money(cur, p75))}, with a median of {_e(_money(cur, p50))}"
        t.append(s + '.')
    ch = _avg(dr['chairperson_annual_retainer'])
    if ch is not None:
        s = f"Chairpersons are paid more: {_e(_money(cur, ch))} on average"
        if chair_ratio:
            s += f", which is {_times(chair_ratio)} the retainer of the other non-executives sitting on the same boards"
        t.append(s + '.')
    oa, ca = _avg(dr['other_ned_meeting_allowance']), _avg(dr['chairperson_meeting_allowance'])
    if oa is not None or ca is not None:
        bits = []
        if oa is not None:
            bits.append(f"{_e(_money(cur, oa))} for each meeting an ordinary non-executive attends")
        if ca is not None:
            bits.append(f"{_e(_money(cur, ca))} for a chairperson")
        t.append('Sitting allowances come on top of retainers: ' + _and(bits) + ' on average.')
    ed, eda = _avg(dr['executive_director_annual_retainer']), _avg(dr['executive_director_meeting_allowance'])
    if ed is not None:
        t.append(f"Executive directors who draw a director retainer receive {_e(_money(cur, ed))} a year on average"
                 + (f", plus {_e(_money(cur, eda))} per meeting" if eda is not None else '') + '.')
    S += _paras(st, t)
    if ned_avg is not None and ch is not None and chair_ratio:
        S += [_callout(st, f"A seat as chairperson is worth {_times(chair_ratio)} an ordinary non-executive seat on the same board.", BLUE)]
    S += [Paragraph('The spread of the market', st['h2'])]
    S += _market_table(st, cur, [
        ('Chairperson — annual retainer', dr['chairperson_annual_retainer'], 1),
        ('Other NED — annual retainer', dr['other_ned_annual_retainer'], 1),
        ('Executive director — annual retainer', dr['executive_director_annual_retainer'], 1),
        ('Chairperson — per meeting', dr['chairperson_meeting_allowance'], 1),
        ('Other NED — per meeting', dr['other_ned_meeting_allowance'], 1),
        ('Executive director — per meeting', dr['executive_director_meeting_allowance'], 1)])
    skew = [lab for lab, m_ in [('chairperson retainer', dr['chairperson_annual_retainer']), ('non-executive retainer', dr['other_ned_annual_retainer']),
                                ('non-executive meeting allowance', dr['other_ned_meeting_allowance']), ('chairperson meeting allowance', dr['chairperson_meeting_allowance'])]
            if _skewed(m_)]
    if skew:
        S += _paras(st, ['<i>The average is more than three times the median for ' + _and(skew) + ', which means one or two filings are pulling it up. '
                         'Use the median as the guide to a typical level, and treat unusually high allowances as figures to confirm against the source filing.</i>'], 'small')
    pay_rows = []
    for r in sorted(rows, key=lambda r: -(r.get('other_ned_annual_retainer') or -1)):
        vals = [r.get('chairperson_annual_retainer'), r.get('other_ned_annual_retainer'), r.get('executive_director_annual_retainer'),
                r.get('chairperson_meeting_allowance'), r.get('other_ned_meeting_allowance')]
        if all(v is None for v in vals):
            continue
        ratio = (r['chairperson_annual_retainer'] / r['other_ned_annual_retainer']) if (r.get('chairperson_annual_retainer') and r.get('other_ned_annual_retainer')) else None
        pay_rows.append([_e(_trunc(r['name'], 30))] + [_fn(v) for v in vals[:3]] + [f"{ratio:.1f}x" if ratio else '—'] + [_fn(v) for v in vals[3:]])
    if pay_rows:
        S += [Paragraph('Director pay by company', st['h2'])]
        S += _grid(st, ['Company', 'Chair retainer', 'NED retainer', 'Exec. retainer', 'Chair / NED', 'Chair per meeting', 'NED per meeting'],
                   pay_rows, [40 * mm, 25 * mm, 25 * mm, 24 * mm, 16 * mm, 20 * mm, 20 * mm], right_cols=(1, 2, 3, 4, 5, 6),
                   caption=f'Amounts in {cur}, written in full. A dash means the company did not file the figure; nothing is estimated.')

    # committees
    cm_chair, cm_mem = _avg(cr['committee_chair_annual_retainer']), _avg(cr['committee_member_annual_retainer'])
    cm_chair_a, cm_mem_a = _avg(cr['committee_chair_meeting_allowance']), _avg(cr['committee_member_meeting_allowance'])
    S += _section(st, 'Part One · Committee Remuneration', 'Paying for committee work',
                  'Audit, remuneration, risk and nominations committees carry their own pay.')
    t = []
    if cm_mem is not None or cm_chair is not None:
        bits = []
        if cm_mem is not None:
            bits.append(f"members receive an average annual retainer of {_e(_money(cur, cm_mem))}")
        if cm_chair is not None:
            bits.append(f"chairs receive {_e(_money(cur, cm_chair))}")
        s = 'For committee service, ' + _and(bits)
        r_ = cr.get('chair_vs_member_annual_retainer_ratio')
        if r_:
            s += f" — a chair earns {_times(r_)} what a member does"
        t.append(s + '.')
    if cm_mem_a is not None or cm_chair_a is not None:
        bits = []
        if cm_mem_a is not None:
            bits.append(f"{_e(_money(cur, cm_mem_a))} per meeting for members")
        if cm_chair_a is not None:
            bits.append(f"{_e(_money(cur, cm_chair_a))} for chairs")
        t.append('Where pay is per sitting instead, allowances average ' + _and(bits) + '.')
    S += _paras(st, t) if t else _paras(st, ['Not enough companies disclosed committee-level pay for a market figure to be published this year.'])
    S += _market_table(st, cur, [
        ('Committee chair — annual retainer', cr['committee_chair_annual_retainer'], 1),
        ('Committee member — annual retainer', cr['committee_member_annual_retainer'], 1),
        ('Committee chair — per meeting', cr['committee_chair_meeting_allowance'], 1),
        ('Committee member — per meeting', cr['committee_member_meeting_allowance'], 1)])
    crow = []
    for r in sorted(rows, key=lambda r: -(r.get('committee_chair_annual_retainer') or -1)):
        vals = [r.get('committee_chair_annual_retainer'), r.get('committee_member_annual_retainer'),
                r.get('committee_chair_meeting_allowance'), r.get('committee_member_meeting_allowance')]
        if all(v is None for v in vals) and r.get('committees_per_board') is None:
            continue
        crow.append([_e(_trunc(r['name'], 32)), _fn(r.get('committees_per_board')), _fn(r.get('committee_meetings_per_year'))] + [_fn(v) for v in vals])
    if crow:
        S += [Paragraph('Committee structure and pay by company', st['h2'])]
        S += _grid(st, ['Company', 'Committees', 'Meetings / yr', 'Chair retainer', 'Member retainer', 'Chair per meeting', 'Member per meeting'],
                   crow, [40 * mm, 17 * mm, 17 * mm, 25 * mm, 25 * mm, 23 * mm, 23 * mm], right_cols=(1, 2, 3, 4, 5, 6),
                   caption=f'Amounts in {cur}, written in full.')

    # CEO/MD
    S += _section(st, 'Part One · CEO/MD Remuneration', 'What chief executives cost',
                  'Reported monthly, so that companies filing in different ways can be compared like for like.')
    cost, sal = _avg(ceo['ceo_monthly_cost_of_employment']), _avg(ceo['ceo_monthly_salary'])
    t = []
    if cost is not None:
        s = (f"The average monthly cost of employing a CEO or managing director is {_e(_money(cur, cost))}, "
             f"or {_e(_money(cur, cost, 12))} over a full year")
        q25, q75 = ceo['ceo_monthly_cost_of_employment'].get('p25'), ceo['ceo_monthly_cost_of_employment'].get('p75')
        if q25 is not None and q75 is not None:
            s += f". Half of the companies sit between {_e(_money(cur, q25))} and {_e(_money(cur, q75))} a month"
        t.append(s + '.')
    comps = []
    for lab, k in [('basic salary', 'ceo_monthly_salary'), ('allowances', 'ceo_monthly_allowances'),
                   ('incentives and bonus', 'ceo_monthly_incentive_bonus'), ('pension', 'ceo_monthly_pension'),
                   ('non-cash benefits', 'ceo_monthly_non_cash_benefits')]:
        v = _avg(ceo[k])
        if v is not None:
            comps.append(f"{lab} {_e(_money(cur, v))}")
    if comps:
        t.append('Looking at the parts of the package, monthly averages are ' + _and(comps) + '.')
    S += _paras(st, t) if t else _paras(st, ['Too few companies disclosed chief-executive pay for a market figure to be published this year.'])
    S += _market_table(st, cur, [
        ('Basic salary (monthly)', ceo['ceo_monthly_salary'], 1), ('Allowances (monthly)', ceo['ceo_monthly_allowances'], 1),
        ('Incentives and bonus (monthly)', ceo['ceo_monthly_incentive_bonus'], 1), ('Deferred incentive (monthly)', ceo['ceo_monthly_deferred_incentive'], 1),
        ('Pension (monthly)', ceo['ceo_monthly_pension'], 1), ('Gratuity (monthly)', ceo['ceo_monthly_gratuity'], 1),
        ('Non-cash benefits (monthly)', ceo['ceo_monthly_non_cash_benefits'], 1), ('Share value (monthly)', ceo['ceo_monthly_share_value'], 1),
        ('Total monthly cost of employment', ceo['ceo_monthly_cost_of_employment'], 1)])
    erow = [[_e(_trunc(r['name'], 34)), _fn(r.get('ceo_monthly_salary')), _fn(r.get('ceo_monthly_cost_of_employment')),
             _fn(r['ceo_monthly_cost_of_employment'] * 12) if r.get('ceo_monthly_cost_of_employment') is not None else '—']
            for r in sorted(rows, key=lambda r: -(r.get('ceo_monthly_cost_of_employment') or -1))
            if r.get('ceo_monthly_cost_of_employment') is not None or r.get('ceo_monthly_salary') is not None]
    if erow:
        S += [Paragraph('Chief executive cost by company', st['h2'])]
        S += _grid(st, ['Company', 'Basic salary / month', 'Total cost / month', 'Total cost / year'], erow,
                   [58 * mm] + [(CONTENT_W - 58 * mm) / 3] * 3, right_cols=(1, 2, 3), caption=f'Amounts in {cur}, written in full.')

    # benefits
    ben = overview.get('ned_benefits_summary', {})
    labels = {'MedicalCover': 'medical cover', 'IndemnityInsurance': 'indemnity insurance', 'TravelAccommodation': 'travel and accommodation',
              'TelephoneAllowance': 'telephone allowance', 'TransportAllowance': 'transport allowance', 'MealAllowance': 'meal allowance',
              'ClubMembership': 'club membership', 'DutyDayAllowance': 'duty-day allowance', 'GroupPersonalAccident': 'group personal accident cover',
              'ShareSchemeParticipation': 'share-scheme participation'}
    ranked = sorted([(labels[k], v['percent_provided'], v['companies_addressing']) for k, v in ben.items()
                     if k in labels and v.get('percent_provided') is not None], key=lambda x: -x[1])
    S += _section(st, 'Part One · Benefits', 'Beyond the retainer',
                  'Non-cash benefits and perquisites offered to non-executive directors.')
    if ranked:
        top = [f"{l} ({p:.0f}%)" for l, p, _ in ranked[:3] if p > 0]
        low = [l for l, p, _ in ranked if p == 0][:3]
        t = []
        if top:
            t.append('The benefits most often provided to non-executive directors are ' + _and(top)
                     + ". Each percentage is measured only against companies that stated a position on that benefit; a company silent on a benefit is not counted against it.")
        if low:
            t.append('No surveyed company that addressed them provides ' + _and(low) + '.')
        S += _paras(st, t)
        d = _hbar([(l.capitalize(), p) for l, p, _ in ranked[:8]], color=GREEN, fmt=lambda v: f"{v:.0f}%", value_w=18 * mm)
        if d:
            S += [d, Paragraph('Share of responding companies providing each benefit.', st['caption'])]
        S += _grid(st, ['Benefit', 'Companies that addressed it', 'Share providing it'],
                   [[_e(l.capitalize()), str(c_), f"{p:.0f}%"] for l, p, c_ in ranked],
                   [80 * mm, 50 * mm, CONTENT_W - 130 * mm], right_cols=(1, 2))
    else:
        S += _paras(st, ['No company has yet stated a position on non-executive benefits for this year.'])

    # sectors
    S += _section(st, 'Part One · Comparative Analysis', 'How sectors compare',
                  'Average non-executive director retainer by sector.')
    sec = [(s_['sector'], s_['avg_other_ned_annual_retainer'], s_['company_count']) for s_ in overview['sector_comparison']
           if s_.get('avg_other_ned_annual_retainer') is not None]
    thin = len(overview['sector_comparison']) - len(sec)
    sec.sort(key=lambda x: -x[1])
    if sec:
        t = [f"{sec[0][0]} pays its non-executive directors the most, at an average retainer of {_e(_money(cur, sec[0][1]))}"
             + (f", while {sec[-1][0]} pays the least at {_e(_money(cur, sec[-1][1]))}" if len(sec) > 1 else '') + '.']
        if len(sec) > 1 and sec[-1][1]:
            t[0] += f" The top sector pays {_times(sec[0][1] / sec[-1][1])} the bottom one."
        if thin:
            t.append(f"{thin} {_plural(thin, 'sector')} could not be compared because fewer than {MIN_COMPANIES_FOR_AVERAGE} of their companies disclosed a retainer.")
        S += _paras(st, t)
        d = _hbar([(a, b) for a, b, _ in sec], color=BLUE, fmt=lambda v: _money(cur, v), value_w=44 * mm)
        if d:
            S += [d, Paragraph('Average annual retainer of an ordinary non-executive director, by sector.', st['caption'])]
    else:
        S += _paras(st, ['No sector yet has enough companies disclosing a retainer for a comparison to be published.'])
    srows = []
    by_sec = {}
    for r in rows:
        by_sec.setdefault(r.get('sector') or 'Unclassified', []).append(r)
    ret_by_sec = {x['sector']: x for x in overview['sector_comparison']}

    def _mean(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None
    for sector_name, rs in sorted(by_sec.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        sc = [r['governance']['score'] for r in rs if (r.get('governance') or {}).get('score') is not None]
        wm = [r['governance']['gender_pct'] for r in rs if (r.get('governance') or {}).get('gender_pct') is not None]
        srows.append([_e(sector_name), str(len(rs)), _fn(_mean([r.get('board_size') for r in rs]), 1),
                      _fp(_mean(wm)) if wm else '—', _fn(_mean(sc), 0) if sc else '—',
                      _fn((ret_by_sec.get(sector_name) or {}).get('avg_other_ned_annual_retainer'))])
    if srows:
        S += [Paragraph('Sector profile', st['h2'])]
        S += _grid(st, ['Sector', 'Companies', 'Avg. board size', 'Avg. women', 'Avg. governance score', f'Avg. NED retainer ({cur})'], srows,
                   [50 * mm] + [(CONTENT_W - 50 * mm) / 5] * 5, right_cols=(1, 2, 3, 4, 5),
                   caption='Board size, women and score are simple averages of the companies in the sector; the retainer average needs at least two companies in the sector to have disclosed it.')

    # ---------------- PART TWO
    S += [PageBreak(), Spacer(1, 40 * mm), Paragraph('PART TWO', st['kicker']),
          Paragraph('Intelligence Layer', ParagraphStyle('pt2', parent=st['h1'], fontSize=30, leading=36)),
          Spacer(1, 2)]

    # rankings
    S += _section(st, 'Part Two · Company Rankings', 'Who pays the most',
                  'Boards ranked by the combined annual retainers they have filed.')
    ranked_co = sorted([r for r in rows if r.get('total_director_remuneration')], key=lambda r: -r['total_director_remuneration'])
    if ranked_co:
        top = ranked_co[0]
        tots = [r['total_director_remuneration'] for r in ranked_co]
        med = _median(tots)
        t = [f"{_e(top['name'])} leads on director pay, with combined annual retainers of {_e(_money(cur, top['total_director_remuneration']))}"
             + (f" — {_times(top['total_director_remuneration'] / med)} the median of {_e(_money(cur, med))} across the {len(tots)} companies that filed enough to be ranked" if med and len(tots) > 2 else '') + '.']
        if len(ranked_co) >= 3:
            t.append(f"{_e(ranked_co[1]['name'])} and {_e(ranked_co[2]['name'])} follow. The ranking adds together the chairperson, "
                     f"other-non-executive and executive-director retainers each company filed, so a company that filed only some of them will rank lower than its real cost.")
        S += _paras(st, t)
        d = _hbar([(r['name'], r['total_director_remuneration']) for r in ranked_co[:10]], color=BLUE, fmt=lambda v: _money(cur, v), value_w=44 * mm)
        if d:
            S += [d, Paragraph('Top companies by combined annual director retainers.', st['caption'])]
        S += [Paragraph('Full ranking', st['h2'])]
        S += _grid(st, ['Rank', 'Company', 'Sector', 'Chair', 'Other NED', 'Exec. director', 'Combined'],
                   [[str(i + 1), _e(_trunc(r['name'], 28)), _e(_trunc(r.get('sector') or '—', 20)), _fn(r.get('chairperson_annual_retainer')),
                     _fn(r.get('other_ned_annual_retainer')), _fn(r.get('executive_director_annual_retainer')), _fn(r['total_director_remuneration'])]
                    for i, r in enumerate(ranked_co)],
                   [11 * mm, 42 * mm, 30 * mm, 22 * mm, 22 * mm, 20 * mm, CONTENT_W - 147 * mm], right_cols=(3, 4, 5, 6),
                   caption=f'Annual retainers in {cur}, written in full. Combined = chair + other non-executive + executive-director retainers filed by the company.')
    else:
        S += _paras(st, ['No company has filed enough retainer figures to be ranked this year.'])

    # governance
    S += _section(st, 'Part Two · Governance & Compliance', 'How well boards are balanced',
                  'An illustrative score built on two Capital Markets Authority governance benchmarks.')
    scored = [r for r in rows if (r.get('governance') or {}).get('score') is not None]
    ind_known = [r for r in rows if (r.get('governance') or {}).get('independence_pct') is not None]
    gen_known = [r for r in rows if (r.get('governance') or {}).get('gender_pct') is not None]
    t = []
    if ind_known:
        k = sum(1 for r in ind_known if r['governance']['independence_compliant'])
        t.append(f"{k} of the {len(ind_known)} boards that reported their independent directors ({_pct(k, len(ind_known))}%) have at least one-third independent non-executive directors.")
    if gen_known:
        k = sum(1 for r in gen_known if r['governance']['gender_compliant'])
        t.append(f"On gender, {k} of {len(gen_known)} boards ({_pct(k, len(gen_known))}%) have at least one-third women.")
    if scored:
        mean = sum(r['governance']['score'] for r in scored) / len(scored)
        best = sorted(scored, key=lambda r: (-r['governance']['score'], r['name']))
        t.append(f"The average governance score is {mean:.0f} out of 100 across {len(scored)} scored companies. "
                 f"{_and([_e(r['name']) + ' (' + str(r['governance']['score']) + ')' for r in best[:3]])} score highest"
                 + (f"; {_and([_e(r['name']) + ' (' + str(r['governance']['score']) + ')' for r in best[-3:][::-1]])} score lowest" if len(best) > 6 else '') + '.')
    S += _paras(st, t) if t else _paras(st, ['Too few companies filed board-composition data for governance to be scored this year.'])
    if scored:
        d = _hbar([(r['name'], r['governance']['score']) for r in sorted(scored, key=lambda r: -r['governance']['score'])[:10]],
                  color=GREEN, fmt=lambda v: f"{v:.0f} / 100", value_w=22 * mm)
        if d:
            S += [d, Paragraph('Highest governance scores.', st['caption'])]
        S += [Paragraph('Governance by company', st['h2'])]
        S += _grid(st, ['Company', 'Independent NEDs', 'Women on board', 'Independence benchmark', 'Gender benchmark', 'Score'],
                   [[_e(_trunc(r['name'], 32)), _fp(r['governance'].get('independence_pct'), 0), _fp(r['governance'].get('gender_pct'), 0),
                     ('Met' if r['governance'].get('independence_compliant') else 'Not met') if r['governance'].get('independence_pct') is not None else '—',
                     ('Met' if r['governance'].get('gender_compliant') else 'Not met') if r['governance'].get('gender_pct') is not None else '—',
                     f"{r['governance']['score']} / 100"]
                    for r in sorted(scored, key=lambda r: (-r['governance']['score'], r['name']))],
                   [52 * mm] + [(CONTENT_W - 52 * mm) / 5] * 5, right_cols=(1, 2, 3, 4, 5),
                   caption='Benchmarks are the one-third independent non-executive and one-third women thresholds.')
        short = [r for r in scored if r['governance'].get('gender_pct') is not None and not r['governance'].get('gender_compliant')]
        if short:
            S += _paras(st, [f"Boards below the one-third gender benchmark ({len(short)}): " + _and([_e(r['name']) for r in sorted(short, key=lambda r: r['name'])]) + '.'], 'small')
        lowind = [r for r in scored if r['governance'].get('independence_pct') is not None and not r['governance'].get('independence_compliant')]
        if lowind:
            S += _paras(st, [f"Boards below the one-third independence benchmark ({len(lowind)}): " + _and([_e(r['name']) for r in sorted(lowind, key=lambda r: r['name'])]) + '.'], 'small')
    S += _paras(st, ['<i>The score is illustrative, not a regulatory compliance determination. Half the points reward board independence and half gender '
                     'balance, each scaled against a one-third benchmark. A full assessment would also weigh risk disclosure, remuneration policy and shareholder '
                     'rights, which this survey does not capture.</i>'], 'small')

    # trends
    S += _section(st, 'Part Two · Historical Trends', 'The direction of travel',
                  'How the market has moved across the fiscal years on file.', min_space=135 * mm)
    series = [s_ for s_ in (trends or {}).get('series', []) if s_.get('has_data')]
    if len(series) >= 2:
        yrs = [s_['fiscal_year'] for s_ in series]

        def col(path):
            out = []
            for s_ in series:
                try:
                    out.append(_avg(s_[path[0]][path[1]]))
                except Exception:
                    out.append(None)
            return out
        ned_s, ed_s, chair_s = (col(('directors_remuneration', k)) for k in
                                ('other_ned_annual_retainer', 'executive_director_annual_retainer', 'chairperson_annual_retainer'))
        size_s = col(('board_overview', 'avg_board_size'))

        def move(vs, lab):
            pts = [(y, v) for y, v in zip(yrs, vs) if v is not None]
            if len(pts) < 2 or not pts[0][1]:
                return None
            (y0, v0), (y1, v1) = pts[0], pts[-1]
            ch = (v1 - v0) / abs(v0) * 100
            return f"{lab} {'rose' if ch > 0.5 else 'fell' if ch < -0.5 else 'held steady'} from {_e(_money(cur, v0))} in {_e(y0)} to {_e(_money(cur, v1))} in {_e(y1)}" + (f" ({ch:+.0f}%)" if abs(ch) > 0.5 else '')
        bits = [b for b in (move(ned_s, 'the average non-executive retainer'), move(chair_s, 'the average chairperson retainer'),
                            move(ed_s, 'the average executive-director retainer')) if b]
        t = [f"Across {len(series)} fiscal years ({_e(yrs[0])} to {_e(yrs[-1])}), " + '; '.join(bits) + '.' if bits else
             f"{len(series)} fiscal years are on file ({_e(yrs[0])} to {_e(yrs[-1])}), but not enough companies disclosed the same pay elements in more than one year to show a trend."]
        t.append("Each year's average is drawn from whichever companies reported that year, so movement can reflect a change in who reported as well as a change in pay. "
                 "Read the lines as direction, not as precise like-for-like growth.")
        S += _paras(st, t)
        d = _line_chart(yrs, [('NED', ned_s, BLUE), ('Chair', chair_s, PURPLE), ('Exec', ed_s, GREEN)], fmt=lambda v: f"{v:,.0f}")
        if d:
            S += [d, Paragraph('Average annual retainers by fiscal year.', st['caption'])]
    else:
        S += _paras(st, [f"Only one fiscal year ({_e(fy)}) is on file, so no trend can be drawn yet. Once a second year of company filings is uploaded, "
                         f"this section will describe how pay and board composition have moved year on year."])

    # data quality
    S += _section(st, 'Method', 'Data quality and method',
                  'What stands behind the numbers, and what has deliberately been left out.')
    t = [f"This report draws on {n} {_plural(n, 'filing')} for {_e(fy)}. Amounts are in {_e(cur)}. A company that reports in another "
         f"currency is converted into {_e(cur)} at that year's annual-average exchange rate and included in every average; "
         f"if no rate is on file for its year it is left out rather than guessed."]
    conv = overview.get('converted_from_other_currency') or []
    if conv:
        t.append('Converted into ' + _e(cur) + ': ' + _and([f"{_e(x['name'])} ({_e(x['currency'])}, 1 {_e(x['currency'])} = "
                                                            f"{x['rate']:.4f} {_e(cur)})" for x in conv])
                 + f". Rates: {_e(conv[0]['source'])}.")
    exc = overview.get('excluded_from_performance_averages') or []
    if exc:
        t.append('Left out of performance averages: ' + _and([f"{_e(x['name'])} ({_e(x['reason'])})" for x in exc]) + '.')
    imp = overview.get('implausible_values') or []
    if imp:
        t.append(f"{len(imp)} reported {_plural(len(imp), 'figure was', 'figures were')} left out of the averages because "
                 + ('it was' if len(imp) == 1 else 'they were') + ' too large to be credible for a listed company, which usually means the filing unit '
                 'was recorded wrongly. These are flagged on the Data Quality tab for review: '
                 + _and([f"{_e(x['company'])} ({_e(x['field'].replace('_', ' '))})" for x in imp[:6]]) + ('…' if len(imp) > 6 else '') + '.')
    S += _paras(st, t)
    if rows:
        fields = [('Board size', 'board_size'), ('Board meetings', 'board_meetings_per_year'), ('Gender split', 'directors_female'),
                  ('Independent NEDs', 'independent_neds_count'), ('Chair retainer', 'chairperson_annual_retainer'),
                  ('NED retainer', 'other_ned_annual_retainer'), ('NED meeting fee', 'other_ned_meeting_allowance'),
                  ('Committee chair retainer', 'committee_chair_annual_retainer'), ('CEO monthly cost', 'ceo_monthly_cost_of_employment'),
                  ('Turnover', 'turnover'), ('Net profit', 'net_profit'), ('Market cap', 'market_cap')]
        S += [Paragraph('How complete the data is', st['h2'])]
        S += _grid(st, ['Field', 'Companies with a figure', 'Share of survey'],
                   [[lab, f"{sum(1 for r in rows if r.get(k) is not None)} of {len(rows)}", f"{_pct(sum(1 for r in rows if r.get(k) is not None), len(rows))}%"]
                    for lab, k in fields], [80 * mm, 50 * mm, CONTENT_W - 130 * mm], right_cols=(1, 2),
                   caption='Low coverage on a field means the market figures for it rest on few companies and should be read with care.')

    # appendix
    S += _section(st, 'Appendix', 'Companies in the survey', f'{n} {_plural(n, "company", "companies")} · {fy}')
    cos = overview.get('companies', [])
    by_id = {r['name']: r for r in rows}
    S += _grid(st, ['Company', 'Ticker', 'Sector', 'Board size', 'Score'],
               [[_e(c['name']), _e(by_id.get(c['name'], {}).get('ticker') or '—'), _e(c.get('sector') or '—'),
                 _fn(by_id.get(c['name'], {}).get('board_size')), _fn(((by_id.get(c['name'], {}).get('governance')) or {}).get('score'))] for c in cos],
               [58 * mm, 22 * mm, 50 * mm, 20 * mm, CONTENT_W - 150 * mm], right_cols=(3, 4))

    doc.build(S, onFirstPage=cover, onLaterPages=later)
    buf.seek(0)
    return buf


# ============================================================================ COMPANY REPORT
_NED_BENEFIT_LABELS = {'MedicalCover': 'medical cover', 'IndemnityInsurance': 'indemnity insurance',
                       'TravelAccommodation': 'travel and accommodation', 'TelephoneAllowance': 'telephone allowance',
                       'TransportAllowance': 'transport allowance', 'MealAllowance': 'meal allowance', 'ClubMembership': 'club membership',
                       'DutyDayAllowance': 'duty-day allowance', 'GroupPersonalAccident': 'group personal accident cover',
                       'ShareSchemeParticipation': 'share-scheme participation'}


def build_company_report(payload, overview, rows):
    """payload = the JSON of /api/companies/<id>/survey-report; overview = build_survey_overview(fy) or None;
    rows = build_company_rows(fy)['rows'] (may be empty)."""
    st = _styles()
    co, sv = payload['company'], payload['survey']
    name, fy = co['name'], sv['fiscal_year']
    cur = (sv.get('currency') or 'KES')
    perf_scale = UNIT_SCALE.get((sv.get('unit') or 'units').strip().lower(), 1)
    pay_scale = UNIT_SCALE.get((sv.get('director_figures_unit') or 'units').strip().lower(), 1)
    fig_scale = UNIT_SCALE.get((sv.get('director_figures_unit') or sv.get('unit') or 'units').strip().lower(), 1)
    sector = sv.get('sector') or co.get('sector')
    generated = datetime.utcnow().strftime('%d %B %Y')
    ticker = co.get('ticker')
    meta = {
        'kicker': 'Company Remuneration & Governance Report',
        'title': name,
        'subtitle': f"{fy} board, remuneration and governance review"
                    + (f" · {sector}" if sector else '') + (f" · {co.get('exchange') or 'NSE'}: {ticker}" if ticker else ''),
        'generated': generated,
        'running': f'{name} · {fy}',
        'footer': f'{APP_NAME} · built from {name}\'s own filing' + (f" ({sv['source_filename']})" if sv.get('source_filename') else ''),
    }
    cover, later = _make_page_callbacks(meta, st)
    buf = BytesIO()
    doc = _doc(buf, f"{name} — {fy}")
    S = []

    def P(field, scale=pay_scale):
        v = sv.get(field)
        return None if v is None else v * scale

    ov = overview if (overview and overview.get('has_data')) else None
    ov_dr = ov['directors_remuneration'] if ov else {}
    ov_cr = ov['committee_remuneration'] if ov else {}
    ov_bo = ov['board_overview'] if ov else {}
    n_peers = ov['companies_surveyed'] if ov else 0
    row_me = next((r for r in rows if r['company_id'] == co['id']), None)

    turnover, profit, mcap = P('turnover', perf_scale), P('net_profit', perf_scale), P('market_cap', perf_scale)
    size, meetings = sv.get('board_size'), sv.get('board_meetings_per_year')
    fem, mal = sv.get('directors_female'), sv.get('directors_male')
    ind, nind = sv.get('independent_neds_count'), sv.get('non_independent_neds_count')
    exe, ned = sv.get('executive_directors_count'), sv.get('non_executive_directors_count')

    # ---------------- cover page content
    S += [Spacer(1, 98 * mm)]
    S += _kpi_strip(st, [
        ('Market capitalisation', _e(_money(cur, mcap)) if mcap is not None else None, BLUE),
        ('Revenue / turnover', _e(_money(cur, turnover)) if turnover is not None else None, GREEN),
        ('Net profit' if (profit is None or profit >= 0) else 'Net loss', _e(_money(cur, abs(profit))) if profit is not None else None, AMBER),
        ('Board size', f"{size} directors" if size is not None else None, PURPLE)])
    S += [Paragraph(f"A plain-language review of how {_e(name)}'s board is composed and paid, drawn from its own {_e(fy)} filing"
                    + (f" and set against the other {n_peers - 1} {_plural(n_peers - 1, 'company', 'companies')} in the {_e(fy)} survey." if n_peers > 1 else '.'), st['body'])]
    S += [PageBreak()]

    # ---------------- 1. summary
    S += _section(st, 'Summary', f'{name} at a glance', f'The {fy} picture in brief.')
    lead = [f"{_e(name)}" + (f" is a {_e(sector)} company" if sector else ' is a company') + (f" listed on the {_e(co.get('exchange') or 'NSE')}" if ticker or co.get('exchange') else '') + '.']
    if turnover is not None:
        s = f"In {_e(fy)} it reported turnover of {_e(_money(cur, turnover))}"
        if profit is not None:
            s += (f" and a net profit of {_e(_money(cur, profit))}" if profit >= 0 else f" and a net loss of {_e(_money(cur, -profit))}")
            if turnover and abs(profit / turnover) <= 1:
                s += f", a margin of {profit / turnover * 100:.1f}%"
        lead.append(s + '.')
    elif profit is not None:
        lead.append(f"In {_e(fy)} it reported {'a net profit' if profit >= 0 else 'a net loss'} of {_e(_money(cur, abs(profit)))}.")
    if mcap is not None:
        lead.append(f"Its market capitalisation stands at {_e(_money(cur, mcap))}.")
    bits = []
    if size is not None:
        bits.append(f"{size} directors" + (f" meeting {meetings} times a year" if meetings is not None else ''))
    if bits:
        lead.append('The board comprises ' + bits[0] + '.')
    S += _paras(st, [' '.join(lead)], 'lead')

    findings = []
    if row_me and row_me['governance'].get('score') is not None:
        findings.append(f"Governance score: {row_me['governance']['score']} out of 100.")
    chair, oned = P('chairperson_annual_retainer'), P('other_ned_annual_retainer')
    rel = _relative(oned, _avg(ov_dr.get('other_ned_annual_retainer')) if ov_dr else None)
    if oned is not None and rel and n_peers > 1:
        findings.append(f"Non-executive directors are paid {rel} the survey average retainer.")
    if findings:
        S += [_callout(st, ' '.join(findings), BLUE)]

    # key figures against the survey
    def _mk(group, key):
        m_ = (ov.get(group) or {}).get(key) if ov and n_peers > 1 else None
        return m_ if (m_ and m_.get('average') is not None) else None

    def _vs(val, m_, is_amt=True, dp=1):
        if val is None and not m_:
            return None
        f = (lambda x: _fn(x)) if is_amt else (lambda x: _fn(x, dp))
        rel = _relative(val, m_.get('p50')) if (m_ and val is not None) else None
        return [f(val) if val is not None else '—', f(m_['p50']) if m_ and m_.get('p50') is not None else '—',
                f(m_['average']) if m_ else '—', (rel.replace(' the', '') if rel else '—')]
    kf = []
    for lab, val, grp, key, amt in [
            ('Board size (directors)', size, 'board_overview', 'avg_board_size', False),
            ('Board meetings per year', meetings, 'board_overview', 'avg_board_meetings_per_year', False),
            ('Committees per board', sv.get('committees_per_board'), 'board_overview', 'avg_committees_per_board', False),
            ('Chairperson annual retainer', P('chairperson_annual_retainer'), 'directors_remuneration', 'chairperson_annual_retainer', True),
            ('Other NED annual retainer', P('other_ned_annual_retainer'), 'directors_remuneration', 'other_ned_annual_retainer', True),
            ('Other NED per meeting', P('other_ned_meeting_allowance'), 'directors_remuneration', 'other_ned_meeting_allowance', True),
            ('Committee chair annual retainer', P('committee_chair_annual_retainer'), 'committee_remuneration', 'committee_chair_annual_retainer', True),
            ('CEO/MD monthly cost', P('ceo_monthly_cost_of_employment'), 'ceo_remuneration', 'ceo_monthly_cost_of_employment', True)]:
        row_ = _vs(val, _mk(grp, key), amt)
        if row_ and val is not None:
            kf.append([lab] + row_)
    if kf:
        S += [Paragraph('Key figures against the survey', st['h2'])]
        S += _grid(st, ['Measure', _e(name), 'Survey median', 'Survey average', 'Against median'], kf,
                   [58 * mm] + [(CONTENT_W - 58 * mm) / 4] * 4, right_cols=(1, 2, 3, 4),
                   caption=f'Amounts in {cur}, written in full. Survey figures appear only where at least {MIN_COMPANIES_FOR_AVERAGE} companies disclosed the measure.')

    # ---------------- 2. board & governance
    S += _section(st, 'Board & governance', 'Who sits on the board',
                  'Composition, independence and diversity.')
    t = []
    comp = []
    if size is not None:
        comp.append(f"The board has {size} members")
    if exe is not None and ned is not None:
        comp.append(f"{ned} non-executive and {exe} executive")
    if comp:
        t.append(', '.join(comp[:1]) + (f" — {comp[1]}" if len(comp) > 1 else '') + '.')
    if ind is not None and nind is not None and (ind + nind):
        pi = _pct(ind, ind + nind)
        s = f"{ind} of its {ind + nind} non-executive directors ({pi}%) are independent"
        s += ', which meets the one-third benchmark' if ind / (ind + nind) >= 1 / 3 else ', which falls short of the one-third benchmark'
        peer_ind = None
        if ov_bo.get('total_independent_neds') is not None and ov_bo.get('total_non_independent_neds') is not None:
            tot = ov_bo['total_independent_neds'] + ov_bo['total_non_independent_neds']
            peer_ind = _pct(ov_bo['total_independent_neds'], tot) if tot and n_peers > 1 else None
        if peer_ind is not None:
            s += f" (across all surveyed boards the figure is {peer_ind}%)"
        t.append(s + '.')
    if fem is not None and mal is not None and (fem + mal):
        pf = _pct(fem, fem + mal)
        s = f"Women hold {fem} of {fem + mal} seats ({pf}%)"
        s += ', meeting the one-third gender-diversity benchmark' if fem / (fem + mal) >= 1 / 3 else ', below the one-third gender-diversity benchmark'
        pf_all = None
        if ov_bo.get('total_directors_female') is not None and ov_bo.get('total_directors_male') is not None and n_peers > 1:
            pf_all = _pct(ov_bo['total_directors_female'], ov_bo['total_directors_female'] + ov_bo['total_directors_male'])
        if pf_all is not None:
            s += f" against {pf_all}% across the survey"
        t.append(s + '.')
    kn, nk = sv.get('neds_kenyan_count'), sv.get('neds_non_kenyan_count')
    if kn is not None and nk is not None and (kn + nk):
        t.append(f"{kn} of {kn + nk} non-executives are Kenyan nationals.")
    ages = [(lab, sv.get(k)) for lab, k in [('executive directors', 'avg_age_executive_directors'), ('non-executive directors', 'avg_age_non_executive_directors')] if sv.get(k) is not None]
    if ages:
        t.append('The average age of ' + _and([f"{lab} is {a:.1f} years" for lab, a in ages]) + '.')
    if meetings is not None:
        s = f"The board met {meetings} times"
        if ov_bo and _avg(ov_bo.get('avg_board_meetings_per_year')) is not None and n_peers > 1:
            s += f", against a survey average of {_avg(ov_bo['avg_board_meetings_per_year']):.1f}"
        t.append(s + '.')
    S += _paras(st, t) if t else _paras(st, [f"{_e(name)}'s filing did not yield board-composition figures for {_e(fy)}."])
    if fem is not None and mal is not None and (fem + mal):
        d = _donut([('Women', int(fem), PURPLE), ('Men', int(mal), BLUE)], f"{_pct(fem, fem + mal)}%")
        if d:
            S += [d, Paragraph('Gender split of the board.', st['caption'])]
    gov = row_me['governance'] if row_me else None
    if gov and gov.get('score') is not None:
        S += _paras(st, [f"On the illustrative governance score, {_e(name)} rates {gov['score']} out of 100. "
                         "Half the points reward board independence and half gender balance, each scaled against a one-third benchmark; the score is not a regulatory compliance determination."], 'small')

    dirs = payload.get('survey_director_rows') or []
    if dirs:
        S += [Paragraph('The directors', st['h2']),
              Paragraph(f"The register lists {len(dirs)} {_plural(len(dirs), 'director')}.", st['body'])]
        rws = []
        for d_ in dirs:
            ind_txt = 'Independent' if d_.get('independent') is True else ('Not independent' if d_.get('independent') is False else '—')
            rws.append([_e(d_.get('director_name')), _e(d_.get('position') or d_.get('role') or '—'), _e(d_.get('gender') or '—'),
                        _e(d_.get('nationality') or '—'), ind_txt, _e(d_.get('appointed_date') or '—'),
                        _e(', '.join(d_.get('committees') or []) or '—')])
        S += _grid(st, ['Director', 'Position', 'Gender', 'Nationality', 'Independence', 'Appointed', 'Committees'], rws,
                   [38 * mm, 32 * mm, 14 * mm, 20 * mm, 22 * mm, 20 * mm, CONTENT_W - 146 * mm])

    # ---------------- 3. how directors are paid
    S += _section(st, 'Remuneration', 'How directors are paid',
                  'Annual retainers and meeting allowances, set against the survey where there is a market figure.')
    mk = lambda key: (_avg(ov_dr.get(key)) if (ov_dr and n_peers > 1) else None)
    t = []
    if oned is not None:
        s = f"{_e(name)} pays each non-executive director an annual retainer of {_e(_money(cur, oned))}"
        r_ = _relative(oned, mk('other_ned_annual_retainer'))
        if r_:
            s += f", which is {r_} the survey average of {_e(_money(cur, mk('other_ned_annual_retainer')))}"
        t.append(s + '.')
    if chair is not None:
        s = f"The chairperson's retainer is {_e(_money(cur, chair))}"
        if oned:
            s += f" — {_times(chair / oned)} that of the other non-executives"
            mr = ov_dr.get('chairperson_vs_other_ned_annual_retainer_ratio') if ov_dr else None
            if mr and n_peers > 1:
                s += f", against a market multiple of {_times(mr)}"
        t.append(s + '.')
    oa, ca = P('other_ned_meeting_allowance'), P('chairperson_meeting_allowance')
    if oa is not None or ca is not None:
        bits = []
        if oa is not None:
            bits.append(f"{_e(_money(cur, oa))} per meeting for non-executives" + (f" ({_relative(oa, mk('other_ned_meeting_allowance'))} the survey average)" if _relative(oa, mk('other_ned_meeting_allowance')) else ''))
        if ca is not None:
            bits.append(f"{_e(_money(cur, ca))} for the chairperson")
        t.append('Sitting allowances are ' + _and(bits) + '.')
    ed, eda = P('executive_director_annual_retainer'), P('executive_director_meeting_allowance')
    if ed is not None or eda is not None:
        t.append('Executive directors receive ' + _and([f"an annual retainer of {_e(_money(cur, ed))}" if ed is not None else None,
                                                       f"{_e(_money(cur, eda))} per meeting" if eda is not None else None]) + '.')
    S += _paras(st, t) if t else _paras(st, [f"No director retainer or allowance figures were captured from {_e(name)}'s {_e(fy)} filing."])
    if oned is not None and mk('other_ned_annual_retainer') is not None:
        d = _hbar([(name, oned), ('Survey average', mk('other_ned_annual_retainer'))], color=BLUE, fmt=lambda v: _money(cur, v), value_w=44 * mm)
        if d:
            S += [d, Paragraph('Annual retainer of a non-executive director: this company against the survey average.', st['caption'])]

    pay_rows = [r for r in (payload.get('director_rows') or []) if not r.get('is_total_row') and not r.get('is_grand_total') and r.get('total') is not None]
    grand = next((r for r in (payload.get('director_rows') or []) if r.get('is_grand_total') and r.get('total') is not None), None)
    if pay_rows:
        top = max(pay_rows, key=lambda r: r['total'])
        s = f"The remuneration report itemises {len(pay_rows)} {_plural(len(pay_rows), 'director')}. {_e(top['director_name'])} received the most, at {_e(_money(cur, top['total'], fig_scale))}"
        if grand:
            s += f", out of a total of {_e(_money(cur, grand['total'], fig_scale))} paid to the board"
        S += [Paragraph('What the directors were paid', st['h2']), Paragraph(s + '.', st['body'])]
        S += _grid(st, ['Director', 'Role', f'Total paid ({cur})', 'Share of board total'],
                   [[_e(r['director_name']), _e((r.get('role') or '—').replace('_', ' ')), _fn(r['total'] * fig_scale),
                     (f"{r['total'] / grand['total'] * 100:.0f}%" if grand and grand.get('total') else '—')] for r in pay_rows],
                   [60 * mm, 32 * mm, 40 * mm, CONTENT_W - 132 * mm], right_cols=(2, 3))
        comp_rows = [r for r in pay_rows if r.get('components')]
        if comp_rows:
            S += [Paragraph('How each director\'s pay is made up', st['h2'])]
            lines = []
            for r in comp_rows:
                parts = []
                for k_, v_ in (r['components'] or {}).items():
                    if isinstance(v_, (int, float)) and v_:
                        parts.append(f"{_e(k_)}: {_num(v_ * fig_scale)}")
                if parts:
                    lines.append([_e(r['director_name']), '; '.join(parts)])
            if lines:
                S += _grid(st, ['Director', f'Components as filed ({cur})'], lines, [48 * mm, CONTENT_W - 48 * mm],
                           caption='Component names are the filing\'s own column headings.')

    # ---------------- 4. committees
    comms = payload.get('committees') or []
    cc, cm = P('committee_chair_annual_retainer'), P('committee_member_annual_retainer')
    cca, cma = P('committee_chair_meeting_allowance'), P('committee_member_meeting_allowance')
    if comms or any(v is not None for v in (cc, cm, cca, cma)):
        S += _section(st, 'Committees', 'Committee work', 'How the board delegates, and what committee service pays.')
        t = []
        if comms:
            mt_total = sum(c_.get('meetings_held') or 0 for c_ in comms)
            t.append(f"The board operates {len(comms)} {_plural(len(comms), 'committee')}: " + _and([_e(c_['name']) for c_ in comms]) + '.'
                     + (f" Between them they held {mt_total} meetings." if mt_total else ''))
            for c_ in comms:
                bits = []
                if c_.get('chairperson_name'):
                    bits.append(f"chaired by {_e(c_['chairperson_name'])}")
                if c_.get('member_count'):
                    bits.append(f"{c_['member_count']} members")
                if c_.get('meetings_held') is not None:
                    bits.append(f"{c_['meetings_held']} meetings")
                if c_.get('attendance_rate') is not None:
                    bits.append(f"{c_['attendance_rate']:.0f}% attendance")
                if bits:
                    t.append(f"<b>{_e(c_['name'])}</b> — " + ', '.join(bits) + '.')
        pay_bits = []
        if cm is not None:
            pay_bits.append(f"members receive an annual retainer of {_e(_money(cur, cm))}"
                            + (f" ({_relative(cm, _avg(ov_cr.get('committee_member_annual_retainer')))} the survey average)" if _relative(cm, _avg(ov_cr.get('committee_member_annual_retainer')) if ov_cr else None) and n_peers > 1 else ''))
        if cc is not None:
            pay_bits.append(f"chairs {_e(_money(cur, cc))}")
        if cma is not None:
            pay_bits.append(f"{_e(_money(cur, cma))} per meeting for members")
        if cca is not None:
            pay_bits.append(f"{_e(_money(cur, cca))} per meeting for chairs")
        if pay_bits:
            t.append('For committee service, ' + _and(pay_bits) + '.')
        S += _paras(st, t)
        if comms:
            S += _grid(st, ['Committee', 'Chair', 'Members', 'Meetings', 'Attendance'],
                       [[_e(c_['name']), _e(c_.get('chairperson_name') or '—'), _fn(c_.get('member_count')), _fn(c_.get('meetings_held')),
                         (f"{c_['attendance_rate']:.0f}%" if c_.get('attendance_rate') is not None else '—')] for c_ in comms],
                       [50 * mm, 52 * mm, 20 * mm, 20 * mm, CONTENT_W - 142 * mm], right_cols=(2, 3, 4))

    # ---------------- 5. CEO/MD
    ceo_cost = P('ceo_monthly_cost_of_employment')
    ceo_parts = [(lab, P(k)) for lab, k in [('basic salary', 'ceo_monthly_salary'), ('allowances', 'ceo_monthly_allowances'),
                 ('incentives and bonus', 'ceo_monthly_incentive_bonus'), ('deferred incentive', 'ceo_monthly_deferred_incentive'),
                 ('pension', 'ceo_monthly_pension'), ('gratuity', 'ceo_monthly_gratuity'), ('non-cash benefits', 'ceo_monthly_non_cash_benefits'),
                 ('share value', 'ceo_monthly_share_value')] if P(k) is not None]
    if ceo_cost is not None or ceo_parts:
        S += _section(st, 'Chief executive', 'CEO / MD pay', 'Shown per month so it can be set against other companies.')
        t = []
        ceo_mk = _avg(ov['ceo_remuneration'].get('ceo_monthly_cost_of_employment')) if ov and n_peers > 1 else None
        if ceo_cost is not None:
            s = f"The chief executive's monthly cost of employment is {_e(_money(cur, ceo_cost))}, or {_e(_money(cur, ceo_cost, 12))} over a year"
            r_ = _relative(ceo_cost, ceo_mk)
            if r_:
                s += f" — {r_} the survey average of {_e(_money(cur, ceo_mk))} a month"
            t.append(s + '.')
        if ceo_parts:
            t.append('The package is made up of monthly ' + _and([f"{lab} of {_e(_money(cur, v))}" for lab, v in ceo_parts]) + '.')
        if sv.get('ceo_monthly_conversion_basis'):
            t.append(f"<i>Monthly figures were derived from the filing as follows: {_e(sv['ceo_monthly_conversion_basis'])}.</i>")
        S += _paras(st, t)
        if ceo_parts:
            tot_ = ceo_cost or sum(v for _, v in ceo_parts)
            S += _grid(st, ['Element', f'Per month ({cur})', f'Per year ({cur})', 'Share of package'],
                       [[lab.capitalize(), _fn(v), _fn(v * 12), (f"{v / tot_ * 100:.0f}%" if tot_ else '—')] for lab, v in ceo_parts]
                       + ([['Total cost of employment', _fn(ceo_cost), _fn(ceo_cost * 12), '100%']] if ceo_cost is not None else []),
                       [60 * mm] + [(CONTENT_W - 60 * mm) / 3] * 3, right_cols=(1, 2, 3),
                       bold_rows=((len(ceo_parts),) if ceo_cost is not None else ()))

    # ---------------- 6. benefits
    nb = sv.get('ned_benefits') or {}
    bn = payload.get('survey_director_benefits') or []
    if nb or bn:
        S += _section(st, 'Benefits', 'Benefits and perquisites', 'What non-executive directors receive beyond pay.')
        t = []
        yes = [_NED_BENEFIT_LABELS[k] for k, v in nb.items() if k in _NED_BENEFIT_LABELS and v.get('provided')]
        no = [_NED_BENEFIT_LABELS[k] for k, v in nb.items() if k in _NED_BENEFIT_LABELS and v.get('provided') is False]
        if yes:
            t.append(f"{_e(name)} provides its non-executive directors with " + _and(yes) + '.')
        if no:
            t.append('It states that it does not provide ' + _and(no) + '.')
        if ov and n_peers > 1 and yes:
            common = []
            for k, v in nb.items():
                if k in _NED_BENEFIT_LABELS and v.get('provided'):
                    pp = (ov.get('ned_benefits_summary') or {}).get(k, {}).get('percent_provided')
                    if pp is not None:
                        common.append(f"{_NED_BENEFIT_LABELS[k]} ({pp:.0f}% of responding companies)")
            if common:
                t.append('For reference, the share of responding companies providing the same benefits is ' + _and(common) + '.')
        if bn:
            tot = sum(b_['total_amount'] for b_ in bn if b_.get('total_amount') is not None)
            t.append(f"Non-cash benefits were itemised for {len(bn)} {_plural(len(bn), 'director')}, totalling {_e(_money(cur, tot, fig_scale))}.")
        S += _paras(st, t)
        cats = payload.get('benefit_categories') or []
        if cats:
            S += [_table(st, ['Benefit', 'For', 'Amount'], [[_e(c_['category']), _e(c_.get('director_name') or 'Board / company'), _e(_money(cur, c_['amount'], fig_scale))] for c_ in cats],
                         [CONTENT_W * .4, CONTENT_W * .33, CONTENT_W * .27], right_cols=(2,))]

    # ---------------- 7. peers
    if row_me and n_peers > 1:
        S += _section(st, 'Peers', 'How it compares', f'Standing among the {n_peers} companies surveyed for {fy}.')
        t = []
        ranked = sorted([r for r in rows if r.get('total_director_remuneration')], key=lambda r: -r['total_director_remuneration'])
        pos = next((i + 1 for i, r in enumerate(ranked) if r['company_id'] == co['id']), None)
        if pos:
            t.append(f"On combined annual director retainers ({_e(_money(cur, row_me['total_director_remuneration']))}), {_e(name)} ranks {pos} of {len(ranked)} companies that filed enough to be ranked.")
            same = [r for r in ranked if (r.get('sector') or '') == (row_me.get('sector') or '') and r.get('sector')]
            if len(same) > 1:
                sp = next(i + 1 for i, r in enumerate(same) if r['company_id'] == co['id'])
                t.append(f"Within {_e(row_me['sector'])}, it ranks {sp} of {len(same)}.")
        sc = sorted([r for r in rows if (r.get('governance') or {}).get('score') is not None], key=lambda r: (-r['governance']['score'], r['name']))
        gpos = next((i + 1 for i, r in enumerate(sc) if r['company_id'] == co['id']), None)
        if gpos:
            t.append(f"On the governance score it ranks {gpos} of {len(sc)}.")
        S += _paras(st, t) if t else _paras(st, ['Peer rankings need more companies to have filed the same figures; none could be drawn for this company yet.'])
        peer_tbl = []
        for lab, key_, amt in [('Chairperson annual retainer', 'chairperson_annual_retainer', True), ('Other NED annual retainer', 'other_ned_annual_retainer', True),
                               ('Executive director annual retainer', 'executive_director_annual_retainer', True),
                               ('Combined director retainers', 'total_director_remuneration', True), ('Board size', 'board_size', False),
                               ('Governance score', None, False)]:
            pool = [(r['company_id'], (r['governance'].get('score') if key_ is None else r.get(key_))) for r in rows
                    if (r['governance'].get('score') if key_ is None else r.get(key_)) is not None]
            mine = next((v for cid, v in pool if cid == co['id']), None)
            if mine is None or len(pool) < 2:
                continue
            vals = sorted([v for _, v in pool], reverse=True)
            peer_tbl.append([lab, _fn(mine, None if amt else 0), _fn(_median(vals), None if amt else 0), _fn(vals[0], None if amt else 0),
                             f"{vals.index(mine) + 1} of {len(vals)}"])
        if peer_tbl:
            S += _grid(st, ['Measure', _e(name), 'Peer median', 'Peer highest', 'Rank'], peer_tbl,
                       [62 * mm] + [(CONTENT_W - 62 * mm) / 4] * 4, right_cols=(1, 2, 3, 4),
                       caption=f'Peers are the {n_peers} companies in the {_e(fy)} survey that filed each measure. Amounts in {cur}.')

    # ---------------- 8. about the data
    S += _section(st, 'Method', 'About this report', 'Where the figures come from, and what they leave out.')
    filled = sum(1 for f_ in QUALITY_FIELDS if sv.get(f_) is not None)
    t = [f"This review is built from {_e(name)}'s own {_e(fy)} filing" + (f" ({_e(sv['source_filename'])})" if sv.get('source_filename') else '')
         + f". It captured {filled} of {len(QUALITY_FIELDS)} core survey fields; anything the filing did not state is omitted here rather than estimated. Amounts are written in full, in {_e(cur)}."]
    review = sv.get('field_review_status') or {}
    appr = sum(1 for v in review.values() if v == 'approved')
    if appr:
        t.append(f"{appr} {_plural(appr, 'figure has', 'figures have')} been checked and approved by a reviewer.")
    confl = payload.get('evidence_conflicts') or []
    if confl:
        t.append(f"{len(confl)} {_plural(len(confl), 'figure')} had conflicting candidate values in the filing and should be confirmed against the source document.")
    t.append("Comparisons with the survey average are shown only where at least two companies supplied the figure. The governance score is illustrative and not a regulatory determination.")
    S += _paras(st, t)

    doc.build(S, onFirstPage=cover, onLaterPages=later)
    buf.seek(0)
    return buf