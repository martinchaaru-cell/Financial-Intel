"""Portfolio and comparison Excel exports, built from the survey figures (one row per company per year).

Why the survey and not the parsed financial statements: the survey rows carry each company's own unit and currency,
so every figure can be written in full and in one currency (KES, with RWF / UGX converted at the annual-average rate
in fx_rates.py). The parsed statements keep figures as printed with no unit, which put Rwandan francs in thousands
next to Kenyan shillings in millions. Return on equity and debt/equity come from the parsed statements because they
are ratios (the unit cancels out); they are only shown when the statement has a real profit and positive equity.
A blank cell always means "not stated", never zero."""
from io import BytesIO

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import fx_rates
import survey_aggregate

HEAD_FILL = PatternFill('solid', fgColor='0F2B5B')
HEAD_FONT = Font(bold=True, color='FFFFFF')
MONEY = '#,##0'
RATIO = '#,##0.00'
WHOLE = '#,##0'


def _ratios(fin_by_period, fy):
    f = fin_by_period.get(fy)
    if f is None:
        return None, None
    roe = de = None
    if f.net_income and f.total_equity and f.total_equity > 0:
        v = f.net_income / f.total_equity * 100
        roe = round(v, 2) if abs(v) <= 100 else None
    if f.total_liabilities and f.total_equity and f.total_equity > 0:
        v = f.total_liabilities / f.total_equity
        de = round(v, 2) if 0 <= v <= 50 else None
    return roe, de


def company_years(Company, Financials):
    """{company_id: {'company': Company, 'years': {fy: row}}} for every company with survey data."""
    out = {}
    fin = {}
    for f in Financials.query.all():
        fin.setdefault(f.company_id, {})[f.period] = f
    for fy in sorted(survey_aggregate.available_fiscal_years(), key=survey_aggregate._fy_sort_key):
        data = survey_aggregate.build_company_rows(fy)
        for r in data.get('rows', []):
            roe, de = _ratios(fin.get(r['company_id'], {}), fy)
            r = dict(r, roe=roe, debt_equity=de)
            entry = out.setdefault(r['company_id'], {'company': None, 'years': {}})
            entry['years'][fy] = r
    for cid, entry in out.items():
        entry['company'] = Company.query.session.get(Company, cid)
    return {k: v for k, v in out.items() if v['company'] is not None}


def _sheet(ws, header, rows, formats, widths):
    ws.append(header)
    for c in ws[1]:
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(wrap_text=True, vertical='center')
    for r in rows:
        ws.append(r)
    for i, (fmt, w) in enumerate(zip(formats, widths), start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
        if fmt:
            for row in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                row[0].number_format = fmt
    ws.freeze_panes = 'B2'


def _r(v, dp=2):
    return None if v is None else round(v, dp)


def _notes(wb, cur):
    ws = wb.create_sheet('Notes')
    for line in [
        f'All amounts are written in full, in {cur}.',
        'Companies reporting in another currency (BK Group in RWF, Umeme in UGX) are converted at the annual-average '
        f'exchange rate for the year ({fx_rates.SOURCE}); the reporting currency is shown in its own column.',
        'Turnover, net profit, market cap, EPS, dividends, staff costs and employees are the figures stated in each '
        "company's annual report for that year (the survey data on Companies Survey Reports).",
        'Return on equity and debt/equity come from the parsed financial statements, shown only where the statement has '
        'a profit figure and positive equity.',
        'A blank cell means the report does not state the figure (or it was not credible for a listed company). It is never zero.',
    ]:
        ws.append([line])
    ws.column_dimensions['A'].width = 140


def build_portfolio(Company, Financials):
    data = company_years(Company, Financials)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Portfolio'
    cur = 'KES'
    rows = []
    for cid, e in sorted(data.items(), key=lambda kv: kv[1]['company'].name.lower()):
        c = e['company']
        fy = max(e['years'], key=survey_aggregate._fy_sort_key)
        r = e['years'][fy]
        cur = r.get('currency') or cur
        rows.append([c.name, c.ticker, c.exchange, r.get('sector') or c.sector, c.country, fy, r.get('reported_currency'),
                     _r(r.get('turnover'), 0), _r(r.get('net_profit'), 0), _r(r.get('profit_margin')), _r(r.get('market_cap'), 0),
                     r.get('roe'), r.get('debt_equity'), _r(r.get('earnings_per_share')), _r(r.get('dividend_per_share')),
                     _r(r.get('employees_count'), 0)])
    _sheet(ws, ['Company', 'Ticker', 'Exchange', 'Sector', 'Country', 'Latest year', 'Reporting currency',
                f'Turnover ({cur})', f'Net profit ({cur})', 'Net margin %', f'Market cap ({cur})', 'ROE %', 'Debt / equity',
                f'EPS ({cur})', f'Dividend per share ({cur})', 'Employees'], rows,
           [None, None, None, None, None, None, None, MONEY, MONEY, RATIO, MONEY, RATIO, RATIO, RATIO, RATIO, WHOLE],
           [34, 9, 9, 24, 10, 11, 11, 20, 20, 12, 22, 9, 11, 10, 14, 11])
    _notes(wb, cur)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_comparison(Company, Financials):
    data = company_years(Company, Financials)
    wb = openpyxl.Workbook()
    summary = wb.active
    summary.title = 'Summary'
    cur = 'KES'
    srows = []
    ordered = sorted(data.items(), key=lambda kv: kv[1]['company'].name.lower())
    for cid, e in ordered:
        c = e['company']
        fy = max(e['years'], key=survey_aggregate._fy_sort_key)
        r = e['years'][fy]
        cur = r.get('currency') or cur
        srows.append([c.name, c.ticker, r.get('sector') or c.sector, fy, _r(r.get('turnover'), 0), _r(r.get('net_profit'), 0),
                      _r(r.get('profit_margin')), r.get('roe'), r.get('debt_equity')])
    srows.sort(key=lambda x: (x[6] is None, -(x[6] or 0)))
    _sheet(summary, ['Company', 'Ticker', 'Sector', 'Latest year', f'Turnover ({cur})', f'Net profit ({cur})', 'Net margin %',
                     'ROE %', 'Debt / equity'], srows,
           [None, None, None, None, MONEY, MONEY, RATIO, RATIO, RATIO], [34, 9, 24, 11, 20, 20, 12, 9, 11])
    used = set()
    for cid, e in ordered:
        c = e['company']
        name = ''.join(ch for ch in c.name if ch.isalnum() or ch in ' _-').strip()[:28] or f'Company {cid}'
        base, i = name, 2
        while name.lower() in used:
            name = f'{base[:25]} {i}'
            i += 1
        used.add(name.lower())
        ws = wb.create_sheet(name)
        yrs = sorted(e['years'], key=survey_aggregate._fy_sort_key, reverse=True)
        rows = [[fy, r.get('reported_currency'), _r(r.get('turnover'), 0), _r(r.get('net_profit'), 0), _r(r.get('profit_margin')),
                 _r(r.get('market_cap'), 0), r.get('roe'), r.get('debt_equity'), _r(r.get('earnings_per_share')),
                 _r(r.get('dividend_per_share')), _r(r.get('staff_costs'), 0), _r(r.get('employees_count'), 0)]
                for fy, r in ((fy, e['years'][fy]) for fy in yrs)]
        _sheet(ws, ['Year', 'Reporting currency', f'Turnover ({cur})', f'Net profit ({cur})', 'Net margin %', f'Market cap ({cur})',
                    'ROE %', 'Debt / equity', f'EPS ({cur})', f'Dividend per share ({cur})', f'Staff costs ({cur})', 'Employees'], rows,
               [None, None, MONEY, MONEY, RATIO, MONEY, RATIO, RATIO, RATIO, RATIO, MONEY, WHOLE],
               [10, 11, 20, 20, 12, 22, 9, 11, 10, 14, 20, 11])
    _notes(wb, cur)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
