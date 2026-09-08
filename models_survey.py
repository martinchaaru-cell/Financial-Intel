"""
Survey model — deliberately separate from models.py.

This backs ONE feature: the Survey page (a multi-company board/director
remuneration benchmark, in the spirit of the old NSE "Board Remuneration
Report" but built up from individual company filings instead of one
big aggregate PDF). Kept in its own file, its own table, on purpose:

- It is NOT a financial statement. It doesn't touch FinancialPeriod,
  FinancialStatement, or FinancialLineItem. A company can have rich
  income-statement data and zero survey data, or vice versa.
- It is NOT per-director. Unlike DirectorRemunerationRow (models.py),
  which stores one row per named person exactly as a filing prints it,
  this table stores COMPANY-LEVEL figures only — the chairperson's
  retainer, the average NED retainer, board size, and so on — the same
  granularity the original NSE survey published (percentiles/averages,
  never named individuals). Someone filling in a SurveyCompanyData row
  from a filing that DOES name individuals (like KCB's) picks out or
  computes the company-level figure themselves; this table doesn't try
  to derive it automatically from DirectorRemunerationRow.
- Every field is optional except company_id and fiscal_year. Missing
  data is left NULL, never a guess or a zero — the Survey page's
  aggregates skip a company for any metric it didn't supply, exactly
  as the rest of this app treats an undisclosed figure.
- Aggregation (sector averages, percentiles, market-wide totals) is
  computed on read across every SurveyCompanyData row for the selected
  fiscal_year — there is no separate "survey" or "edition" record to
  upload; the survey IS however many companies have a row for that year.
"""

from datetime import datetime
from models import db


class SurveyCompanyData(db.Model):
    """One company's board/remuneration/performance figures for one
    fiscal year, scoped to only what the Survey page displays. One row
    per (company_id, fiscal_year)."""
    __tablename__ = 'survey_company_data'

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    fiscal_year = db.Column(db.String(20), nullable=False)   # e.g. "FY2025" — the survey's own grouping label, not a date
    sector = db.Column(db.String(80))                        # survey sector bucket (may differ from Company.sector's own label)
    source_filename = db.Column(db.String(255))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    # ---- Performance (Executive Summary) ----
    turnover = db.Column(db.Float)              # revenue/turnover, in the survey's stated currency+unit
    net_profit = db.Column(db.Float)
    market_cap = db.Column(db.Float)
    currency = db.Column(db.String(10), default='KES')
    unit = db.Column(db.String(20), default='millions')   # e.g. "millions", "billions" — states the scale of turnover/net_profit/market_cap

    # ---- Board composition (Board Overview / Executive Summary) ----
    board_size = db.Column(db.Integer)
    board_meetings_per_year = db.Column(db.Integer)
    committees_per_board = db.Column(db.Integer)
    committee_meetings_per_year = db.Column(db.Integer)

    directors_female = db.Column(db.Integer)
    directors_male = db.Column(db.Integer)

    executive_directors_count = db.Column(db.Integer)
    non_executive_directors_count = db.Column(db.Integer)
    independent_neds_count = db.Column(db.Integer)
    non_independent_neds_count = db.Column(db.Integer)

    neds_kenyan_count = db.Column(db.Integer)          # domicile split — labelled "kenyan" to match the source survey's own
    neds_non_kenyan_count = db.Column(db.Integer)      # terminology; a future non-NSE upload can still populate these two

    avg_age_executive_directors = db.Column(db.Float)
    avg_age_non_executive_directors = db.Column(db.Float)
    avg_age_independent_neds = db.Column(db.Float)
    avg_age_non_independent_neds = db.Column(db.Float)

    # ---- Director remuneration (Directors' Remuneration / NED / Executive Directors tabs) ----
    chairperson_annual_retainer = db.Column(db.Float)
    other_ned_annual_retainer = db.Column(db.Float)
    chairperson_meeting_allowance = db.Column(db.Float)     # per board meeting
    other_ned_meeting_allowance = db.Column(db.Float)       # per board meeting

    executive_director_annual_retainer = db.Column(db.Float)
    executive_director_meeting_allowance = db.Column(db.Float)

    # ---- Committee remuneration (Committee Remuneration tab) ----
    committee_chair_annual_retainer = db.Column(db.Float)
    committee_member_annual_retainer = db.Column(db.Float)
    committee_chair_meeting_allowance = db.Column(db.Float)  # per committee meeting
    committee_member_meeting_allowance = db.Column(db.Float)

    # ---- CEO/MD remuneration (Chief Executive Officer / Managing Director tab) ----
    # Company-level monthly figures only, same discipline as the rest of this
    # table - if a filing names the CEO individually (annual, not monthly),
    # pick out or convert the company-level monthly figure yourself, same as
    # DIRECTOR_PAY already asks for the chairperson/average NED figure.
    ceo_monthly_salary = db.Column(db.Float)
    ceo_monthly_allowances = db.Column(db.Float)
    ceo_monthly_incentive_bonus = db.Column(db.Float)
    ceo_monthly_deferred_incentive = db.Column(db.Float)
    ceo_monthly_non_cash_benefits = db.Column(db.Float)
    ceo_monthly_pension = db.Column(db.Float)
    ceo_monthly_gratuity = db.Column(db.Float)
    ceo_monthly_share_value = db.Column(db.Float)
    ceo_monthly_cost_of_employment = db.Column(db.Float)  # total - stated by the filing itself if given, else left NULL rather than summed here (avoids double-counting/mismatched figures if a filing's own total uses a different basis than the sum of components above)

    # Optional CEO pay derivation trail - populated only when the
    # ceo_monthly_* figures above were converted/derived rather than
    # transcribed directly (e.g. filing states an annual, named-individual
    # figure). Lets the company drill-down page show its arithmetic instead
    # of presenting a monthly figure as if the filing printed it directly.
    ceo_annual_salary_as_stated = db.Column(db.Float)
    ceo_monthly_conversion_basis = db.Column(db.String(120))   # e.g. "annual / 12"

    # ---- NED benefits (qualitative - Directors' Benefits tab) ----
    # {"MedicalCover": {"provided": true, "detail": "..."}, "ClubMembership": {"provided": false}, ...}
    # Keys are the same field names as NED_BENEFITS in SURVEY_FORMAT_SPEC.md.
    # A key absent from the dict means the filing didn't address that
    # benefit at all - never inferred or defaulted to False.
    ned_benefits = db.Column(db.JSON)

    source_notes = db.Column(db.Text)   # free text — methodology-level notes that don't map to one field (see SOURCE section style 2)

    # Per-field sources - {"turnover": "page 11", "chairperson_annual_retainer": "page 75 — derived: pooled across subsidiaries"}
    # Keys are SurveyCompanyData column names (snake_case), populated from
    # the SOURCE section's style-1 "FieldName: page ref" lines. Lets the
    # company page show a citation next to each individual figure instead
    # of one undifferentiated paragraph for the whole company.
    field_sources = db.Column(db.JSON)

    __table_args__ = (
        db.UniqueConstraint('company_id', 'fiscal_year', name='uq_survey_company_fiscal_year'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'company_id': self.company_id,
            'fiscal_year': self.fiscal_year,
            'sector': self.sector,
            'source_filename': self.source_filename,
            'uploaded_at': self.uploaded_at.isoformat() if self.uploaded_at else None,
            'currency': self.currency,
            'unit': self.unit,
            'turnover': self.turnover,
            'net_profit': self.net_profit,
            'market_cap': self.market_cap,
            'board_size': self.board_size,
            'board_meetings_per_year': self.board_meetings_per_year,
            'committees_per_board': self.committees_per_board,
            'committee_meetings_per_year': self.committee_meetings_per_year,
            'directors_female': self.directors_female,
            'directors_male': self.directors_male,
            'executive_directors_count': self.executive_directors_count,
            'non_executive_directors_count': self.non_executive_directors_count,
            'independent_neds_count': self.independent_neds_count,
            'non_independent_neds_count': self.non_independent_neds_count,
            'neds_kenyan_count': self.neds_kenyan_count,
            'neds_non_kenyan_count': self.neds_non_kenyan_count,
            'avg_age_executive_directors': self.avg_age_executive_directors,
            'avg_age_non_executive_directors': self.avg_age_non_executive_directors,
            'avg_age_independent_neds': self.avg_age_independent_neds,
            'avg_age_non_independent_neds': self.avg_age_non_independent_neds,
            'chairperson_annual_retainer': self.chairperson_annual_retainer,
            'other_ned_annual_retainer': self.other_ned_annual_retainer,
            'chairperson_meeting_allowance': self.chairperson_meeting_allowance,
            'other_ned_meeting_allowance': self.other_ned_meeting_allowance,
            'executive_director_annual_retainer': self.executive_director_annual_retainer,
            'executive_director_meeting_allowance': self.executive_director_meeting_allowance,
            'committee_chair_annual_retainer': self.committee_chair_annual_retainer,
            'committee_member_annual_retainer': self.committee_member_annual_retainer,
            'committee_chair_meeting_allowance': self.committee_chair_meeting_allowance,
            'committee_member_meeting_allowance': self.committee_member_meeting_allowance,
            'ceo_monthly_salary': self.ceo_monthly_salary,
            'ceo_monthly_allowances': self.ceo_monthly_allowances,
            'ceo_monthly_incentive_bonus': self.ceo_monthly_incentive_bonus,
            'ceo_monthly_deferred_incentive': self.ceo_monthly_deferred_incentive,
            'ceo_monthly_non_cash_benefits': self.ceo_monthly_non_cash_benefits,
            'ceo_monthly_pension': self.ceo_monthly_pension,
            'ceo_monthly_gratuity': self.ceo_monthly_gratuity,
            'ceo_monthly_share_value': self.ceo_monthly_share_value,
            'ceo_monthly_cost_of_employment': self.ceo_monthly_cost_of_employment,
            'ceo_annual_salary_as_stated': self.ceo_annual_salary_as_stated,
            'ceo_monthly_conversion_basis': self.ceo_monthly_conversion_basis,
            'ned_benefits': self.ned_benefits,
            'source_notes': self.source_notes,
            'field_sources': self.field_sources,
        }
