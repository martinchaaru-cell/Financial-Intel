"""
Phase 1 — normalized financial-report data model.

Replaces the single flat `Financials` table with a structure that can hold
a full annual report: multiple statements per period, an arbitrary-depth
line-item tree per statement, segments, KPIs, notes, source documents, and
calculated (as opposed to reported) metrics.

Design principles:
- financial_line_items is the workhorse table. It is self-referential
  (parent_id) so it can represent "Operating expenses -> Employee costs"
  without a schema change, no matter how a given company's report is
  organized.
- Every line item keeps its provenance: which source document, which page,
  and a confidence score from the parser. That's what lets the UI answer
  "where did this number come from?".
- Reported figures (FinancialLineItem) and derived figures
  (CalculatedMetric) are kept in separate tables on purpose. A ratio is
  never allowed to silently masquerade as something the company reported.
- Nothing here changes how Company works today - it's additive alongside
  your existing Company/Financials tables so the migration in
  migrate_to_v2.py can run without data loss.
"""

from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


# ---------- COMPANY (unchanged shape, just imported into this module) ----------
# If you already have a Company model in app.py, don't duplicate it - this
# stub is here only so this file is self-contained / importable on its own
# for review. In the real app, keep using your existing Company class and
# just add the relationship shown in the comment below.

class Company(db.Model):
    __tablename__ = 'company'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    ticker = db.Column(db.String(20))
    exchange = db.Column(db.String(20))
    sector = db.Column(db.String(60))
    country = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    periods = db.relationship('FinancialPeriod', backref='company', lazy=True,
                               cascade='all, delete-orphan')
    source_documents = db.relationship('SourceDocument', backref='company', lazy=True,
                                        cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'ticker': self.ticker,
            'exchange': self.exchange, 'sector': self.sector, 'country': self.country,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


# ---------- SOURCE DOCUMENTS ----------

class SourceDocument(db.Model):
    """One row per filing PDF. Line items point back here + a page number."""
    __tablename__ = 'source_documents'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    url = db.Column(db.String(500))
    filename = db.Column(db.String(255))
    period_label = db.Column(db.String(20))          # e.g. "FY2025" - best guess at upload time
    page_count = db.Column(db.Integer)
    sha256 = db.Column(db.String(64))                 # dedupe re-uploads of the same filing
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'company_id': self.company_id, 'url': self.url,
            'filename': self.filename, 'period_label': self.period_label,
            'page_count': self.page_count,
            'uploaded_at': self.uploaded_at.isoformat() if self.uploaded_at else None
        }


# ---------- FINANCIAL PERIOD ----------

class FinancialPeriod(db.Model):
    """One reporting period for one company, e.g. FY2025 or Q3 2026."""
    __tablename__ = 'financial_periods'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    period_label = db.Column(db.String(20), nullable=False)   # "FY2025", "Q3 2026"
    period_type = db.Column(db.String(10), default='FY')      # FY, Q1, Q2, Q3, Q4, H1, H2
    fiscal_year = db.Column(db.Integer)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    currency = db.Column(db.String(10), default='KES')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    statements = db.relationship('FinancialStatement', backref='period', lazy=True,
                                  cascade='all, delete-orphan')
    segments = db.relationship('FinancialSegment', backref='period', lazy=True,
                                cascade='all, delete-orphan')
    operational_metrics = db.relationship('OperationalMetric', backref='period', lazy=True,
                                           cascade='all, delete-orphan')
    notes = db.relationship('FinancialNote', backref='period', lazy=True,
                             cascade='all, delete-orphan')
    calculated_metrics = db.relationship('CalculatedMetric', backref='period', lazy=True,
                                          cascade='all, delete-orphan')

    __table_args__ = (
        db.UniqueConstraint('company_id', 'period_label', name='uq_company_period'),
    )

    def statement(self, statement_type):
        return next((s for s in self.statements if s.statement_type == statement_type), None)

    def to_dict(self, include_line_items=True):
        return {
            'id': self.id, 'company_id': self.company_id,
            'period_label': self.period_label, 'period_type': self.period_type,
            'fiscal_year': self.fiscal_year,
            'start_date': self.start_date.isoformat() if self.start_date else None,
            'end_date': self.end_date.isoformat() if self.end_date else None,
            'currency': self.currency,
            'statements': {s.statement_type: s.to_dict(include_line_items) for s in self.statements},
            'segments': [s.to_dict() for s in self.segments],
            'operational_metrics': [m.to_dict() for m in self.operational_metrics],
            'notes': [n.to_dict() for n in self.notes],
            'calculated_metrics': {m.metric_name: m.value for m in self.calculated_metrics},
        }


# ---------- FINANCIAL STATEMENT ----------

STATEMENT_TYPES = ('income_statement', 'balance_sheet', 'cash_flow', 'equity')


class FinancialStatement(db.Model):
    """One statement (income statement / balance sheet / cash flow / equity)
    within a period. Holds the top-level line items via the tree below."""
    __tablename__ = 'financial_statements'
    id = db.Column(db.Integer, primary_key=True)
    period_id = db.Column(db.Integer, db.ForeignKey('financial_periods.id'), nullable=False)
    statement_type = db.Column(db.String(20), nullable=False)   # one of STATEMENT_TYPES
    source_document_id = db.Column(db.Integer, db.ForeignKey('source_documents.id'))

    line_items = db.relationship('FinancialLineItem', backref='statement', lazy=True,
                                  cascade='all, delete-orphan',
                                  order_by='FinancialLineItem.order_index')

    __table_args__ = (
        db.UniqueConstraint('period_id', 'statement_type', name='uq_period_statement'),
    )

    def to_dict(self, include_line_items=True):
        d = {'id': self.id, 'statement_type': self.statement_type,
             'source_document_id': self.source_document_id}
        if include_line_items:
            # only top-level items; each carries its own children
            top_level = [li for li in self.line_items if li.parent_id is None]
            d['line_items'] = [li.to_dict() for li in top_level]
        return d


# ---------- FINANCIAL LINE ITEM (the workhorse table) ----------

class FinancialLineItem(db.Model):
    """
    Self-referential tree so arbitrary report structures fit without a
    schema change - e.g.:

        Operating expenses (parent_id=None)
          -> Employee costs (parent_id=<operating expenses id>)
          -> Network operating costs (parent_id=<operating expenses id>)

    normalized_name is a snake_case key ("network_operating_costs") used
    for cross-company comparison and for the ratio calculator; label is
    the verbatim text as it appeared in the filing.
    """
    __tablename__ = 'financial_line_items'
    id = db.Column(db.Integer, primary_key=True)
    statement_id = db.Column(db.Integer, db.ForeignKey('financial_statements.id'), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('financial_line_items.id'))

    label = db.Column(db.String(200), nullable=False)          # verbatim from the filing
    normalized_name = db.Column(db.String(100))                # snake_case, for comparisons
    section = db.Column(db.String(100))                        # e.g. "operating_expenses"
    amount = db.Column(db.Float)
    currency = db.Column(db.String(10), default='KES')

    page = db.Column(db.Integer)                                # provenance: page in source PDF
    confidence = db.Column(db.Float)                            # parser confidence, 0-1
    order_index = db.Column(db.Integer, default=0)              # preserve original report order

    children = db.relationship('FinancialLineItem',
                                backref=db.backref('parent', remote_side=[id]),
                                lazy=True, cascade='all, delete-orphan',
                                order_by='FinancialLineItem.order_index')

    def to_dict(self):
        return {
            'id': self.id, 'label': self.label, 'normalized_name': self.normalized_name,
            'section': self.section, 'amount': self.amount, 'currency': self.currency,
            'page': self.page, 'confidence': self.confidence,
            'children': [c.to_dict() for c in self.children]
        }


# ---------- SEGMENTS ----------

class FinancialSegment(db.Model):
    """Segment reporting, e.g. Safaricom's M-PESA / Mobile / Fixed segments."""
    __tablename__ = 'financial_segments'
    id = db.Column(db.Integer, primary_key=True)
    period_id = db.Column(db.Integer, db.ForeignKey('financial_periods.id'), nullable=False)
    segment_name = db.Column(db.String(120), nullable=False)
    revenue = db.Column(db.Float)
    operating_profit = db.Column(db.Float)
    assets = db.Column(db.Float)
    notes = db.Column(db.Text)

    def to_dict(self):
        return {
            'id': self.id, 'segment_name': self.segment_name, 'revenue': self.revenue,
            'operating_profit': self.operating_profit, 'assets': self.assets, 'notes': self.notes
        }


# ---------- OPERATIONAL KPIs ----------

class OperationalMetric(db.Model):
    """Non-financial KPIs disclosed alongside the numbers - subscriber
    counts, branch counts, headcount, loan book size, etc."""
    __tablename__ = 'operational_metrics'
    id = db.Column(db.Integer, primary_key=True)
    period_id = db.Column(db.Integer, db.ForeignKey('financial_periods.id'), nullable=False)
    metric_name = db.Column(db.String(120), nullable=False)
    value = db.Column(db.Float)
    unit = db.Column(db.String(30))            # "customers", "branches", "employees", etc.

    def to_dict(self):
        return {'id': self.id, 'metric_name': self.metric_name,
                'value': self.value, 'unit': self.unit}


# ---------- NOTES / DISCLOSURES ----------

class FinancialNote(db.Model):
    __tablename__ = 'financial_notes'
    id = db.Column(db.Integer, primary_key=True)
    period_id = db.Column(db.Integer, db.ForeignKey('financial_periods.id'), nullable=False)
    title = db.Column(db.String(200))
    content = db.Column(db.Text)
    page = db.Column(db.Integer)

    def to_dict(self):
        return {'id': self.id, 'title': self.title, 'content': self.content, 'page': self.page}


# ---------- CALCULATED METRICS (kept separate from reported figures) ----------

class CalculatedMetric(db.Model):
    """Derived ratios - never something the company itself reported.
    Recomputed whenever the underlying line items change."""
    __tablename__ = 'calculated_metrics'
    id = db.Column(db.Integer, primary_key=True)
    period_id = db.Column(db.Integer, db.ForeignKey('financial_periods.id'), nullable=False)
    metric_name = db.Column(db.String(60), nullable=False)     # "net_margin", "roe", "fcf_margin"...
    value = db.Column(db.Float)
    formula_description = db.Column(db.String(200))
    calculated_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('period_id', 'metric_name', name='uq_period_metric'),
    )


# ---------- IMPORT JOBS (audit trail for the NSE pipeline) ----------

class ImportJob(db.Model):
    """One row per scan->parse->save attempt, so the review UI can show
    status/history and nothing has to be inferred from Financials.source."""
    __tablename__ = 'import_jobs'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    source_document_id = db.Column(db.Integer, db.ForeignKey('source_documents.id'))
    status = db.Column(db.String(20), default='pending')   # pending, parsed, reviewed, saved, failed
    error_message = db.Column(db.Text)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime)

    def to_dict(self):
        return {
            'id': self.id, 'company_id': self.company_id,
            'source_document_id': self.source_document_id, 'status': self.status,
            'error_message': self.error_message,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
        }