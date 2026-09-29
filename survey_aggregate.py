"""
Aggregation logic for the Survey feature - shared by the JSON overview
route (/api/survey/overview) and the PDF export route
(/api/survey/export/report.pdf), so the two never drift apart or
compute an average two different ways.

Every average here is computed only from companies that actually
supplied that specific field (never defaulted to 0), and any average
backed by fewer than MIN_COMPANIES_FOR_AVERAGE companies comes back as
None with its real company_count alongside it - callers show "not
enough data yet" instead of a misleading single-company figure dressed
up as a market average.
"""

from models import db, Company
from models_survey import SurveyCompanyData

MIN_COMPANIES_FOR_AVERAGE = 2  # below this, an "average" is just one company's own number wearing a label

# Canonical scale every company's PERFORMANCE figures (turnover, net_profit,
# market_cap) are normalized to before any cross-company average is computed.
# Board counts and ages are never affected by any Unit (they're plain counts).
# Remuneration/CEO/committee-fee figures ARE affected too, by a SEPARATE field
# (SurveyCompanyData.director_figures_unit, not this Unit) - see _pay_scale and
# the long comment above PAY_FIELDS further down for why and how those get
# normalized. Without either normalization, a company reporting in "billions"
# alongside one reporting in "millions" (Unit), or one whose pay tables are in
# "thousands" alongside one in raw currency (director_figures_unit), gets
# averaged as if the numbers were on the same scale.
UNIT_SCALE = {
    'units': 1, 'ones': 1, '': 1,
    'thousands': 1_000,
    'millions': 1_000_000,
    'billions': 1_000_000_000,
}
CANONICAL_UNIT = 'millions'


def _unit_scale(unit):
    """Multiplier to convert a company's stated Unit into raw currency
    units, or None if unrecognized - callers must treat None as 'cannot
    safely normalize', never guess a scale."""
    return UNIT_SCALE.get((unit or '').strip().lower())


# 2026-09-26 fix: remuneration/CEO/committee-fee figures are NOT always stored in raw
# currency the way this module originally assumed (see the UNIT_SCALE comment above,
# which explicitly said so) - app.py's import pipeline scales extract_ned_policy's and
# extract_ceo_pay's whole-currency output DOWN to match SurveyCompanyData.
# director_figures_unit, so that a company's own per-director filed pay table and its
# survey-level policy figures sit on the same scale next to each other on the Survey
# Report page (confirmed real: KCB Group Plc states turnover in billions but its
# director pay tables in thousands). Averaging those un-normalized across companies -
# which is exactly what every metric() call on a pay field below did - silently mixes
# scales whenever two companies have different director_figures_unit values. Same fix
# shape as _unit_scale/normalized_perf above, applied to the pay fields instead of the
# performance fields.
PAY_FIELDS = [
    'chairperson_annual_retainer', 'other_ned_annual_retainer',
    'chairperson_meeting_allowance', 'other_ned_meeting_allowance',
    'executive_director_annual_retainer', 'executive_director_meeting_allowance',
    'committee_chair_annual_retainer', 'committee_member_annual_retainer',
    'committee_chair_meeting_allowance', 'committee_member_meeting_allowance',
    'ceo_monthly_salary', 'ceo_monthly_allowances', 'ceo_monthly_incentive_bonus',
    'ceo_monthly_deferred_incentive', 'ceo_monthly_non_cash_benefits', 'ceo_monthly_pension',
    'ceo_monthly_gratuity', 'ceo_monthly_share_value', 'ceo_monthly_cost_of_employment',
]


def _pay_scale(r):
    """Multiplier to convert a SurveyCompanyData row's pay-scale fields (PAY_FIELDS)
    into raw currency units. Unlike _unit_scale, a missing director_figures_unit means
    'was stored as raw currency' (app.py's own to_pay_unit defaults the same way when
    nothing was detected), not 'unrecognized' - so this defaults to 1 rather than None."""
    return UNIT_SCALE.get((r.director_figures_unit or 'units').strip().lower(), 1)


def _avg(values):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _sum_or_none(values):
    vals = [v for v in values if v is not None]
    return round(sum(vals), 2) if vals else None


def _percentile(values, pct):
    """Linear-interpolation percentile (same convention as numpy's default
    / Excel's PERCENTILE.INC), gated by MIN_COMPANIES_FOR_AVERAGE same as
    every other stat here - a percentile from 1-2 companies isn't a market
    benchmark either."""
    vals = sorted(v for v in values if v is not None)
    if len(vals) < MIN_COMPANIES_FOR_AVERAGE:
        return None
    if len(vals) == 1:
        return round(vals[0], 2)
    k = (len(vals) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(vals) - 1)
    if f == c:
        return round(vals[f], 2)
    return round(vals[f] + (vals[c] - vals[f]) * (k - f), 2)


def _compa_ratio(higher_metric, lower_metric):
    """Ratio of two metric() dicts' averages (e.g. Chairperson vs Other
    NED). None if either side lacks enough companies for its own average -
    a ratio of a real average to a hidden one would be misleading."""
    if not higher_metric or not lower_metric:
        return None
    hi, lo = higher_metric.get('average'), lower_metric.get('average')
    if hi is None or not lo:
        return None
    return round(hi / lo, 2)


def available_fiscal_years():
    return [r[0] for r in db.session.query(SurveyCompanyData.fiscal_year).distinct().all()]


def default_fiscal_year():
    """Whichever fiscal_year has the most companies on file - used when
    the caller doesn't specify one."""
    counts = db.session.query(
        SurveyCompanyData.fiscal_year, db.func.count(SurveyCompanyData.id)
    ).group_by(SurveyCompanyData.fiscal_year).all()
    if not counts:
        return None
    return sorted(counts, key=lambda c: -c[1])[0][0]


def build_survey_overview(fiscal_year=None):
    """Returns the full aggregated payload for one fiscal year, or
    {'has_data': False, ...} if there's nothing on file for it (or at
    all, if none was ever uploaded)."""
    all_years = available_fiscal_years()

    if not fiscal_year:
        fiscal_year = default_fiscal_year()
        if not fiscal_year:
            return {'has_data': False, 'available_fiscal_years': []}

    row_pairs = db.session.query(SurveyCompanyData, Company).join(
        Company, SurveyCompanyData.company_id == Company.id
    ).filter(SurveyCompanyData.fiscal_year == fiscal_year).all()
    rows = [r for r, c in row_pairs]
    company_by_id = {c.id: c for r, c in row_pairs}

    if not rows:
        return {'has_data': False, 'fiscal_year': fiscal_year, 'available_fiscal_years': sorted(all_years, reverse=True)}

    # ---- Normalize PERFORMANCE figures to a common scale before averaging ----
    # Currency is NOT auto-converted (no exchange-rate source in this app) -
    # a fiscal year mixing currencies excludes the non-primary-currency
    # companies from PERFORMANCE figures and flags them, rather than
    # silently combining e.g. KES and UGX. Unit IS auto-converted (a pure
    # multiplier, unlike currency) - an unrecognized Unit string is
    # likewise excluded and flagged rather than guessed.
    currency_counts = {}
    for r in rows:
        cur = (r.currency or 'KES').strip().upper()
        currency_counts[cur] = currency_counts.get(cur, 0) + 1
    primary_currency = max(currency_counts, key=currency_counts.get)

    perf_scale = {}          # row.id -> multiplier to CANONICAL_UNIT
    excluded_companies = []  # surfaced, not silently dropped
    for r in rows:
        cur = (r.currency or 'KES').strip().upper()
        scale = _unit_scale(r.unit)
        name = company_by_id[r.company_id].name
        if cur != primary_currency:
            excluded_companies.append({'name': name, 'reason': f'currency {cur} (survey is in {primary_currency}, no FX conversion applied)'})
            perf_scale[r.id] = None
        elif scale is None:
            excluded_companies.append({'name': name, 'reason': f'unrecognized Unit "{r.unit}"'})
            perf_scale[r.id] = None
        else:
            perf_scale[r.id] = scale / UNIT_SCALE[CANONICAL_UNIT]

    def normalized_perf(r, field_name):
        raw = getattr(r, field_name)
        if raw is None or perf_scale.get(r.id) is None:
            return None
        return raw * perf_scale[r.id]

    def normalized_pay(r, field_name):
        raw = getattr(r, field_name)
        return None if raw is None else raw * _pay_scale(r)

    def field(name):
        return [getattr(r, name) for r in rows]

    def metric(name):
        vals = field(name)
        present = [v for v in vals if v is not None]
        enough = len(present) >= MIN_COMPANIES_FOR_AVERAGE
        return {
            'average': _avg(vals) if enough else None,
            'p25': _percentile(vals, 25) if enough else None,
            'p50': _percentile(vals, 50) if enough else None,
            'p75': _percentile(vals, 75) if enough else None,
            'company_count': len(present),
        }

    def pay_metric(name):
        """Same shape as metric(), but normalizes each row through
        _pay_scale first - use this instead of metric() for any field in
        PAY_FIELDS."""
        vals = [normalized_pay(r, name) for r in rows]
        present = [v for v in vals if v is not None]
        enough = len(present) >= MIN_COMPANIES_FOR_AVERAGE
        return {
            'average': _avg(vals) if enough else None,
            'p25': _percentile(vals, 25) if enough else None,
            'p50': _percentile(vals, 50) if enough else None,
            'p75': _percentile(vals, 75) if enough else None,
            'company_count': len(present),
        }

    def paired_ratio(hi_field, lo_field):
        """Average of hi_field / average of lo_field over ONLY the companies that report BOTH. The old
        ratio divided one field's average over its own companies by another field's average over a
        different set (e.g. chairperson retainer from 2 companies vs NED retainer from 6), so the premium
        reflected which companies happened to disclose what, not how much more a chair earns. Needs at
        least MIN_COMPANIES_FOR_AVERAGE paired companies, else None (never a single company's ratio
        dressed up as a market figure)."""
        pairs = [(normalized_pay(r, hi_field), normalized_pay(r, lo_field)) for r in rows]
        pairs = [(h, l) for h, l in pairs if h is not None and l]
        if len(pairs) < MIN_COMPANIES_FOR_AVERAGE:
            return None
        hi_avg = sum(h for h, _ in pairs) / len(pairs)
        lo_avg = sum(l for _, l in pairs) / len(pairs)
        return round(hi_avg / lo_avg, 2) if lo_avg else None

    # ---- Executive Summary ----
    turnover_vals = [normalized_perf(r, 'turnover') for r in rows if normalized_perf(r, 'turnover') is not None]
    net_profit_vals = [normalized_perf(r, 'net_profit') for r in rows if normalized_perf(r, 'net_profit') is not None]
    market_cap_vals = [normalized_perf(r, 'market_cap') for r in rows if normalized_perf(r, 'market_cap') is not None]
    margin_pairs = [(normalized_perf(r, 'net_profit'), normalized_perf(r, 'turnover')) for r in rows
                     if normalized_perf(r, 'net_profit') is not None and normalized_perf(r, 'turnover') not in (None, 0)]
    margin_vals = [(npf / tv * 100) for npf, tv in margin_pairs]

    def _metric_from_vals(vals):
        enough = len(vals) >= MIN_COMPANIES_FOR_AVERAGE
        return {
            'average': _avg(vals) if enough else None,
            'p25': _percentile(vals, 25) if enough else None,
            'p50': _percentile(vals, 50) if enough else None,
            'p75': _percentile(vals, 75) if enough else None,
            'company_count': len(vals),
        }

    executive_summary = {
        'companies_with_data': len(rows),
        'avg_turnover': _metric_from_vals(turnover_vals),
        'total_market_cap': _sum_or_none(market_cap_vals),
        'avg_net_profit': _metric_from_vals(net_profit_vals),
        'avg_profit_margin': _metric_from_vals(margin_vals),
        'avg_board_size': metric('board_size'),
        'avg_board_meetings_per_year': metric('board_meetings_per_year'),
        'avg_committees_per_board': metric('committees_per_board'),
        'avg_committee_meetings_per_year': metric('committee_meetings_per_year'),
        'total_directors_female': _sum_or_none(field('directors_female')),
        'total_directors_male': _sum_or_none(field('directors_male')),
    }

    # ---- Board Overview ----
    board_overview = {
        'avg_board_size': metric('board_size'),
        'avg_board_meetings_per_year': metric('board_meetings_per_year'),
        'avg_committees_per_board': metric('committees_per_board'),
        'avg_committee_meetings_per_year': metric('committee_meetings_per_year'),
        'total_directors_female': _sum_or_none(field('directors_female')),
        'total_directors_male': _sum_or_none(field('directors_male')),
        'total_executive_directors': _sum_or_none(field('executive_directors_count')),
        'total_non_executive_directors': _sum_or_none(field('non_executive_directors_count')),
        'total_independent_neds': _sum_or_none(field('independent_neds_count')),
        'total_non_independent_neds': _sum_or_none(field('non_independent_neds_count')),
        'total_neds_kenyan': _sum_or_none(field('neds_kenyan_count')),
        'total_neds_non_kenyan': _sum_or_none(field('neds_non_kenyan_count')),
        'avg_age_executive_directors': metric('avg_age_executive_directors'),
        'avg_age_non_executive_directors': metric('avg_age_non_executive_directors'),
        'avg_age_independent_neds': metric('avg_age_independent_neds'),
        'avg_age_non_independent_neds': metric('avg_age_non_independent_neds'),
    }

    # ---- Directors' Remuneration / NED / Executive Directors ----
    directors_remuneration = {
        'chairperson_annual_retainer': pay_metric('chairperson_annual_retainer'),
        'other_ned_annual_retainer': pay_metric('other_ned_annual_retainer'),
        'chairperson_meeting_allowance': pay_metric('chairperson_meeting_allowance'),
        'other_ned_meeting_allowance': pay_metric('other_ned_meeting_allowance'),
        'executive_director_annual_retainer': pay_metric('executive_director_annual_retainer'),
        'executive_director_meeting_allowance': pay_metric('executive_director_meeting_allowance'),
    }
    directors_remuneration['chairperson_vs_other_ned_annual_retainer_ratio'] = paired_ratio(
        'chairperson_annual_retainer', 'other_ned_annual_retainer')
    directors_remuneration['chairperson_vs_other_ned_meeting_allowance_ratio'] = paired_ratio(
        'chairperson_meeting_allowance', 'other_ned_meeting_allowance')

    # ---- Committee Remuneration ----
    committee_remuneration = {
        'committee_chair_annual_retainer': pay_metric('committee_chair_annual_retainer'),
        'committee_member_annual_retainer': pay_metric('committee_member_annual_retainer'),
        'committee_chair_meeting_allowance': pay_metric('committee_chair_meeting_allowance'),
        'committee_member_meeting_allowance': pay_metric('committee_member_meeting_allowance'),
    }
    committee_remuneration['chair_vs_member_annual_retainer_ratio'] = paired_ratio(
        'committee_chair_annual_retainer', 'committee_member_annual_retainer')
    committee_remuneration['chair_vs_member_meeting_allowance_ratio'] = paired_ratio(
        'committee_chair_meeting_allowance', 'committee_member_meeting_allowance')

    # ---- CEO/MD Remuneration ----
    ceo_remuneration = {
        'ceo_monthly_salary': pay_metric('ceo_monthly_salary'),
        'ceo_monthly_allowances': pay_metric('ceo_monthly_allowances'),
        'ceo_monthly_incentive_bonus': pay_metric('ceo_monthly_incentive_bonus'),
        'ceo_monthly_deferred_incentive': pay_metric('ceo_monthly_deferred_incentive'),
        'ceo_monthly_non_cash_benefits': pay_metric('ceo_monthly_non_cash_benefits'),
        'ceo_monthly_pension': pay_metric('ceo_monthly_pension'),
        'ceo_monthly_gratuity': pay_metric('ceo_monthly_gratuity'),
        'ceo_monthly_share_value': pay_metric('ceo_monthly_share_value'),
        'ceo_monthly_cost_of_employment': pay_metric('ceo_monthly_cost_of_employment'),
    }

    # ---- Comparative Analysis: by sector ----
    # Every pay metric gets a per-sector average, same MIN_COMPANIES_FOR_AVERAGE
    # gate as the market-wide figures - a sector with only 1 company reporting
    # a field shows that field as None rather than a single company's number
    # dressed up as a sector average.
    SECTOR_METRIC_FIELDS = [
        'chairperson_annual_retainer', 'other_ned_annual_retainer',
        'chairperson_meeting_allowance', 'other_ned_meeting_allowance',
        'executive_director_annual_retainer', 'executive_director_meeting_allowance',
        'committee_chair_annual_retainer', 'committee_member_annual_retainer',
        'committee_chair_meeting_allowance', 'committee_member_meeting_allowance',
        'ceo_monthly_salary', 'ceo_monthly_cost_of_employment',
    ]

    sectors = {}
    for r in rows:
        sec = r.sector or company_by_id[r.company_id].sector or 'Unclassified'
        sectors.setdefault(sec, []).append(r)

    sector_comparison = []
    for sec, sec_rows in sorted(sectors.items()):
        sector_entry = {'sector': sec, 'company_count': len(sec_rows)}
        for fname in SECTOR_METRIC_FIELDS:
            vals = [normalized_pay(r, fname) for r in sec_rows]
            present = [v for v in vals if v is not None]
            enough = len(present) >= MIN_COMPANIES_FOR_AVERAGE
            sector_entry[f'avg_{fname}'] = _avg(vals) if enough else None
            sector_entry[f'avg_{fname}_company_count'] = len(present)
            # a sector with exactly one reporting company: keep the average gated (it is not a benchmark) but
            # expose the lone figure so the UI can show it labelled "1 company" instead of "Not available"
            sector_entry[f'single_{fname}'] = present[0] if len(present) == 1 else None
        sector_comparison.append(sector_entry)

    # ---- NED Benefits: % of companies providing each benefit ----
    # Denominator is companies that ADDRESSED the benefit at all (stated
    # Yes or No), never all companies in the fiscal year - a company that
    # never mentioned club membership shouldn't silently count against it.
    BENEFIT_KEYS = [
        'MedicalCover', 'IndemnityInsurance', 'TravelAccommodation',
        'TelephoneAllowance', 'TransportAllowance', 'MealAllowance',
        'ClubMembership', 'DutyDayAllowance', 'GroupPersonalAccident',
        'ShareSchemeParticipation',
    ]
    ned_benefits_summary = {}
    for key in BENEFIT_KEYS:
        addressed = []
        for r in rows:
            if r.ned_benefits and key in r.ned_benefits:
                addressed.append(r.ned_benefits[key].get('provided', False))
        ned_benefits_summary[key] = {
            'percent_provided': round(100 * sum(addressed) / len(addressed), 1) if addressed else None,
            'companies_addressing': len(addressed),
        }

    # ---- Appendix: company list ----
    company_list = sorted([
        {'company_id': r.company_id, 'fiscal_year': r.fiscal_year,
         'name': company_by_id[r.company_id].name, 'sector': r.sector or company_by_id[r.company_id].sector,
         'source_filename': r.source_filename}
        for r in rows
    ], key=lambda c: c['name'])

    return {
        'has_data': True,
        'fiscal_year': fiscal_year,
        'available_fiscal_years': sorted(all_years, reverse=True),
        'companies_surveyed': len(rows),
        'currency': primary_currency,
        'unit': CANONICAL_UNIT,
        'min_companies_for_average': MIN_COMPANIES_FOR_AVERAGE,
        'excluded_from_performance_averages': excluded_companies,
        'executive_summary': executive_summary,
        'board_overview': board_overview,
        'directors_remuneration': directors_remuneration,
        'committee_remuneration': committee_remuneration,
        'ceo_remuneration': ceo_remuneration,
        'sector_comparison': sector_comparison,
        'ned_benefits_summary': ned_benefits_summary,
        'companies': company_list,
    }


# ---------------------------------------------------------------------
# Intelligence Layer support (added 2026-09-13) - per-company rows and a
# governance score, neither of which build_survey_overview() above
# returns (it only aggregates). Used by the Benchmarking, Company
# Profiles, Company Rankings, and Governance & Compliance tabs, which
# all need to compare INDIVIDUAL companies against each other or against
# the aggregate, not just read the aggregate itself.
# ---------------------------------------------------------------------

# CMA Code of Corporate Governance thresholds this illustrative score is
# built from - deliberately simple and fully shown in the UI next to the
# score, NOT a claim of regulatory compliance. Real CMA compliance
# assessment would need far more than these two ratios (risk management
# disclosure, shareholder rights process, remuneration policy existence,
# etc - none of which this app extracts structurally today).
GOVERNANCE_INDEPENDENCE_THRESHOLD = 1/3   # CMA: at least 1/3 of the board should be independent NEDs
GOVERNANCE_GENDER_THRESHOLD = 1/3         # CMA: at least 1/3 gender diversity recommended


def _governance_score(row):
    """A simple, transparent 0-100 illustrative score for ONE company's
    SurveyCompanyData row: 50 points for meeting the independence
    threshold (scaled down if under, capped at 50 if over), 50 points
    for meeting the gender-diversity threshold, same scaling. Returns
    None (not a fake score) if the row lacks the raw counts needed.
    Always paired with the raw underlying % in the UI, never shown
    alone - see GOVERNANCE_SCORE_METHODOLOGY string below, which the
    frontend displays verbatim next to every score."""
    indep = row.independent_neds_count
    non_indep = row.non_independent_neds_count
    female = row.directors_female
    male = row.directors_male
    if indep is None or non_indep is None or (indep + non_indep) == 0:
        indep_pct = None
    else:
        indep_pct = indep / (indep + non_indep)
    if female is None or male is None or (female + male) == 0:
        gender_pct = None
    else:
        gender_pct = female / (female + male)
    if indep_pct is None and gender_pct is None:
        return {'score': None, 'independence_pct': None, 'gender_pct': None}
    parts = []
    if indep_pct is not None:
        parts.append(min(50, round(50 * indep_pct / GOVERNANCE_INDEPENDENCE_THRESHOLD)))
    if gender_pct is not None:
        parts.append(min(50, round(50 * gender_pct / GOVERNANCE_GENDER_THRESHOLD)))
    # If only one half is known, scale that half up to 100 rather than
    # silently capping at 50 - an unknown half should not look like a
    # known bad half.
    score = round(sum(parts) * (100 / (50 * len(parts)))) if parts else None
    return {
        'score': score,
        'independence_pct': round(indep_pct * 100, 1) if indep_pct is not None else None,
        'gender_pct': round(gender_pct * 100, 1) if gender_pct is not None else None,
        'independence_compliant': indep_pct is not None and indep_pct >= GOVERNANCE_INDEPENDENCE_THRESHOLD,
        'gender_compliant': gender_pct is not None and gender_pct >= GOVERNANCE_GENDER_THRESHOLD,
    }


GOVERNANCE_SCORE_METHODOLOGY = (
    "Illustrative score only, not a regulatory compliance determination. "
    "Based on two CMA Code of Corporate Governance thresholds this app can "
    "measure from survey data: board independence \u2265 1/3 and gender "
    "diversity \u2265 1/3, each worth up to 50 points, scaled linearly up to "
    "the threshold and capped there. A real CMA assessment covers many "
    "criteria (risk disclosure, remuneration policy, shareholder rights "
    "process, etc.) this app does not extract."
)


def build_company_rows(fiscal_year=None):
    """One row per company with SurveyCompanyData for the given fiscal
    year (defaults like build_survey_overview), each carrying its own
    raw board/pay figures plus _governance_score() - the per-company
    comparison unit the Intelligence Layer's tabs need. Board composition
    counts are never Unit-scaled (they're plain counts), but the pay
    figures ARE run through _pay_scale (see the long comment above
    PAY_FIELDS) - the Benchmarking and Company Rankings tabs sort and
    compare these numbers ACROSS companies, so leaving one company's
    figures in thousands next to another's in raw currency would rank
    them wrong even though nothing here does arithmetic on its own."""
    if not fiscal_year:
        fiscal_year = default_fiscal_year()
        if not fiscal_year:
            return {'has_data': False, 'available_fiscal_years': [], 'rows': []}
    row_pairs = db.session.query(SurveyCompanyData, Company).join(
        Company, SurveyCompanyData.company_id == Company.id
    ).filter(SurveyCompanyData.fiscal_year == fiscal_year).all()
    if not row_pairs:
        return {'has_data': False, 'fiscal_year': fiscal_year,
                'available_fiscal_years': sorted(available_fiscal_years(), reverse=True), 'rows': []}
    out = []
    for r, c in row_pairs:
        gov = _governance_score(r)
        pay_scale = _pay_scale(r)
        chair = r.chairperson_annual_retainer * pay_scale if r.chairperson_annual_retainer is not None else None
        other_ned = r.other_ned_annual_retainer * pay_scale if r.other_ned_annual_retainer is not None else None
        exec_dir = r.executive_director_annual_retainer * pay_scale if r.executive_director_annual_retainer is not None else None
        out.append({
            'company_id': c.id, 'name': c.name, 'sector': r.sector or c.sector, 'ticker': c.ticker,
            'fiscal_year': r.fiscal_year, 'currency': r.currency, 'unit': r.unit,
            'board_size': r.board_size, 'board_meetings_per_year': r.board_meetings_per_year,
            'independent_neds_count': r.independent_neds_count, 'non_independent_neds_count': r.non_independent_neds_count,
            'directors_female': r.directors_female, 'directors_male': r.directors_male,
            'chairperson_annual_retainer': chair,
            'other_ned_annual_retainer': other_ned,
            'executive_director_annual_retainer': exec_dir,
            'total_director_remuneration': _sum_or_none([chair, other_ned, exec_dir]),
            'governance': gov,
        })
    return {
        'has_data': True, 'fiscal_year': fiscal_year,
        'available_fiscal_years': sorted(available_fiscal_years(), reverse=True),
        'governance_methodology': GOVERNANCE_SCORE_METHODOLOGY,
        'rows': out,
    }


def build_historical_trends():
    """One build_survey_overview() call per available fiscal year, so
    the Historical Trends tab can chart real multi-year series - never
    interpolated or backfilled, a year with no SurveyCompanyData rows
    simply doesn't appear in the series."""
    years = sorted(available_fiscal_years())
    series = []
    for y in years:
        ov = build_survey_overview(fiscal_year=y)
        if ov.get('has_data'):
            series.append(ov)
    return {'has_data': len(series) > 0, 'years': years, 'series': series}


# ---------------------------------------------------------------------
# Portfolio-wide data quality (added 2026-09-13, for Overview) - SAME
# field list and per-field complete/needs-review/missing classification
# the Survey Report page's Data Quality tab already applies to one
# company (see SR_MAPPABLE_FIELDS in the frontend), just summed across
# every company's LATEST fiscal year instead of shown one company at a
# time. Kept in sync with SR_MAPPABLE_FIELDS by hand (no single shared
# source of truth between Python and the template) - if one changes,
# the other should too.
# ---------------------------------------------------------------------
QUALITY_FIELDS = [
    'board_size', 'board_meetings_per_year', 'committees_per_board', 'committee_meetings_per_year',
    'directors_female', 'directors_male', 'executive_directors_count', 'non_executive_directors_count',
    'independent_neds_count', 'non_independent_neds_count', 'chairperson_annual_retainer',
    'other_ned_annual_retainer', 'chairperson_meeting_allowance', 'other_ned_meeting_allowance',
    'executive_director_annual_retainer', 'executive_director_meeting_allowance',
    'committee_chair_annual_retainer', 'committee_member_annual_retainer',
    'committee_chair_meeting_allowance', 'committee_member_meeting_allowance',
    'turnover', 'net_profit', 'market_cap',
]


def build_portfolio_data_quality():
    """Complete / Needs Review / Missing counts across QUALITY_FIELDS,
    summed over every company's most recent SurveyCompanyData row (one
    row per company - a company with multiple fiscal years only counts
    its latest). 'Complete' = has a value AND (no confidence score OR
    confidence >= 0.75); 'Needs Review' = has a value with confidence <
    0.75; 'Missing' = no value at all. Returns has_data: False if no
    company has any SurveyCompanyData at all."""
    all_rows = SurveyCompanyData.query.order_by(SurveyCompanyData.fiscal_year.desc()).all()
    latest_by_company = {}
    for r in all_rows:
        if r.company_id not in latest_by_company:
            latest_by_company[r.company_id] = r
    rows = list(latest_by_company.values())
    if not rows:
        return {'has_data': False, 'complete': 0, 'needs_review': 0, 'missing': 0, 'total_fields': 0, 'companies_counted': 0}

    complete = needs_review = missing = 0
    for r in rows:
        conf = r.field_confidence or {}
        for f in QUALITY_FIELDS:
            val = getattr(r, f, None)
            if val is None:
                missing += 1
            elif f in conf and conf[f] is not None and conf[f] < 0.75:
                needs_review += 1
            else:
                complete += 1
    return {
        'has_data': True, 'complete': complete, 'needs_review': needs_review, 'missing': missing,
        'total_fields': complete + needs_review + missing, 'companies_counted': len(rows),
    }