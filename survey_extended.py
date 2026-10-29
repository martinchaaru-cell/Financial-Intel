"""Cross-company figures for the extended survey fields (audit, board effectiveness, executive pay, workforce,
shareholders) - shared by the Survey Analytics page, the Intelligence Report and the PDF reports, so all three
show the same numbers.

Same rules as survey_aggregate.py: a figure is only used when the company states it (never 0 by default), money is
brought to whole units of the survey currency (each company's own unit, then the annual-average exchange rate for
companies reporting in RWF / UGX), and an average needs at least MIN_COMPANIES_FOR_AVERAGE companies."""
import fx_rates
import tab_modules
from models_survey import SurveyDirector

MIN_COMPANIES = 2
UNIT_SCALE = {'units': 1, 'ones': 1, '': 1, 'thousands': 1_000, 'millions': 1_000_000, 'billions': 1_000_000_000}

AUDIT_FIELDS = ['audit_fee', 'non_audit_fees', 'key_audit_matters_count', 'auditor_tenure_years']
BOARD_FIELDS = ['board_attendance_pct', 'independent_neds_over_9_years']
PAY_FIELDS = ['kmp_short_term_benefits', 'kmp_post_employment_benefits', 'kmp_share_based_payments',
              'kmp_termination_benefits', 'kmp_total_compensation', 'total_directors_fees',
              'total_executive_directors_emoluments', 'cfo_annual_total_pay', 'loans_to_directors', 'kmp_count']
WORKFORCE_FIELDS = ['employees_count', 'staff_costs', 'women_in_workforce_pct', 'women_in_senior_management_pct',
                    'staff_turnover_pct']
SHAREHOLDER_FIELDS = ['earnings_per_share', 'dividend_per_share', 'shareholders_count', 'top10_shareholding_pct',
                      'free_float_pct', 'directors_shareholding_pct']
YESNO = ['chair_ceo_separate', 'chair_independent', 'lead_independent_director', 'governance_audit_done',
         'going_concern_uncertainty']
KMP_PARTS = ['kmp_short_term_benefits', 'kmp_post_employment_benefits', 'kmp_share_based_payments',
             'kmp_termination_benefits']


def _scale(unit, default=None):
    u = (unit or '').strip().lower()
    if len(u) == 3 and u.isalpha() and u not in UNIT_SCALE:      # unit saved as the currency code = whole units
        return 1
    return UNIT_SCALE.get(u, default)


def _fx(r, survey_cur):
    return fx_rates.rate_to(survey_cur, (r.currency or 'KES').strip().upper(), r.fiscal_year)


def value(r, field, survey_cur='KES'):
    """One company's figure for `field`, comparable across companies: money in whole units of the survey currency,
    counts and percentages as stated. None when not stated (or no exchange rate / unknown unit)."""
    raw = getattr(r, field, None)
    if raw is None or raw == '':
        return None
    kind = tab_modules.field_kind(field)
    if kind in ('text', 'yesno'):
        return raw
    if kind in ('headline', 'pay', 'pershare'):
        fx = _fx(r, survey_cur)
        if fx is None:
            return None
        if kind == 'headline':
            s = _scale(r.unit)
        elif kind == 'pay':
            s = _scale(r.director_figures_unit, 1)
        else:
            s = 1
        return None if s is None else float(raw) * s * fx
    return float(raw)


def kmp_total(r, survey_cur='KES'):
    """Total key-management pay: the stated total, else the sum of the stated parts (salaries must be among them)."""
    t = value(r, 'kmp_total_compensation', survey_cur)
    if t is not None:
        return t
    parts = [value(r, f, survey_cur) for f in KMP_PARTS]
    return sum(p for p in parts if p is not None) if parts[0] is not None else None


def turnover_full(r, survey_cur='KES'):
    s, fx = _scale(r.unit), _fx(r, survey_cur)
    return None if (r.turnover is None or s is None or fx is None) else r.turnover * s * fx


def _pct(vals, p):
    vals = sorted(vals)
    if len(vals) < MIN_COMPANIES:
        return None
    k = (len(vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    return round(vals[f] + (vals[c] - vals[f]) * (k - f), 2)


def stat(pairs, top=5, low_is_good=False):
    """pairs = [(company name, value or None)] -> {average, p25, p50, p75, min, max, company_count, top:[...]}."""
    pairs = [(n, v) for n, v in pairs if v is not None]
    vals = [v for _, v in pairs]
    enough = len(vals) >= MIN_COMPANIES
    ranked = sorted(pairs, key=lambda p: p[1], reverse=not low_is_good)
    return {
        'average': round(sum(vals) / len(vals), 2) if enough else None,
        'p25': _pct(vals, 25), 'p50': _pct(vals, 50), 'p75': _pct(vals, 75),
        'min': round(min(vals), 2) if enough else None, 'max': round(max(vals), 2) if enough else None,
        'company_count': len(vals),
        'top': [{'name': n, 'value': round(v, 2)} for n, v in ranked[:top]],
    }


def yesno(pairs):
    ans = [(n, str(v).strip().lower()) for n, v in pairs if v not in (None, '')]
    yes = [n for n, v in ans if v.startswith('y')]
    no = [n for n, v in ans if v.startswith('n')]
    answered = len(yes) + len(no)
    return {'yes': len(yes), 'no': len(no), 'answered': answered,
            'pct_yes': round(100 * len(yes) / answered, 1) if answered else None,
            'yes_companies': sorted(yes), 'no_companies': sorted(no)}


def shares(pairs):
    """[(company, text)] -> [{label, companies, pct}] most common first (same firm written two ways counts once)."""
    counts, names = {}, {}
    for n, v in pairs:
        if not v:
            continue
        key = ' '.join(str(v).replace('&', 'and').lower().replace('llp', '').replace('.', '').split())
        names.setdefault(key, str(v).strip())
        counts.setdefault(key, []).append(n)
    total = sum(len(c) for c in counts.values())
    out = [{'label': names[k], 'companies': len(c), 'company_names': sorted(c),
            'pct': round(100 * len(c) / total, 1) if total else None} for k, c in counts.items()]
    return sorted(out, key=lambda d: (-d['companies'], d['label']))


def build(rows, name_of, survey_cur='KES'):
    """rows: SurveyCompanyData rows of ONE fiscal year; name_of: company_id -> name."""
    def col(field):
        return [(name_of(r.company_id), value(r, field, survey_cur)) for r in rows]

    def paired(num_fn, den_fn, mult=1.0, cond=None):
        out = []
        for r in rows:
            a, b = num_fn(r), den_fn(r)
            if a is not None and b not in (None, 0) and (cond is None or cond(a, b)):
                out.append((name_of(r.company_id), a / b * mult))
        return out

    v = lambda f: (lambda r: value(r, f, survey_cur))
    audit = {f: stat(col(f)) for f in AUDIT_FIELDS}
    audit['audit_fee_pct_of_turnover'] = stat(paired(v('audit_fee'), lambda r: turnover_full(r, survey_cur), 100))
    audit['auditor_share'] = shares([(name_of(r.company_id), r.auditor_name) for r in rows])
    ops = {}
    for r in rows:
        if r.audit_opinion:
            ops.setdefault(str(r.audit_opinion).strip().capitalize(), []).append(name_of(r.company_id))
    audit['opinions'] = {k: {'companies': len(c), 'company_names': sorted(c)} for k, c in sorted(ops.items())}
    audit['opinions_answered'] = sum(len(c) for c in ops.values())
    audit['going_concern'] = yesno([(name_of(r.company_id), r.going_concern_uncertainty) for r in rows])

    board = {f: stat(col(f)) for f in BOARD_FIELDS}
    for f in ('chair_ceo_separate', 'chair_independent', 'lead_independent_director', 'governance_audit_done'):
        board[f] = yesno([(name_of(r.company_id), getattr(r, f, None)) for r in rows])
    board['governance_auditors'] = shares([(name_of(r.company_id), r.governance_audit_by) for r in rows])
    board['director_attendance'] = _director_attendance(rows, name_of)

    pay = {f: stat(col(f)) for f in PAY_FIELDS}
    pay['kmp_total'] = stat([(name_of(r.company_id), kmp_total(r, survey_cur)) for r in rows])

    work = {f: stat(col(f)) for f in WORKFORCE_FIELDS}
    work['staff_cost_per_employee'] = stat([(n_, round(x)) for n_, x in paired(v('staff_costs'), v('employees_count'))])
    work['staff_costs_pct_of_turnover'] = stat(paired(v('staff_costs'), lambda r: turnover_full(r, survey_cur), 100))

    holders = {f: stat(col(f)) for f in SHAREHOLDER_FIELDS}
    holders['payout_ratio_pct'] = stat(paired(v('dividend_per_share'), v('earnings_per_share'), 100,
                                              cond=lambda a, b: b > 0))

    fields = AUDIT_FIELDS + BOARD_FIELDS + PAY_FIELDS + WORKFORCE_FIELDS + SHAREHOLDER_FIELDS + YESNO + ['auditor_name', 'audit_opinion']
    with_any = sum(1 for r in rows if any(getattr(r, f, None) not in (None, '') for f in fields))
    return {'currency': survey_cur, 'companies_with_any': with_any, 'audit': audit, 'board_effectiveness': board,
            'executive_pay': pay, 'workforce': work, 'shareholders': holders}


def _director_attendance(rows, name_of):
    """Per-director board attendance (meetings attended / meetings held while in office) for the year."""
    if not rows:
        return {'directors': 0}
    fy = rows[0].fiscal_year
    ids = {r.company_id for r in rows}
    ds = [d for d in SurveyDirector.query.filter(SurveyDirector.fiscal_year == fy,
                                                SurveyDirector.company_id.in_(ids)).all()
          if d.board_meetings_attended is not None and d.board_meetings_eligible]
    if not ds:
        return {'directors': 0}
    pcts = [min(100.0, 100 * d.board_meetings_attended / d.board_meetings_eligible) for d in ds]
    return {'directors': len(ds), 'companies': len({d.company_id for d in ds}),
            'average_pct': round(sum(pcts) / len(pcts), 1),
            'full_attendance': sum(1 for p in pcts if p >= 100),
            'below_75': sum(1 for p in pcts if p < 75)}


TEXT_FIELDS = ['auditor_name', 'audit_opinion', 'governance_audit_by']


def company_fields(r, survey_cur='KES'):
    """Every extended figure for one company-year, converted like build() (money in whole survey-currency units),
    plus the worked-out measures. Used by the Intelligence Report, the company PDF report and the Excel downloads."""
    v = lambda f: value(r, f, survey_cur)
    out = {f: v(f) for f in AUDIT_FIELDS + BOARD_FIELDS + PAY_FIELDS + WORKFORCE_FIELDS + SHAREHOLDER_FIELDS}
    out.update({f: (getattr(r, f, None) or None) for f in YESNO + TEXT_FIELDS})
    staff, emp, tov = out['staff_costs'], out['employees_count'], turnover_full(r, survey_cur)
    eps, dps = out['earnings_per_share'], out['dividend_per_share']
    out.update({
        'kmp_total': kmp_total(r, survey_cur),
        'staff_cost_per_employee': staff / emp if (staff is not None and emp) else None,
        'staff_costs_pct_of_turnover': (staff / tov * 100) if (staff is not None and tov) else None,
        'audit_fee_pct_of_turnover': (out['audit_fee'] / tov * 100) if (out['audit_fee'] is not None and tov) else None,
        'payout_ratio_pct': (dps / eps * 100) if (dps is not None and eps and eps > 0) else None,
    })
    return out


# ---------------------------------------------------------------- one catalogue for every export
# (key, label, kind). Kinds: money / pershare / pct / pct2 / count / num1 / yesno / text, and for the market view
# share (which firms, how many companies), opinions and attendance. Exports show a row only when it has data.
CATALOGUE = [
    ('audit', 'Audit', [
        ('auditor_name', 'External auditor', 'text', 'auditor_share', 'share'),
        ('audit_fee', 'Audit fee', 'money', None, None),
        ('audit_fee_pct_of_turnover', 'Audit fee as % of turnover', 'pct2', None, None),
        ('non_audit_fees', 'Non-audit fees paid to the auditor', 'money', None, None),
        ('auditor_tenure_years', 'Years the auditor has been in office', 'num1', None, None),
        ('key_audit_matters_count', 'Key audit matters in the report', 'num1', None, None),
        ('audit_opinion', 'Audit opinion', 'text', 'opinions', 'opinions'),
        ('going_concern_uncertainty', 'Material uncertainty on going concern', 'yesno', 'going_concern', 'yesno'),
    ]),
    ('board_effectiveness', 'Board effectiveness', [
        ('board_attendance_pct', 'Average board meeting attendance', 'pct', None, None),
        (None, 'Directors with attendance on file', None, 'director_attendance', 'attendance'),
        ('independent_neds_over_9_years', 'Independent NEDs serving more than 9 years', 'num1', None, None),
        ('chair_ceo_separate', 'Chair and CEO roles held by different people', 'yesno', None, None),
        ('chair_independent', 'Chairperson is independent', 'yesno', None, None),
        ('lead_independent_director', 'Lead / senior independent director appointed', 'yesno', None, None),
        ('governance_audit_done', 'Governance audit carried out', 'yesno', None, None),
        ('governance_audit_by', 'Governance auditor', 'text', 'governance_auditors', 'share'),
    ]),
    ('executive_pay', 'Executive pay (key management)', [
        ('kmp_count', 'Key management personnel (people)', 'count', None, None),
        ('kmp_total', 'Key management: total pay', 'money', None, None),
        ('kmp_short_term_benefits', 'Key management: salaries and short-term benefits', 'money', None, None),
        ('kmp_post_employment_benefits', 'Key management: pension / post-employment benefits', 'money', None, None),
        ('kmp_share_based_payments', 'Key management: share-based payments', 'money', None, None),
        ('kmp_termination_benefits', 'Key management: termination benefits', 'money', None, None),
        ('total_directors_fees', "Total non-executive directors' fees", 'money', None, None),
        ('total_executive_directors_emoluments', "Total executive directors' emoluments", 'money', None, None),
        ('cfo_annual_total_pay', 'CFO / finance director total pay for the year', 'money', None, None),
        ('loans_to_directors', 'Loans to directors (balance at year end)', 'money', None, None),
    ]),
    ('workforce', 'Workforce', [
        ('employees_count', 'Employees', 'count', None, None),
        ('staff_costs', 'Staff costs', 'money', None, None),
        ('staff_cost_per_employee', 'Staff cost per employee', 'money', None, None),
        ('staff_costs_pct_of_turnover', 'Staff costs as % of turnover', 'pct', None, None),
        ('women_in_workforce_pct', 'Women in the workforce', 'pct', None, None),
        ('women_in_senior_management_pct', 'Women in senior management', 'pct', None, None),
        ('staff_turnover_pct', 'Staff turnover', 'pct', None, None),
    ]),
    ('shareholders', 'Shareholders and returns', [
        ('earnings_per_share', 'Earnings per share', 'pershare', None, None),
        ('dividend_per_share', 'Dividend per share', 'pershare', None, None),
        ('payout_ratio_pct', 'Dividend payout (dividend / earnings)', 'pct', None, None),
        ('shareholders_count', 'Shareholders', 'count', None, None),
        ('top10_shareholding_pct', 'Top 10 shareholders, combined holding', 'pct', None, None),
        ('free_float_pct', 'Free float', 'pct', None, None),
        ('directors_shareholding_pct', 'Directors, combined holding', 'pct', None, None),
    ]),
]


def fmt(kind, v, cur='KES'):
    """A single company figure as text ('' when missing)."""
    if v is None or v == '':
        return ''
    if kind == 'money':
        return f'{cur} {round(v):,}'
    if kind == 'pershare':
        return f'{cur} {v:,.2f}'
    if kind == 'pct':
        return f'{v:,.1f}%'
    if kind == 'pct2':
        return f'{v:,.2f}%'
    if kind == 'count':
        return f'{round(v):,}'
    if kind == 'num1':
        return f'{int(v):,}' if float(v).is_integer() else f'{v:,.1f}'
    return str(v)


def market_rows(ext, cur='KES'):
    """[(group label, [(label, headline, detail, companies)])] for the market reports - only rows with data, only
    groups with at least one row. headline = median (or the one company's figure); detail = the middle half, or
    who / how many."""
    out = []
    for gkey, glabel, items in CATALOGUE:
        g = ext.get(gkey) or {}
        rows = []
        for key, label, kind, mkey, mkind in items:
            obj = g.get(mkey or key)
            if mkind == 'share' or (mkind is None and kind == 'text'):
                obj = g.get(mkey) if mkey else None
                if obj:
                    total = sum(x['companies'] for x in obj)
                    rows.append((label, f"{obj[0]['label']} ({obj[0]['companies']})",
                                 ' · '.join(f"{x['label']} {x['companies']}" for x in obj[1:4]), total))
            elif mkind == 'opinions':
                if obj:
                    answered = sum(x['companies'] for x in obj.values())
                    clean = (obj.get('Unmodified') or {}).get('companies', 0)
                    other = '; '.join(f"{k}: {', '.join(x['company_names'])}" for k, x in obj.items() if k != 'Unmodified')
                    rows.append((label, f'Unmodified at {clean} of {answered}', other, answered))
            elif mkind == 'attendance':
                if obj and obj.get('directors'):
                    rows.append((label, f"{obj['average_pct']:.1f}% average",
                                 f"{obj['directors']} directors at {obj['companies']} compan{'y' if obj['companies'] == 1 else 'ies'} · {obj['full_attendance']} at every meeting · {obj['below_75']} below 75%",
                                 obj['companies']))
            elif kind == 'yesno' or mkind == 'yesno':
                if obj and obj.get('answered'):
                    rows.append((label, f"Yes at {obj['yes']} of {obj['answered']}", f"{obj['pct_yes']:.0f}% of the companies that state it", obj['answered']))
            elif obj and obj.get('company_count'):
                n = obj['company_count']
                if n >= MIN_COMPANIES and obj.get('p50') is not None:
                    rows.append((label, fmt(kind, obj['p50'], cur), f"{fmt(kind, obj['p25'], cur)} – {fmt(kind, obj['p75'], cur)}", n))
                elif obj.get('top'):
                    t = obj['top'][0]
                    rows.append((label, fmt(kind, t['value'], cur), f"only {t['name']} states it", n))
        if rows:
            out.append((glabel, rows))
    return out


def company_rows(me, rows, cur='KES'):
    """[(group label, [(label, this company's figure, market median / share, rank)])] for one company - only the
    figures this company states. rows = every company's company_fields() for the same year."""
    out = []
    for _gkey, glabel, items in CATALOGUE:
        lines = []
        for key, label, kind, _mkey, _mkind in items:
            if key is None:
                continue
            mine = me.get(key)
            if mine is None or mine == '':
                continue
            if kind in ('yesno', 'text'):
                pool = [str(r.get(key)).strip() for r in rows if r.get(key)]
                same = sum(1 for x in pool if x.lower() == str(mine).strip().lower())
                lines.append((label, str(mine), f'same at {same} of {len(pool)} companies' if len(pool) >= 2 else '', ''))
                continue
            pool = sorted([r.get(key) for r in rows if r.get(key) is not None], reverse=True)
            med = None
            if len(pool) >= MIN_COMPANIES:
                srt = sorted(pool)
                m = len(srt)
                med = srt[m // 2] if m % 2 else (srt[m // 2 - 1] + srt[m // 2]) / 2
            lines.append((label, fmt(kind, mine, cur), fmt(kind, med, cur) if med is not None else '',
                          f'{pool.index(mine) + 1} of {len(pool)}' if len(pool) >= MIN_COMPANIES else ''))
        if lines:
            out.append((glabel, lines))
    return out
