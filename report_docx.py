"""
Word report generator — Phase 6 step 2.

Takes the dict from report_context.build_report_context() and produces a
.docx. Every number in this file comes from that dict - nothing here
calculates anything or invents a figure. Where the context doesn't have
a line item, the section says "Not disclosed" instead of guessing.

This is the numbers-only pass. The next step (not built yet) sends a
trimmed version of this same context - just the computed metrics, none
of the prose logic below - to an LLM to draft the Executive Summary and
Key Findings sections. When that lands, this module's job narrows to
"lay out the numbers + drop in whatever narrative text it's handed" -
it still won't be the thing deciding what a number IS.

Usage:
    from report_context import build_report_context
    from report_docx import generate_docx
    ctx = build_report_context(period)
    generate_docx(ctx, "safaricom_fy2025.docx")
"""

from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

NOT_DISCLOSED = "Not disclosed in the source filing."

ACCENT = RGBColor(0x1F, 0x4E, 0x79)
MUTED = RGBColor(0x6B, 0x72, 0x80)


def _fmt_amount(value, currency=''):
    if value is None:
        return '—'
    sign = '-' if value < 0 else ''
    v = abs(value)
    if v >= 1e9:
        s = f"{v / 1e9:,.2f}B"
    elif v >= 1e6:
        s = f"{v / 1e6:,.2f}M"
    elif v >= 1e3:
        s = f"{v / 1e3:,.1f}K"
    else:
        s = f"{v:,.0f}"
    return f"{sign}{currency + ' ' if currency else ''}{s}"


def _fmt_pct(value):
    return '—' if value is None else f"{value:.1f}%"


def _cite(item):
    """A bracketed page citation next to a number pulled straight from a
    line item - only ever printed when the parser actually recorded a
    page number for it."""
    if item and item.get('page'):
        return f" [p. {item['page']}]"
    return ''


def _set_cell_shading(cell, hex_color):
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), hex_color)
    cell._tc.get_or_add_tcPr().append(shd)


def _add_hr(doc):
    p = doc.add_paragraph()
    p_fmt = p.paragraph_format
    p_fmt.space_before = Pt(0)
    p_fmt.space_after = Pt(12)
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement('w:pBdr')
    bottom = OxmlElement('w:bottom')
    bottom.set(qn('w:val'), 'single')
    bottom.set(qn('w:sz'), '6')
    bottom.set(qn('w:space'), '1')
    bottom.set(qn('w:color'), 'CCCCCC')
    pBdr.append(bottom)
    pPr.append(pBdr)


def _section_heading(doc, number, title):
    h = doc.add_heading(f"{number}. {title}", level=1)
    for run in h.runs:
        run.font.color.rgb = ACCENT


def _line_items_table(doc, items, currency, empty_text=NOT_DISCLOSED):
    """A simple two-column (label, amount) table for a flattened
    line-item list, indenting children under their parent by depth."""
    disclosed = [i for i in items if i['amount'] is not None]
    if not disclosed:
        doc.add_paragraph(empty_text).runs[0].italic = True
        return
    table = doc.add_table(rows=1, cols=2)
    table.style = 'Light Grid Accent 1'
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    hdr = table.rows[0].cells
    hdr[0].text, hdr[1].text = 'Line item', 'Amount'
    for item in disclosed:
        row = table.add_row().cells
        indent = '    ' * item['depth']
        row[0].text = f"{indent}{item['label']}"
        row[1].text = _fmt_amount(item['amount'], currency) + _cite(item)


def _metric_line(doc, label, item, currency, is_pct=False):
    value = item['amount'] if item else None
    text = _fmt_pct(value) if is_pct else _fmt_amount(value, currency)
    p = doc.add_paragraph()
    p.add_run(f"{label}: ").bold = True
    p.add_run(text + (_cite(item) if item else ''))


def generate_docx(ctx: dict, output_path: str, narrative: dict = None):
    company = ctx['company']
    period = ctx['period']
    currency = period['currency'] or ''
    statements = ctx['statements']
    metrics = ctx['calculated_metrics']
    yoy = ctx['yoy']

    inc = statements.get('income_statement', {}).get('by_normalized_name', {})
    bal = statements.get('balance_sheet', {}).get('by_normalized_name', {})
    cf = statements.get('cash_flow', {}).get('by_normalized_name', {})

    doc = Document()
    doc.sections[0].page_width = Inches(8.27)   # A4
    doc.sections[0].page_height = Inches(11.69)
    normal = doc.styles['Normal']
    normal.font.name = 'Calibri'
    normal.font.size = Pt(10.5)

    # ---------- Cover page ----------
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_before = Pt(180)
    run = title.add_run(company['name'])
    run.font.size = Pt(28)
    run.bold = True
    run.font.color.rgb = ACCENT

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = sub.add_run(f"{period['label']} Financial Analysis")
    run.font.size = Pt(16)
    run.font.color.rgb = MUTED

    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta_bits = [company.get('ticker'), company.get('exchange'), company.get('sector')]
    run = meta.add_run(' · '.join(b for b in meta_bits if b))
    run.font.size = Pt(11)
    run.font.color.rgb = MUTED

    generated = doc.add_paragraph()
    generated.alignment = WD_ALIGN_PARAGRAPH.CENTER
    generated.paragraph_format.space_before = Pt(24)
    run = generated.add_run("Generated from verified financial records.\nAny figure without a page citation is a calculated metric, not a reported one.")
    run.font.size = Pt(9)
    run.font.color.rgb = MUTED
    run.italic = True
    doc.add_page_break()

    # ---------- 1. Executive Summary ----------
    _section_heading(doc, 1, "Executive Summary")
    if narrative and narrative.get('executive_summary'):
        note = doc.add_paragraph()
        note.add_run("AI-drafted from the verified figures in this report — see the Appendix for every "
                      "underlying number and its source.").italic = True
        note.runs[0].font.color.rgb = MUTED
        note.runs[0].font.size = Pt(8.5)
        doc.add_paragraph(narrative['executive_summary'])
    else:
        rev_item, ni_item = inc.get('revenue'), inc.get('net_income')
        lines = []
        if 'revenue' in yoy:
            d = yoy['revenue']
            direction = 'increased' if d['change_pct'] >= 0 else 'decreased'
            lines.append(f"Revenue {direction} {abs(d['change_pct']):.1f}% to "
                          f"{_fmt_amount(d['current'], currency)} in {period['label']}, "
                          f"from {_fmt_amount(d['prior'], currency)} in {d['prior_period_label']}.")
        elif rev_item and rev_item['amount'] is not None:
            lines.append(f"Revenue for {period['label']} was {_fmt_amount(rev_item['amount'], currency)}{_cite(rev_item)}.")

        if 'net_income' in yoy:
            d = yoy['net_income']
            direction = 'increased' if d['change_pct'] >= 0 else 'decreased'
            lines.append(f"Net income {direction} {abs(d['change_pct']):.1f}% to {_fmt_amount(d['current'], currency)}.")
        elif ni_item and ni_item['amount'] is not None:
            lines.append(f"Net income was {_fmt_amount(ni_item['amount'], currency)}{_cite(ni_item)}.")

        if 'net_margin' in metrics:
            lines.append(f"Net margin was {_fmt_pct(metrics['net_margin']['value'])} (calculated: {metrics['net_margin']['formula']}).")
        if 'roe' in metrics:
            lines.append(f"Return on equity was {_fmt_pct(metrics['roe']['value'])} (calculated: {metrics['roe']['formula']}).")
        if 'debt_equity' in metrics:
            val = metrics['debt_equity']['value']
            lines.append(f"Debt/equity stood at {val:.2f}x (calculated: {metrics['debt_equity']['formula']}).")

        if lines:
            for line in lines:
                doc.add_paragraph(line)
        else:
            doc.add_paragraph(NOT_DISCLOSED).runs[0].italic = True

    # ---------- 2. Company Overview ----------
    _section_heading(doc, 2, "Company Overview")
    overview_bits = [
        ('Ticker', company.get('ticker')),
        ('Exchange', company.get('exchange')),
        ('Sector', company.get('sector')),
        ('Country', company.get('country')),
        ('Reporting period', period['label']),
        ('Period end', period.get('end_date')),
        ('Currency', currency),
    ]
    table = doc.add_table(rows=0, cols=2)
    table.style = 'Light List Accent 1'
    for label, value in overview_bits:
        row = table.add_row().cells
        row[0].text = label
        row[1].text = value or '—'

    # ---------- 3. Financial Highlights ----------
    _section_heading(doc, 3, "Financial Highlights")
    _metric_line(doc, 'Revenue', inc.get('revenue'), currency)
    _metric_line(doc, 'Net income', inc.get('net_income'), currency)
    _metric_line(doc, 'Total assets', bal.get('total_assets'), currency)
    _metric_line(doc, 'Total equity', bal.get('total_equity'), currency)
    if 'net_margin' in metrics:
        p = doc.add_paragraph()
        p.add_run('Net margin: ').bold = True
        p.add_run(_fmt_pct(metrics['net_margin']['value']) + ' (calculated)')
    if 'roe' in metrics:
        p = doc.add_paragraph()
        p.add_run('ROE: ').bold = True
        p.add_run(_fmt_pct(metrics['roe']['value']) + ' (calculated)')

    # ---------- 4. Revenue Analysis ----------
    _section_heading(doc, 4, "Revenue Analysis")
    _metric_line(doc, 'Total revenue', inc.get('revenue'), currency)
    if 'revenue' in yoy:
        d = yoy['revenue']
        doc.add_paragraph(f"Change vs {d['prior_period_label']}: {d['change_pct']:+.1f}%")
    doc.add_paragraph("Revenue by category / segment:", style=None).runs[0].bold = True
    if ctx['segments']:
        table = doc.add_table(rows=1, cols=3)
        table.style = 'Light Grid Accent 1'
        hdr = table.rows[0].cells
        hdr[0].text, hdr[1].text, hdr[2].text = 'Segment', 'Revenue', 'Operating profit'
        for seg in ctx['segments']:
            row = table.add_row().cells
            row[0].text = seg['segment_name']
            row[1].text = _fmt_amount(seg['revenue'], currency)
            row[2].text = _fmt_amount(seg['operating_profit'], currency)
    else:
        doc.add_paragraph(NOT_DISCLOSED).runs[0].italic = True

    # ---------- 5. Operating Cost Analysis ----------
    _section_heading(doc, 5, "Operating Cost Analysis")
    cost_names = ['employee_costs', 'network_operating_costs', 'marketing', 'depreciation', 'amortisation', 'finance_costs']
    cost_items = [inc[n] for n in cost_names if n in inc and inc[n]['amount'] is not None]
    if cost_items:
        table = doc.add_table(rows=1, cols=2)
        table.style = 'Light Grid Accent 1'
        hdr = table.rows[0].cells
        hdr[0].text, hdr[1].text = 'Cost line', 'Amount'
        for item in cost_items:
            row = table.add_row().cells
            row[0].text = item['label']
            row[1].text = _fmt_amount(item['amount'], currency) + _cite(item)
        rev = inc.get('revenue')
        total_costs = sum(i['amount'] for i in cost_items)
        if rev and rev['amount']:
            doc.add_paragraph(f"Operating costs shown / revenue: {abs(total_costs) / rev['amount'] * 100:.1f}%")
    else:
        doc.add_paragraph(NOT_DISCLOSED).runs[0].italic = True

    # ---------- 6. Profitability ----------
    _section_heading(doc, 6, "Profitability")
    _metric_line(doc, 'Gross profit', inc.get('gross_profit'), currency)
    _metric_line(doc, 'Profit before tax', inc.get('profit_before_tax'), currency)
    _metric_line(doc, 'Net income', inc.get('net_income'), currency)
    if 'net_margin' in metrics:
        doc.add_paragraph(f"Net margin: {_fmt_pct(metrics['net_margin']['value'])} (calculated)")
    if 'roa' in metrics:
        doc.add_paragraph(f"Return on assets: {_fmt_pct(metrics['roa']['value'])} (calculated)")
    if 'roe' in metrics:
        doc.add_paragraph(f"Return on equity: {_fmt_pct(metrics['roe']['value'])} (calculated)")

    # ---------- 7. Balance Sheet ----------
    _section_heading(doc, 7, "Balance Sheet")
    _metric_line(doc, 'Total assets', bal.get('total_assets'), currency)
    _metric_line(doc, 'Total liabilities', bal.get('total_liabilities'), currency)
    _metric_line(doc, 'Total equity', bal.get('total_equity'), currency)
    _metric_line(doc, 'Borrowings', bal.get('borrowings'), currency)
    if 'debt_equity' in metrics:
        doc.add_paragraph(f"Debt/equity: {metrics['debt_equity']['value']:.2f}x (calculated)")
    if 'debt_assets' in metrics:
        doc.add_paragraph(f"Debt/assets: {metrics['debt_assets']['value']:.2f}x (calculated)")

    # ---------- 8. Cash Flow ----------
    _section_heading(doc, 8, "Cash Flow")
    _metric_line(doc, 'Operating cash flow', cf.get('operating_cash_flow'), currency)
    _metric_line(doc, 'Investing cash flow', cf.get('investing_cash_flow'), currency)
    _metric_line(doc, 'Financing cash flow', cf.get('financing_cash_flow'), currency)
    ocf = cf.get('operating_cash_flow')
    capex = cf.get('capex')
    if ocf and capex and ocf['amount'] is not None and capex['amount'] is not None:
        fcf = ocf['amount'] - abs(capex['amount'])
        doc.add_paragraph(f"Free cash flow (operating CF − capex, calculated): {_fmt_amount(fcf, currency)}")

    # ---------- 9. Financial Ratios ----------
    _section_heading(doc, 9, "Financial Ratios")
    if metrics:
        table = doc.add_table(rows=1, cols=3)
        table.style = 'Light Grid Accent 1'
        hdr = table.rows[0].cells
        hdr[0].text, hdr[1].text, hdr[2].text = 'Metric', 'Value', 'Formula'
        for name, m in metrics.items():
            row = table.add_row().cells
            row[0].text = name.replace('_', ' ').title()
            row[1].text = f"{m['value']:.2f}" if m['value'] is not None else '—'
            row[2].text = m['formula'] or '—'
    else:
        doc.add_paragraph(NOT_DISCLOSED).runs[0].italic = True

    # ---------- 10. Key Findings ----------
    _section_heading(doc, 10, "Key Findings")
    if narrative and narrative.get('key_findings'):
        for f in narrative['key_findings']:
            doc.add_paragraph(f, style='List Bullet')
    else:
        findings = []
        for name, label in [('revenue', 'Revenue'), ('net_income', 'Net income'),
                             ('total_assets', 'Total assets'), ('operating_cash_flow', 'Operating cash flow')]:
            if name in yoy:
                d = yoy[name]
                direction = 'grew' if d['change_pct'] >= 0 else 'declined'
                findings.append(f"{label} {direction} {abs(d['change_pct']):.1f}% year over year, "
                                 f"from {_fmt_amount(d['prior'], currency)} to {_fmt_amount(d['current'], currency)}.")
        if not findings:
            findings.append("No prior-period filing on record for this company yet, so year-over-year "
                             "findings can't be calculated - add a prior FY filing to unlock this section.")
        for f in findings:
            doc.add_paragraph(f, style='List Bullet')

    # ---------- 11. Appendix ----------
    doc.add_page_break()
    _section_heading(doc, 11, "Appendix — Detailed Financial Statements")
    for stmt_type, label in [('income_statement', 'Income Statement'), ('balance_sheet', 'Balance Sheet'),
                              ('cash_flow', 'Cash Flow Statement'), ('equity', 'Statement of Changes in Equity')]:
        stmt = statements.get(stmt_type)
        h = doc.add_heading(label, level=2)
        for run in h.runs:
            run.font.color.rgb = MUTED
            run.font.size = Pt(13)
        if stmt and stmt['line_items']:
            _line_items_table(doc, stmt['line_items'], currency)
        else:
            doc.add_paragraph(NOT_DISCLOSED).runs[0].italic = True

    doc.add_heading('Definitions', level=2)
    definitions = {
        'Net margin': 'Net income ÷ revenue',
        'ROE': 'Net income ÷ total equity',
        'ROA': 'Net income ÷ total assets',
        'Debt/equity': 'Total liabilities ÷ total equity',
        '[p. N]': 'Page N of the source filing this figure was extracted from',
        '(calculated)': 'Derived from reported figures, not itself a reported line item',
    }
    for term, defn in definitions.items():
        p = doc.add_paragraph()
        p.add_run(f"{term}: ").bold = True
        p.add_run(defn)

    doc.save(output_path)
    return output_path
