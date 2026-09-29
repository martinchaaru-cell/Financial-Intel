"""What an uploaded PDF WOULD save, computed with the same readers the import uses but WITHOUT
writing anything. Shown on Settings > Review > Uploads so an admin approves knowing the company it
will land on, the period, the key figures, the identity checks and what the board / pay / policy
readers found. No database access here; the app adds the company match."""
from collections import Counter

from pdf_parse import (extract_pdf_document, detect_company_name, detect_period_label, detect_interim_period_label,
                       detect_prior_period_label, detect_statement_unit, looks_like_interim_report,
                       extract_director_remuneration_detail, _pypdf_page_texts_cached)

_KEY = ['revenue', 'profit_before_tax', 'tax_expense', 'net_income', 'total_assets', 'total_liabilities', 'total_equity']


def _close(a, b):
    return abs(a - b) <= max(2.0, 0.002 * max(abs(a), abs(b)))


def build_preview(pdf_bytes: bytes, filename: str) -> dict:
    from board_extract import extract_board_bundle
    from policy_extract import extract_ned_policy, extract_ceo_pay, extract_ned_benefits, extract_committee_prose
    extracted = extract_pdf_document(pdf_bytes)
    pages_text, statements = extracted['pages_text'], extracted.get('statements') or {}
    survey_only = not statements
    detect_pages = pages_text if (pages_text and not survey_only) else (_pypdf_page_texts_cached(pdf_bytes) or [])
    interim = detect_interim_period_label(detect_pages)
    period = interim or detect_period_label(detect_pages, filename=filename)
    detected = detect_company_name(detect_pages, filename=filename)
    unit = detect_statement_unit(pages_text)
    warnings, checks = [], []

    key, per_stmt = {}, {}
    for st, d in statements.items():
        items = d.get('line_items') or []
        per_stmt[st] = {'items': len(items),
                        'avg_confidence': round(sum(i.get('confidence', 0) for i in items) / len(items), 2) if items else None}
        for it in items:
            nm = it.get('normalized_name')
            if nm in _KEY and nm not in key and it.get('amount') is not None:
                key[nm] = it['amount']
    pbt, tax, ni = key.get('profit_before_tax'), key.get('tax_expense'), key.get('net_income')
    if None not in (pbt, tax, ni):
        checks.append({'check': 'profit_before_tax - tax = net income', 'ok': _close(pbt - tax, ni) or _close(pbt + tax, ni),
                       'detail': f'{pbt:,.0f} - {tax:,.0f} vs {ni:,.0f}'})
    ta, tl, te = key.get('total_assets'), key.get('total_liabilities'), key.get('total_equity')
    if None not in (ta, tl, te):
        checks.append({'check': 'assets = liabilities + equity', 'ok': _close(ta, tl + te), 'detail': f'{ta:,.0f} vs {tl + te:,.0f}'})
    if survey_only:
        warnings.append('No financial statements found in this file - only board / pay / governance content will be saved.')
    if interim:
        warnings.append(f'Interim report - saved as its own period ({interim}), not a fiscal year.')
    if not period:
        warnings.append('No fiscal year could be detected - the import will fail unless the period is given separately.')
    if not detected:
        warnings.append('No company name could be detected.')
    if statements and unit is None:
        warnings.append("The statements' unit (thousands / millions) was not stated - Turnover and Net Profit will stay empty.")

    bundle = extract_board_bundle(pdf_bytes)
    reg = bundle['register']
    roles = Counter(d.get('role') for d in reg)
    board = {'register_kind': bundle['register_kind'], 'directors': len(reg),
             'roles_known': sum(v for k, v in roles.items() if k in ('executive', 'non_executive')),
             'sample': [{'name': d['director_name'], 'role': d.get('role')} for d in reg[:14]],
             'facts': {k: v.get('value', v) if isinstance(v, dict) else v for k, v in (bundle['facts'] or {}).items()
                       if k in ('board_meetings', 'market_cap', 'composition')},
             'attendance_committees': [{'name': c['name'], 'meetings': c['meetings'], 'members': len(c['members'])}
                                       for c in ((bundle['attendance'] or {}).get('committees') or [])]}
    fy = None
    if period:
        import re
        m = re.search(r"((?:19|20)\d\d)", period)
        fy = int(m.group(1)) if m else None
    pay_rows = extract_director_remuneration_detail(pdf_bytes, period) if period else []
    persons = [r for r in pay_rows if not r.get('is_grand_total') and not r.get('is_total_row')]
    totals = [r['total'] for r in pay_rows if r.get('is_grand_total') and r.get('total') is not None]
    pay = {'rows': len(persons), 'grand_totals': totals[:3]}
    sums = [r['total'] for r in persons if r.get('total') is not None]
    if totals and sums:
        ok = any(abs(sum(sums) - t) <= max(1.0, len(sums)) for t in totals)
        checks.append({'check': 'director pay rows add up to the printed total', 'ok': ok,
                       'detail': f'rows {sum(sums):,.0f} vs printed {", ".join(f"{t:,.0f}" for t in totals[:2])}'})
    ceo, pol, ben, com = extract_ceo_pay(pdf_bytes), extract_ned_policy(pdf_bytes), extract_ned_benefits(pdf_bytes), extract_committee_prose(pdf_bytes)
    return {
        'filename': filename, 'pages': extracted.get('num_pages'), 'survey_only': survey_only,
        'company': {'detected': detected}, 'period': period, 'prior_period': None if (survey_only or not period) else detect_prior_period_label(period),
        'unit': unit, 'statements': per_stmt, 'key_figures': key, 'board': board, 'pay': pay,
        'ceo': {'name': ceo['name'], 'cost_of_employment_annual': ceo['components'].get('cost_of_employment'), 'unit': ceo['unit']} if ceo else None,
        'policy_fields': sorted(pol), 'benefits': {k: v['provided'] for k, v in ben.items()},
        'prose_committees': [c['name'] for c in com], 'checks': checks, 'warnings': warnings,
    }
