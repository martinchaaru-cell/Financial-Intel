"""One module per Survey Report tab: what the tab needs, where each piece can come from, and
whether it has it.

The problem this fixes: the tabs are fed by SEPARATE pipelines - the financial-statement import saves
director pay rows (so the pay tables list every executive and non-executive director), while the
survey side (Register, Board Composition, Committees) is filled by its own readers. When a survey-side
reader fails or finds nothing, one tab is full and the next says "Not available" although the same
directors are right there in the pay table.

Each tab module below declares its metrics and its fallbacks, in the order tried:

    1. rule-based readers (run at upload)            -> written by app.py / board_extract / policy_extract
    2. cross-tab reconciliation  (THIS MODULE)       -> a tab is filled from what the OTHER tabs already hold
    3. AI extraction, only for what is still empty   -> ai_extract (Settings > AI)
    4. manual entry (Settings > Review > Manual)     -> for data that is in the report but that nothing could read

reconcile_tabs() is step 2 and works on the database alone (no PDF), so it can also repair data that was
uploaded before it existed. tab_status() reports, per tab and per metric, what is filled and what is
still missing - the same list drives the empty-tab banners and the manual-entry page.
"""
import json
import re

from models import db, FinancialPeriod, DirectorRemunerationRow, Committee, CommitteeMember
from models_survey import SurveyCompanyData, SurveyDirector

from board_extract import derive_board_composition, same_person, _split_honorific_and_name, _looks_like_person

BOARD_METRICS = [
    ('board_size', 'Board size'), ('board_meetings_per_year', 'Board meetings held'),
    ('committees_per_board', 'Committees'), ('committee_meetings_per_year', 'Committee meetings'),
    ('directors_female', 'Female directors'), ('directors_male', 'Male directors'),
    ('executive_directors_count', 'Executive directors'), ('non_executive_directors_count', 'Non-executive directors'),
    ('independent_neds_count', 'Independent NEDs'), ('non_independent_neds_count', 'Non-independent NEDs'),
    ('neds_kenyan_count', 'Kenyan NEDs'), ('neds_non_kenyan_count', 'Non-Kenyan NEDs'),
    ('avg_age_executive_directors', 'Avg age - executives'), ('avg_age_non_executive_directors', 'Avg age - NEDs'),
    ('avg_age_independent_neds', 'Avg age - independent NEDs'), ('avg_age_non_independent_neds', 'Avg age - non-independent NEDs'),
]
OVERVIEW_METRICS = [('turnover', 'Turnover / revenue'), ('net_profit', 'Net profit'), ('market_cap', 'Market cap')]
POLICY_METRICS = [
    ('chairperson_annual_retainer', 'Chairperson annual retainer'), ('other_ned_annual_retainer', 'Other NED annual retainer'),
    ('chairperson_meeting_allowance', 'Chairperson meeting allowance'), ('other_ned_meeting_allowance', 'Other NED meeting allowance'),
    ('executive_director_annual_retainer', 'Executive director annual retainer'),
    ('executive_director_meeting_allowance', 'Executive director meeting allowance'),
    ('committee_chair_annual_retainer', 'Committee chair annual retainer'), ('committee_member_annual_retainer', 'Committee member annual retainer'),
    ('committee_chair_meeting_allowance', 'Committee chair meeting allowance'),
    ('committee_member_meeting_allowance', 'Committee member meeting allowance'),
]
CEO_METRICS = [
    ('ceo_monthly_salary', 'CEO monthly salary'), ('ceo_monthly_allowances', 'CEO monthly allowances'),
    ('ceo_monthly_incentive_bonus', 'CEO monthly incentive bonus'), ('ceo_monthly_deferred_incentive', 'CEO monthly deferred incentive'),
    ('ceo_monthly_non_cash_benefits', 'CEO monthly non-cash benefits'), ('ceo_monthly_pension', 'CEO monthly pension'),
    ('ceo_monthly_gratuity', 'CEO monthly gratuity'), ('ceo_monthly_share_value', 'CEO monthly share value'),
    ('ceo_monthly_cost_of_employment', 'CEO monthly cost of employment'),
    ('ceo_annual_salary_as_stated', 'CEO annual salary (as stated in the report)'),
]
# Free-text survey columns a person may enter by hand (everything in 'metrics' is numeric).
# ceo_monthly_conversion_basis is the derivation note that goes with ceo_annual_salary_as_stated, e.g. "annual / 12".
CEO_TEXT_METRICS = [('ceo_monthly_conversion_basis', 'CEO monthly conversion basis (text, e.g. annual / 12)')]
BENEFIT_KEYS = ['MedicalCover', 'IndemnityInsurance', 'TravelAccommodation', 'TelephoneAllowance', 'TransportAllowance',
                'MealAllowance', 'ClubMembership', 'DutyDayAllowance', 'GroupPersonalAccident', 'ShareSchemeParticipation']

# The tabs and what each needs. 'metrics' are survey columns; 'lists' are row sets.
TAB_MODULES = [
    {'id': 'company-overview', 'label': 'Company Overview', 'metrics': OVERVIEW_METRICS, 'lists': []},
    {'id': 'board-composition', 'label': 'Board Composition', 'metrics': BOARD_METRICS, 'lists': []},
    {'id': 'directors-register', 'label': "Directors' Register", 'metrics': [], 'lists': ['register']},
    {'id': 'remuneration', 'label': 'Remuneration', 'metrics': POLICY_METRICS + CEO_METRICS, 'text_metrics': CEO_TEXT_METRICS,
     'lists': ['pay_rows']},
    {'id': 'committees', 'label': 'Committees', 'metrics': [], 'lists': ['committees']},
    {'id': 'benefits', 'label': 'Benefits & Allowances', 'metrics': [], 'lists': ['ned_benefits']},
]

# ---- extended survey fields (migrate_v14_survey_fields.py) ------------------------------------------------------
# Same pipeline as the core fields (Manual Entry form, upload, review, company report), but 'extended': True keeps
# them OUT of the core data-quality score, so adding them does not drop every company's score overnight.
EXEC_PAY_METRICS = [
    ('kmp_count', 'Key management personnel (number of people)'),
    ('kmp_short_term_benefits', 'Key management: salaries and short-term benefits'),
    ('kmp_post_employment_benefits', 'Key management: pension / post-employment benefits'),
    ('kmp_share_based_payments', 'Key management: share-based payments'),
    ('kmp_termination_benefits', 'Key management: termination benefits'),
    ('kmp_total_compensation', 'Key management: total compensation'),
    ('total_directors_fees', "Total non-executive directors' fees"),
    ('total_executive_directors_emoluments', "Total executive directors' emoluments"),
    ('cfo_annual_total_pay', 'CFO / finance director total pay for the year'),
    ('loans_to_directors', 'Loans to directors (balance at year end)'),
]
BOARD_EFFECTIVENESS_METRICS = [
    ('board_attendance_pct', 'Average board meeting attendance (%)'),
    ('independent_neds_over_9_years', 'Independent NEDs serving more than 9 years'),
]
BOARD_EFFECTIVENESS_TEXT = [
    ('chair_ceo_separate', 'Chair and CEO roles held by different people (Yes / No)'),
    ('chair_independent', 'Chairperson is independent (Yes / No)'),
    ('lead_independent_director', 'Lead / senior independent director appointed (Yes / No)'),
    ('governance_audit_done', 'Governance audit carried out (Yes / No)'),
    ('governance_audit_by', 'Governance auditor (firm)'),
]
AUDIT_METRICS = [
    ('audit_fee', 'Audit fee'), ('non_audit_fees', 'Non-audit fees paid to the auditor'),
    ('auditor_tenure_years', 'Years the auditor has been in office'), ('key_audit_matters_count', 'Key audit matters (number)'),
]
AUDIT_TEXT = [
    ('auditor_name', 'External auditor (firm)'),
    ('audit_opinion', 'Audit opinion (Unmodified / Qualified / Adverse / Disclaimer)'),
    ('going_concern_uncertainty', 'Material uncertainty on going concern (Yes / No)'),
]
WORKFORCE_METRICS = [
    ('employees_count', 'Employees (number)'), ('staff_costs', 'Staff costs'),
    ('women_in_workforce_pct', 'Women in the workforce (%)'), ('women_in_senior_management_pct', 'Women in senior management (%)'),
    ('staff_turnover_pct', 'Staff turnover (%)'),
]
SHAREHOLDER_METRICS = [
    ('earnings_per_share', 'Earnings per share'), ('dividend_per_share', 'Dividend per share'),
    ('shareholders_count', 'Shareholders (number)'), ('top10_shareholding_pct', 'Top 10 shareholders - combined holding (%)'),
    ('free_float_pct', 'Free float (%)'), ('directors_shareholding_pct', 'Directors - combined holding (%)'),
]
EXTENDED_GROUPS = [
    {'id': 'executive-pay', 'label': 'Executive pay (key management)', 'metrics': EXEC_PAY_METRICS, 'lists': [], 'extended': True},
    {'id': 'board-effectiveness', 'label': 'Board effectiveness', 'metrics': BOARD_EFFECTIVENESS_METRICS,
     'text_metrics': BOARD_EFFECTIVENESS_TEXT, 'lists': [], 'extended': True},
    {'id': 'audit', 'label': 'Audit', 'metrics': AUDIT_METRICS, 'text_metrics': AUDIT_TEXT, 'lists': [], 'extended': True},
    {'id': 'workforce', 'label': 'Workforce', 'metrics': WORKFORCE_METRICS, 'lists': [], 'extended': True},
    {'id': 'shareholders', 'label': 'Shareholders & returns', 'metrics': SHAREHOLDER_METRICS, 'lists': [], 'extended': True},
]
TAB_MODULES.extend(EXTENDED_GROUPS)

# How each extended field is measured, so forms show the right unit and the company report converts it correctly:
#   pay      - same unit as the director pay figures (director_figures_unit)
#   headline - same unit as turnover / net profit (unit)
#   pershare - whole currency units per share
#   pct / count / years - as named;  text / yesno - free text (Yes / No answers are normalised on upload)
FIELD_KIND = {k: 'pay' for k, _ in EXEC_PAY_METRICS}
FIELD_KIND.update({'kmp_count': 'count', 'board_attendance_pct': 'pct', 'independent_neds_over_9_years': 'count',
                   'audit_fee': 'headline', 'non_audit_fees': 'headline', 'auditor_tenure_years': 'years',
                   'key_audit_matters_count': 'count', 'employees_count': 'count', 'staff_costs': 'headline',
                   'women_in_workforce_pct': 'pct', 'women_in_senior_management_pct': 'pct', 'staff_turnover_pct': 'pct',
                   'earnings_per_share': 'pershare', 'dividend_per_share': 'pershare', 'shareholders_count': 'count',
                   'top10_shareholding_pct': 'pct', 'free_float_pct': 'pct', 'directors_shareholding_pct': 'pct'})
YESNO_FIELDS = {'chair_ceo_separate', 'chair_independent', 'lead_independent_director', 'governance_audit_done',
                'going_concern_uncertainty'}
FIELD_KIND.update({k: 'yesno' for k in YESNO_FIELDS})
FIELD_KIND.update({k: 'text' for k in ('governance_audit_by', 'auditor_name', 'audit_opinion')})
INT_FIELDS = {'independent_neds_over_9_years', 'auditor_tenure_years'}     # whole numbers not ending in _count
# Per-director extended fields on SurveyDirector: (attribute, label)
DIRECTOR_EXTRA = [('board_meetings_attended', 'Board meetings attended'), ('board_meetings_eligible', 'Board meetings held while in office'),
                  ('other_directorships', 'Other directorships'), ('shares_held', 'Shares held')]


def field_kind(col):
    return FIELD_KIND.get(col)


def normalise_yes_no(raw):
    """'y', 'yes', 'true', '1' -> 'Yes'; 'n', 'no', 'false', '0' -> 'No'; anything else is kept as typed."""
    t = str(raw or '').strip()
    low = t.lower().rstrip('.')
    if low in ('y', 'yes', 'true', '1'):
        return 'Yes'
    if low in ('n', 'no', 'false', '0', 'none'):
        return 'No'
    return t


def core_metric_labels():
    """The original survey fields only (what the data-quality score is measured on)."""
    return {k: lbl for m in TAB_MODULES if not m.get('extended')
            for k, lbl in list(m['metrics']) + list(m.get('text_metrics', []))}


def survey_metric_columns():
    """Every NUMERIC survey column a person may enter by hand."""
    return {k for m in TAB_MODULES for k, _ in m['metrics']}


def survey_text_columns():
    """Free-text survey columns a person may enter by hand (stored as a short string, not a number)."""
    return {k for m in TAB_MODULES for k, _ in m.get('text_metrics', [])}


def all_metric_labels():
    """{column: label} for every hand-enterable survey column, numeric and text."""
    return {k: lbl for m in TAB_MODULES for k, lbl in list(m['metrics']) + list(m.get('text_metrics', []))}


def ensure_period(company_id, fiscal_year):
    period = FinancialPeriod.query.filter_by(company_id=company_id, period_label=fiscal_year).first()
    if period is None:
        m = re.match(r'^(H[12]|Q[1-4])\b', fiscal_year or '')
        ym = re.search(r"((?:19|20)\d\d)", fiscal_year or '')
        period = FinancialPeriod(company_id=company_id, period_label=fiscal_year,
                                 period_type=m.group(1) if m else 'FY', fiscal_year=int(ym.group(1)) if ym else None)
        db.session.add(period)
        db.session.flush()
    return period


def survey_row(company_id, fiscal_year, create=False):
    row = SurveyCompanyData.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).first()
    if row is None and create:
        row = SurveyCompanyData(company_id=company_id, fiscal_year=fiscal_year, currency='KES', unit='millions')
        db.session.add(row)
        db.session.flush()
    return row


_BENEFIT_KEY = re.compile(r"housing|vehicle|car|medical|health|travel|pension|benefit|allowance|insurance|mortgage|fuel", re.I)


def _benefit_components(pay_rows):
    """Benefit-like columns in the filed pay rows (Retirement benefits, Other employee benefits, ...) - the
    Benefits tab shows these, so they count as that tab having data."""
    keys = set()
    for r in pay_rows:
        try:
            comps = json.loads(r.components) if isinstance(r.components, str) else (r.components or {})
        except ValueError:
            comps = {}
        keys |= {k for k in comps if _BENEFIT_KEY.search(k) and not k.lower().startswith('total')}
    return len(keys)


def tab_status(company_id, fiscal_year):
    """{tab_id: {'label', 'filled', 'total', 'missing': [keys], 'rows': n}} - what each tab holds right now."""
    row = survey_row(company_id, fiscal_year)
    period = FinancialPeriod.query.filter_by(company_id=company_id, period_label=fiscal_year).first()
    reg = SurveyDirector.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).all()
    pay = DirectorRemunerationRow.query.filter_by(period_id=period.id).all() if period else []
    comms = Committee.query.filter_by(period_id=period.id).all() if period else []
    out = {}
    for mod in TAB_MODULES:
        filled, missing = 0, []
        for key, label in list(mod['metrics']) + list(mod.get('text_metrics', [])):
            val = getattr(row, key, None) if row is not None else None
            if val is not None and str(val).strip() != '':
                filled += 1
            else:
                missing.append(key)
        total = len(mod['metrics']) + len(mod.get('text_metrics', []))
        rows = 0
        for lst in mod['lists']:
            n = {'register': len(reg), 'pay_rows': len([r for r in pay if not r.is_grand_total and not r.is_total_row]),
                 'committees': len(comms), 'ned_benefits': len((row.ned_benefits or {}) if row else {}) + _benefit_components(pay)}[lst]
            rows += n
            total += 1
            if n:
                filled += 1
            else:
                missing.append(lst)
        out[mod['id']] = {'label': mod['label'], 'filled': filled, 'total': total, 'missing': missing, 'rows': rows}
    return out


def _fill(row, sources, conf, col, value, text, confidence=0.6):
    """Fill an empty survey field, or refresh one this module wrote earlier. Never touches a value that
    came from anywhere else (rules, AI, a person)."""
    if col in (row.field_review_status or {}):
        return False          # a person confirmed, corrected or deliberately blanked this field
    ours = str(sources.get(col, '')).startswith('from the director roster')
    if getattr(row, col, None) is None or ours:
        setattr(row, col, value)
        sources[col] = text
        conf[col] = confidence
        return True
    return False


def reconcile_tabs(company_id, fiscal_year):
    """Step 2 of the ladder - make the tabs agree, using only what is already stored.

      a. Register  <- director pay rows: if the register has fewer than 3 directors but the pay tables name
         people, they ARE the board's directors (role from the pay table's own executive / non-executive
         heading; gender only from a Mr./Mrs./Ms. the pay table printed).
      b. Register enrichment: a listed director with no role / gender picks it up from the pay row for the
         same person (fuzzy name match).
      c. Board Composition <- Register: counts, only where EVERY listed director states the fact.
    Returns {'register_added': n, 'register_enriched': n, 'fields': [...]}."""
    result = {'register_added': 0, 'register_enriched': 0, 'fields': []}
    period = FinancialPeriod.query.filter_by(company_id=company_id, period_label=fiscal_year).first()
    pay = [r for r in (DirectorRemunerationRow.query.filter_by(period_id=period.id).all() if period else [])
           if not r.is_grand_total and not r.is_total_row]
    reg = SurveyDirector.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).order_by(SurveyDirector.order_index).all()

    pay_people = []
    for r in pay:
        hon, name = _split_honorific_and_name(r.director_name)
        if name and _looks_like_person(name):        # 2026-09-28: Kenya Airways' AGM vote-count rows were being adopted as directors
            pay_people.append({'name': name, 'gender': hon, 'role': r.role if r.role in ('executive', 'non_executive') else None,
                               'page': r.page})

    if len(reg) < 3 and len(pay_people) >= 3:
        for p in pay_people:
            if any(same_person(p['name'], d.director_name) for d in reg):
                continue
            db.session.add(SurveyDirector(company_id=company_id, fiscal_year=fiscal_year, director_name=p['name'],
                                          role=p['role'] or 'unknown', gender=p['gender'], order_index=len(reg) + result['register_added'],
                                          page=p['page'], confidence=0.6))
            result['register_added'] += 1
        db.session.flush()
        reg = SurveyDirector.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).order_by(SurveyDirector.order_index).all()
    else:
        for d in reg:
            match = next((p for p in pay_people if same_person(p['name'], d.director_name)), None)
            if not match:
                continue
            changed = False
            if d.role in (None, 'unknown') and match['role']:
                d.role, changed = match['role'], True
            if not d.gender and match['gender']:
                d.gender, changed = match['gender'], True
            result['register_enriched'] += 1 if changed else 0

    if len(reg) >= 3:
        row = survey_row(company_id, fiscal_year, create=True)
        sources, conf = dict(row.field_sources or {}), dict(row.field_confidence or {})
        rows = [{'role': d.role, 'independent': d.independent, 'gender': d.gender, 'nationality': d.nationality,
                 'age': None, 'page': d.page} for d in reg]
        derived = derive_board_composition(
            rows, where=f"director register ({len(rows)} directors, as listed on this report's director tables)")
        for col, val in derived['fields'].items():
            if _fill(row, sources, conf, col, val, 'from the director roster: ' + derived['notes'][col],
                     0.6 if col == 'board_size' else 0.75):
                result['fields'].append(col)
        row.field_sources, row.field_confidence = sources, conf
    result['fields'] += fill_policy_from_pay_rows(company_id, fiscal_year)
    db.session.commit()
    return result


# ---------------------------------------------------------------- pay tables -> policy metrics
_FEE_KEY = re.compile(r"\bfees?\b|retainer|honorari", re.I)
_NOT_FEE_KEY = re.compile(r"sitting|attendance|allowance|total|expense|non-?cash|benefit|committee", re.I)
PAY_TABLE_SOURCE = 'from the director pay table'


def _components(row):
    try:
        c = json.loads(row.components) if isinstance(row.components, str) else (row.components or {})
    except ValueError:
        return {}
    return c if isinstance(c, dict) else {}


def _fixed_fee(row):
    """The fee/retainer column of a NED's own filed pay row - never the sitting-allowance, expense or Total
    columns (those are not the retainer). None when the row has no such column or it holds no figure."""
    for key, val in _components(row).items():
        if _FEE_KEY.search(str(key)) and not _NOT_FEE_KEY.search(str(key)):
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                return float(val)
    return None


_CHAIR_MARK = re.compile(r"\bchair(?:man|person|woman)?\b", re.I)


def fill_policy_from_pay_rows(company_id, fiscal_year):
    """Fallback for the NED retainer fields: when the policy text gave nothing, take them from the filed
    per-director pay table. Returns the list of survey columns filled.

      * other_ned_annual_retainer  <- the most common fee among non-executive rows (a PAID fee stands in
        for the rate, so confidence is kept low);
      * chairperson_annual_retainer <- only when the table itself marks a row as chair - never guessed.

    Never touches a value that came from another source (rules, AI, a person); only refreshes one this
    function wrote earlier."""
    period = FinancialPeriod.query.filter_by(company_id=company_id, period_label=fiscal_year).first()
    if period is None:
        return []
    rows = [r for r in DirectorRemunerationRow.query.filter_by(period_id=period.id).all()
            if not r.is_grand_total and not r.is_total_row and r.role == 'non_executive']
    chair_fees, other_fees = [], []
    for r in rows:
        fee = _fixed_fee(r)
        if fee is None:
            continue
        (chair_fees if _CHAIR_MARK.search(r.director_name or '') else other_fees).append(fee)
    if not chair_fees and not other_fees:
        return []

    def most_common(vals):
        counts = {}
        for v in vals:
            counts[v] = counts.get(v, 0) + 1
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]

    row = survey_row(company_id, fiscal_year, create=True)
    sources, conf = dict(row.field_sources or {}), dict(row.field_confidence or {})
    filled = []
    wanted = []
    if other_fees:
        wanted.append(('other_ned_annual_retainer', most_common(other_fees), len(other_fees)))
    if chair_fees:
        wanted.append(('chairperson_annual_retainer', most_common(chair_fees), len(chair_fees)))
    reviewed = row.field_review_status or {}
    for col, value, n in wanted:
        if col in reviewed:
            continue          # a person confirmed, corrected or deliberately blanked this field
        ours = str(sources.get(col, '')).startswith(PAY_TABLE_SOURCE)
        if getattr(row, col, None) is None or ours:
            setattr(row, col, value)
            sources[col] = f"{PAY_TABLE_SOURCE}: fee column, {n} director(s) - a paid fee, not a stated rate"
            conf[col] = 0.45
            filled.append(col)
    row.field_sources, row.field_confidence = sources, conf
    return filled