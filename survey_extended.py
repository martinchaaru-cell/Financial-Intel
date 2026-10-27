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


def company_fields(r, survey_cur='KES'):
    """Per-company extended figures for the Intelligence Report (same conversions as build())."""
    v = lambda f: value(r, f, survey_cur)
    staff, emp, tov = v('staff_costs'), v('employees_count'), turnover_full(r, survey_cur)
    eps, dps = v('earnings_per_share'), v('dividend_per_share')
    out = {f: v(f) for f in ('audit_fee', 'staff_costs', 'employees_count', 'earnings_per_share', 'dividend_per_share',
                             'board_attendance_pct', 'women_in_workforce_pct', 'women_in_senior_management_pct',
                             'total_directors_fees', 'top10_shareholding_pct', 'shareholders_count')}
    out.update({
        'kmp_total': kmp_total(r, survey_cur),
        'staff_cost_per_employee': staff / emp if (staff is not None and emp) else None,
        'audit_fee_pct_of_turnover': (out['audit_fee'] / tov * 100) if (out['audit_fee'] is not None and tov) else None,
        'payout_ratio_pct': (dps / eps * 100) if (dps is not None and eps and eps > 0) else None,
        'auditor_name': r.auditor_name, 'audit_opinion': r.audit_opinion,
        'going_concern_uncertainty': r.going_concern_uncertainty, 'governance_audit_done': r.governance_audit_done,
        'chair_ceo_separate': r.chair_ceo_separate, 'chair_independent': r.chair_independent,
        'lead_independent_director': r.lead_independent_director,
    })
    return out
