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


def _avg(values):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _sum_or_none(values):
    vals = [v for v in values if v is not None]
    return round(sum(vals), 2) if vals else None


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

    def field(name):
        return [getattr(r, name) for r in rows]

    def metric(name):
        vals = field(name)
        present = [v for v in vals if v is not None]
        return {'average': _avg(vals) if len(present) >= MIN_COMPANIES_FOR_AVERAGE else None,
                'company_count': len(present)}

    # ---- Executive Summary ----
    turnover_vals = [r.turnover for r in rows if r.turnover is not None]
    net_profit_vals = [r.net_profit for r in rows if r.net_profit is not None]
    market_cap_vals = [r.market_cap for r in rows if r.market_cap is not None]
    margin_rows = [r for r in rows if r.net_profit is not None and r.turnover not in (None, 0)]

    executive_summary = {
        'companies_with_data': len(rows),
        'avg_turnover': _avg(turnover_vals) if len(turnover_vals) >= MIN_COMPANIES_FOR_AVERAGE else None,
        'total_market_cap': _sum_or_none(market_cap_vals),
        'avg_net_profit': _avg(net_profit_vals) if len(net_profit_vals) >= MIN_COMPANIES_FOR_AVERAGE else None,
        'avg_profit_margin': _avg([(r.net_profit / r.turnover * 100) for r in margin_rows]) if len(margin_rows) >= MIN_COMPANIES_FOR_AVERAGE else None,
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
        'chairperson_annual_retainer': metric('chairperson_annual_retainer'),
        'other_ned_annual_retainer': metric('other_ned_annual_retainer'),
        'chairperson_meeting_allowance': metric('chairperson_meeting_allowance'),
        'other_ned_meeting_allowance': metric('other_ned_meeting_allowance'),
        'executive_director_annual_retainer': metric('executive_director_annual_retainer'),
        'executive_director_meeting_allowance': metric('executive_director_meeting_allowance'),
    }

    # ---- Committee Remuneration ----
    committee_remuneration = {
        'committee_chair_annual_retainer': metric('committee_chair_annual_retainer'),
        'committee_member_annual_retainer': metric('committee_member_annual_retainer'),
        'committee_chair_meeting_allowance': metric('committee_chair_meeting_allowance'),
        'committee_member_meeting_allowance': metric('committee_member_meeting_allowance'),
    }

    # ---- Comparative Analysis: by sector ----
    sectors = {}
    for r in rows:
        sec = r.sector or company_by_id[r.company_id].sector or 'Unclassified'
        sectors.setdefault(sec, []).append(r)

    sector_comparison = []
    for sec, sec_rows in sorted(sectors.items()):
        ned_retainers = [r.other_ned_annual_retainer for r in sec_rows if r.other_ned_annual_retainer is not None]
        sector_comparison.append({
            'sector': sec,
            'company_count': len(sec_rows),
            'avg_other_ned_annual_retainer': _avg(ned_retainers) if len(ned_retainers) >= MIN_COMPANIES_FOR_AVERAGE else None,
            'avg_other_ned_annual_retainer_company_count': len(ned_retainers),
        })

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
        'currency': rows[0].currency or 'KES',
        'unit': rows[0].unit or 'millions',
        'min_companies_for_average': MIN_COMPANIES_FOR_AVERAGE,
        'executive_summary': executive_summary,
        'board_overview': board_overview,
        'directors_remuneration': directors_remuneration,
        'committee_remuneration': committee_remuneration,
        'sector_comparison': sector_comparison,
        'companies': company_list,
    }
