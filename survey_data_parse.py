"""
Parser for the Survey Data condensed format (SURVEY_FORMAT_SPEC.md).

Deliberately separate from pdf_parse.py's parse_condensed_filing(),
which reads the financial-statement condensed format. This one is
shorter, keyed to a different set of section headers, and produces a
dict shaped for SurveyCompanyData (models_survey.py) - one row per
(company, fiscal_year), never a financial statement.

Like the rest of this app's condensed-format parsing: a field that
isn't present in the text is simply absent from the returned dict
(None) - never defaulted to 0 or guessed. Comment lines (#) and blank
lines are ignored. This module never fetches or invents a company - it
only returns parsed values; the caller decides how to match/create the
Company row.
"""

import re

SECTION_RE = re.compile(r'^===([A-Z_]+)===\s*$')
FIELD_RE = re.compile(r'^([A-Za-z][A-Za-z0-9]*)\s*:\s*(.*)$')

# Maps condensed-file field names (as written in SURVEY_FORMAT_SPEC.md)
# to SurveyCompanyData column names, per section.
FIELD_MAP = {
    'COMPANY': {
        'Name': 'company_name',       # not a model column directly - used to resolve/match Company
        'Sector': 'sector',
        'Currency': 'currency',
        'Unit': 'unit',
    },
    'PERIOD': {
        'FiscalYear': 'fiscal_year',
    },
    'PERFORMANCE': {
        'Turnover': 'turnover',
        'NetProfit': 'net_profit',
        'MarketCap': 'market_cap',
    },
    'BOARD_COMPOSITION': {
        'BoardSize': 'board_size',
        'BoardMeetingsPerYear': 'board_meetings_per_year',
        'CommitteesPerBoard': 'committees_per_board',
        'CommitteeMeetingsPerYear': 'committee_meetings_per_year',
        'DirectorsFemale': 'directors_female',
        'DirectorsMale': 'directors_male',
        'ExecutiveDirectorsCount': 'executive_directors_count',
        'NonExecutiveDirectorsCount': 'non_executive_directors_count',
        'IndependentNEDsCount': 'independent_neds_count',
        'NonIndependentNEDsCount': 'non_independent_neds_count',
        'NEDsKenyanCount': 'neds_kenyan_count',
        'NEDsNonKenyanCount': 'neds_non_kenyan_count',
        'AvgAgeExecutiveDirectors': 'avg_age_executive_directors',
        'AvgAgeNonExecutiveDirectors': 'avg_age_non_executive_directors',
        'AvgAgeIndependentNEDs': 'avg_age_independent_neds',
        'AvgAgeNonIndependentNEDs': 'avg_age_non_independent_neds',
    },
    'DIRECTOR_PAY': {
        'ChairpersonAnnualRetainer': 'chairperson_annual_retainer',
        'OtherNEDAnnualRetainer': 'other_ned_annual_retainer',
        'ChairpersonMeetingAllowance': 'chairperson_meeting_allowance',
        'OtherNEDMeetingAllowance': 'other_ned_meeting_allowance',
        'ExecutiveDirectorAnnualRetainer': 'executive_director_annual_retainer',
        'ExecutiveDirectorMeetingAllowance': 'executive_director_meeting_allowance',
    },
    'COMMITTEE_PAY': {
        'CommitteeChairAnnualRetainer': 'committee_chair_annual_retainer',
        'CommitteeMemberAnnualRetainer': 'committee_member_annual_retainer',
        'CommitteeChairMeetingAllowance': 'committee_chair_meeting_allowance',
        'CommitteeMemberMeetingAllowance': 'committee_member_meeting_allowance',
    },
    'CEO_PAY': {
        'CEOMonthlySalary': 'ceo_monthly_salary',
        'CEOMonthlyAllowances': 'ceo_monthly_allowances',
        'CEOMonthlyIncentiveBonus': 'ceo_monthly_incentive_bonus',
        'CEOMonthlyDeferredIncentive': 'ceo_monthly_deferred_incentive',
        'CEOMonthlyNonCashBenefits': 'ceo_monthly_non_cash_benefits',
        'CEOMonthlyPension': 'ceo_monthly_pension',
        'CEOMonthlyGratuity': 'ceo_monthly_gratuity',
        'CEOMonthlyShareValue': 'ceo_monthly_share_value',
        'CEOMonthlyCostOfEmployment': 'ceo_monthly_cost_of_employment',
    },
}

INT_FIELDS = {
    'board_size', 'board_meetings_per_year', 'committees_per_board',
    'committee_meetings_per_year', 'directors_female', 'directors_male',
    'executive_directors_count', 'non_executive_directors_count',
    'independent_neds_count', 'non_independent_neds_count',
    'neds_kenyan_count', 'neds_non_kenyan_count',
}
FLOAT_FIELDS = {
    'turnover', 'net_profit', 'market_cap',
    'avg_age_executive_directors', 'avg_age_non_executive_directors',
    'avg_age_independent_neds', 'avg_age_non_independent_neds',
    'chairperson_annual_retainer', 'other_ned_annual_retainer',
    'chairperson_meeting_allowance', 'other_ned_meeting_allowance',
    'executive_director_annual_retainer', 'executive_director_meeting_allowance',
    'committee_chair_annual_retainer', 'committee_member_annual_retainer',
    'committee_chair_meeting_allowance', 'committee_member_meeting_allowance',
    'ceo_monthly_salary', 'ceo_monthly_allowances', 'ceo_monthly_incentive_bonus',
    'ceo_monthly_deferred_incentive', 'ceo_monthly_non_cash_benefits',
    'ceo_monthly_pension', 'ceo_monthly_gratuity', 'ceo_monthly_share_value',
    'ceo_monthly_cost_of_employment',
}


def is_survey_data_format(text: str) -> bool:
    """True if this looks like a Survey Data condensed file rather than
    the financial condensed format or a raw PDF dump - checked before
    attempting to parse, same pattern as pdf_parse.py's
    is_condensed_format()."""
    return bool(re.search(r'^===COMPANY===\s*$', text, re.MULTILINE)) and \
        bool(re.search(r'^===PERIOD===\s*$', text, re.MULTILINE)) and \
        bool(re.search(r'^===(PERFORMANCE|BOARD_COMPOSITION|DIRECTOR_PAY|COMMITTEE_PAY|CEO_PAY)===\s*$', text, re.MULTILINE))


def _num(token):
    token = (token or '').strip().replace(',', '')
    if token == '':
        return None
    try:
        return float(token)
    except ValueError:
        return None


def parse_survey_data(text: str) -> dict:
    """Returns a flat dict of SurveyCompanyData-shaped fields, plus
    'company_name' (used by the caller to resolve/create the Company
    row - not a SurveyCompanyData column itself) and 'source_notes'
    (raw text of the ===SOURCE=== section, if present). Fields not
    present in the text are simply absent from the dict - the caller
    should treat a missing key as None/not-disclosed, never as 0."""
    result = {}
    source_lines = []
    current_section = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip('\n')
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue

        section_match = SECTION_RE.match(stripped)
        if section_match:
            current_section = section_match.group(1)
            continue

        if current_section == 'SOURCE':
            source_lines.append(stripped)
            continue

        if current_section not in FIELD_MAP:
            continue

        field_match = FIELD_RE.match(stripped)
        if not field_match:
            continue
        field_name, value = field_match.group(1), field_match.group(2).strip()
        column = FIELD_MAP[current_section].get(field_name)
        if not column:
            continue

        if column == 'company_name':
            result['company_name'] = value
        elif column in INT_FIELDS:
            n = _num(value)
            result[column] = int(n) if n is not None else None
        elif column in FLOAT_FIELDS:
            result[column] = _num(value)
        else:
            # plain string fields: sector, currency, unit, fiscal_year
            result[column] = value

    if source_lines:
        result['source_notes'] = '\n'.join(source_lines)

    return result


def parse_survey_data_file(path: str) -> dict:
    with open(path, 'r', encoding='utf-8') as fh:
        return parse_survey_data(fh.read())
