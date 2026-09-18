"""
Renders the Survey page (survey_aggregate.build_survey_overview output)
as a downloadable PDF - a board/director remuneration benchmark report
built from however many companies have SurveyCompanyData on file for
the selected fiscal year.

Design notes:
- Every section mirrors the on-screen Survey page 1:1 - Executive
  Summary, Board Overview, Directors' Remuneration, Committee
  Remuneration, Comparative Analysis, Appendix (company list) - so the
  PDF is never a surprise relative to what the person was just looking
  at.
- A metric with fewer than MIN_COMPANIES_FOR_AVERAGE companies renders
  as "Not enough data yet (n of 2+ companies)" rather than being
  silently dropped or shown as a number - the same honesty rule the
  JSON API and the on-screen page both follow. This module doesn't
  recompute that rule; it only renders what build_survey_overview
  already decided.
- No invented company count, no rounding away a "not enough data"
  gap into a false precision.
"""

from io import BytesIO
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)

# FinSight palette, adapted for print (white background, same accent hues
# used on-screen in templates/index.html's :root custom properties)
NAVY = colors.HexColor('#0F2B5B')
BLUE = colors.HexColor('#1E6DFF')
GREEN = colors.HexColor('#1FA971')
AMBER = colors.HexColor('#B8790C')
PURPLE = colors.HexColor('#7C5CD6')
TEXT_DIM = colors.HexColor('#5A6478')
TEXT_FAINT = colors.HexColor('#8B93A8')
PANEL_BG = colors.HexColor('#F4F6FB')
BORDER = colors.HexColor('#E2E6F0')

NOT_ENOUGH_DATA = 'Not enough data yet'


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle('CoverTitle', fontName='Helvetica-Bold', fontSize=28, leading=34, textColor=NAVY))
    ss.add(ParagraphStyle('CoverSub', fontName='Helvetica', fontSize=13, leading=18, textColor=TEXT_DIM))
    ss.add(ParagraphStyle('SectionDivider', fontName='Helvetica-Bold', fontSize=22, leading=28, textColor=NAVY))
    ss.add(ParagraphStyle('SectionSub', fontName='Helvetica', fontSize=11, leading=15, textColor=TEXT_DIM))
    ss.add(ParagraphStyle('H2', fontName='Helvetica-Bold', fontSize=15, leading=19, textColor=NAVY, spaceBefore=4, spaceAfter=8))
    ss.add(ParagraphStyle('PanelTitle', fontName='Helvetica-Bold', fontSize=10.5, leading=13, textColor=NAVY, spaceAfter=4))
    ss.add(ParagraphStyle('Body', fontName='Helvetica', fontSize=9.5, leading=14, textColor=colors.HexColor('#222833')))
    ss.add(ParagraphStyle('Faint', fontName='Helvetica', fontSize=8, leading=11, textColor=TEXT_FAINT))
    ss.add(ParagraphStyle('KPILabel', fontName='Helvetica', fontSize=8.5, leading=11, textColor=TEXT_DIM))
    ss.add(ParagraphStyle('KPIValue', fontName='Helvetica-Bold', fontSize=17, leading=21, textColor=NAVY))
    ss.add(ParagraphStyle('TableHeader', fontName='Helvetica-Bold', fontSize=8.5, leading=11, textColor=colors.white))
    ss.add(ParagraphStyle('TableCell', fontName='Helvetica', fontSize=8.5, leading=11, textColor=colors.HexColor('#222833')))
    ss.add(ParagraphStyle('TableCellFaint', fontName='Helvetica-Oblique', fontSize=8.5, leading=11, textColor=TEXT_FAINT))
    return ss


def _fmt_amount(value):
    """Matches the app's own fmtAmount() convention: large figures get
    a K/M/B suffix so a wide retainer table doesn't overflow."""
    if value is None:
        return None
    v = abs(value)
    sign = '-' if value < 0 else ''
    if v >= 1_000_000_000:
        return f'{sign}{v/1_000_000_000:.2f}B'
    if v >= 1_000_000:
        return f'{sign}{v/1_000_000:.2f}M'
    if v >= 1_000:
        return f'{sign}{v/1_000:.1f}K'
    return f'{sign}{v:.0f}'


def _fmt_metric(metric, currency='', suffix='', decimals=0, is_amount=False, with_percentiles=False):
    """metric is {'average': float|None, 'p25'/'p50'/'p75': float|None,
    'company_count': int} as produced by survey_aggregate.py. Renders the
    honest not-enough-data fallback rather than ever showing 0 or blank
    for a genuinely missing figure. with_percentiles=True appends the
    25th/50th/75th percentile breakdown, matching the source survey PDF's
    percentile-table convention for compensation figures."""
    if metric is None or metric.get('average') is None:
        count = (metric or {}).get('company_count', 0)
        return f'{NOT_ENOUGH_DATA} ({count} of 2+ companies)'

    def _one(val):
        body = _fmt_amount(val) if is_amount else f'{val:.{decimals}f}'
        prefix = f'{currency} ' if currency else ''
        return f'{prefix}{body}{suffix}'

    out = _one(metric['average'])
    if with_percentiles and metric.get('p25') is not None:
        out += f"  (25th: {_one(metric['p25'])} · 50th: {_one(metric['p50'])} · 75th: {_one(metric['p75'])})"
    return out


def _fmt_total(value):
    return str(value) if value is not None else '—'


def _kpi_table(items, styles):
    """items: list of (label, value_str) tuples, laid out as a 2x2 or
    1xN row of boxed KPI cells."""
    cells = []
    for label, value in items:
        cell = [
            Paragraph(label, styles['KPILabel']),
            Paragraph(value, styles['KPIValue']),
        ]
        cells.append(cell)
    col_width = (170 * mm) / len(cells)
    table = Table([cells], colWidths=[col_width] * len(cells))
    table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BACKGROUND', (0, 0), (-1, -1), PANEL_BG),
        ('BOX', (0, 0), (-1, -1), 0.5, BORDER),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, BORDER),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
    ]))
    return table


def _kv_panel(title, rows, styles, note=None):
    """A titled panel of label/value rows - the print equivalent of the
    on-screen .panel / .ir-kv-list."""
    body_rows = []
    for label, value in rows:
        body_rows.append([Paragraph(label, styles['Body']), Paragraph(str(value), styles['Body'])])
    tbl = Table(body_rows, colWidths=[95 * mm, 65 * mm])
    tbl.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LINEBELOW', (0, 0), (-1, -2), 0.4, BORDER),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
    ]))
    elements = [Paragraph(title, styles['PanelTitle']), tbl]
    if note:
        elements.append(Spacer(1, 3))
        elements.append(Paragraph(note, styles['Faint']))
    wrapper = Table([[e] for e in elements], colWidths=[170 * mm])
    wrapper.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, BORDER),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    return wrapper


def _section_divider(story, styles, title, subtitle):
    story.append(PageBreak())
    story.append(Spacer(1, 60 * mm))
    story.append(Paragraph(title, styles['SectionDivider']))
    story.append(Spacer(1, 6))
    story.append(Paragraph(subtitle, styles['SectionSub']))


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont('Helvetica', 7.5)
    canvas.setFillColor(TEXT_FAINT)
    canvas.drawString(20 * mm, 12 * mm, 'FinSight — Board Remuneration Survey')
    canvas.drawRightString(190 * mm, 12 * mm, f'Page {doc.page}')
    canvas.restoreState()


def build_survey_pdf(overview: dict) -> BytesIO:
    """overview is the dict returned by survey_aggregate.build_survey_overview()
    with has_data=True. Returns an in-memory PDF buffer."""
    styles = _styles()
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm, bottomMargin=20 * mm,
        title=f"Board Remuneration Survey — {overview['fiscal_year']}",
    )
    story = []

    currency = overview.get('currency', 'KES')
    unit = overview.get('unit', 'millions')
    fiscal_year = overview['fiscal_year']
    n = overview['companies_surveyed']
    min_n = overview.get('min_companies_for_average', 2)

    # ---- Cover ----
    story.append(Spacer(1, 40 * mm))
    story.append(Paragraph('Board Remuneration Survey', styles['CoverTitle']))
    story.append(Spacer(1, 4))
    story.append(Paragraph('Executive and Non-Executive Directors&rsquo; Remuneration Survey', styles['CoverSub']))
    story.append(Spacer(1, 30))
    story.append(Paragraph(f'Fiscal Year: {fiscal_year}', styles['Body']))
    story.append(Paragraph(f'{n} compan{"y" if n == 1 else "ies"} surveyed', styles['Body']))
    story.append(Paragraph(f'Figures in {currency} ({unit}) unless stated otherwise', styles['Body']))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f'Generated {datetime.utcnow().strftime("%d %B %Y")} — built from company filings uploaded to FinSight. '
        f'A figure is shown as an average only once at least {min_n} companies have supplied it; otherwise it is '
        f'marked "{NOT_ENOUGH_DATA}" rather than estimated.',
        styles['Faint']
    ))

    # ---- Table of contents ----
    story.append(Spacer(1, 26))
    story.append(Paragraph('Contents', styles['H2']))
    toc_items = [
        '1. Executive Summary', '2. Board Overview', '3. Directors&rsquo; Remuneration',
        '4. Committee Remuneration', '5. CEO/MD Remuneration', '6. NED Benefits',
        '7. Comparative Analysis', '8. Appendix — Companies Surveyed',
    ]
    for item in toc_items:
        story.append(Paragraph(item, styles['Body']))

    # ================= EXECUTIVE SUMMARY =================
    _section_divider(story, styles, 'Executive Summary', f'Key findings across {n} surveyed compan{"y" if n == 1 else "ies"} — {fiscal_year}')
    story.append(PageBreak())
    story.append(Paragraph('1. Executive Summary', styles['H2']))

    es = overview['executive_summary']
    kpis = [
        ('Companies Surveyed', str(n)),
        ('Avg. Turnover', _fmt_metric(es['avg_turnover'], currency, is_amount=True)),
        ('Avg. Net Profit', _fmt_metric(es['avg_net_profit'], currency, is_amount=True)),
        ('Avg. Profit Margin', _fmt_metric(es['avg_profit_margin'], suffix='%')),
    ]
    story.append(_kpi_table(kpis, styles))
    story.append(Spacer(1, 12))

    board_rows = [
        ('Average Board Size', _fmt_metric(es['avg_board_size'], decimals=1)),
        ('Average Board Meetings / Year', _fmt_metric(es['avg_board_meetings_per_year'], decimals=1)),
        ('Average Committees / Board', _fmt_metric(es['avg_committees_per_board'], decimals=1)),
        ('Average Committee Meetings / Year', _fmt_metric(es['avg_committee_meetings_per_year'], decimals=1)),
        ('Female Directors (total, surveyed companies)', _fmt_total(es['total_directors_female'])),
        ('Male Directors (total, surveyed companies)', _fmt_total(es['total_directors_male'])),
    ]
    story.append(_kv_panel('Board Composition Snapshot', board_rows, styles))

    # ================= BOARD OVERVIEW =================
    _section_divider(story, styles, 'Board Overview', 'Composition, gender, independence and age profile')
    story.append(PageBreak())
    story.append(Paragraph('2. Board Overview', styles['H2']))

    bo = overview['board_overview']
    gender_rows = [
        ('Female Directors (total)', _fmt_total(bo['total_directors_female'])),
        ('Male Directors (total)', _fmt_total(bo['total_directors_male'])),
        ('Executive Directors (total)', _fmt_total(bo['total_executive_directors'])),
        ('Non-Executive Directors (total)', _fmt_total(bo['total_non_executive_directors'])),
        ('Independent NEDs (total)', _fmt_total(bo['total_independent_neds'])),
        ('Non-Independent NEDs (total)', _fmt_total(bo['total_non_independent_neds'])),
        ('NEDs — Kenyan (total)', _fmt_total(bo['total_neds_kenyan'])),
        ('NEDs — Non-Kenyan (total)', _fmt_total(bo['total_neds_non_kenyan'])),
    ]
    story.append(_kv_panel('Gender, Role & Independence (totals across surveyed companies)', gender_rows, styles))
    story.append(Spacer(1, 10))

    age_rows = [
        ('Avg. Age — Executive Directors', _fmt_metric(bo['avg_age_executive_directors'], suffix=' yrs')),
        ('Avg. Age — Non-Executive Directors', _fmt_metric(bo['avg_age_non_executive_directors'], suffix=' yrs')),
        ('Avg. Age — Independent NEDs', _fmt_metric(bo['avg_age_independent_neds'], suffix=' yrs')),
        ('Avg. Age — Non-Independent NEDs', _fmt_metric(bo['avg_age_non_independent_neds'], suffix=' yrs')),
    ]
    story.append(_kv_panel('Age Profile', age_rows, styles,
                           note='Averages shown only where at least two companies supplied this figure.'))
    story.append(Spacer(1, 10))

    meeting_rows = [
        ('Avg. Board Size', _fmt_metric(bo['avg_board_size'], decimals=1)),
        ('Avg. Board Meetings / Year', _fmt_metric(bo['avg_board_meetings_per_year'], decimals=1)),
        ('Avg. Committees / Board', _fmt_metric(bo['avg_committees_per_board'], decimals=1)),
        ('Avg. Committee Meetings / Year', _fmt_metric(bo['avg_committee_meetings_per_year'], decimals=1)),
    ]
    story.append(_kv_panel('Governance Cadence', meeting_rows, styles))

    # ================= DIRECTORS' REMUNERATION =================
    _section_divider(story, styles, "Directors' Remuneration", 'Chairperson, Non-Executive and Executive Director pay')
    story.append(PageBreak())
    story.append(Paragraph("3. Directors' Remuneration", styles['H2']))

    dr = overview['directors_remuneration']
    ned_rows = [
        ('Chairperson Annual Retainer', _fmt_metric(dr['chairperson_annual_retainer'], currency, is_amount=True, with_percentiles=True)),
        ('Other NED Annual Retainer', _fmt_metric(dr['other_ned_annual_retainer'], currency, is_amount=True, with_percentiles=True)),
        ('Chairperson Meeting Allowance (per meeting)', _fmt_metric(dr['chairperson_meeting_allowance'], currency, is_amount=True, with_percentiles=True)),
        ('Other NED Meeting Allowance (per meeting)', _fmt_metric(dr['other_ned_meeting_allowance'], currency, is_amount=True, with_percentiles=True)),
    ]
    story.append(_kv_panel('Chairperson vs. Other Non-Executive Directors', ned_rows, styles))
    story.append(Spacer(1, 10))

    ed_rows = [
        ('Executive Director Annual Retainer', _fmt_metric(dr['executive_director_annual_retainer'], currency, is_amount=True, with_percentiles=True)),
        ('Executive Director Meeting Allowance (per meeting)', _fmt_metric(dr['executive_director_meeting_allowance'], currency, is_amount=True, with_percentiles=True)),
    ]
    story.append(_kv_panel('Executive Directors', ed_rows, styles))

    # ================= COMMITTEE REMUNERATION =================
    _section_divider(story, styles, 'Committee Remuneration', 'Committee chair and member compensation')
    story.append(PageBreak())
    story.append(Paragraph('4. Committee Remuneration', styles['H2']))

    cr = overview['committee_remuneration']
    committee_rows = [
        ('Committee Chair Annual Retainer', _fmt_metric(cr['committee_chair_annual_retainer'], currency, is_amount=True, with_percentiles=True)),
        ('Committee Member Annual Retainer', _fmt_metric(cr['committee_member_annual_retainer'], currency, is_amount=True, with_percentiles=True)),
        ('Committee Chair Meeting Allowance (per meeting)', _fmt_metric(cr['committee_chair_meeting_allowance'], currency, is_amount=True, with_percentiles=True)),
        ('Committee Member Meeting Allowance (per meeting)', _fmt_metric(cr['committee_member_meeting_allowance'], currency, is_amount=True, with_percentiles=True)),
    ]
    story.append(_kv_panel('Committee Chair vs. Member', committee_rows, styles))

    # ================= CEO/MD REMUNERATION =================
    _section_divider(story, styles, 'CEO/MD Remuneration', 'Chief Executive Officer / Managing Director monthly compensation')
    story.append(PageBreak())
    story.append(Paragraph('5. CEO/MD Remuneration', styles['H2']))

    cp = overview['ceo_remuneration']
    ceo_rows = [
        ('Salary (monthly)', _fmt_metric(cp['ceo_monthly_salary'], currency, is_amount=True, with_percentiles=True)),
        ('Allowances (monthly)', _fmt_metric(cp['ceo_monthly_allowances'], currency, is_amount=True, with_percentiles=True)),
        ('Incentives/Bonus (monthly)', _fmt_metric(cp['ceo_monthly_incentive_bonus'], currency, is_amount=True, with_percentiles=True)),
        ('Deferred Incentive (monthly)', _fmt_metric(cp['ceo_monthly_deferred_incentive'], currency, is_amount=True, with_percentiles=True)),
        ('Non-Cash Benefits (monthly)', _fmt_metric(cp['ceo_monthly_non_cash_benefits'], currency, is_amount=True, with_percentiles=True)),
        ('Pension (monthly)', _fmt_metric(cp['ceo_monthly_pension'], currency, is_amount=True, with_percentiles=True)),
        ('Gratuity (monthly)', _fmt_metric(cp['ceo_monthly_gratuity'], currency, is_amount=True, with_percentiles=True)),
        ('Share Value (monthly)', _fmt_metric(cp['ceo_monthly_share_value'], currency, is_amount=True, with_percentiles=True)),
        ('Monthly Cost of Employment (total)', _fmt_metric(cp['ceo_monthly_cost_of_employment'], currency, is_amount=True, with_percentiles=True)),
    ]
    story.append(_kv_panel('CEO/MD Compensation Components', ceo_rows, styles))

    # ================= NED BENEFITS =================
    _section_divider(story, styles, 'NED Benefits', 'Non-cash benefits and perquisites offered to Non-Executive Directors')
    story.append(PageBreak())
    story.append(Paragraph('6. NED Benefits', styles['H2']))

    benefits = overview.get('ned_benefits_summary', {})
    benefit_labels = {
        'MedicalCover': 'Medical Cover',
        'IndemnityInsurance': 'Indemnity / Liability Insurance',
        'TravelAccommodation': 'Travel & Accommodation',
        'TelephoneAllowance': 'Telephone Allowance',
        'TransportAllowance': 'Transport Allowance',
        'MealAllowance': 'Meal Allowance',
        'ClubMembership': 'Club Membership',
        'DutyDayAllowance': 'Duty Day Allowance',
        'GroupPersonalAccident': 'Group Personal Accident',
        'ShareSchemeParticipation': 'Remuneration via Share Schemes',
    }
    benefit_rows = []
    for key, label in benefit_labels.items():
        b = benefits.get(key, {})
        pct = b.get('percent_provided')
        count = b.get('companies_addressing', 0)
        if pct is None:
            benefit_rows.append((label, f'{NOT_ENOUGH_DATA} (0 companies addressed this)'))
        else:
            benefit_rows.append((label, f'{pct:.1f}% of {count} compan{"y" if count == 1 else "ies"} that addressed this'))
    story.append(_kv_panel('% of Companies Providing Each Benefit', benefit_rows, styles))

    # ================= COMPARATIVE ANALYSIS =================
    _section_divider(story, styles, 'Comparative Analysis', 'Sector-by-sector comparison')
    story.append(PageBreak())
    story.append(Paragraph('7. Comparative Analysis — by Sector', styles['H2']))

    sector_table_data = [[
        Paragraph('Sector', styles['TableHeader']),
        Paragraph('Companies', styles['TableHeader']),
        Paragraph('Avg. Other NED Annual Retainer', styles['TableHeader']),
    ]]
    for s in overview['sector_comparison']:
        if s['avg_other_ned_annual_retainer'] is not None:
            retainer_cell = Paragraph(f"{currency} {_fmt_amount(s['avg_other_ned_annual_retainer'])}", styles['TableCell'])
        else:
            retainer_cell = Paragraph(
                f"{NOT_ENOUGH_DATA} ({s['avg_other_ned_annual_retainer_company_count']} of {s['company_count']} in sector)",
                styles['TableCellFaint']
            )
        sector_table_data.append([
            Paragraph(s['sector'], styles['TableCell']),
            Paragraph(str(s['company_count']), styles['TableCell']),
            retainer_cell,
        ])
    sector_table = Table(sector_table_data, colWidths=[60 * mm, 30 * mm, 80 * mm], repeatRows=1)
    sector_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('LINEBELOW', (0, 0), (-1, -1), 0.4, BORDER),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PANEL_BG]),
    ]))
    story.append(sector_table)

    # ================= APPENDIX =================
    _section_divider(story, styles, 'Appendix', 'Companies surveyed and data sources')
    story.append(PageBreak())
    story.append(Paragraph('8. Appendix — Companies Surveyed', styles['H2']))
    story.append(Paragraph(
        f'This survey draws on {n} compan{"y" if n == 1 else "ies"}&rsquo;{"s" if n == 1 else ""} own filings, '
        f'uploaded individually to FinSight. Unlike a single aggregate survey document, this list — and every '
        f'figure above — grows as more companies are added, with no need to re-upload anything already on file.',
        styles['Body']
    ))
    story.append(Spacer(1, 10))

    company_table_data = [[
        Paragraph('Company', styles['TableHeader']),
        Paragraph('Sector', styles['TableHeader']),
        Paragraph('Source File', styles['TableHeader']),
    ]]
    for c in overview['companies']:
        company_table_data.append([
            Paragraph(c['name'], styles['TableCell']),
            Paragraph(c['sector'] or '—', styles['TableCell']),
            Paragraph(c['source_filename'] or '—', styles['TableCellFaint']),
        ])
    company_table = Table(company_table_data, colWidths=[65 * mm, 45 * mm, 60 * mm], repeatRows=1)
    company_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('LINEBELOW', (0, 0), (-1, -1), 0.4, BORDER),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PANEL_BG]),
    ]))
    story.append(company_table)

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buf.seek(0)
    return buf
