"""Extended survey fields: executive pay (key management), board effectiveness, audit, workforce, shareholders
(survey_company_data) and per-director attendance, other directorships and shareholdings (survey_directors).

Idempotent and safe on every boot (same pattern as v5-v13): each column is added only if it is missing. Nothing is
filled in - the new fields start empty and arrive through the Manual Entry forms (or, later, the report readers).
"""
from sqlalchemy import inspect, text

from models import db

_COMPANY = {
    'kmp_count': 'INTEGER', 'kmp_short_term_benefits': 'FLOAT', 'kmp_post_employment_benefits': 'FLOAT',
    'kmp_share_based_payments': 'FLOAT', 'kmp_termination_benefits': 'FLOAT', 'kmp_total_compensation': 'FLOAT',
    'total_directors_fees': 'FLOAT', 'total_executive_directors_emoluments': 'FLOAT', 'cfo_annual_total_pay': 'FLOAT',
    'loans_to_directors': 'FLOAT',
    'board_attendance_pct': 'FLOAT', 'independent_neds_over_9_years': 'INTEGER', 'chair_ceo_separate': 'VARCHAR(120)',
    'chair_independent': 'VARCHAR(120)', 'lead_independent_director': 'VARCHAR(120)', 'governance_audit_done': 'VARCHAR(120)',
    'governance_audit_by': 'VARCHAR(120)',
    'auditor_name': 'VARCHAR(120)', 'audit_fee': 'FLOAT', 'non_audit_fees': 'FLOAT', 'auditor_tenure_years': 'INTEGER',
    'audit_opinion': 'VARCHAR(120)', 'key_audit_matters_count': 'INTEGER', 'going_concern_uncertainty': 'VARCHAR(120)',
    'employees_count': 'INTEGER', 'staff_costs': 'FLOAT', 'women_in_workforce_pct': 'FLOAT',
    'women_in_senior_management_pct': 'FLOAT', 'staff_turnover_pct': 'FLOAT',
    'earnings_per_share': 'FLOAT', 'dividend_per_share': 'FLOAT', 'shareholders_count': 'INTEGER',
    'top10_shareholding_pct': 'FLOAT', 'free_float_pct': 'FLOAT', 'directors_shareholding_pct': 'FLOAT',
}
_DIRECTOR = {'board_meetings_attended': 'INTEGER', 'board_meetings_eligible': 'INTEGER',
             'other_directorships': 'INTEGER', 'shares_held': 'FLOAT'}


def _add(table, cols):
    insp = inspect(db.engine)
    if table not in insp.get_table_names():
        return
    have = {c['name'] for c in insp.get_columns(table)}
    for name, ddl in cols.items():
        if name not in have:
            db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
    db.session.commit()


def run_migration():
    _add('survey_company_data', _COMPANY)
    _add('survey_director', _DIRECTOR)
