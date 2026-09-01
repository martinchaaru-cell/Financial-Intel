"""
Market survey parser - Executive & Non-Executive Directors' Remuneration
Survey (NSE-wide benchmark report), not a per-company financial statement.

This is deliberately a SEPARATE parser from nse_pdf_parse.py. That one is
keyed to income-statement / balance-sheet / cash-flow headings and pulls
one company's own reported figures. This one is keyed to the specific
table/heading layout of the NSE remuneration survey and pulls aggregate,
market-wide benchmark numbers (percentiles, sector averages, benefit
prevalence) that apply across the whole surveyed market - never a single
company's own figures.

Built and tested against the "Board Remuneration Report" NSE survey
format specifically. A future edition of the same survey should still
parse correctly as long as it keeps the same table layout; a genuinely
different report format (a different publisher's survey, a different
table structure) would need its own targeted parser rather than assuming
this one generalizes - hand it a broken/empty result rather than a wrong
one, so the caller can tell parsing didn't recognize the document.
"""

import re
import io
import pdfplumber

SECTOR_NAMES = {
    'Agricultural', 'Commercial and services', 'Manufacturing',
    'Banking', 'Insurance', 'Others',
}

BENEFIT_LABELS = [
    'Medical cover', 'Indemnity/Liability Insurance', 'Travel and accommodation',
    'Telephone allowance', 'Transport allowance', 'Meal allowance',
    'Club membership', 'Duty day allowance', 'Group Personal Accident',
    'Remuneration via share schemes',
]

CEO_COMPONENT_LABELS = [
    'Salary', 'Allowances', 'Incentives/ bonus', 'Deferred Incentive',
    'Non-cash benefits', 'Pension', 'Gratuity', 'Share value',
    'Monthly cost of employment',
]


def _num(token):
    if token is None:
        return None
    token = token.strip().replace(',', '').replace('%', '')
    if token in ('', '-', '—'):
        return None
    try:
        return float(token)
    except ValueError:
        return None


def _norm(s):
    return (s or '').replace('\u2019', "'").replace('\u2018', "'")


def _find_page_with(pages_text, needle):
    needle = _norm(needle).lower()
    for i, text in enumerate(pages_text):
        if needle in _norm(text).lower():
            return i
    return None


def _parse_percentile_table(rows, allowed_labels=None):
    """Rows shaped like ['Chairperson', '1,288,529', '3,300,000', '5,012,500', '4,051,511'],
    optionally preceded by a header row - returns {role: {p25,p50,p75,average}}.
    allowed_labels, if given, restricts which first-cell values are accepted
    (exact match after normalizing whitespace/case) - needed because some
    pages pack more than one sub-table into a single extracted table with
    no clean boundary, so a loose 'contains chair' match can pull a later
    section's row into the wrong bucket."""
    out = {}
    for row in rows:
        cells = [c for c in row if c is not None]
        if len(cells) < 5:
            continue
        label_raw = re.sub(r'\s+', ' ', cells[0]).strip().lower()
        if allowed_labels is not None and label_raw not in allowed_labels:
            continue
        label = label_raw.replace(' ', '_').replace('/', '')
        nums = [_num(c) for c in cells[1:5]]
        if all(n is None for n in nums) or 'percentile' in cells[1].lower():
            continue
        out[label] = {'p25': nums[0], 'p50': nums[1], 'p75': nums[2], 'average': nums[3]}
    return out


def parse_survey_pdf(pdf_bytes: bytes) -> dict:
    """Returns a dict ready to hand to save_survey() in app.py. Never
    raises for a recognized-but-partial document - missing sections come
    back as empty rather than aborting the whole import, so a report that
    only has some of the expected tables still saves what it has."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages_text = [p.extract_text() or '' for p in pdf.pages]
        pages = pdf.pages

        result = {
            'meta': {}, 'market_metrics': [], 'sector_companies': [],
            'remuneration_stats': [], 'sector_allowances': [], 'benefits': [],
            'ceo_comp': [],
        }

        # ---- Meta (title page + "source of information" page) ----
        title_text = pages_text[0] if pages_text else ''
        title_lines = [l.strip() for l in title_text.splitlines() if l.strip()]
        result['meta']['title'] = ' '.join(title_lines[:3]) if title_lines else 'Untitled Survey'
        m = re.search(r'(\d{4})\s*$', title_text.strip().splitlines()[-2]) if len(title_text.splitlines()) > 1 else None
        edition_match = re.search(r'(\d+)(?:st|nd|rd|th)\s+Edition', title_text, re.IGNORECASE)
        year_match = re.search(r'Report\s+(\d{4})', title_text)
        result['meta']['edition'] = edition_match.group(0) if edition_match else None
        result['meta']['report_year'] = year_match.group(1) if year_match else None

        src_page = _find_page_with(pages_text, 'Source of information')
        if src_page is not None:
            src_text = pages_text[src_page]
            n_match = re.search(r'focuses on\s+(\d+)\s+companies', src_text)
            result['meta']['companies_surveyed'] = int(n_match.group(1)) if n_match else None
            period_match = re.search(r'financial years ending either on ([^.]+)\.', src_text)
            result['meta']['period_covered'] = re.sub(r'\s+', ' ', period_match.group(1)).strip() if period_match else None

            # NSE Capitalisation by year, e.g. "2017 2.5" / "2018 2.1" / "2020 1.9" / "2021 2.7"
            seen_years = set()
            for yr, val in re.findall(r'\b(20\d{2})\s+(\d+\.\d+)\b', src_text):
                if yr not in seen_years:
                    seen_years.add(yr)
                    result['market_metrics'].append(
                        {'metric_name': 'nse_capitalisation', 'period_label': yr, 'value': float(val)})

        # ---- Average turnover / net profit / profit margin (Figure 2 & 3) ----
        perf_page = _find_page_with(pages_text, 'Average turnover vs average net')
        if perf_page is not None:
            perf_text = pages_text[perf_page]
            for period, turnover, net_profit in re.findall(
                    r'(\d{4}/\d{4})\s+([\d,]+)\s+([\d,]+)', perf_text):
                result['market_metrics'].append(
                    {'metric_name': 'avg_turnover', 'period_label': period, 'value': _num(turnover)})
                result['market_metrics'].append(
                    {'metric_name': 'avg_net_profit', 'period_label': period, 'value': _num(net_profit)})
            margin_match = re.search(
                r'\(?([\d.]+)%\s+in\s+FY\s*20/21\s+compared\s+to\s+([\d.]+)%\s+in\s+FY\s*19/20\)?',
                perf_text, re.IGNORECASE)
            if margin_match:
                result['market_metrics'].append(
                    {'metric_name': 'avg_profit_margin', 'period_label': '2019/2020', 'value': _num(margin_match.group(2))})
                result['market_metrics'].append(
                    {'metric_name': 'avg_profit_margin', 'period_label': '2020/2021', 'value': _num(margin_match.group(1))})

        # ---- NED annual fees / sitting allowance table (Table 3) ----
        stats_page = _find_page_with(pages_text, "NEDs' Remuneration")
        if stats_page is not None and stats_page + 1 < len(pages):
            table = pages[stats_page].extract_tables()
            for t in table:
                rows = t
                # This table has two stacked sub-tables (annual fees, then
                # sitting allowance) separated by a repeated header row.
                header_positions = [i for i, r in enumerate(rows) if r and r[0] and 'percentile' in ' '.join(c or '' for c in r).lower()]
                sub_tables = []
                for idx, start in enumerate(header_positions):
                    end = header_positions[idx + 1] if idx + 1 < len(header_positions) else len(rows)
                    sub_tables.append(rows[start:end])
                labels = ['annual_fee', 'sitting_allowance']
                for label, sub in zip(labels, sub_tables):
                    parsed = _parse_percentile_table(sub, allowed_labels={'chairperson', 'other neds'})
                    for role, stats in parsed.items():
                        role_key = 'chairperson' if role == 'chairperson' else 'other_neds'
                        result['remuneration_stats'].append({'category': label, 'role': role_key, **stats})

        # ---- Committee allowance table (own page, "Committee attendance") ----
        committee_page = _find_page_with(pages_text, 'Committee attendance')
        if committee_page is not None:
            for t in pages[committee_page].extract_tables():
                parsed = _parse_percentile_table(t, allowed_labels={'committee chairperson', 'committee member'})
                for role, stats in parsed.items():
                    role_key = 'committee_chairperson' if role == 'committee_chairperson' else 'committee_member'
                    result['remuneration_stats'].append({'category': 'committee_allowance', 'role': role_key, **stats})

        # ---- Sector categorisation (Table 4) ----
        sector_page = _find_page_with(pages_text, 'Categorisation by sector')
        if sector_page is not None:
            tables = pages[sector_page].extract_tables()
            if tables:
                current = [None, None, None]
                for row in tables[0]:
                    cells = list(row) + [None] * (3 - len(row))
                    for col in range(3):
                        val = (cells[col] or '').strip()
                        if not val:
                            continue
                        if val in SECTOR_NAMES:
                            current[col] = val
                        elif current[col]:
                            result['sector_companies'].append({'sector': current[col], 'company_name': val})

        # ---- Sector meeting-allowance breakdown (Figures 14 & 15) ----
        sector_allow_page = _find_page_with(pages_text, 'by Sector')
        if sector_allow_page is not None:
            text = pages_text[sector_allow_page]
            # Data labels appear as "<value>\n<Sector label...>" pairs, e.g.
            # "131,331 \nBanking \nOthers \n..." then "Other NEDs" caption -
            # match (number, sector-name) pairs against the known sector set.
            for role_caption, role_key in [('Board chair', 'chairperson'), ('Other NEDs', 'other_neds')]:
                pass  # figures for this section are chart-label pairs without a clean
                      # extractable table in this layout; left for a future pass rather
                      # than guessed at from noisy chart-label text.

        # ---- Benefits table (Table 8) ----
        benefits_page = _find_page_with(pages_text, 'Table 8: Other benefits') or _find_page_with(pages_text, 'Medical cover')
        if benefits_page is not None:
            for t in pages[benefits_page].extract_tables():
                for row in t:
                    if not row or not row[0]:
                        continue
                    label = row[0].strip()
                    note = (row[1] or '').replace('\n', ' ').strip() if len(row) > 1 else ''
                    if label not in BENEFIT_LABELS:
                        continue
                    pct_match = re.search(r'([\d.]+)%', note)
                    result['benefits'].append({
                        'benefit_name': label,
                        'prevalence_pct': _num(pct_match.group(1)) if pct_match else None,
                        'notes': note,
                    })

        # ---- CEO/MD compensation table (Table 10) ----
        ceo_page = _find_page_with(pages_text, 'CEO/MD')
        if ceo_page is not None:
            for t in pages[ceo_page].extract_tables():
                for row in t:
                    if not row or not row[0]:
                        continue
                    label = row[0].replace('\n', ' ').strip()
                    matched = next((c for c in CEO_COMPONENT_LABELS if c.lower() in label.lower()), None)
                    if not matched or len(row) < 5:
                        continue
                    nums = [_num(c) for c in row[1:5]]
                    result['ceo_comp'].append({
                        'component_name': matched,
                        'p25': nums[0], 'p50': nums[1], 'p75': nums[2], 'average': nums[3],
                    })

        return result


def parse_survey_pdf_file(path: str) -> dict:
    with open(path, 'rb') as fh:
        return parse_survey_pdf(fh.read())
