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

    # Per-field confidence - {"turnover": 0.95, "board_size": 0.8, ...},
    # 0-1, populated by intelligence_extractor.py's PDF-extraction path
    # (a hand-typed Survey Data .txt upload has no confidence score of
    # its own - a person typing SOURCE lines IS the review step, so
    # those fields simply don't appear in this dict at all, same as an
    # extracted field the extractor wasn't confident enough to return).
    field_confidence = db.Column(db.JSON)

    # Review workflow for PDF-EXTRACTED fields specifically. A hand-typed
    # Survey Data .txt upload never touches these - typing the file WAS
    # the review. review_status is keyed by field name, e.g.
    # {"board_size": "approved", "chairperson_annual_retainer": "pending"}.
    # A field absent from this dict and present in field_confidence is
    # implicitly "pending" - this dict only needs to record fields once
    # a person has actually acted on them (approved, rejected, or
    # corrected), not a status for every extracted field up front.
    field_review_status = db.Column(db.JSON)

    # Corrections a reviewer made to an extracted value BEFORE approving
    # it - {"board_size": {"extracted_value": 9, "approved_value": 11,
    # "reviewed_by": "...", "reviewed_at": "2026-09-11T10:00:00"}}. The
    # live column value (e.g. self.board_size) is always the CURRENT
    # value shown everywhere else in the app; this dict is purely an
    # audit trail of what the extractor originally said versus what a
    # person corrected it to, for a field where those two differ.
    field_review_corrections = db.Column(db.JSON)

    __table_args__ = (
        db.UniqueConstraint('company_id', 'fiscal_year', name='uq_survey_company_fiscal_year'),
    )

    # ---- Confidence-threshold helpers ----
    # Named thresholds instead of scattering "if confidence < 0.45"
    # comparisons across app.py/intelligence_extractor.py - the numbers
    # here MUST stay in sync with intelligence_extractor.py's own
    # confidence bands (documented in that module's docstring): below
    # 0.45 the extractor doesn't even return the field, 0.45-0.75 is
    # "medium" (surfaced, but flagged for review), >=0.75 is "high".
    HIGH_CONFIDENCE_THRESHOLD = 0.75
    REVIEW_THRESHOLD = 0.45

    def high_confidence_fields(self) -> list:
        """Field names extracted at/above HIGH_CONFIDENCE_THRESHOLD -
        candidates for auto-approval without a person looking at them,
        though this method only identifies them; nothing auto-approves
        on its own."""
        conf = self.field_confidence or {}
        return [f for f, c in conf.items() if c >= self.HIGH_CONFIDENCE_THRESHOLD]

    def requires_review_fields(self) -> list:
        """Field names extracted with SOME confidence but below
        HIGH_CONFIDENCE_THRESHOLD - present in the data (unlike a field
        the extractor rejected outright below REVIEW_THRESHOLD, which
        never appears in field_confidence at all), but a person should
        look at the source sentence before trusting it fully."""
        conf = self.field_confidence or {}
        return [f for f, c in conf.items() if self.REVIEW_THRESHOLD <= c < self.HIGH_CONFIDENCE_THRESHOLD]

    def pending_review_fields(self) -> list:
        """Extracted fields that haven't been explicitly approved,
        rejected, or corrected yet - present in field_confidence, but
        with no matching key in field_review_status."""
        conf = self.field_confidence or {}
        status = self.field_review_status or {}
        return [f for f in conf if f not in status]

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
            'field_confidence': self.field_confidence,
            'field_review_status': self.field_review_status,
            'field_review_corrections': self.field_review_corrections,
            'high_confidence_fields': self.high_confidence_fields(),
            'requires_review_fields': self.requires_review_fields(),
            'pending_review_fields': self.pending_review_fields(),
        }


class RemunerationPolicy(db.Model):
    """One company's REMUNERATION POLICY disclosures for one fiscal
    year - yes/no facts and short free text, not amounts. Deliberately
    separate from SurveyCompanyData: "does this company have a share
    option scheme" isn't a number to average across companies the way
    turnover or board_size is, and it doesn't fit SurveyExtraField's
    generic (section, label, raw-string) shape well either, since
    these are a small, known, recurring set of questions every
    filing's remuneration report tends to address one way or another
    (yes, no, or silent) - worth their own typed columns rather than
    free-text extras.

    Examples actually observed across real filings: NSE's report
    states it has no LTIP; Equity's states no share option scheme;
    KCB's remuneration report describes fixed-term executive
    contracts; Safaricom's describes targeting the 75th percentile of
    market pay via a named benchmarking exercise. None of that is an
    amount - it's a policy fact, sometimes with a specific detail
    attached (which percentile, which benchmarking firm)."""
    __tablename__ = 'remuneration_policy'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    fiscal_year = db.Column(db.String(20), nullable=False)

    # Each is Boolean or NULL - NULL means the filing never addressed
    # the question at all (not found either way), which is a different,
    # weaker claim than False ("filing explicitly states it does NOT
    # have this"). Never defaulted to False on absence.
    has_share_option_scheme = db.Column(db.Boolean)
    has_ltip = db.Column(db.Boolean)
    has_performance_bonus = db.Column(db.Boolean)

    remuneration_benchmark_percentile = db.Column(db.String(40))   # e.g. "75th percentile" - kept as the filing's own phrase, not coerced to a bare number
    remuneration_benchmark_provider = db.Column(db.String(120))    # e.g. "PwC" - the named consultant/survey, if disclosed
    contract_type_notes = db.Column(db.Text)                       # e.g. "Executive directors serve on fixed-term contracts renewable every 3 years"

    source_notes = db.Column(db.Text)     # free text - page references / direct context for the facts above
    field_sources = db.Column(db.JSON)    # per-field page/quote, same shape as SurveyCompanyData.field_sources

    __table_args__ = (
        db.UniqueConstraint('company_id', 'fiscal_year', name='uq_remun_policy_company_fiscal_year'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'company_id': self.company_id, 'fiscal_year': self.fiscal_year,
            'has_share_option_scheme': self.has_share_option_scheme,
            'has_ltip': self.has_ltip,
            'has_performance_bonus': self.has_performance_bonus,
            'remuneration_benchmark_percentile': self.remuneration_benchmark_percentile,
            'remuneration_benchmark_provider': self.remuneration_benchmark_provider,
            'contract_type_notes': self.contract_type_notes,
            'source_notes': self.source_notes,
            'field_sources': self.field_sources,
        }


class GovernancePolicy(db.Model):
    """One company's GOVERNANCE policy disclosures for one fiscal year
    - the non-remuneration counterpart to RemunerationPolicy. A
    director-independence policy, a board diversity policy, a
    succession-planning statement: things a governance report commonly
    states it has or doesn't, distinct from the numeric board
    composition figures already on SurveyCompanyData (board_size,
    directors_female, etc.)."""
    __tablename__ = 'governance_policy'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    fiscal_year = db.Column(db.String(20), nullable=False)

    has_diversity_policy = db.Column(db.Boolean)
    has_board_evaluation_process = db.Column(db.Boolean)
    has_succession_plan = db.Column(db.Boolean)
    has_director_induction_program = db.Column(db.Boolean)

    board_evaluation_conducted_by = db.Column(db.String(120))   # e.g. "external consultant", "internal self-assessment"
    notes = db.Column(db.Text)

    source_notes = db.Column(db.Text)
    field_sources = db.Column(db.JSON)

    __table_args__ = (
        db.UniqueConstraint('company_id', 'fiscal_year', name='uq_gov_policy_company_fiscal_year'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'company_id': self.company_id, 'fiscal_year': self.fiscal_year,
            'has_diversity_policy': self.has_diversity_policy,
            'has_board_evaluation_process': self.has_board_evaluation_process,
            'has_succession_plan': self.has_succession_plan,
            'has_director_induction_program': self.has_director_induction_program,
            'board_evaluation_conducted_by': self.board_evaluation_conducted_by,
            'notes': self.notes,
            'source_notes': self.source_notes,
            'field_sources': self.field_sources,
        }


class EvidenceConflict(db.Model):
    """Records a case where two DIFFERENT chunks of the same PDF gave
    two DIFFERENT candidate values for the same field, instead of
    silently keeping the higher-scoring one and discarding the other
    (which is what intelligence_extractor.py does today - see its
    "best_value" selection loop). A person can review a conflict and
    decide which figure (if either) is right; until then the field
    that triggered it stays out of SurveyCompanyData entirely, same as
    any other not-confident-enough field.

    Kept as its own table rather than another JSON blob column because
    a conflict is inherently a THING to resolve (has its own lifecycle
    - open, resolved-to-A, resolved-to-B, resolved-to-neither), not a
    passive fact about the row the way field_sources/field_confidence
    are."""
    __tablename__ = 'evidence_conflict'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    fiscal_year = db.Column(db.String(20), nullable=False)
    field_name = db.Column(db.String(80), nullable=False)

    candidate_a_value = db.Column(db.Float)
    candidate_a_page = db.Column(db.Integer)
    candidate_a_source_text = db.Column(db.Text)

    candidate_b_value = db.Column(db.Float)
    candidate_b_page = db.Column(db.Integer)
    candidate_b_source_text = db.Column(db.Text)

    resolution = db.Column(db.String(20))       # NULL (open) | "candidate_a" | "candidate_b" | "neither" | "other"
    resolution_value = db.Column(db.Float)      # only set when resolution == "other" (reviewer supplied a third value)
    resolved_by = db.Column(db.String(120))
    resolved_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'company_id': self.company_id, 'fiscal_year': self.fiscal_year,
            'field_name': self.field_name,
            'candidate_a': {'value': self.candidate_a_value, 'page': self.candidate_a_page,
                             'source_text': self.candidate_a_source_text},
            'candidate_b': {'value': self.candidate_b_value, 'page': self.candidate_b_page,
                             'source_text': self.candidate_b_source_text},
            'resolution': self.resolution,
            'resolution_value': self.resolution_value,
            'resolved_by': self.resolved_by,
            'resolved_at': self.resolved_at.isoformat() if self.resolved_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'is_open': self.resolution is None,
        }


class SurveyDirector(db.Model):
    """One NAMED director's own row for one company's survey fiscal
    year - the counterpart to SurveyCompanyData's company-level board
    composition COUNTS (board_size, directors_female, etc.). A filing
    that states "5 directors, 2 independent" but never names anyone
    still only gets a SurveyCompanyData row; a filing whose Directors'
    Register/board-profile pages actually list people by name gets
    SurveyDirector rows too, one per person, in addition.

    Deliberately NOT the same table as DirectorRemunerationRow
    (models.py) - that one is keyed to a FinancialPeriod on the
    financial-statement side and stores each person's own filed PAY
    breakdown (components column, exactly as the filing's remuneration
    table prints it). This table is keyed to the Survey side
    (company_id + fiscal_year, same as SurveyCompanyData) and stores
    each person's PROFILE facts - position, independence, nationality,
    appointment date - the kind of thing a filing's board-composition/
    directors'-register pages state, not its remuneration tables. A
    real company will usually have entries in both tables for the same
    fiscal year; this app does not try to merge or cross-derive them
    automatically, since one can exist without the other (a filing
    might name directors on its board page but only give company-level
    totals in its remuneration report, or vice versa).

    Every field nullable except director_name - most NSE filings print
    a name and position reliably, but rarely print gender, nationality,
    or appointment date in the same place, and this app never infers
    those from a name or from context. A field genuinely not found in
    the filing stays NULL, not a guess.
    """
    __tablename__ = 'survey_director'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    fiscal_year = db.Column(db.String(20), nullable=False)

    director_name = db.Column(db.String(150), nullable=False)
    position = db.Column(db.String(80))          # as printed - "Chairman", "Non-Executive Director", "CEO", etc.
    role = db.Column(db.String(20))               # 'executive' | 'non_executive' | 'unknown' - derived from position text, same convention as DirectorRemunerationRow.role
    gender = db.Column(db.String(10))              # 'Male' | 'Female' - only when the filing states it directly (a roster caption, a "Mr."/"Ms." title, a stated M/F column); never inferred from a name
    nationality = db.Column(db.String(60))
    independent = db.Column(db.Boolean)            # NULL = not addressed either way; False = filing states "Not Independent"/similar; True = filing states "Independent"

    appointed_date = db.Column(db.String(40))       # kept as a string, not a Date - filings print varying precision ("12 Jan 2019", "2019", "Since incorporation")
    end_of_term = db.Column(db.String(40))

    committees = db.Column(db.JSON)                 # list of committee names this person is stated to sit on, if the filing's board/committee pages say so directly - NOT cross-derived from SurveyCompanyData's committee counts

    order_index = db.Column(db.Integer, default=0)  # position in the filing's own register/table, so the UI can preserve filing order rather than re-sorting

    source_document_id = db.Column(db.Integer, db.ForeignKey('source_documents.id'))
    page = db.Column(db.Integer)
    confidence = db.Column(db.Float)                 # 0-1, set by the PDF extractor; NULL for a hand-entered row (same convention as SurveyCompanyData.field_confidence - no fabricated score for typed data)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'company_id': self.company_id, 'fiscal_year': self.fiscal_year,
            'director_name': self.director_name, 'position': self.position, 'role': self.role,
            'gender': self.gender, 'nationality': self.nationality, 'independent': self.independent,
            'appointed_date': self.appointed_date, 'end_of_term': self.end_of_term,
            'committees': self.committees,
            'order_index': self.order_index,
            'source_document_id': self.source_document_id, 'page': self.page, 'confidence': self.confidence,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
