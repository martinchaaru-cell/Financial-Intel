import os
import csv
import io
import re
import json
import hashlib
import tempfile
import uuid
import queue
import threading
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_file, session, Response
from datetime import datetime
import openpyxl

from models import (
    db, Company, FinancialPeriod, FinancialStatement, FinancialLineItem,
    SourceDocument, ImportJob, CalculatedMetric, FinancialSegment,
    OperationalMetric, FinancialNote,
    MarketDataSnapshot, PrincipalRisk, ManagementGuidance, DirectorRemunerationRow,
    Committee, CommitteeMember, User, LOW_EXTRACTION_SCORE_THRESHOLD,
)
from models_survey import SurveyCompanyData, RemunerationPolicy, GovernancePolicy, EvidenceConflict, SurveyDirector, SurveyDirectorBenefit, SurveyBenefitCategory, ActivityLogEntry
from survey_data_parse import is_survey_data_format, parse_survey_data
from intelligence_extractor import extract_survey_schema, extract_policy_disclosures
from survey_aggregate import build_survey_overview, build_company_rows, build_historical_trends, build_portfolio_data_quality
from survey_report_pdf import build_survey_pdf
from ratios import calculate_ratios
from company_directory import match_company
from pdf_parse import (
    match_canonical_label, extract_pdf_document,
    detect_period_label, detect_prior_period_label, detect_company_name,
    extract_director_remuneration,
    extract_market_data, extract_management_guidance, extract_principal_risks,
    extract_director_remuneration_detail,
    extract_survey_directors,
    extract_survey_director_benefits,
    is_condensed_format, parse_condensed_filing,
)
from report_context import build_report_context
from report_docx import generate_docx
from report_narrative import generate_narrative

# ---------- SCORING ----------
# Simple, transparent financial health score (0-100).
# Weighted blend of margin, ROE, and leverage (inverse of debt/equity).
# All three inputs must be present to produce a score; otherwise None (honest "not enough data").

def calc_financial_score(net_margin, roe, debt_equity):
    if net_margin is None or roe is None or debt_equity is None:
        return None
    margin_score = max(0, min(100, net_margin * 2.5))       # 40% margin -> 100
    roe_score = max(0, min(100, roe * 2.5))                  # 40% ROE -> 100
    leverage_score = max(0, min(100, 100 - (debt_equity * 10)))  # lower debt/equity is better
    score = (margin_score * 0.4) + (roe_score * 0.4) + (leverage_score * 0.2)
    return round(max(0, min(100, score)))

def score_band(score):
    if score is None:
        return None
    if score >= 70:
        return 'strong'
    if score >= 40:
        return 'moderate'
    return 'weak'

app = Flask(__name__)

# Flask's default JSON provider alphabetizes dict keys (sort_keys=True),
# which silently re-orders period-keyed dicts like
# director_rows_by_period ({'FY2024': ..., 'FY2025': ...} -> FY2024
# first alphabetically, even though _ordered_periods() already builds
# them newest-year-first). Disable that so callers get back exactly the
# insertion order this app already computed.
app.json.sort_keys = False

# Fix for hosts (e.g. Replit's provisioned Postgres) that hand back a
# "postgres://" URL — SQLAlchemy 1.4+ requires "postgresql://".
database_url = os.environ.get('DATABASE_URL', 'sqlite:///test.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Session cookie signing key. Set SESSION_SECRET in the environment for
# real deployments - sessions (and every logged-in user) get silently
# invalidated on restart if this falls back to the dev default, since a
# new random-looking-but-fixed string only holds within one process.
app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET', 'dev-only-change-me')

# Hosted Postgres (Replit's provisioned DB is Neon-backed) silently drops
# idle connections after a short window. Without this, SQLAlchemy's pool
# can hand out a connection that the server already closed - the driver
# only discovers this mid-query, which surfaces as
# "psycopg2.OperationalError: SSL connection has been closed unexpectedly"
# on whatever query happens to run next (e.g. the very first
# Company.query.get() in an upload request). pool_pre_ping runs a
# cheap "SELECT 1" before handing out a pooled connection and
# transparently reconnects if it's dead; pool_recycle proactively retires
# connections before the server's own idle timeout gets a chance to kill
# them mid-request.
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 280,
}

# No hard cap on request body size: file COUNT is capped (10 per batch,
# enforced client-side and, more importantly, server-side in
# /api/import/upload-batch itself - see that route) but total MB is not,
# since a batch of real integrated reports (dense, image-heavy, 200-300
# pages) can comfortably exceed what any fixed MB number would assume.
# MAX_CONTENT_LENGTH is intentionally left unset (Flask/Werkzeug default:
# no limit) rather than raised to a new fixed number, so a future batch
# of unusually large reports can't hit the same wall again.

db.init_app(app)


# ---------- ERROR HANDLING ----------
# Every API route on this app returns JSON - including errors. Without
# this, an uncaught exception (or a request-too-large rejection) falls
# through to Flask/Werkzeug's default HTML error page, which breaks any
# frontend code doing res.json() on the response (this is exactly what
# was happening: an oversized/slow PDF upload failed with the JSON parser
# choking on the words "Internal Server Error" instead of showing the
# real problem). These handlers are the last line of defense - route
# handlers should still catch what they can locally for a more specific
# per-file error message (see /api/import/upload-batch).

@app.errorhandler(413)
def handle_request_too_large(e):
    max_mb = app.config['MAX_CONTENT_LENGTH'] / (1024 * 1024)
    return jsonify({
        'error': f'Upload too large - this request is over the {max_mb:.0f} MB limit. '
                 'Upload fewer files at once, or a smaller file.'
    }), 413


@app.errorhandler(404)
def handle_not_found(e):
    return jsonify({'error': 'Not found.'}), 404


# ---------- AUTH ----------
# Three-tier access: 'admin' (upload/edit/delete/view/download), 'user'
# (view/download only), and an unauthenticated visitor - no session at
# all - treated as 'guest' (view only) everywhere below. There is no
# guest account/row; "guest" just means current_role() falls through to
# the default. The page itself (GET /) never redirects to a login wall -
# guests can open and browse the app with zero login, per spec.

def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    return User.query.get(uid)

def current_role():
    u = current_user()
    return u.role if u else 'guest'

def require_role(*roles):
    """Server-side gate for a route. Hiding a button in the frontend is
    just UI polish - this decorator is the actual enforcement, since a
    guest or user could otherwise call the API endpoint directly."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if current_role() not in roles:
                return jsonify({'error': "You don't have permission to do that."}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator

@app.route('/api/register', methods=['POST'])
def register():
    """Public self-registration. Always creates a 'user' role account -
    self-registering as admin is never allowed; new admins can only be
    created by an existing admin from the User Management panel. The
    name given here is what shows in the topbar everywhere in the app -
    replaces the old hardcoded name."""
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not name or not email or not password:
        return jsonify({'error': 'Name, email, and password are all required.'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters.'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'An account with that email already exists.'}), 409
    user = User(name=name, email=email, role='user')
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    session['user_id'] = user.id
    return jsonify(user.to_dict()), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(force=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({'error': 'Incorrect email or password.'}), 401
    session['user_id'] = user.id
    return jsonify(user.to_dict())

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/me')
def me():
    """Tells the frontend who's logged in (or that nobody is, i.e.
    guest) so it can show the right name in the topbar and show/hide
    upload/edit/delete/download controls. Always 200 - guest is a valid,
    expected state, not an error."""
    u = current_user()
    if not u:
        return jsonify({'logged_in': False, 'role': 'guest', 'name': None, 'email': None})
    d = u.to_dict()
    d['logged_in'] = True
    return jsonify(d)


# ---------- API: USER MANAGEMENT (admin only) ----------
# Lets an admin promote/demote roles, create new accounts of any role
# (including new admins), and reset a locked-out user's password by hand
# - there's no email-based password-reset flow in this app.

@app.route('/api/users', methods=['GET'])
@require_role('admin')
def list_users():
    users = User.query.order_by(User.created_at.asc()).all()
    return jsonify([u.to_dict() for u in users])

@app.route('/api/users', methods=['POST'])
@require_role('admin')
def create_user():
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    role = data.get('role') or 'user'
    if role not in ('admin', 'user'):
        return jsonify({'error': "Role must be 'admin' or 'user'."}), 400
    if not name or not email or not password:
        return jsonify({'error': 'Name, email, and password are all required.'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters.'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'An account with that email already exists.'}), 409
    user = User(name=name, email=email, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return jsonify(user.to_dict()), 201

@app.route('/api/users/<int:user_id>/role', methods=['PATCH'])
@require_role('admin')
def update_user_role(user_id):
    user = User.query.get_or_404(user_id)
    data = request.get_json(force=True) or {}
    role = data.get('role')
    if role not in ('admin', 'user'):
        return jsonify({'error': "Role must be 'admin' or 'user'."}), 400
    if user.id == session.get('user_id') and role != 'admin':
        return jsonify({'error': "You can't demote your own account."}), 400
    if user.role == 'admin' and role != 'admin':
        remaining_admins = User.query.filter(User.role == 'admin', User.id != user.id).count()
        if remaining_admins == 0:
            return jsonify({'error': 'At least one admin account must remain.'}), 400
    user.role = role
    db.session.commit()
    return jsonify(user.to_dict())

@app.route('/api/users/<int:user_id>/reset-password', methods=['POST'])
@require_role('admin')
def reset_user_password(user_id):
    user = User.query.get_or_404(user_id)
    data = request.get_json(force=True) or {}
    password = data.get('password') or ''
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters.'}), 400
    user.set_password(password)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@require_role('admin')
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == session.get('user_id'):
        return jsonify({'error': "You can't delete your own account while logged in."}), 400
    if user.role == 'admin':
        remaining_admins = User.query.filter(User.role == 'admin', User.id != user.id).count()
        if remaining_admins == 0:
            return jsonify({'error': 'At least one admin account must remain.'}), 400
    db.session.delete(user)
    db.session.commit()
    return jsonify({'ok': True})


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    # Preserve real HTTP errors (404 above, explicit abort(400) calls,
    # etc.) - only unexpected/unhandled exceptions fall through to here.
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify({'error': e.description or e.name}), e.code
    app.logger.exception('Unhandled exception')
    return jsonify({'error': f'Unexpected server error: {e}'}), 500

# ---------- MODELS ----------
# Company now lives in models.py (imported above), alongside the new
# FinancialPeriod/FinancialStatement/FinancialLineItem/etc. schema.
#
# Financials (below) is kept here on purpose, unchanged, as the old flat
# table - it's your rollback path while the new schema settles in. Once
# you're confident in the migrated data (Phase 2) and the new endpoints,
# this class - and everything that reads from it - can be retired.

class Financials(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    period = db.Column(db.String(20))
    currency = db.Column(db.String(10), default='KES')
    revenue = db.Column(db.Float)
    net_income = db.Column(db.Float)
    total_assets = db.Column(db.Float)
    total_liabilities = db.Column(db.Float)
    total_equity = db.Column(db.Float)
    source = db.Column(db.String(20), default='manual')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        margin = (self.net_income / self.revenue * 100) if self.revenue else None
        roe = (self.net_income / self.total_equity * 100) if self.total_equity else None
        debt_equity = (self.total_liabilities / self.total_equity) if self.total_equity else None
        score = calc_financial_score(margin, roe, debt_equity)
        return {
            'id': self.id, 'period': self.period, 'currency': self.currency,
            'revenue': self.revenue, 'net_income': self.net_income,
            'total_assets': self.total_assets, 'total_liabilities': self.total_liabilities,
            'total_equity': self.total_equity, 'source': self.source,
            'net_margin': margin, 'roe': roe, 'debt_equity': debt_equity,
            'financial_score': score, 'score_band': score_band(score)
        }

def latest_financials(company_id):
    return Financials.query.filter_by(company_id=company_id).order_by(Financials.period.desc()).first()

def latest_two_financials(company_id):
    """Latest and prior-period Financials rows for one company (prior may be None)."""
    rows = Financials.query.filter_by(company_id=company_id).order_by(Financials.period.desc()).limit(2).all()
    latest = rows[0] if len(rows) > 0 else None
    prior = rows[1] if len(rows) > 1 else None
    return latest, prior

def latest_three_financials(company_id):
    """Up to 3 most recent Financials rows, newest first (missing slots are None)."""
    rows = Financials.query.filter_by(company_id=company_id).order_by(Financials.period.desc()).limit(3).all()
    rows = rows + [None] * (3 - len(rows))
    return rows[0], rows[1], rows[2]

def pct_change(new, old):
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old) * 100

def extract_year(period_label):
    """Best-effort year extraction from a free-text period label like
    'FY2025', 'H1 2025', 'Q2 2025 6M'. Manual entry means format isn't
    strictly controlled, so this just grabs the first 4-digit number
    that looks like a year."""
    if not period_label:
        return None
    m = re.search(r'(19|20)\d{2}', period_label)
    return int(m.group(0)) if m else None

# ---------- FinancialPeriod -> legacy-Financials-shaped view ----------
# The Intelligence Report (and anything else built against the old flat
# Financials.to_dict() shape - assess_company_risks/opportunity, peer
# ranking, etc.) shouldn't have to change just because the underlying
# numbers now come from the richer FinancialPeriod/FinancialLineItem/
# CalculatedMetric tables instead of the old 5-field table. This wraps
# one FinancialPeriod so it can be dropped in wherever a Financials row
# used to be passed - same .to_dict() shape, same attribute names - but
# every value is read live from the normalized schema (whatever the PDF
# parser actually captured, via ratios.py's already-computed
# CalculatedMetric rows), not from the old table's dual-write.

def _line_item_amount(period, normalized_name):
    for stmt in period.statements:
        for li in stmt.line_items:
            found = _line_item_amount_in_tree(li, normalized_name)
            if found is not None:
                return found
    return None

def _line_item_amount_in_tree(li, normalized_name):
    if li.normalized_name == normalized_name:
        return li.amount
    for child in li.children:
        found = _line_item_amount_in_tree(child, normalized_name)
        if found is not None:
            return found
    return None

class PeriodFinancialsView:
    """Duck-types the old Financials model closely enough to be used
    anywhere one was: same attributes, same to_dict() shape."""
    def __init__(self, period):
        self.period_obj = period
        self.id = period.id
        self.period = period.period_label
        self.currency = period.currency
        self.revenue = _line_item_amount(period, 'revenue')
        self.net_income = _line_item_amount(period, 'net_income')
        self.total_assets = _line_item_amount(period, 'total_assets')
        self.total_liabilities = _line_item_amount(period, 'total_liabilities')
        self.total_equity = _line_item_amount(period, 'total_equity')
        self.gross_profit = _line_item_amount(period, 'gross_profit')
        self.operating_profit = _line_item_amount(period, 'operating_profit')
        self.shares_outstanding = _line_item_amount(period, 'shares_outstanding')
        metrics = {m.metric_name: m.value for m in period.calculated_metrics}
        self._net_margin = metrics.get('net_margin')
        self._roe = metrics.get('roe')
        self._roa = metrics.get('roa')
        self._debt_equity = metrics.get('debt_equity')
        # gross_margin/operating_margin/eps: only present in
        # CalculatedMetric when ratios.py actually had the underlying
        # line item to compute them from (see calculate_ratios) - stay
        # None (never estimated) otherwise, same "honest absence" rule
        # the rest of this view already follows for net_margin/roe/roa.
        self._gross_margin = metrics.get('gross_margin')
        self._operating_margin = metrics.get('operating_margin')
        self._eps = metrics.get('eps')
        self._current_ratio = metrics.get('current_ratio')
        self._interest_coverage = metrics.get('interest_coverage')
        self._net_debt_to_ebitda = metrics.get('net_debt_to_ebitda')
        self._ebitda = metrics.get('ebitda')
        self._free_cash_flow = metrics.get('free_cash_flow')
        self._ocf_margin = metrics.get('ocf_margin')
        self._roic = metrics.get('roic')
        has_source = any(s.source_document_id for s in period.statements)
        self.source = 'pdf_upload' if has_source else 'manual'

    def to_dict(self):
        net_margin = self._net_margin
        roe = self._roe
        debt_equity = self._debt_equity
        score = calc_financial_score(net_margin, roe, debt_equity)
        return {
            'id': self.id, 'period': self.period, 'currency': self.currency,
            'revenue': self.revenue, 'net_income': self.net_income,
            'total_assets': self.total_assets, 'total_liabilities': self.total_liabilities,
            'total_equity': self.total_equity, 'source': self.source,
            'gross_profit': self.gross_profit, 'operating_profit': self.operating_profit,
            'shares_outstanding': self.shares_outstanding,
            'net_margin': net_margin, 'roe': roe, 'roa': self._roa, 'debt_equity': debt_equity,
            'gross_margin': self._gross_margin, 'operating_margin': self._operating_margin,
            'eps': self._eps, 'current_ratio': self._current_ratio,
            'interest_coverage': self._interest_coverage,
            'net_debt_to_ebitda': self._net_debt_to_ebitda, 'ebitda': self._ebitda,
            'free_cash_flow': self._free_cash_flow, 'ocf_margin': self._ocf_margin,
            'roic': self._roic,
            'financial_score': score, 'score_band': score_band(score),
        }

def _ordered_periods(company_id):
    """All of a company's FinancialPeriods, newest fiscal year first -
    same ordering rule get_period_detail_view already uses, so the
    Intelligence Report and the Company Detail page never disagree about
    which period is 'latest'."""
    periods = FinancialPeriod.query.filter_by(company_id=company_id).all()
    return sorted(
        periods,
        key=lambda p: (p.fiscal_year if p.fiscal_year is not None else extract_year(p.period_label) or 0),
        reverse=True,
    )

def log_activity(company_id, action, detail=None, fiscal_year=None, actor=None):
    """Write one ActivityLogEntry row - called from the survey write
    paths that actually mutate data (see each call site). Deliberately
    swallows its own errors rather than ever failing the caller's real
    write: logging an activity is a courtesy for the Admin Dashboard's
    Recent Activities panel, not something that should turn a
    successful survey upload or field review into a 500 if, say, the
    activity_log_entries table hasn't been migrated in yet on some
    older deployment. db.session.commit() here is separate from the
    caller's own commit (already done by the time this runs at every
    call site), so a failure here can never roll back real data that
    was already saved."""
    try:
        db.session.add(ActivityLogEntry(
            company_id=company_id, fiscal_year=fiscal_year,
            action=action, detail=detail, actor=actor,
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()

def latest_period_view(company_id):
    periods = _ordered_periods(company_id)
    return PeriodFinancialsView(periods[0]) if periods else None

def bulk_financials_by_company(company_ids=None):
    """One query for every company's full financials history, instead of
    a separate query per company (the N+1 pattern latest_financials() /
    latest_two_financials() / latest_three_financials() have when called
    in a loop over many companies - each one is a single cheap query, but
    a portfolio-wide page that calls one of them per company turns into
    dozens-to-hundreds of round trips). Returns {company_id: [rows...]},
    each list sorted newest-period-first - same ordering
    (Financials.period.desc(), a string sort) the per-company helpers
    already use, so callers that switch to this get identical results,
    just without the extra round trips.
    """
    q = Financials.query
    if company_ids is not None:
        q = q.filter(Financials.company_id.in_(company_ids))
    rows = q.order_by(Financials.company_id, Financials.period.desc()).all()
    by_company = {}
    for r in rows:
        by_company.setdefault(r.company_id, []).append(r)
    return by_company

# ---------- PAGE ----------

@app.route('/')
def index():
    return render_template('index.html')

# ---------- API: OVERVIEW ----------

@app.route('/api/overview')
def overview_stats():
    companies = Company.query.all()
    total_companies = len(companies)
    fin_by_company = bulk_financials_by_company()  # 1 query total, was 1 per company before

    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    margins, roes, revenues, net_incomes, scores = [], [], [], [], []
    prior_margins, prior_roes = [], []
    revenue_growth_pcts = []
    prior_revenue_growth_pcts = []
    latest_revenue_sum, prior_revenue_sum = 0.0, 0.0
    latest_income_sum, prior_income_sum = 0.0, 0.0
    has_prior_revenue, has_prior_income = False, False
    companies_with_data = 0
    companies_added_this_month = 0
    companies_updated_this_month = 0
    high_risk_added_this_month = 0
    performers = []
    health_counts = {'strong': 0, 'moderate': 0, 'weak': 0}
    latest_updated = None

    for c in companies:
        if c.created_at and c.created_at >= month_start:
            companies_added_this_month += 1

        rows_desc = fin_by_company.get(c.id, [])  # newest-period-first
        latest = rows_desc[0] if rows_desc else None
        prior = rows_desc[1] if len(rows_desc) > 1 else None
        two_back = rows_desc[2] if len(rows_desc) > 2 else None
        if latest:
            d = latest.to_dict()
            companies_with_data += 1
            if latest.updated_at and latest.updated_at >= month_start:
                companies_updated_this_month += 1
            if d['net_margin'] is not None:
                margins.append(d['net_margin'])
            if d['roe'] is not None:
                roes.append(d['roe'])
            if d['revenue']:
                revenues.append(d['revenue'])
                latest_revenue_sum += d['revenue']
            if d['net_income']:
                net_incomes.append(d['net_income'])
                latest_income_sum += d['net_income']
            if d['financial_score'] is not None:
                scores.append(d['financial_score'])
                health_counts[d['score_band']] += 1
                if d['score_band'] == 'weak' and latest.updated_at and latest.updated_at >= month_start:
                    high_risk_added_this_month += 1
            performers.append({
                'id': c.id, 'name': c.name, 'ticker': c.ticker,
                'sector': c.sector, 'period': d['period'],
                'net_margin': d['net_margin'], 'roe': d['roe'],
                'financial_score': d['financial_score']
            })
            if latest.updated_at and (latest_updated is None or latest.updated_at > latest_updated):
                latest_updated = latest.updated_at

            if prior:
                pd = prior.to_dict()
                if pd['revenue']:
                    prior_revenue_sum += pd['revenue']
                    has_prior_revenue = True
                if pd['net_income']:
                    prior_income_sum += pd['net_income']
                    has_prior_income = True
                if pd['net_margin'] is not None:
                    prior_margins.append(pd['net_margin'])
                if pd['roe'] is not None:
                    prior_roes.append(pd['roe'])
                g = pct_change(d['revenue'], pd['revenue'])
                if g is not None:
                    revenue_growth_pcts.append(g)
                if two_back:
                    tbd = two_back.to_dict()
                    pg = pct_change(pd['revenue'], tbd['revenue'])
                    if pg is not None:
                        prior_revenue_growth_pcts.append(pg)

    top_performers = sorted(
        [p for p in performers if p['financial_score'] is not None],
        key=lambda p: p['financial_score'], reverse=True
    )[:5]

    activity = []
    for c in companies:
        if c.created_at:
            activity.append({'type': 'company_added', 'label': f'{c.name} added to portfolio', 'at': c.created_at})
    company_by_id = {c.id: c for c in companies}  # reuse the list already fetched above, no extra query
    recent_financials = Financials.query.order_by(Financials.updated_at.desc()).limit(10).all()
    for f in recent_financials:
        comp = company_by_id.get(f.company_id)
        if comp:
            activity.append({'type': 'financials_added', 'label': f'{comp.name} {f.period} financials added', 'at': f.updated_at})
    activity.sort(key=lambda a: a['at'] or datetime.min, reverse=True)
    activity = activity[:8]
    for a in activity:
        a['at'] = a['at'].isoformat() if a['at'] else None

    avg_net_margin = (sum(margins) / len(margins)) if margins else None
    avg_roe = (sum(roes) / len(roes)) if roes else None
    avg_prior_margin = (sum(prior_margins) / len(prior_margins)) if prior_margins else None
    avg_prior_roe = (sum(prior_roes) / len(prior_roes)) if prior_roes else None
    avg_revenue_growth = (sum(revenue_growth_pcts) / len(revenue_growth_pcts)) if revenue_growth_pcts else None
    avg_prior_revenue_growth = (sum(prior_revenue_growth_pcts) / len(prior_revenue_growth_pcts)) if prior_revenue_growth_pcts else None

    return jsonify({
        'total_companies': total_companies,
        'companies_with_data': companies_with_data,
        'combined_revenue': sum(revenues) if revenues else None,
        'combined_net_income': sum(net_incomes) if net_incomes else None,
        'avg_net_margin': avg_net_margin,
        'avg_roe': avg_roe,
        'avg_financial_score': (sum(scores) / len(scores)) if scores else None,
        'avg_revenue_growth': avg_revenue_growth,
        'health_distribution': health_counts,
        'top_performers': top_performers,
        'recent_activity': activity,
        'last_updated': latest_updated.isoformat() if latest_updated else None,
        'trends': {
            'companies_added_this_month': companies_added_this_month,
            'companies_updated_this_month': companies_updated_this_month,
            'companies_updated_pct_of_total': (companies_with_data / total_companies * 100) if total_companies else None,
            'combined_revenue_yoy': pct_change(latest_revenue_sum, prior_revenue_sum) if has_prior_revenue else None,
            'combined_income_yoy': pct_change(latest_income_sum, prior_income_sum) if has_prior_income else None,
            # margin/ROE/growth deltas are in percentage points (current avg minus prior avg), not % change
            'net_margin_delta': (avg_net_margin - avg_prior_margin) if (avg_net_margin is not None and avg_prior_margin is not None) else None,
            'roe_delta': (avg_roe - avg_prior_roe) if (avg_roe is not None and avg_prior_roe is not None) else None,
            'revenue_growth_delta': (avg_revenue_growth - avg_prior_revenue_growth) if (avg_revenue_growth is not None and avg_prior_revenue_growth is not None) else None,
            'high_risk_added_this_month': high_risk_added_this_month,
        }
    })

def assess_company_risks(c, latest, prior):
    """Risk candidates for one company: (severity_rank, issue_label, metric_label).
    severity_rank: 3=High, 2=Medium. Built only from fields the schema
    actually captures (revenue, net income, debt/equity) - no liquidity
    data exists here, so a "Low Liquidity" issue type is intentionally
    not produced; adding it would mean fabricating a number."""
    d = latest.to_dict()
    candidates = []

    if d['debt_equity'] is not None:
        if d['debt_equity'] > 3.0:
            candidates.append((3, 'High Debt', f"Debt/Equity: {round(d['debt_equity'],1)}"))
        elif d['debt_equity'] > 2.0:
            candidates.append((2, 'High Debt', f"Debt/Equity: {round(d['debt_equity'],1)}"))

    if prior:
        pd = prior.to_dict()
        income_change = pct_change(d['net_income'], pd['net_income'])
        if income_change is not None and income_change < 0:
            sev = 3 if income_change < -20 else 2
            candidates.append((sev, 'Falling Profit', f"Net Profit ↓ {round(abs(income_change))}%"))

        revenue_change = pct_change(d['revenue'], pd['revenue'])
        if revenue_change is not None and revenue_change < 0:
            sev = 3 if revenue_change < -15 else 2
            candidates.append((sev, 'Revenue Decline', f"Revenue ↓ {round(abs(revenue_change))}%"))

    if not candidates and d['score_band'] == 'weak':
        candidates.append((2, 'Weak Financial Health', f"Score: {d['financial_score']}/100"))

    return candidates

def assess_company_opportunity(c, latest, prior):
    """A single positive-signal candidate for one company, or None.
    Only fires on real YoY revenue growth vs the prior period - no
    forward-looking projection, since the model has no basis for one."""
    if not prior:
        return None
    d, pd = latest.to_dict(), prior.to_dict()
    revenue_change = pct_change(d['revenue'], pd['revenue'])
    if revenue_change is not None and revenue_change > 20:
        return (f"Revenue ↑ {round(revenue_change)}%",)
    return None

# Risk categories a real annual filing's numbers can actually speak to
# vs ones that genuinely can't be scored from financial statements alone
# (regulatory change, competitive intensity, technology/cyber exposure,
# ESG) - those require qualitative/external research this app has no
# data source for, so they're listed as categories with score=None
# ('Not available') rather than a fabricated number. Only 'financial'
# and 'operational' (to the extent operational risk shows up as
# liquidity/leverage strain) get a real computed score here.
RISK_CATEGORY_LABELS = {
    'financial': 'Financial',
    'operational': 'Operational',
    'regulatory': 'Regulatory & Compliance',
    'competitive': 'Competitive',
    'technology': 'Technology & Innovation',
    'strategic': 'Strategic',
    'esg': 'ESG & Reputational',
}

def _risk_band(score):
    if score is None:
        return None
    if score <= 20:
        return 'Very Low Risk'
    if score <= 40:
        return 'Low Risk'
    if score <= 60:
        return 'Moderate Risk'
    if score <= 80:
        return 'High Risk'
    return 'Very High Risk'

def compute_financial_risk_score(latest_dict, prior_dict):
    """0-100 risk score (higher = riskier) built ONLY from ratios this
    period's own filed figures support: debt/equity, current ratio,
    interest coverage, and net margin direction vs prior period. Any
    input that's missing simply doesn't contribute to the blend (weights
    renormalize over whatever's actually present) rather than being
    treated as a bad value - a company with an unusually clean balance
    sheet that omits, say, a current-liabilities breakdown shouldn't be
    penalized for a gap in what got extracted from its filing."""
    components = []  # (weight, risk_0_100)

    de = latest_dict.get('debt_equity')
    if de is not None:
        # 0 at D/E=0, 100 at D/E>=4 - linear, capped
        components.append((0.35, max(0, min(100, de / 4 * 100))))

    cr = latest_dict.get('current_ratio')
    if cr is not None:
        # Risk falls as current ratio rises above 1; a ratio below 1
        # (current liabilities exceed current assets) is treated as
        # maximum risk on this component.
        components.append((0.25, max(0, min(100, (1.5 - min(cr, 1.5)) / 1.5 * 100))))

    ic = latest_dict.get('interest_coverage')
    if ic is not None:
        # Coverage of 8x+ treated as effectively riskless on this
        # component; below 1x (can't cover interest from operating
        # profit) treated as maximum risk.
        components.append((0.25, max(0, min(100, (8 - min(ic, 8)) / 8 * 100))))

    if prior_dict and latest_dict.get('net_margin') is not None and prior_dict.get('net_margin') is not None:
        margin_delta = latest_dict['net_margin'] - prior_dict['net_margin']
        # A margin that narrowed meaningfully raises risk; one that held
        # or improved doesn't add risk on this component (floored at 0).
        components.append((0.15, max(0, min(100, -margin_delta * 10))))

    if not components:
        return None
    total_weight = sum(w for w, _ in components)
    return round(sum(w * s for w, s in components) / total_weight, 0)

@app.route('/api/overview/charts')
def overview_charts():
    companies = Company.query.all()
    fin_by_company = bulk_financials_by_company()  # 1 query total, was up to 3 per company before

    # ---- Revenue Growth Trend: combined revenue by year, YoY growth % ----
    # Year is extracted from each Financials row's free-text period label.
    # Manual entry means periods aren't strictly standardized, so where a
    # company has more than one statement tagged with the same year, the
    # most recently updated one is used (avoids double-counting a year).
    year_revenue = {}   # year -> {company_id -> revenue}
    for c in companies:
        rows = sorted(fin_by_company.get(c.id, []), key=lambda f: f.updated_at or datetime.min, reverse=True)
        seen_years = set()
        for f in rows:
            yr = extract_year(f.period)
            if yr is None or yr in seen_years or not f.revenue:
                continue
            seen_years.add(yr)
            year_revenue.setdefault(yr, {})[c.id] = f.revenue

    years_sorted = sorted(year_revenue.keys())[-4:]  # last 4 years with any data
    revenue_trend = []
    prev_total = None
    for yr in years_sorted:
        total = sum(year_revenue[yr].values())
        revenue_trend.append({
            'year': yr,
            'combined_revenue': total,
            'growth_pct': pct_change(total, prev_total) if prev_total is not None else None
        })
        prev_total = total

    # ---- Profitability Comparison: top 5 companies by net margin ----
    profitability = []
    for c in companies:
        rows_desc = fin_by_company.get(c.id, [])
        if rows_desc:
            d = rows_desc[0].to_dict()
            if d['net_margin'] is not None:
                profitability.append({'name': c.name, 'ticker': c.ticker, 'net_margin': d['net_margin']})
    profitability.sort(key=lambda p: p['net_margin'], reverse=True)
    profitability = profitability[:5]

    # ---- Sector Performance: avg financial score by sector ----
    sector_scores = {}  # sector -> list of scores
    for c in companies:
        rows_desc = fin_by_company.get(c.id, [])
        if rows_desc:
            d = rows_desc[0].to_dict()
            if d['financial_score'] is not None:
                sector = c.sector or 'Unclassified'
                sector_scores.setdefault(sector, []).append(d['financial_score'])
    sector_performance = sorted(
        [{'sector': s, 'avg_score': round(sum(v) / len(v))} for s, v in sector_scores.items()],
        key=lambda x: x['avg_score'], reverse=True
    )[:8]

    return jsonify({
        'revenue_trend': revenue_trend,
        'profitability': profitability,
        'sector_performance': sector_performance,
    })

@app.route('/api/overview/attention')
def overview_attention():
    companies = Company.query.all()

    # ---- Companies Requiring Attention ----
    # Built only from data the model actually captures (revenue, net income,
    # debt/equity). There's no liquidity data in this schema (no current
    # assets/liabilities), so a "Low Liquidity" issue type is intentionally
    # not included here - it would have to be fabricated.
    issues = []
    for c in companies:
        latest, prior = latest_two_financials(c.id)
        if not latest:
            continue
        candidates = assess_company_risks(c, latest, prior)

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            sev_rank, issue, metric = candidates[0]
            issues.append({
                'company_id': c.id, 'company_name': c.name,
                'issue': issue, 'metric': metric,
                'severity': 'High' if sev_rank == 3 else 'Medium',
                'last_updated': latest.updated_at.isoformat() if latest.updated_at else None,
            })

    severity_order = {'High': 0, 'Medium': 1}
    issues.sort(key=lambda i: (severity_order.get(i['severity'], 2),
                                i['last_updated'] or ''), reverse=False)

    # ---- Data Summary ----
    all_financials = Financials.query.all()
    total_statements = len(all_financials)
    quarterly = 0
    for f in all_financials:
        label = (f.period or '').upper()
        if re.search(r'\bQ[1-4]\b', label) or re.search(r'\b[369]M\b', label):
            quarterly += 1
    annual = total_statements - quarterly
    data_sources = len({f.source for f in all_financials if f.source})

    return jsonify({
        'issues': issues[:8],
        'data_summary': {
            'total_statements': total_statements,
            'quarterly_reports': quarterly,
            'annual_reports': annual,
            'kpis_tracked': 4,  # net margin, ROE, debt/equity, financial score
            'data_sources': data_sources,
        }
    })

ALERT_TITLES = {
    'High Debt': 'High Debt Alert',
    'Falling Profit': 'Profit Declining',
    'Revenue Decline': 'Revenue Slowing',
    'Weak Financial Health': 'Weak Financial Health',
}

def _compute_alert_feed():
    """Shared by /api/overview/alerts (top-4 widget) and /api/alerts (the
    full Alerts page) so both always agree on what counts as a risk or
    an opportunity - same detection logic, just different amounts shown."""
    companies = Company.query.all()
    risk_items, opportunity_items = [], []

    for c in companies:
        latest, prior = latest_two_financials(c.id)
        if not latest:
            continue

        risks = assess_company_risks(c, latest, prior)
        if risks:
            risks.sort(key=lambda x: x[0], reverse=True)
            sev_rank, issue, metric = risks[0]
            risk_items.append({
                'kind': 'risk',
                'title': ALERT_TITLES.get(issue, issue),
                'company_id': c.id, 'company_name': c.name,
                'metric': metric,
                'severity': 'High' if sev_rank == 3 else 'Medium',
                'last_updated': latest.updated_at.isoformat() if latest.updated_at else None,
            })

        opp = assess_company_opportunity(c, latest, prior)
        if opp:
            (metric,) = opp
            opportunity_items.append({
                'kind': 'opportunity',
                'title': 'Strong Opportunity',
                'company_id': c.id, 'company_name': c.name,
                'metric': metric,
                'severity': 'Positive',
                'last_updated': latest.updated_at.isoformat() if latest.updated_at else None,
            })

    risk_items.sort(key=lambda i: (0 if i['severity'] == 'High' else 1, i['last_updated'] or ''))
    opportunity_items.sort(key=lambda i: i['last_updated'] or '', reverse=True)
    return risk_items, opportunity_items

@app.route('/api/overview/alerts')
def overview_alerts():
    """Alerts & Opportunities widget on Overview: a short, mixed feed
    capped at 4 items - lead with up to 3 risks, fill the rest with
    opportunities. For the full, uncapped list see /api/alerts."""
    risk_items, opportunity_items = _compute_alert_feed()
    alerts = risk_items[:3] + opportunity_items[:max(0, 4 - min(3, len(risk_items)))]
    return jsonify({'alerts': alerts[:4]})

@app.route('/api/alerts')
def all_alerts():
    """Full Alerts page: every risk and every opportunity currently
    detected, not just the top 4 shown on Overview."""
    risk_items, opportunity_items = _compute_alert_feed()
    return jsonify({
        'risks': risk_items,
        'opportunities': opportunity_items,
        'risk_count': len(risk_items),
        'opportunity_count': len(opportunity_items),
    })

# ---------- API: COMPANIES ----------

@app.route('/api/companies', methods=['GET'])
def list_companies():
    sort = request.args.get('sort', 'name')
    companies = Company.query.all()
    fin_by_company = bulk_financials_by_company()  # 1 query total, was 1-3 per company before

    result = []
    for c in companies:
        d = c.to_dict()
        rows_desc = fin_by_company.get(c.id, [])  # already newest-period-first
        latest = rows_desc[0] if rows_desc else None
        prior = rows_desc[1] if len(rows_desc) > 1 else None

        latest_dict = latest.to_dict() if latest else None
        if latest_dict:
            prior_dict = prior.to_dict() if prior else None
            latest_dict['revenue_yoy'] = pct_change(latest_dict['revenue'], prior_dict['revenue']) if prior_dict else None
            latest_dict['net_income_yoy'] = pct_change(latest_dict['net_income'], prior_dict['net_income']) if prior_dict else None
            latest_dict['net_margin_delta'] = (
                latest_dict['net_margin'] - prior_dict['net_margin']
                if prior_dict and latest_dict['net_margin'] is not None and prior_dict['net_margin'] is not None else None
            )
            latest_dict['roe_delta'] = (
                latest_dict['roe'] - prior_dict['roe']
                if prior_dict and latest_dict['roe'] is not None and prior_dict['roe'] is not None else None
            )
            # rows_desc is newest-first; reverse for oldest-first trend, same as the old .period.asc() query
            trend_rows_asc = list(reversed(rows_desc))
            latest_dict['score_trend'] = [r.to_dict()['financial_score'] for r in trend_rows_asc[-8:]]
        d['latest'] = latest_dict
        result.append(d)

    if sort == 'score':
        result.sort(key=lambda r: (r['latest']['financial_score'] if r['latest'] and r['latest']['financial_score'] is not None else -1), reverse=True)
    elif sort == 'revenue':
        result.sort(key=lambda r: (r['latest']['revenue'] if r['latest'] and r['latest']['revenue'] is not None else -1), reverse=True)
    else:
        result.sort(key=lambda r: r['name'].lower())

    return jsonify(result)

@app.route('/api/sectors')
def list_sectors():
    """Sector-level rollup for the Sectors page: per-sector company count,
    combined revenue, revenue weight vs the whole tracked portfolio, and
    averages of the same growth/margin/score figures already computed for
    every company on the Companies page (bulk_financials_by_company +
    pct_change, same as list_companies() above) - so a sector's numbers
    are always consistent with what its member companies show
    individually, never a separately-derived figure that could disagree."""
    companies = Company.query.filter(Company.sector.isnot(None), Company.sector != '').all()
    fin_by_company = bulk_financials_by_company([c.id for c in companies])  # 1 query total

    by_sector = {}
    for c in companies:
        rows_desc = fin_by_company.get(c.id, [])
        latest = rows_desc[0] if rows_desc else None
        prior = rows_desc[1] if len(rows_desc) > 1 else None
        if latest is None:
            continue
        ld = latest.to_dict()
        pd_ = prior.to_dict() if prior else None
        bucket = by_sector.setdefault(c.sector, {
            'revenues': [], 'revenue_yoys': [], 'net_margins': [], 'scores': [], 'company_count': 0,
        })
        bucket['company_count'] += 1
        if ld['revenue'] is not None:
            bucket['revenues'].append(ld['revenue'])
        if pd_ is not None:
            yoy = pct_change(ld['revenue'], pd_['revenue'])
            if yoy is not None:
                bucket['revenue_yoys'].append(yoy)
        if ld['net_margin'] is not None:
            bucket['net_margins'].append(ld['net_margin'])
        if ld['financial_score'] is not None:
            bucket['scores'].append(ld['financial_score'])

    def avg(vals):
        return round(sum(vals) / len(vals), 2) if vals else None

    total_revenue_all = sum(sum(b['revenues']) for b in by_sector.values())

    sectors = []
    for sector, b in by_sector.items():
        sector_revenue = sum(b['revenues'])
        sectors.append({
            'sector': sector,
            'company_count': b['company_count'],
            'total_revenue': sector_revenue if b['revenues'] else None,
            'weight_pct': round(sector_revenue / total_revenue_all * 100, 1) if total_revenue_all else None,
            'avg_revenue_growth': avg(b['revenue_yoys']),
            'avg_net_margin': avg(b['net_margins']),
            'avg_financial_score': round(avg(b['scores'])) if b['scores'] else None,
        })
    sectors.sort(key=lambda s: (s['total_revenue'] or 0), reverse=True)

    return jsonify({'sectors': sectors, 'total_revenue': total_revenue_all})

@app.route('/api/companies', methods=['POST'])
@require_role('admin')
def add_company():
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Company name is required'}), 400
    c = Company(
        name=name,
        ticker=(data.get('ticker') or '').strip(),
        exchange=(data.get('exchange') or '').strip(),
        sector=(data.get('sector') or '').strip(),
        country=(data.get('country') or '').strip()
    )
    db.session.add(c)
    db.session.commit()
    return jsonify(c.to_dict()), 201

@app.route('/api/companies/<int:company_id>', methods=['GET'])
def get_company(company_id):
    c = Company.query.get_or_404(company_id)
    financials = Financials.query.filter_by(company_id=company_id).order_by(Financials.period.desc()).all()
    result = c.to_dict()
    result['financials'] = [f.to_dict() for f in financials]
    return jsonify(result)

@app.route('/api/companies/<int:company_id>', methods=['DELETE'])
@require_role('admin')
def delete_company(company_id):
    """Deletes a company and everything that hangs off it.

    Company.periods and Company.source_documents both cascade
    ('all, delete-orphan') in models.py, so FinancialPeriod ->
    FinancialStatement -> FinancialLineItem (and segments/notes/
    operational_metrics/calculated_metrics) are removed automatically by
    the ORM when the Company row is deleted.

    Two things live OUTSIDE that relationship graph and need cleaning up
    by hand, or they'd be left as orphaned rows pointing at a company_id
    that no longer exists:
      - Financials: the old flat legacy table (kept in app.py on purpose,
        see its class docstring) - no relationship/cascade is defined on
        it at all.
      - ImportJob: has a company_id FK but no relationship/backref was
        added in models.py, so SQLAlchemy won't touch it automatically.
    A further four (DirectorRemunerationRow, MarketDataSnapshot,
    PrincipalRisk, ManagementGuidance) each have a REAL foreign-key
    constraint on period_id (and source_document_id) with no
    ondelete=CASCADE and no ORM relationship either - each must be
    deleted before its parent FinancialPeriod rows or the database
    itself rejects the delete with an IntegrityError (this was a real
    bug: any company with saved remuneration/market-data/risk/guidance
    rows couldn't be deleted at all once those tables were added, until
    this was fixed - it surfaced to the user as "the delete button
    doesn't work").
    """
    c = Company.query.get_or_404(company_id)

    try:
        # Delete everything explicitly, in dependency order, instead of
        # relying on ORM cascade ordering across two separate relationship
        # trees (Company.periods and Company.source_documents). Those two
        # trees aren't linked by an ORM relationship even though
        # FinancialStatement.source_document_id is a real FK to
        # SourceDocument - so cascade-deleting both in the same flush can
        # hit a FK violation if statements aren't cleared before their
        # source documents are. Doing it by hand removes that risk.
        period_ids = [p.id for p in FinancialPeriod.query.filter_by(company_id=company_id)]
        if period_ids:
            statement_ids = [
                s.id for s in FinancialStatement.query.filter(
                    FinancialStatement.period_id.in_(period_ids)
                )
            ]
            if statement_ids:
                FinancialLineItem.query.filter(
                    FinancialLineItem.statement_id.in_(statement_ids)
                ).delete(synchronize_session=False)
                FinancialStatement.query.filter(
                    FinancialStatement.id.in_(statement_ids)
                ).delete(synchronize_session=False)

        for period_id in period_ids:
            CalculatedMetric.query.filter_by(period_id=period_id).delete(synchronize_session=False)
            FinancialSegment.query.filter_by(period_id=period_id).delete(synchronize_session=False)
            OperationalMetric.query.filter_by(period_id=period_id).delete(synchronize_session=False)
            FinancialNote.query.filter_by(period_id=period_id).delete(synchronize_session=False)
            # Every one of these four has a real FK to financial_periods.id
            # with no ondelete=CASCADE and no ORM relationship either -
            # each was added in a later session than this route, and none
            # of them were wired into its cleanup at the time, so a
            # company with any of this data saved (remuneration rows,
            # market data snapshot, principal risks, management guidance)
            # used to fail to delete at all with an uncaught IntegrityError
            # once FinancialPeriod rows were deleted out from under them
            # below - that's the delete-company-button-doesn't-work bug:
            # the request 500'd instead of silently doing nothing, but the
            # visible effect looked the same either way.
            DirectorRemunerationRow.query.filter_by(period_id=period_id).delete(synchronize_session=False)
            MarketDataSnapshot.query.filter_by(period_id=period_id).delete(synchronize_session=False)
            PrincipalRisk.query.filter_by(period_id=period_id).delete(synchronize_session=False)
            ManagementGuidance.query.filter_by(period_id=period_id).delete(synchronize_session=False)
            # Committee/CommitteeMember: added in a later session than this
            # route too (models.py), same period_id FK/no-cascade shape as
            # the four above - would hit the identical IntegrityError for
            # any company with committee data saved. The ORM relationship's
            # cascade='all, delete-orphan' (models.py) only fires on an
            # ORM-level session.delete(), never on this route's bulk
            # .delete() queries - so CommitteeMember still needs its own
            # explicit cleanup here before Committee, same as everywhere
            # else in this function.
            committee_ids = [comm.id for comm in Committee.query.filter_by(period_id=period_id)]
            if committee_ids:
                CommitteeMember.query.filter(CommitteeMember.committee_id.in_(committee_ids)).delete(synchronize_session=False)
            Committee.query.filter_by(period_id=period_id).delete(synchronize_session=False)

        FinancialPeriod.query.filter_by(company_id=company_id).delete(synchronize_session=False)
        ImportJob.query.filter_by(company_id=company_id).delete(synchronize_session=False)
        Financials.query.filter_by(company_id=company_id).delete(synchronize_session=False)
        SourceDocument.query.filter_by(company_id=company_id).delete(synchronize_session=False)

        # Survey-side tables (models_survey.py) - added in a later session
        # than this route and never wired into its cleanup, so a company
        # with ANY survey data (SurveyCompanyData row, named directors,
        # benefits, remuneration/governance policy answers, or a flagged
        # evidence conflict) hit the exact same class of bug documented
        # above: a real FK to company.id, nullable=False, no
        # ondelete=CASCADE, no ORM relationship - the delete 500'd with an
        # IntegrityError instead of silently doing nothing. All keyed
        # directly by company_id (none of them FK to each other), so order
        # among these seven doesn't matter, only that they run before the
        # Company row itself. ActivityLogEntry included for the same
        # reason (added this same session, same unindexed FK-only shape).
        SurveyDirector.query.filter_by(company_id=company_id).delete(synchronize_session=False)
        SurveyDirectorBenefit.query.filter_by(company_id=company_id).delete(synchronize_session=False)
        SurveyBenefitCategory.query.filter_by(company_id=company_id).delete(synchronize_session=False)
        RemunerationPolicy.query.filter_by(company_id=company_id).delete(synchronize_session=False)
        GovernancePolicy.query.filter_by(company_id=company_id).delete(synchronize_session=False)
        EvidenceConflict.query.filter_by(company_id=company_id).delete(synchronize_session=False)
        SurveyCompanyData.query.filter_by(company_id=company_id).delete(synchronize_session=False)
        ActivityLogEntry.query.filter_by(company_id=company_id).delete(synchronize_session=False)

        db.session.delete(c)
        db.session.commit()
        return jsonify({'deleted': company_id}), 200
    except Exception as e:
        db.session.rollback()
        app.logger.exception('Failed to delete company %s', company_id)
        return jsonify({'error': f'Delete failed: {e}'}), 500

# ---------- API: FINANCIALS ----------

@app.route('/api/companies/<int:company_id>/financials', methods=['POST'])
@require_role('admin')
def add_financials(company_id):
    Company.query.get_or_404(company_id)
    data = request.get_json(force=True) or {}

    def to_float(field):
        val = data.get(field)
        try:
            return float(val) if val not in (None, '') else 0.0
        except (ValueError, TypeError):
            return 0.0

    period = (data.get('period') or '').strip()
    if not period:
        return jsonify({'error': 'Period is required'}), 400

    f = Financials(
        company_id=company_id,
        period=period,
        currency=(data.get('currency') or 'KES').strip() or 'KES',
        revenue=to_float('revenue'),
        net_income=to_float('net_income'),
        total_assets=to_float('total_assets'),
        total_liabilities=to_float('total_liabilities'),
        total_equity=to_float('total_equity'),
        source='manual'
    )
    db.session.add(f)
    db.session.commit()
    return jsonify(f.to_dict()), 201

def _statements_for_column(statements_payload, column='current'):
    """The parser now carries both a current-period 'amount' and a
    comparative-period 'prior_amount' on every line item (see
    nse_pdf_parse.parse_financials_text's docstring). _save_one_import
    still expects the old single-'amount' shape (one figure per line,
    one period per call) - this projects the two-column parse output
    down to whichever column is requested, dropping any line that has
    nothing in that column (a line the parser only found one number for,
    or a synthetic derived line - e.g. total_liabilities - that could
    only be computed for one of the two periods). Used to save the
    current and prior period as two separate FinancialPeriod rows from
    a single parsed PDF, instead of silently discarding the comparative
    column the way a single /save call always did before."""
    key = 'amount' if column == 'current' else 'prior_amount'
    projected = {}
    for statement_type, statement_data in (statements_payload or {}).items():
        line_items = (statement_data or {}).get('line_items') or []
        kept = []
        for li in line_items:
            value = li.get(key)
            if value is None:
                continue
            new_li = dict(li)
            new_li['amount'] = value
            kept.append(new_li)
        if kept:
            projected[statement_type] = {'line_items': kept}
    return projected


def _save_current_and_prior_period(base_payload, statements_payload, period_label, prior_period_label):
    """Saves the current period from base_payload/statements_payload
    exactly as before, then - if a prior period was detected and the
    parsed statements actually have a comparative column for at least
    one line - saves that second period too, under the same company.
    Returns (current_result, current_status, prior_result_or_None,
    prior_status_or_None) so callers can report both without treating a
    prior-period save failure as fatal to the (already-successful)
    current-period save."""
    current_payload = dict(base_payload)
    current_payload['period'] = period_label
    current_payload['statements'] = _statements_for_column(statements_payload, 'current')
    current_result, current_status = _save_one_import(current_payload)

    prior_result, prior_status = None, None
    if prior_period_label and current_status == 201:
        prior_statements = _statements_for_column(statements_payload, 'prior')
        if prior_statements:
            prior_payload = dict(base_payload)
            prior_payload['period'] = prior_period_label
            prior_payload['statements'] = prior_statements
            # Reuse the same company the current-period save just
            # resolved/created, rather than re-running name/ticker
            # matching a second time (and risking it landing on a
            # different company row for some edge-case duplicate name).
            prior_payload['company_id'] = current_result.get('company_id')
            prior_payload.pop('company_name', None)
            prior_payload.pop('ticker', None)
            prior_payload.pop('sector', None)
            prior_result, prior_status = _save_one_import(prior_payload)

    return current_result, current_status, prior_result, prior_status


def compute_extraction_score(source_document_id):
    """Aggregate, honest per-document extraction-quality score (0-100) -
    the average of every real per-field/per-row confidence score the
    parser attached to data it pulled from this specific document
    (financial line items via their statement, plus every
    directly-linked confidence-bearing row: market data, principal
    risks, management guidance, director remuneration, committees,
    survey directors/benefits/categories). Never a fabricated number -
    returns None (not a fake 0 or 100) when this document has no
    confidence-bearing rows at all yet."""
    if not source_document_id:
        return None
    scores = []

    li_rows = db.session.query(FinancialLineItem.confidence).join(
        FinancialStatement, FinancialLineItem.statement_id == FinancialStatement.id
    ).filter(
        FinancialStatement.source_document_id == source_document_id,
        FinancialLineItem.confidence.isnot(None),
    ).all()
    scores.extend(r[0] for r in li_rows)

    for Model in (MarketDataSnapshot, PrincipalRisk, ManagementGuidance,
                  DirectorRemunerationRow, Committee,
                  SurveyDirector, SurveyDirectorBenefit, SurveyBenefitCategory):
        rows = Model.query.filter(
            Model.source_document_id == source_document_id,
            Model.confidence.isnot(None),
        ).all()
        scores.extend(r.confidence for r in rows)

    if not scores:
        return None
    avg = sum(scores) / len(scores)
    return round(max(0.0, min(1.0, avg)) * 100, 1)


def _finalize_source_document_score(source_document_id):
    """Computes and stores the score for one just-saved document,
    right after all of its confidence-bearing rows have been
    committed. Best-effort: a scoring failure should never turn an
    otherwise-successful upload into a failed one."""
    if not source_document_id:
        return None
    try:
        doc = SourceDocument.query.get(source_document_id)
        if doc is None:
            return None
        score = compute_extraction_score(source_document_id)
        doc.extraction_score = score
        doc.extraction_score_computed_at = datetime.utcnow()
        db.session.commit()
        return score
    except Exception:
        db.session.rollback()
        app.logger.exception(f'Extraction score computation failed for source_document_id={source_document_id} (non-fatal)')
        return None


def _save_one_import(data):
    """Shared save logic behind /api/import/upload-batch (and, per file,
    the current/prior-period pair via _save_current_and_prior_period
    above). Saves parsed statements into the normalized schema - one
    FinancialPeriod, one FinancialStatement per statement type present,
    and a FinancialLineItem per parsed line - plus a matching flat
    `Financials` row (source='pdf_upload') so older routes that read that
    table (overview stats, some exports) keep working; see models.py's
    migration notes for why that table is kept around.

    Expected payload shape:
        {
          "company_id": 1,            # or company_name/ticker/sector to create one
          "period": "FY2025",
          "statements": {
            "income_statement": {"line_items": [{"label", "normalized_name",
                                                   "amount", "page", "confidence"}, ...]},
            "balance_sheet": {...}, "cash_flow": {...}, "equity": {...}
          }
        }

    Returns (result_dict, status_code) instead of a Flask response
    directly, so batch callers (the per-file loop in
    upload_documents_batch) can collect per-item results without each
    item needing its own HTTP round trip.
    """
    company_id = data.get('company_id')
    if not company_id:
        company_name = (data.get('company_name') or '').strip()
        ticker = (data.get('ticker') or '').strip()
        if not company_name:
            return {'error': 'company_id or company_name is required'}, 400

        # Reuse an existing Company instead of creating a duplicate row.
        # Match by ticker first (more reliable, unique per NSE listing),
        # then fall back to a case-insensitive name match.
        c = None
        if ticker:
            c = Company.query.filter(db.func.lower(Company.ticker) == ticker.lower()).first()
        if c is None:
            c = Company.query.filter(db.func.lower(Company.name) == company_name.lower()).first()

        if c is None:
            c = Company(
                name=company_name,
                ticker=ticker,
                exchange='NSE',
                sector=(data.get('sector') or '').strip(),
                country='Kenya'
            )
            db.session.add(c)
            db.session.commit()
        else:
            # Backfill metadata an older/earlier-created row is missing -
            # e.g. a company first added before sector matching existed,
            # or before this filing's match included a sector at all.
            # Never overwrites a value that's already set (this only fills
            # gaps, it doesn't treat the current save as more authoritative
            # than whatever's already there).
            changed = False
            if not (c.sector or '').strip() and (data.get('sector') or '').strip():
                c.sector = data['sector'].strip()
                changed = True
            if not (c.ticker or '').strip() and ticker:
                c.ticker = ticker
                changed = True
            if changed:
                db.session.commit()
        company_id = c.id
    else:
        if Company.query.get(company_id) is None:
            return {'error': f'company_id {company_id} not found'}, 404

    period_label = (data.get('period') or '').strip()
    if not period_label:
        return {'error': 'period is required'}, 400

    statements_payload = data.get('statements') or {}
    # A dict with statement-type keys but empty `line_items` lists (e.g.
    # {"income_statement": {"line_items": []}}) is truthy and used to slip
    # past this check, which let a FinancialPeriod get created (or an
    # existing one reused) with nothing actually saved to it - an "empty"
    # period that then shows up with a Financial Year selector but no
    # data and no ratios. Require at least one real line item, not just a
    # non-empty dict.
    has_line_items = any((s or {}).get('line_items') for s in statements_payload.values())
    if not statements_payload or not has_line_items:
        return {'error': 'statements (with at least one line item) is required'}, 400

    # Optional source document, so line items can point back to "which PDF,
    # which page" - created either from a pdf_url (the NSE scan/save flow)
    # or a filename + content hash (a directly uploaded file, which has no
    # URL of its own - see /api/import/upload-batch). Without this, a
    # directly-uploaded PDF's line items would keep their own page numbers
    # but the Company Detail page's "Source Document" panel and the
    # Intelligence Report's Source Evidence tab would have nothing to show,
    # even though the upload itself succeeded.
    source_doc_id = None
    pdf_url = (data.get('pdf_url') or '').strip()
    upload_filename = (data.get('source_filename') or '').strip()
    if pdf_url or upload_filename:
        # Re-uploading the same file for the same period reuses the
        # existing SourceDocument instead of stacking a new one each
        # time (sha256 was always stored "to dedupe re-uploads" but
        # nothing ever actually looked it up). Stale duplicate docs are
        # what made the Evidence panel list the same filing repeatedly
        # and let old bad-extraction docs keep feeding scores/renders.
        # Same file under a DIFFERENT period_label still gets its own
        # row on purpose, so the mis-tagged-year case stays visible.
        doc = None
        sha = data.get('source_sha256')
        if sha:
            doc = SourceDocument.query.filter_by(
                company_id=company_id, sha256=sha, period_label=period_label
            ).order_by(SourceDocument.id.desc()).first()
        if doc is None and pdf_url:
            # URL-sourced filings (NSE scan flow) carry no sha256
            doc = SourceDocument.query.filter_by(
                company_id=company_id, url=pdf_url, period_label=period_label
            ).order_by(SourceDocument.id.desc()).first()
        if doc is not None:
            doc.uploaded_at = datetime.utcnow()
            if data.get('source_page_count'):
                doc.page_count = data.get('source_page_count')
        else:
            doc = SourceDocument(
                company_id=company_id, url=pdf_url or None, filename=upload_filename or None,
                period_label=period_label, page_count=data.get('source_page_count'),
                sha256=sha,
            )
            db.session.add(doc)
        db.session.flush()
        source_doc_id = doc.id

    job = ImportJob(company_id=company_id, source_document_id=source_doc_id, status='pending')
    db.session.add(job)
    db.session.flush()

    try:
        period = FinancialPeriod.query.filter_by(
            company_id=company_id, period_label=period_label
        ).first()
        if period is None:
            period = FinancialPeriod(
                company_id=company_id, period_label=period_label,
                period_type='FY' if 'Q' not in period_label else period_label[:2],
                currency='KES',
            )
            db.session.add(period)
            db.session.flush()

        flat = {}  # collect the 5 legacy fields as we go, for the dual-write below

        for statement_type, statement_data in statements_payload.items():
            line_items = (statement_data or {}).get('line_items') or []
            if not line_items:
                continue

            stmt = period.statement(statement_type)
            if stmt is None:
                stmt = FinancialStatement(
                    period_id=period.id, statement_type=statement_type,
                    source_document_id=source_doc_id
                )
                db.session.add(stmt)
                db.session.flush()
            else:
                # Re-import of a period that already has this statement:
                # REPLACE its line items. Previously every re-import
                # appended a fresh copy on top of the old one (4 imports
                # = every line item 4x), which inflated the rendered
                # tables and skewed ratios. All callers save a whole
                # document's statement at once, so replacing is safe.
                FinancialLineItem.query.filter_by(statement_id=stmt.id).update(
                    {FinancialLineItem.parent_id: None}, synchronize_session=False)
                FinancialLineItem.query.filter_by(statement_id=stmt.id).delete(
                    synchronize_session=False)
                if source_doc_id:
                    stmt.source_document_id = source_doc_id

            for idx, li in enumerate(line_items):
                amount = li.get('amount')
                if amount in (None, ''):
                    continue
                try:
                    amount = float(amount)
                except (TypeError, ValueError):
                    continue
                normalized = (li.get('normalized_name') or '').strip() or None
                if not normalized:
                    # Manual entry (report builder, or a hand-added NSE
                    # review row) won't have this set by the parser -
                    # try to resolve it from the label text itself so
                    # ratios.py and the legacy dual-write below still work.
                    normalized = match_canonical_label(statement_type, li.get('label') or '')
                db.session.add(FinancialLineItem(
                    statement_id=stmt.id,
                    label=(li.get('label') or '').strip() or 'Unlabeled',
                    normalized_name=normalized,
                    section=li.get('section'),
                    amount=amount,
                    currency='KES',
                    page=li.get('page'),
                    confidence=li.get('confidence'),
                    order_index=li.get('order_index', idx),
                ))
                if normalized in ('revenue', 'net_income', 'total_assets',
                                  'total_liabilities', 'total_equity'):
                    flat[normalized] = amount

        db.session.commit()
        calculate_ratios(period)

        job.status = 'saved'
        job.finished_at = datetime.utcnow()
        db.session.commit()

    except Exception as e:
        db.session.rollback()
        job.status = 'failed'
        job.error_message = str(e)
        job.finished_at = datetime.utcnow()
        db.session.commit()
        return {'error': f'Save failed: {e}'}, 500

    # Dual-write: keep the old flat Financials table populated too, so
    # /api/overview and the pre-Phase-6 export routes keep working exactly
    # as before while those get upgraded to read from FinancialPeriod
    # directly.
    # Upsert (not insert): one legacy row per company+period, so a
    # re-import updates it instead of stacking duplicates.
    f = Financials.query.filter_by(company_id=company_id, period=period_label).order_by(Financials.id.desc()).first()
    if f is None:
        f = Financials(company_id=company_id, period=period_label)
        db.session.add(f)
    f.currency = 'KES'
    f.revenue = flat.get('revenue', 0.0)
    f.net_income = flat.get('net_income', 0.0)
    f.total_assets = flat.get('total_assets', 0.0)
    f.total_liabilities = flat.get('total_liabilities', 0.0)
    f.total_equity = flat.get('total_equity', 0.0)
    f.source = 'pdf_upload'
    f.updated_at = datetime.utcnow()
    db.session.commit()

    return {
        'company_id': company_id,
        'period': period.to_dict(),
        'financials': f.to_dict(),   # legacy shape, for any frontend code still reading it
        'source_document_id': source_doc_id,
    }, 201

# ---------- LIVE EVENTS / UPLOAD PROGRESS ----------
# The dashboard keeps one SSE connection open for notifications that data
# changed. Each request gets its own queue so a slow browser cannot block
# another subscriber or an upload-progress stream.
_event_subscribers = set()
_event_subscribers_lock = threading.Lock()

def _event_emit(event):
    """Broadcast a JSON-serializable event to connected dashboard clients."""
    with _event_subscribers_lock:
        subscribers = list(_event_subscribers)
    for subscriber in subscribers:
        subscriber.put(event)

@app.route('/api/events')
def event_stream():
    subscriber = queue.Queue()
    with _event_subscribers_lock:
        _event_subscribers.add(subscriber)

    def gen():
        try:
            yield ': connected\n\n'
            while True:
                try:
                    event = subscriber.get(timeout=25)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    # Keep proxies from closing an otherwise idle SSE stream.
                    yield ': keep-alive\n\n'
        finally:
            with _event_subscribers_lock:
                _event_subscribers.discard(subscriber)

    return Response(gen(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
    })

@app.route('/favicon.ico')
def favicon():
    # Keep the browser request valid without adding a binary asset to the
    # deployment. The same mark is used by the application shell.
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#4285f4"/>'
        '<path d="M18 16h28v32H18z" fill="#fff" opacity=".95"/>'
        '<path d="M24 24h16M24 31h16M24 38h10" stroke="#4285f4" '
        'stroke-width="4" stroke-linecap="round"/>'
        '</svg>'
    )
    return Response(svg, mimetype='image/svg+xml')

# ---------- UPLOAD PROGRESS (live, per-file backend stage log) ----------
# The frontend generates a batch_id per upload attempt, opens
# GET /api/upload-progress/<batch_id> (Server-Sent Events) right before
# POSTing the files, and the upload route below emits one real,
# timestamped event at each genuine stage boundary it actually reaches
# for each file (reading -> detecting format -> parsing -> matching
# company -> saving -> scoring -> done/failed), plus a final
# 'batch_done' event with the total elapsed time. Nothing here is
# simulated - every event fires exactly when that line of code runs.
_progress_channels = {}  # batch_id -> queue.Queue, one per in-flight upload

def _progress_emit(batch_id, event):
    if not batch_id:
        return
    event = {**event, 'ts': datetime.utcnow().isoformat()}
    q = _progress_channels.get(batch_id)
    if q is not None:
        q.put(event)

@app.route('/api/upload-progress/<batch_id>')
@require_role('admin')
def upload_progress_stream(batch_id):
    q = _progress_channels.setdefault(batch_id, queue.Queue())

    def gen():
        try:
            while True:
                event = q.get(timeout=120)  # upload can't legitimately hang longer than this
                yield f"data: {json.dumps(event)}\n\n"
                if event.get('type') == 'batch_done':
                    break
        except queue.Empty:
            yield f"data: {json.dumps({'type': 'timeout', 'ts': datetime.utcnow().isoformat()})}\n\n"
        finally:
            _progress_channels.pop(batch_id, None)

    return Response(gen(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
    })

@app.route('/api/import/upload-batch', methods=['POST'])
@require_role('admin')
def upload_documents_batch():
    """Upload several annual-report PDFs at once (multipart/form-data,
    repeated 'files' field) - any mix of companies and years in a single
    batch. Unlike /scan -> /parse -> /save, there's no NSE filing-title
    metadata to key off, so each file is self-identified from its own
    content: detect_company_name() + match_company() guess which company
    it belongs to, detect_period_label() guesses the fiscal year, and the
    result is parsed and saved in the same request - no review step.

    Because there's no human review gate here, low-confidence guesses go
    to the database exactly like high-confidence ones. What keeps this
    honest instead:
      - every saved line item still carries the parser's own per-item
        confidence score (unchanged from the reviewed flow) so low-trust
        numbers are still visibly low-trust in Source Evidence, they just
        weren't screened out before saving;
      - company/period identification failures are hard-reported per file
        (never silently guessed past a low match score) so a
        misidentified file shows up as a failure, not a wrong save;
      - every save still goes through the existing ImportJob audit trail,
        so 'what got auto-saved and when' is always reconstructable and
        reversible after the fact.

    Returns one result per uploaded file:
        {"results": [
            {"filename":..., "ok": true, "company_id":..., "company_name":...,
             "period":..., "match_score":..., "created_company": bool, ...}
            or
            {"filename":..., "ok": false, "error":...}
        ], "saved": <count>, "failed": <count>}
    """
    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': "No files uploaded (expected form field 'files', one entry per file)."}), 400
    if len(files) > 10:
        return jsonify({'error': f'Too many files ({len(files)}). Upload at most 10 at a time.'}), 400

    # Client-generated id for the matching GET /api/upload-progress/<batch_id>
    # SSE stream, opened by the frontend right before this POST. Optional -
    # an old client (or a direct API call) that doesn't send one just
    # doesn't get live progress; the upload itself works exactly the same.
    batch_id = request.form.get('batch_id')
    batch_started = datetime.utcnow()
    _progress_emit(batch_id, {'type': 'batch_start', 'file_count': len(files)})

    # Optional: called from a specific Company Detail page ("Upload
    # Documents" there, as opposed to the general multi-company import).
    # When present, every file in this batch is attributed to this one
    # company directly - detect_company_name()/match_company() are
    # skipped entirely, so a report with an unusual cover page (or one
    # for a subsidiary sharing a similar name) can never get attributed
    # to the wrong company. Only the fiscal year still needs detecting
    # per file.
    forced_company_id = request.form.get('company_id', type=int)
    forced_company = None
    if forced_company_id:
        forced_company = Company.query.get(forced_company_id)
        if forced_company is None:
            return jsonify({'error': f'company_id {forced_company_id} not found'}), 404

    existing_by_name = {c.name.strip().lower(): c.id for c in Company.query.all()}
    existing_by_ticker = {c.ticker.strip().lower(): c.id for c in Company.query.all() if c.ticker}

    results = []
    saved = 0
    for file in files:
        filename = file.filename or 'unnamed.pdf'
        file_started = datetime.utcnow()
        _progress_emit(batch_id, {'type': 'stage', 'filename': filename, 'stage': 'reading'})
        try:
            pdf_bytes = file.read()
        except Exception as e:
            _progress_emit(batch_id, {'type': 'file_failed', 'filename': filename, 'error': f'Could not read upload: {e}'})
            results.append({'filename': filename, 'ok': False, 'error': f'Could not read upload: {e}'})
            continue

        # Standard condensed filing (see condensed_format/SPEC.md) - a
        # plain-text alternative to a raw PDF that sidesteps every
        # layout quirk the PDF extractors below have to work around.
        # Detected by content (a "===COMPANY===" marker), not by file
        # extension, so a .txt OR a .pdf containing this format both
        # work - only decoded as UTF-8 text once that marker is found,
        # so a real PDF's binary bytes are never treated as this format.
        is_condensed = False
        is_survey_data = False
        try:
            sniff_text = pdf_bytes[:4096].decode('utf-8', errors='ignore')
            is_condensed = is_condensed_format(sniff_text)
            is_survey_data = is_survey_data_format(sniff_text)
        except Exception:
            pass
        _progress_emit(batch_id, {
            'type': 'stage', 'filename': filename, 'stage': 'detecting_format',
            'format': 'condensed' if is_condensed else ('survey_data' if is_survey_data else 'pdf'),
        })

        if is_survey_data:
            results.append({'filename': filename, 'ok': False,
                             'error': 'This is a Survey Data file (board/remuneration benchmark), not an '
                                      'annual report. Upload it from the Survey page instead.'})
            continue

        if is_condensed:
            _progress_emit(batch_id, {'type': 'stage', 'filename': filename, 'stage': 'parsing'})
            try:
                condensed_text = pdf_bytes.decode('utf-8')
                condensed = parse_condensed_filing(condensed_text)
            except (ValueError, UnicodeDecodeError) as e:
                results.append({'filename': filename, 'ok': False, 'error': f'Malformed condensed file: {e}'})
                continue

            period_label = condensed['period']['label']
            company_info = condensed['company']

            if forced_company:
                company_id = forced_company.id
                created_company = False
            else:
                company_id = (existing_by_ticker.get((company_info.get('ticker') or '').lower())
                               or existing_by_name.get((company_info.get('name') or '').strip().lower()))
                created_company = company_id is None

            if not condensed['statements']:
                results.append({
                    'filename': filename, 'ok': False,
                    'error': 'No recognized financial statement fields found in this condensed file '
                             '(check field labels against condensed_format/SPEC.md).',
                })
                continue

            save_payload = {
                'source_filename': filename,
                'source_page_count': None,
                'source_sha256': hashlib.sha256(pdf_bytes).hexdigest(),
            }
            if company_id:
                save_payload['company_id'] = company_id
            else:
                save_payload['company_name'] = company_info.get('name')
                save_payload['ticker'] = company_info.get('ticker')
                save_payload['sector'] = company_info.get('sector')

            # A condensed file describes exactly one period per file (no
            # "detect the prior period from a comparative column" step
            # needed - each year gets its own file) - but its statement
            # lines still carry a prior_amount for the SAME comparative
            # convention a PDF upload uses, so the current-period save
            # picks those up as this period's own YoY comparison the
            # same way. No second FinancialPeriod row is created for a
            # condensed file's own comparative column - the person is
            # expected to upload that prior year's own condensed file
            # separately for a full period row of its own, same
            # expectation as this whole feature already sets in
            # condensed_format/SPEC.md.
            try:
                _progress_emit(batch_id, {'type': 'stage', 'filename': filename, 'stage': 'saving'})
                result, status = _save_one_import({**save_payload, 'period': period_label, 'statements': condensed['statements']})
            except Exception as e:
                db.session.rollback()
                app.logger.exception(f'Unexpected error saving condensed file {filename}')
                _progress_emit(batch_id, {'type': 'file_failed', 'filename': filename, 'error': f'Could not save: {e}'})
                results.append({'filename': filename, 'ok': False, 'error': f'Could not save: {e}'})
                continue

            if status != 201:
                _progress_emit(batch_id, {'type': 'file_failed', 'filename': filename, 'error': result.get('error', 'Save failed.')})
                results.append({'filename': filename, 'ok': False, 'error': result.get('error', 'Save failed.')})
                continue

            saved += 1
            if result.get('company_id'):
                existing_by_name[(company_info.get('name') or '').strip().lower()] = result['company_id']
                if company_info.get('ticker'):
                    existing_by_ticker[company_info['ticker'].lower()] = result['company_id']

            period_row = FinancialPeriod.query.filter_by(
                company_id=result['company_id'], period_label=period_label
            ).first()
            director_remuneration_rows_parsed = len(condensed['director_remuneration'])
            director_remuneration_rows_saved = 0
            director_remuneration_save_error = None
            if period_row:
                if condensed['market_data']:
                    existing_md = MarketDataSnapshot.query.filter_by(period_id=period_row.id).first()
                    if existing_md is None:
                        existing_md = MarketDataSnapshot(period_id=period_row.id)
                        db.session.add(existing_md)
                    for field, value in condensed['market_data'].items():
                        setattr(existing_md, field, value)
                if condensed['management_guidance']:
                    ManagementGuidance.query.filter_by(period_id=period_row.id).delete()
                    for row in condensed['management_guidance']:
                        db.session.add(ManagementGuidance(period_id=period_row.id, **row))
                if condensed['principal_risks']:
                    PrincipalRisk.query.filter_by(period_id=period_row.id).delete()
                    for row in condensed['principal_risks']:
                        db.session.add(PrincipalRisk(period_id=period_row.id, **row))
                if condensed['director_remuneration']:
                    try:
                        DirectorRemunerationRow.query.filter_by(period_id=period_row.id).delete()
                        for row in condensed['director_remuneration']:
                            components = row.pop('components', None)
                            db.session.add(DirectorRemunerationRow(
                                period_id=period_row.id,
                                components=json.dumps(components) if components else None,
                                **{**row, 'source_document_id': result.get('source_document_id')},
                            ))
                        db.session.flush()  # surface any column/constraint error now, inside this try, instead of at the final commit where it would be harder to attribute to this specific step
                        director_remuneration_rows_saved = director_remuneration_rows_parsed
                    except Exception as e:
                        db.session.rollback()
                        app.logger.exception(f'Director remuneration save failed for {filename}')
                        director_remuneration_save_error = str(e)
                    # Also populate the single-figure "Total Director
                    # Remuneration - As Filed" summary (OperationalMetric,
                    # same metric_name/shape the real-PDF path writes via
                    # extract_director_remuneration - see that function's
                    # docstring and its "cluster_labeled_rows" comment) so
                    # a condensed file's total shows there too, not just
                    # in the per-director breakdown below it. Only when
                    # the section has EXACTLY ONE ROW, TOTAL - not "exactly
                    # one row named GRAND TOTAL". An earlier version of
                    # this check counted only is_grand_total rows, which
                    # is wrong: confirmed on KCB's real FY2025 filing, the
                    # section has a "GRAND TOTAL" row for Non-Executive
                    # Directors (97,395) PLUS two separately-named
                    # Executive Director rows with their own totals (Paul
                    # Russo 285,306; Lawrence Kimathi 147,772) - that
                    # earlier check saw exactly one is_grand_total row and
                    # would have written 97,395 as if it were the WHOLE
                    # company's director remuneration, silently dropping
                    # both executives' pay - a wrong total is worse than
                    # no total, since it looks authoritative. The real-PDF
                    # path avoids this exact trap by counting every
                    # labeled total row in the section, named "GRAND
                    # TOTAL" or not, and only trusting a single row when
                    # there's truly only one in the whole section - this
                    # must count the same way for the two paths to agree.
                    if len(condensed['director_remuneration']) == 1 and director_remuneration_save_error is None:
                        only_row = condensed['director_remuneration'][0]
                        existing_metric = OperationalMetric.query.filter_by(
                            period_id=period_row.id, metric_name='total_director_remuneration'
                        ).first()
                        if existing_metric:
                            existing_metric.value = only_row['total']
                        else:
                            db.session.add(OperationalMetric(
                                period_id=period_row.id,
                                metric_name='total_director_remuneration',
                                value=only_row['total'],
                                # Always Ksh '000 - see DirectorRemunerationRow.total's
                                # own docstring in models.py ("this row's own printed
                                # Total column, in Ksh '000 as filed"). NOT
                                # company_info.get('unit') - that's the unit the
                                # ===INCOME_STATEMENT===/===BALANCE_SHEET===/
                                # ===CASH_FLOW=== sections are stated in (millions,
                                # for a bank like this), but a filing's own Directors'
                                # Remuneration Report conventionally states its
                                # figures in thousands regardless of what unit the
                                # rest of the filing uses - confirmed against a real
                                # KCB filing ("Amounts in Kshs '000") - so this
                                # section is deliberately NOT unit-converted the way
                                # the statement sections are. A condensed file's own
                                # ===DIRECTOR_REMUNERATION=== values must always be
                                # entered in thousands to match - see
                                # CONDENSED_FORMAT_SPEC.md's note on this.
                                unit=company_info.get('currency', 'KES') + " thousands",
                            ))
                db.session.commit()

            _progress_emit(batch_id, {'type': 'stage', 'filename': filename, 'stage': 'scoring'})
            extraction_score = _finalize_source_document_score(result.get('source_document_id'))
            file_elapsed = (datetime.utcnow() - file_started).total_seconds()
            _progress_emit(batch_id, {
                'type': 'file_done', 'filename': filename, 'elapsed_seconds': round(file_elapsed, 1),
                'extraction_score': extraction_score,
            })

            results.append({
                'filename': filename, 'ok': True, 'company_id': result.get('company_id'),
                'company_name': company_info.get('name'), 'period': period_label,
                'match_score': 1.0 if company_id and not created_company else 0.0,
                'created_company': created_company, 'periods_saved': [period_label],
                'prior_period_error': None, 'format': 'condensed',
                # Diagnostics for the director-remuneration section
                # specifically, surfaced here rather than only in server
                # logs - added after a real case where the upload
                # reported full success while every remuneration row had
                # silently failed a step downstream of the main save, and
                # there was no way to tell from the dialog alone. 0/0
                # (parsed=0) just means the file's own
                # ===DIRECTOR_REMUNERATION=== section was empty or absent -
                # not an error.
                'director_remuneration_rows_parsed': director_remuneration_rows_parsed,
                'director_remuneration_rows_saved': director_remuneration_rows_saved,
                'director_remuneration_save_error': director_remuneration_save_error,
            })
            continue

        # One slow pdfplumber pass covers both company/period detection
        # AND statement parsing - see extract_pdf_document's docstring for
        # why this used to be two separate full passes over the same file
        # (the main reason a large report like a 300-page integrated
        # report could time out).
        try:
            _progress_emit(batch_id, {'type': 'stage', 'filename': filename, 'stage': 'parsing'})
            extracted = extract_pdf_document(pdf_bytes)
        except Exception as e:
            _progress_emit(batch_id, {'type': 'file_failed', 'filename': filename, 'error': f'Could not read/parse PDF: {e}'})
            results.append({'filename': filename, 'ok': False, 'error': f'Could not read/parse PDF: {e}'})
            continue

        pages_text = extracted['pages_text']

        period_label = detect_period_label(pages_text, filename=filename)
        if not period_label:
            results.append({
                'filename': filename, 'ok': False,
                'error': 'Could not detect a fiscal year from this PDF - no "for the year ended" or '
                         '"at <date>" statement heading was found. Re-upload with the period specified separately.',
            })
            continue

        _progress_emit(batch_id, {'type': 'stage', 'filename': filename, 'stage': 'matching_company'})
        if forced_company:
            detected_name = forced_company.name
            matched, score = {'name': forced_company.name, 'ticker': forced_company.ticker,
                               'sector': forced_company.sector}, 1.0
            company_id = forced_company.id
        else:
            detected_name = detect_company_name(pages_text, filename=filename)
            matched, score = (match_company(detected_name) if detected_name else (None, 0.0))

            company_id = None
            if matched:
                company_id = existing_by_ticker.get(matched['ticker'].lower()) or existing_by_name.get(matched['name'].strip().lower())
            if not company_id and detected_name:
                company_id = existing_by_name.get(detected_name.strip().lower())
        created_company = False

        if not extracted.get('statements'):
            results.append({
                'filename': filename, 'ok': False,
                'error': 'No recognizable financial statements found in this PDF.',
            })
            continue

        save_payload = {
            'source_filename': filename,
            'source_page_count': len(pages_text),
            'source_sha256': hashlib.sha256(pdf_bytes).hexdigest(),
        }
        if company_id:
            save_payload['company_id'] = company_id
        elif matched:
            save_payload['company_name'] = matched['name']
            save_payload['ticker'] = matched['ticker']
            save_payload['sector'] = matched['sector']
            created_company = True
        elif detected_name:
            save_payload['company_name'] = detected_name
            created_company = True
        else:
            results.append({
                'filename': filename, 'ok': False,
                'error': 'Could not identify which company this PDF belongs to. '
                         'Re-upload using the single-file import and specify company_id directly.',
            })
            continue

        # Annual reports carry a prior-year comparative column right next
        # to the current year on every statement line (see
        # pdf_parse.detect_prior_period_label's docstring) - save both
        # periods from this one PDF instead of only the current one, so a
        # single upload populates two FinancialPeriod rows' worth of
        # history (needed for every YoY figure across the Intelligence
        # Report's 8 tabs) rather than leaving the comparative column on
        # the page unsaved.
        prior_period_label = detect_prior_period_label(period_label)
        try:
            _progress_emit(batch_id, {'type': 'stage', 'filename': filename, 'stage': 'saving'})
            result, status, prior_result, prior_status = _save_current_and_prior_period(
                save_payload, extracted['statements'], period_label, prior_period_label
            )
        except Exception as e:
            # Defense in depth: _save_current_and_prior_period/_save_one_import
            # normally return an ('error', 4xx) pair for expected problems,
            # but an unexpected DB/data-shape issue on one file shouldn't
            # take down the rest of the batch (or the whole request) - it
            # should show up as a per-file failure instead.
            db.session.rollback()
            app.logger.exception(f'Unexpected error saving {filename}')
            _progress_emit(batch_id, {'type': 'file_failed', 'filename': filename, 'error': f'Could not save: {e}'})
            results.append({'filename': filename, 'ok': False, 'error': f'Could not save: {e}'})
            continue
        if status == 201:
            saved += 1
            if result.get('company_id') and matched:
                existing_by_name[matched['name'].strip().lower()] = result['company_id']
            elif result.get('company_id') and detected_name:
                existing_by_name[detected_name.strip().lower()] = result['company_id']
            periods_saved = [period_label]
            if prior_status == 201:
                saved += 1
                periods_saved.append(prior_period_label)

            # The main statement save is complete. The following optional
            # filing-section extractors each scan the PDF for their own
            # tables, so expose that work separately from the final score.
            _progress_emit(batch_id, {'type': 'stage', 'filename': filename, 'stage': 'enriching'})

            # Director remuneration total (see extract_director_remuneration's
            # docstring for scope: only the filing's OWN printed grand total,
            # never a computed/summed one) - belongs to the CURRENT period
            # specifically, not the derived prior-year comparative period,
            # since the table's own "Total" column is for one reporting year
            # at a time. Best-effort and non-fatal: a failure here should
            # never turn an otherwise-successful statement import into a
            # failed one, so it's wrapped separately from the save above.
            try:
                remuneration = extract_director_remuneration(pdf_bytes)
                if remuneration and result.get('company_id'):
                    period_row = FinancialPeriod.query.filter_by(
                        company_id=result['company_id'], period_label=period_label
                    ).first()
                    if period_row:
                        existing_metric = OperationalMetric.query.filter_by(
                            period_id=period_row.id, metric_name='total_director_remuneration'
                        ).first()
                        if existing_metric:
                            existing_metric.value = remuneration['total']
                            existing_metric.unit = remuneration.get('currency_hint') or existing_metric.unit
                        else:
                            db.session.add(OperationalMetric(
                                period_id=period_row.id,
                                metric_name='total_director_remuneration',
                                value=remuneration['total'],
                                unit=remuneration.get('currency_hint') or 'KES thousands',
                            ))
                        db.session.commit()
            except Exception:
                db.session.rollback()
                app.logger.exception(f'Director remuneration extraction failed for {filename} (non-fatal)')

            # Market data (share price, market cap, dividend, TSR,
            # shareholding structure) - same non-fatal, best-effort
            # pattern as remuneration above, and same current-period-only
            # scope (this is the filing's own point-in-time investor
            # information, not something with a prior-period comparative
            # to also save).
            try:
                market_data = extract_market_data(pdf_bytes)
                if market_data and result.get('company_id'):
                    period_row = FinancialPeriod.query.filter_by(
                        company_id=result['company_id'], period_label=period_label
                    ).first()
                    if period_row:
                        existing_md = MarketDataSnapshot.query.filter_by(period_id=period_row.id).first()
                        if existing_md is None:
                            existing_md = MarketDataSnapshot(period_id=period_row.id)
                            db.session.add(existing_md)
                        for field in (
                            'share_price', 'prior_share_price', 'market_cap', 'shares_issued',
                            'shares_authorized', 'free_float_pct', 'shareholder_count',
                            'prior_shareholder_count', 'dividend_per_share', 'interim_dividend_per_share',
                            'final_dividend_per_share', 'dividend_yield', 'total_shareholder_return',
                            'local_institutional_pct', 'local_individual_pct', 'foreign_investor_pct',
                            'page',
                        ):
                            if field in market_data:
                                setattr(existing_md, field, market_data[field])
                        db.session.commit()
            except Exception:
                db.session.rollback()
                app.logger.exception(f'Market data extraction failed for {filename} (non-fatal)')

            # Management guidance (forward-looking KPI ranges, as printed
            # in the filing's own Outlook table) - belongs to the CURRENT
            # period since it's this filing's forecast for the year ahead
            # of it, not a historical figure with a prior-year comparative.
            try:
                guidance_rows = extract_management_guidance(pdf_bytes)
                if guidance_rows and result.get('company_id'):
                    period_row = FinancialPeriod.query.filter_by(
                        company_id=result['company_id'], period_label=period_label
                    ).first()
                    if period_row:
                        ManagementGuidance.query.filter_by(period_id=period_row.id).delete()
                        for row in guidance_rows:
                            db.session.add(ManagementGuidance(period_id=period_row.id, **row))
                        db.session.commit()
            except Exception:
                db.session.rollback()
                app.logger.exception(f'Management guidance extraction failed for {filename} (non-fatal)')

            # Principal risks (named qualitative risk categories with the
            # filing's own description/mitigation text, no numeric score -
            # see extract_principal_risks' docstring for why a short list
            # is treated as "not extracted" rather than saved partially).
            try:
                risk_rows = extract_principal_risks(pdf_bytes)
                if risk_rows and result.get('company_id'):
                    period_row = FinancialPeriod.query.filter_by(
                        company_id=result['company_id'], period_label=period_label
                    ).first()
                    if period_row:
                        PrincipalRisk.query.filter_by(period_id=period_row.id).delete()
                        for row in risk_rows:
                            db.session.add(PrincipalRisk(period_id=period_row.id, **row))
                        db.session.commit()
            except Exception:
                db.session.rollback()
                app.logger.exception(f'Principal risks extraction failed for {filename} (non-fatal)')

            # Director remuneration detail (per-director rows, real
            # filed totals - see extract_director_remuneration_detail's
            # docstring for why target_period_label matters here: the
            # filing prints both this year's and last year's tables
            # together, and only this upload's own period_label's rows
            # are kept, so a separate upload of the prior year's own
            # filing owns that year's rows instead of duplicating them).
            try:
                rem_rows = extract_director_remuneration_detail(pdf_bytes, target_period_label=period_label)
                if rem_rows and result.get('company_id'):
                    period_row = FinancialPeriod.query.filter_by(
                        company_id=result['company_id'], period_label=period_label
                    ).first()
                    if period_row:
                        DirectorRemunerationRow.query.filter_by(period_id=period_row.id).delete()
                        for row in rem_rows:
                            row.pop('fiscal_year', None)  # already implied by period_id; not its own column
                            components = row.pop('components', None)
                            db.session.add(DirectorRemunerationRow(
                                period_id=period_row.id,
                                components=json.dumps(components) if components else None,
                                **{**row, 'source_document_id': result.get('source_document_id')},
                            ))
                        db.session.commit()
            except Exception:
                db.session.rollback()
                app.logger.exception(f'Director remuneration detail extraction failed for {filename} (non-fatal)')

            _progress_emit(batch_id, {'type': 'stage', 'filename': filename, 'stage': 'scoring'})
            extraction_score = _finalize_source_document_score(result.get('source_document_id'))
            file_elapsed = (datetime.utcnow() - file_started).total_seconds()
            _progress_emit(batch_id, {
                'type': 'file_done', 'filename': filename, 'elapsed_seconds': round(file_elapsed, 1),
                'extraction_score': extraction_score,
            })

            results.append({
                'filename': filename, 'ok': True,
                'company_id': result['company_id'],
                'company_name': (matched['name'] if matched else detected_name),
                'period': period_label,
                'periods_saved': periods_saved,
                'prior_period_error': (prior_result.get('error') if prior_result and prior_status != 201 else None),
                'match_score': score,
                'created_company': created_company,
            })
        else:
            _progress_emit(batch_id, {'type': 'file_failed', 'filename': filename, 'error': result.get('error', 'Unknown error')})
            results.append({'filename': filename, 'ok': False, 'error': result.get('error', 'Unknown error')})

    total_elapsed = (datetime.utcnow() - batch_started).total_seconds()
    failed_count = sum(1 for r in results if not r.get('ok'))
    _progress_emit(batch_id, {
        'type': 'batch_done', 'saved': saved, 'failed': failed_count,
        'elapsed_seconds': round(total_elapsed, 1),
    })
    return jsonify({'results': results, 'saved': saved, 'failed': failed_count}), 200


# ---------- SURVEY (multi-company board/remuneration benchmark) ----------
# Built up from individual company uploads (SurveyCompanyData, one row per
# company per fiscal year - see models_survey.py and SURVEY_FORMAT_SPEC.md)
# rather than one big aggregate document. Every aggregate figure on the
# Survey page is computed live, across however many companies have a row
# for the selected fiscal year - there's nothing to re-upload when a new
# company is added, and nothing here ever touches FinancialPeriod/
# FinancialStatement. Aggregation itself lives in survey_aggregate.py,
# shared with the PDF export route below.

def _find_or_create_company_for_survey(name, sector):
    """Same reuse-by-name pattern as the financial-condensed-file import
    (see save_condensed_filing) - looks for an existing Company by
    case-insensitive exact name match before creating a new one, so a
    company already tracked for its financials doesn't get duplicated
    just because its name is typed slightly differently in casing."""
    name = (name or '').strip()
    if not name:
        return None
    c = Company.query.filter(db.func.lower(Company.name) == name.lower()).first()
    if c is None:
        c = Company(name=name, sector=(sector or '').strip() or None, exchange='NSE', country='Kenya')
        db.session.add(c)
        db.session.commit()
    elif sector and not c.sector:
        c.sector = sector.strip()
        db.session.commit()
    return c


@app.route('/api/survey-data/import', methods=['POST'])
@require_role('admin')
def import_survey_data():
    """Accepts an uploaded Survey Data condensed .txt file (multipart/
    form-data, field name 'file') for ONE company/fiscal-year, parses it,
    and upserts a SurveyCompanyData row. Re-uploading the same company +
    fiscal_year replaces that row rather than creating a duplicate."""
    if 'file' not in request.files:
        return jsonify({'error': "No file uploaded (expected form field 'file')."}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected.'}), 400

    try:
        text = file.read().decode('utf-8', errors='replace')
    except Exception as e:
        return jsonify({'error': f'Could not read file: {e}'}), 400

    if not is_survey_data_format(text):
        return jsonify({'error': 'This file does not look like a Survey Data condensed file '
                                  '(expected ===COMPANY===, ===PERIOD===, and at least one of '
                                  '===PERFORMANCE===/===BOARD_COMPOSITION===/===DIRECTOR_PAY===/===COMMITTEE_PAY===).'}), 422

    parsed = parse_survey_data(text)
    company_name = parsed.get('company_name')
    fiscal_year = parsed.get('fiscal_year')
    if not company_name:
        return jsonify({'error': '===COMPANY=== Name is required.'}), 422
    if not fiscal_year:
        return jsonify({'error': '===PERIOD=== FiscalYear is required.'}), 422

    company = _find_or_create_company_for_survey(company_name, parsed.get('sector'))

    row = SurveyCompanyData.query.filter_by(company_id=company.id, fiscal_year=fiscal_year).first()
    is_update = row is not None
    if row is None:
        row = SurveyCompanyData(company_id=company.id, fiscal_year=fiscal_year)
        db.session.add(row)

    # Only column names SurveyCompanyData actually has - company_name
    # isn't a column (it resolved `company` above), source_notes maps
    # straight through.
    model_columns = {c.name for c in SurveyCompanyData.__table__.columns}
    for key, value in parsed.items():
        if key == 'company_name':
            continue
        if key in model_columns:
            setattr(row, key, value)
    row.source_filename = file.filename

    db.session.commit()
    log_activity(company.id, 'survey_updated' if is_update else 'survey_uploaded',
                  detail=f"Survey data {'updated' if is_update else 'uploaded'} from {file.filename}",
                  fiscal_year=fiscal_year)

    return jsonify({
        'ok': True,
        'updated_existing': is_update,
        'company': company.to_dict(),
        'survey_data': row.to_dict(),
    }), 201


@app.route('/api/survey-data/import-pdf', methods=['POST'])
@require_role('admin')
def import_survey_data_from_pdf():
    """Accepts a raw filing PDF (multipart/form-data, field name 'file')
    plus REQUIRED form fields 'company_name' and 'fiscal_year' (the
    intelligence layer extracts board/pay figures from body content -
    it doesn't re-derive who the filing is even about, so the caller
    must already know that from the upload context, same as a person
    naming the file before they upload it).

    Runs document_chunk + remuneration_ontology + intelligence_extractor
    against the PDF and upserts a SurveyCompanyData row from whatever
    it found - exactly the same upsert code path as the hand-typed
    Survey Data .txt import above, since extract_survey_schema()
    returns the identical dict shape parse_survey_data() does. A field
    the extractor wasn't confident about is simply absent from the
    result, same as an undisclosed field in a hand-typed file - this
    route never fills a gap with a guess.

    Optional 'start_page'/'end_page' form fields (1-indexed) let the
    caller scope extraction to a known page range instead of chunking
    an entire multi-hundred-page report - useful once a person knows
    roughly where a company's governance/remuneration section starts,
    since chunking is the slow part of this pipeline (seconds per page
    on a real filing) and most of a large annual report is irrelevant
    to what this route is looking for."""
    if 'file' not in request.files:
        return jsonify({'error': "No file uploaded (expected form field 'file')."}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected.'}), 400

    company_name = (request.form.get('company_name') or '').strip()
    fiscal_year = (request.form.get('fiscal_year') or '').strip()
    if not company_name:
        return jsonify({'error': "Form field 'company_name' is required - the PDF "
                                  "intelligence layer extracts figures from the filing's "
                                  "body content, it doesn't identify which company the "
                                  "filing is about."}), 422
    if not fiscal_year:
        return jsonify({'error': "Form field 'fiscal_year' is required, e.g. 'FY2025'."}), 422

    try:
        start_page = int(request.form.get('start_page', 1))
        end_page_raw = request.form.get('end_page')
        end_page = int(end_page_raw) if end_page_raw else None
    except ValueError:
        return jsonify({'error': "'start_page'/'end_page' must be integers."}), 400

    # Resolve/create the company BEFORE extraction (not after, as the
    # hand-typed-file import route does) so its own Company.country -
    # if already on file - can be handed to the currency-inference
    # fallback in extract_survey_schema. An optional 'company_country'
    # form field lets the caller supply one for a brand-new company
    # that has no country on file yet; it is used only for this
    # extraction call, never written back onto the Company row itself
    # (this route isn't the place to be setting company identity
    # fields from a form value nobody asked it to trust for that).
    company = _find_or_create_company_for_survey(company_name, None)
    company_country = company.country or (request.form.get('company_country') or '').strip() or None

    tmp_path = os.path.join(tempfile.gettempdir(), f"survey_pdf_{uuid.uuid4().hex}.pdf")
    file.save(tmp_path)
    try:
        parsed = extract_survey_schema(tmp_path, start_page=start_page, end_page=end_page,
                                        company_country=company_country)
        with open(tmp_path, 'rb') as f:
            pdf_bytes = f.read()
        directors_result = extract_survey_directors(pdf_bytes)
        director_benefits = extract_survey_director_benefits(pdf_bytes, fiscal_year=fiscal_year)
    except Exception as e:
        return jsonify({'error': f'Could not process PDF: {e}'}), 400
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # Backfill turnover/net_profit from the financial-statement pipeline's
    # already-extracted Financials row for this same company+period, when
    # the ontology scan above didn't find them itself. Real gap confirmed
    # on KCB Group Plc's own 2024 filing: its income statement genuinely
    # never uses the words "revenue"/"turnover" anywhere (says "Total
    # income" instead), so extract_survey_schema's label-glued-to-number
    # ontology match has nothing to latch onto and correctly comes back
    # empty rather than guessing - but the identical figure is already
    # sitting there, correctly extracted, in Financials, if this
    # company+fiscal_year has already been through the standalone
    # financial-statement import (a common real sequence: the same PDF,
    # or a same-scope one, imported through both pipelines). Financials
    # always stores in millions; converted here to whatever unit this
    # survey record's own scan detected, since the two can genuinely
    # differ (KCB: financial-statement figures in millions, survey
    # ontology's own unit detection here found "thousands").
    if 'turnover' not in parsed or 'net_profit' not in parsed:
        fin = Financials.query.filter_by(company_id=company.id, period=fiscal_year).first()
        if fin:
            _unit_multiplier_from_millions = {'thousands': 1000, 'millions': 1, 'billions': 0.001}
            multiplier = _unit_multiplier_from_millions.get(parsed.get('unit', 'millions'), 1)
            parsed.setdefault('field_sources', {})
            parsed.setdefault('field_confidence', {})
            if 'turnover' not in parsed and fin.revenue is not None:
                parsed['turnover'] = fin.revenue * multiplier
                parsed['field_sources']['turnover'] = (
                    f"backfilled from this company's financial-statement import for {fiscal_year} "
                    "(this filing's own income statement doesn't state revenue/turnover by that name)")
                parsed['field_confidence']['turnover'] = 0.85
            if 'net_profit' not in parsed and fin.net_income is not None:
                parsed['net_profit'] = fin.net_income * multiplier
                parsed['field_sources']['net_profit'] = (
                    f"backfilled from this company's financial-statement import for {fiscal_year}")
                parsed['field_confidence']['net_profit'] = 0.85

    row = SurveyCompanyData.query.filter_by(company_id=company.id, fiscal_year=fiscal_year).first()
    is_update = row is not None
    if row is None:
        row = SurveyCompanyData(company_id=company.id, fiscal_year=fiscal_year)
        db.session.add(row)

    model_columns = {c.name for c in SurveyCompanyData.__table__.columns}
    for key, value in parsed.items():
        if key in model_columns:
            setattr(row, key, value)
    row.source_filename = file.filename

    # Directors' Register: replace this company/year's existing rows
    # entirely rather than appending, so re-uploading a corrected PDF
    # (or uploading a same-year restated filing) doesn't leave stale
    # duplicate director rows behind - same "re-upload replaces" model
    # the SurveyCompanyData row above already follows column-by-column.
    directors_extracted = directors_result.get('directors', [])
    if directors_extracted:
        SurveyDirector.query.filter_by(company_id=company.id, fiscal_year=fiscal_year).delete()
        for d in directors_extracted:
            db.session.add(SurveyDirector(
                company_id=company.id, fiscal_year=fiscal_year,
                director_name=d['director_name'], position=d.get('position'),
                role=d.get('role'), independent=d.get('independent'),
                order_index=d.get('order_index', 0), page=d.get('page'),
                confidence=None,   # this extractor's pattern-match either finds a clean row or skips it entirely - it doesn't produce a graded confidence score the way the ontology-ranked SurveyCompanyData fields do, so this is left NULL rather than a fabricated fixed number
            ))

    db.session.commit()

    # Per-director benefit totals: same "re-upload replaces" model as
    # the Directors' Register above, and kept as its own delete/insert
    # pass (not folded into the directors loop) since these come from a
    # different source page/table and a company can have one without
    # the other.
    if director_benefits:
        SurveyDirectorBenefit.query.filter_by(company_id=company.id, fiscal_year=fiscal_year).delete()
        for b in director_benefits:
            db.session.add(SurveyDirectorBenefit(
                company_id=company.id, fiscal_year=fiscal_year,
                director_name=b['director_name'], position=b.get('position'),
                total_amount=b.get('total_amount'), detail=b.get('detail'),
                page=b.get('page'), confidence=None,
            ))
        db.session.commit()

    fields_extracted_count = len([k for k in parsed if k not in ('field_sources', 'field_confidence')])
    log_activity(company.id, 'survey_pdf_extracted',
                  detail=f"{fields_extracted_count} field(s), {len(directors_extracted)} director(s) extracted from {file.filename}",
                  fiscal_year=fiscal_year)

    return jsonify({
        'ok': True,
        'updated_existing': is_update,
        'company': company.to_dict(),
        'survey_data': row.to_dict(),
        'fields_extracted': fields_extracted_count,
        'field_confidence': parsed.get('field_confidence', {}),
        'directors_extracted': len(directors_extracted),
        'directors_layout_supported': not directors_result.get('unsupported_layout', False),
        'director_benefits_extracted': len(director_benefits),
    }), 201


@app.route('/api/survey-data/import-policy-pdf', methods=['POST'])
@require_role('admin')
def import_remuneration_policy_from_pdf():
    """Same shape as /api/survey-data/import-pdf, but for
    RemunerationPolicy's yes/no + free-text fields instead of
    SurveyCompanyData's numeric ones - uses
    intelligence_extractor.extract_policy_disclosures() rather than
    extract_survey_schema(). Kept as a separate route (not folded into
    import-pdf) because it upserts a different model and the two
    extraction functions return differently-shaped dicts - merging
    them would mean this route silently swallowing whichever set of
    fields the OTHER extractor happened to find, which is more
    confusing than two small, clearly-named routes."""
    if 'file' not in request.files:
        return jsonify({'error': "No file uploaded (expected form field 'file')."}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected.'}), 400

    company_name = (request.form.get('company_name') or '').strip()
    fiscal_year = (request.form.get('fiscal_year') or '').strip()
    if not company_name:
        return jsonify({'error': "Form field 'company_name' is required."}), 422
    if not fiscal_year:
        return jsonify({'error': "Form field 'fiscal_year' is required, e.g. 'FY2025'."}), 422

    try:
        start_page = int(request.form.get('start_page', 1))
        end_page_raw = request.form.get('end_page')
        end_page = int(end_page_raw) if end_page_raw else None
    except ValueError:
        return jsonify({'error': "'start_page'/'end_page' must be integers."}), 400

    tmp_path = os.path.join(tempfile.gettempdir(), f"policy_pdf_{uuid.uuid4().hex}.pdf")
    file.save(tmp_path)
    try:
        parsed = extract_policy_disclosures(tmp_path, start_page=start_page, end_page=end_page)
    except Exception as e:
        return jsonify({'error': f'Could not process PDF: {e}'}), 400
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    company = _find_or_create_company_for_survey(company_name, None)

    row = RemunerationPolicy.query.filter_by(company_id=company.id, fiscal_year=fiscal_year).first()
    is_update = row is not None
    if row is None:
        row = RemunerationPolicy(company_id=company.id, fiscal_year=fiscal_year)
        db.session.add(row)

    model_columns = {c.name for c in RemunerationPolicy.__table__.columns}
    for key, value in parsed.items():
        if key in model_columns:
            setattr(row, key, value)

    db.session.commit()

    return jsonify({
        'ok': True,
        'updated_existing': is_update,
        'company': company.to_dict(),
        'remuneration_policy': row.to_dict(),
    }), 201


@app.route('/api/survey-data/<int:company_id>/<fiscal_year>/review', methods=['POST'])
@require_role('admin')
def review_survey_field(company_id, fiscal_year):
    """Approve, reject, or correct ONE extracted field on a
    SurveyCompanyData row. Body: {"field": "board_size", "action":
    "approve"} or {"field": "board_size", "action": "correct",
    "corrected_value": 11}. This is the missing half of the PDF-
    extraction pipeline - extraction alone (import-pdf) only produces
    a row with values and a confidence score attached; nothing before
    this route let a person record that they'd actually looked at a
    given field and either confirmed it or fixed it. "reject" clears
    the field back to NULL rather than leaving a value nobody trusts
    sitting in the live column.

    Only meaningful for a field that's actually IN field_confidence
    (i.e. came from PDF extraction) - a field a person typed directly
    into a hand-typed Survey Data file was never "pending review" in
    the first place, so reviewing it here is a no-op beyond recording
    the status."""
    row = SurveyCompanyData.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).first()
    if row is None:
        return jsonify({'error': 'No survey data found for this company/fiscal year.'}), 404

    body = request.get_json(silent=True) or {}
    field = body.get('field')
    action = body.get('action')
    if not field:
        return jsonify({'error': "'field' is required."}), 400
    if action not in ('approve', 'reject', 'correct'):
        return jsonify({'error': "'action' must be one of: approve, reject, correct."}), 400

    model_columns = {c.name for c in SurveyCompanyData.__table__.columns}
    if field not in model_columns:
        return jsonify({'error': f"{field!r} is not a SurveyCompanyData field."}), 400

    review_status = dict(row.field_review_status or {})
    corrections = dict(row.field_review_corrections or {})
    reviewer = (body.get('reviewed_by') or '').strip() or None

    if action == 'reject':
        review_status[field] = 'rejected'
        setattr(row, field, None)
    elif action == 'approve':
        review_status[field] = 'approved'
    elif action == 'correct':
        if 'corrected_value' not in body:
            return jsonify({'error': "'corrected_value' is required for action='correct'."}), 400
        extracted_value = getattr(row, field)
        corrected_value = body['corrected_value']
        setattr(row, field, corrected_value)
        review_status[field] = 'approved'
        corrections[field] = {
            'extracted_value': extracted_value,
            'approved_value': corrected_value,
            'reviewed_by': reviewer,
            'reviewed_at': datetime.utcnow().isoformat(),
        }

    row.field_review_status = review_status
    row.field_review_corrections = corrections
    db.session.commit()
    activity_action = {'approve': 'field_approved', 'reject': 'field_rejected', 'correct': 'field_corrected'}[action]
    log_activity(company_id, activity_action, detail=f"{field} {action}d" if action != 'correct' else f"{field} corrected",
                  fiscal_year=fiscal_year, actor=reviewer)

    return jsonify({'ok': True, 'survey_data': row.to_dict()}), 200


@app.route('/api/survey/companies', methods=['GET'])
def list_survey_companies():
    """Every company with at least one SurveyCompanyData row, with the
    fiscal years available for each - lets the frontend build a fiscal-
    year picker without guessing what's on file. Also includes, for
    each company's LATEST fiscal year row (by the same fiscal_year
    string-descending convention used elsewhere, e.g.
    available_survey_years above), a real review-completion status -
    not the Admin Dashboard's old mocked status. "Completed"/"In
    Progress"/"Pending Review" is derived from field_review_status
    (see review_survey_field's own docstring): a field is only
    "reviewed" once a person has explicitly approved, rejected, or
    corrected it there - a value that was simply extracted with a
    confidence score, or hand-typed via the plain Survey Data import,
    was never marked as reviewed."""
    rows = db.session.query(SurveyCompanyData, Company).join(
        Company, SurveyCompanyData.company_id == Company.id
    ).order_by(Company.name).all()
    by_company = {}
    for r, c in rows:
        entry = by_company.setdefault(r.company_id, {
            'company_id': r.company_id, 'company_name': c.name,
            'sector': r.sector or c.sector, 'fiscal_years': [],
            '_latest_row': None,
        })
        entry['fiscal_years'].append(r.fiscal_year)
        if entry['_latest_row'] is None or r.fiscal_year > entry['_latest_row'].fiscal_year:
            entry['_latest_row'] = r

    result = []
    for entry in by_company.values():
        latest = entry.pop('_latest_row')
        scored_fields = latest.field_confidence or {}
        review_status_map = latest.field_review_status or {}
        total_scored = len(scored_fields)
        reviewed = len([f for f in scored_fields if f in review_status_map])
        if total_scored == 0:
            # No PDF-extracted (scored) fields at all - this company's
            # latest data was entered via the hand-typed Survey Data
            # import, which never produces a confidence score to review
            # in the first place (see review_survey_field's own
            # docstring: hand-typed fields were never "pending review").
            # Nothing here is awaiting review, so this counts as done.
            status, progress_pct = 'completed', 100
        else:
            progress_pct = round(reviewed / total_scored * 100)
            status = 'completed' if progress_pct == 100 else ('in-progress' if progress_pct > 0 else 'pending')
        entry['latest_fiscal_year'] = latest.fiscal_year
        entry['review_status'] = status
        entry['review_progress_pct'] = progress_pct
        entry['last_updated'] = latest.uploaded_at.isoformat() if latest.uploaded_at else None
        result.append(entry)
    return jsonify(result)


@app.route('/api/survey/activity', methods=['GET'])
def survey_activity_feed():
    """Most recent real ActivityLogEntry rows across all companies, for
    the Admin Dashboard's Recent Activities panel. ?limit=N (default
    10). Each entry already carries its own company_id; the frontend
    resolves company_name from the company list it already has rather
    than this route re-joining Company for a field the caller likely
    already has cached."""
    try:
        limit = min(int(request.args.get('limit', 10)), 50)
    except ValueError:
        limit = 10
    entries = ActivityLogEntry.query.order_by(ActivityLogEntry.created_at.desc()).limit(limit).all()
    company_ids = {e.company_id for e in entries}
    companies_by_id = {c.id: c.name for c in Company.query.filter(Company.id.in_(company_ids)).all()} if company_ids else {}
    return jsonify([{**e.to_dict(), 'company_name': companies_by_id.get(e.company_id)} for e in entries])


@app.route('/api/survey/companies/<int:company_id>/<fiscal_year>', methods=['GET'])
def get_survey_company_data(company_id, fiscal_year):
    """The full SurveyCompanyData row for ONE company/fiscal-year -
    every field exactly as uploaded, before it's folded into any
    cross-company average. This is the per-company review surface: a
    place to check what a submitted file actually captured (and what it
    didn't) separately from the aggregate report at /api/survey/overview,
    which never shows a single company's own figures on their own."""
    row = SurveyCompanyData.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).first_or_404()
    company = Company.query.get_or_404(company_id)
    directors = SurveyDirector.query.filter_by(
        company_id=company_id, fiscal_year=fiscal_year
    ).order_by(SurveyDirector.order_index).all()
    director_benefits = SurveyDirectorBenefit.query.filter_by(
        company_id=company_id, fiscal_year=fiscal_year
    ).all()
    benefit_categories = SurveyBenefitCategory.query.filter_by(
        company_id=company_id, fiscal_year=fiscal_year
    ).all()
    return jsonify({
        'company': {'id': company.id, 'name': company.name, 'sector': company.sector},
        'survey_data': row.to_dict(),
        'directors': [d.to_dict() for d in directors],
        'director_benefits': [b.to_dict() for b in director_benefits],
        'benefit_categories': [c.to_dict() for c in benefit_categories],
    })


@app.route('/api/survey/companies/<int:company_id>/<fiscal_year>', methods=['DELETE'])
@require_role('admin')
def delete_survey_company_data(company_id, fiscal_year):
    row = SurveyCompanyData.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).first_or_404()
    SurveyDirector.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).delete()
    SurveyDirectorBenefit.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).delete()
    SurveyBenefitCategory.query.filter_by(company_id=company_id, fiscal_year=fiscal_year).delete()
    db.session.delete(row)
    db.session.commit()
    return jsonify({'deleted': True}), 200


@app.route('/api/survey/overview', methods=['GET'])
def survey_overview():
    """Everything the Survey page needs for one fiscal year, aggregated
    live across every company with a SurveyCompanyData row for that
    year. ?fiscal_year=FY2025 - if omitted, uses whichever fiscal_year
    has the most companies on file. See survey_aggregate.py for the
    aggregation rules (also used by the PDF export route, so the two
    never compute a figure two different ways)."""
    overview = build_survey_overview(request.args.get('fiscal_year'))
    return jsonify(overview)


# ---------------------------------------------------------------------
# Company Survey Data page (frontend's "Survey Report" nav item, 9
# tabs: Company Overview / Board Composition / Directors' Register /
# Remuneration / Committees / Benefits & Allowances / Evidence &
# Sources / Survey Mapping / Data Quality) - a per-company drill-down
# distinct from /api/survey/companies/<id>/<fy> above (that route is
# a flatter, single-purpose view; this one assembles everything all 9
# tabs need in one call, from four independently-populated sources:
# SurveyCompanyData, DirectorRemunerationRow (models.py, financial-
# statement side), SurveyDirector + SurveyDirectorBenefit
# (models_survey.py, survey side), and Committee/CommitteeMember
# (models.py). None of these are merged/reconciled with each other -
# see each model's own docstring for why matching them by name at
# write time would be unsafe.
# ---------------------------------------------------------------------

def _survey_report_completeness(company):
    """A rough 0-100 completeness score used only to pick the DEFAULT
    company the Survey Report page opens to (whichever company has the
    richest data to actually show), never displayed to the user as a
    'quality score' - that's a different, real concept (see the Data
    Quality tab, which scores field_confidence instead). Counts: any
    SurveyCompanyData row, any DirectorRemunerationRow, any Committee
    row."""
    score = 0
    if SurveyCompanyData.query.filter_by(company_id=company.id).first():
        score += 40
    period_ids = [p.id for p in FinancialPeriod.query.filter_by(company_id=company.id).all()]
    if period_ids:
        if DirectorRemunerationRow.query.filter(DirectorRemunerationRow.period_id.in_(period_ids)).first():
            score += 40
        if Committee.query.filter(Committee.period_id.in_(period_ids)).first():
            score += 20
    return score


@app.route('/api/survey-report/default-company')
def survey_report_default_company():
    """Which company the Survey Report page should open to by default -
    whichever has the most complete data across SurveyCompanyData/
    DirectorRemunerationRow/Committee (see _survey_report_completeness),
    not just alphabetically first. Ties broken by company id (stable,
    arbitrary) rather than name, so the default doesn't reshuffle every
    time two companies are renamed to sort differently."""
    companies = Company.query.all()
    if not companies:
        return jsonify({'company_id': None})
    scored = sorted(companies, key=lambda c: (-_survey_report_completeness(c), c.id))
    return jsonify({'company_id': scored[0].id, 'completeness': _survey_report_completeness(scored[0])})


@app.route('/api/companies/<int:company_id>/survey-report')
def company_survey_report(company_id):
    """Everything the Survey Report page's 9 tabs need for ONE company,
    in one call. Pulls from FOUR separate, independently-populated
    sources - a company can have any subset of these:
      - SurveyCompanyData (models_survey.py): company-level board/pay
        totals, field_confidence/field_sources/field_review_status for
        Evidence & Sources / Survey Mapping / Data Quality tabs.
      - DirectorRemunerationRow (models.py, via FinancialPeriod): named
        directors with filed pay figures - the SAME data the
        Intelligence Report's Remuneration tab uses.
      - SurveyDirector (models_survey.py): named-director PROFILE facts
        (gender/nationality/independence/appointed date/committees),
        keyed to company_id+fiscal_year (the survey side), not
        period_id - a separate query from DirectorRemunerationRow
        above, since these come from different pages of a filing and
        aren't guaranteed to agree on who's listed.
      - SurveyDirectorBenefit (models_survey.py): named-director
        non-cash-benefit TOTALS - see that model's own docstring for
        why it's matched to a director by name, not a foreign key.
      - Committee/CommitteeMember (models.py): committee structure and
        membership.
    Never merges/reconciles these across sources - if a name only
    appears in one, it appears in only that tab's table; this endpoint
    doesn't try to match a Committee member to a DirectorRemunerationRow
    row by name (see Committee model docstring)."""
    c = Company.query.get_or_404(company_id)
    fiscal_year = request.args.get('fiscal_year')

    survey_q = SurveyCompanyData.query.filter_by(company_id=company_id)
    if fiscal_year:
        survey_q = survey_q.filter_by(fiscal_year=fiscal_year)
    survey_row = survey_q.order_by(SurveyCompanyData.fiscal_year.desc()).first()
    available_survey_years = [r.fiscal_year for r in SurveyCompanyData.query.filter_by(company_id=company_id)
                               .order_by(SurveyCompanyData.fiscal_year.desc()).all()]

    # Every year's own field_confidence, for the Data Quality tab's
    # multi-year Quality Trend chart - a genuine per-year score
    # computed client-side from each year's real confidence map, not a
    # single interpolated/fabricated series. Small (field_confidence is
    # the only column pulled beyond the join key), so fetched in this
    # same call rather than one request per year from the frontend.
    all_year_rows = SurveyCompanyData.query.filter_by(company_id=company_id).order_by(SurveyCompanyData.fiscal_year).all()
    quality_by_year = [{'fiscal_year': r.fiscal_year, 'field_confidence': r.field_confidence or {},
                         'field_count': len([col for col in SurveyCompanyData.__table__.columns
                                              if col.name not in ('id', 'company_id', 'fiscal_year', 'sector', 'source_filename',
                                                                   'uploaded_at', 'currency', 'unit', 'ned_benefits', 'source_notes',
                                                                   'field_sources', 'field_confidence', 'field_review_status',
                                                                   'field_review_corrections')
                                              and getattr(r, col.name) is not None])}
                        for r in all_year_rows]

    periods = _ordered_periods(company_id)
    period = periods[0] if periods else None

    director_rows = []
    committees = []
    if period:
        director_rows = [r.to_dict() for r in DirectorRemunerationRow.query.filter_by(
            period_id=period.id).order_by(DirectorRemunerationRow.order_index).all()]
        committees = [comm.to_dict() for comm in Committee.query.filter_by(
            period_id=period.id).order_by(Committee.order_index).all()]

    survey_director_rows = []
    survey_director_benefits = []
    if survey_row:
        survey_director_rows = [d.to_dict() for d in SurveyDirector.query.filter_by(
            company_id=company_id, fiscal_year=survey_row.fiscal_year).order_by(SurveyDirector.order_index).all()]
        survey_director_benefits = [b.to_dict() for b in SurveyDirectorBenefit.query.filter_by(
            company_id=company_id, fiscal_year=survey_row.fiscal_year).all()]

    survey_dict = None
    if survey_row:
        survey_dict = {col.name: getattr(survey_row, col.name) for col in SurveyCompanyData.__table__.columns
                        if col.name not in ('field_sources', 'field_confidence', 'field_review_status', 'field_review_corrections')}
        survey_dict['field_sources'] = survey_row.field_sources or {}
        survey_dict['field_confidence'] = survey_row.field_confidence or {}
        survey_dict['field_review_status'] = survey_row.field_review_status or {}
        survey_dict['field_review_corrections'] = survey_row.field_review_corrections or {}
        survey_dict['uploaded_at'] = survey_row.uploaded_at.isoformat() if survey_row.uploaded_at else None

    # EvidenceConflict rows: a genuinely unresolved case where two
    # different chunks of the same filing gave two different candidate
    # values for one field - the closest real concept this app has to
    # a "validation error" for the Data Quality tab.
    evidence_conflicts = []
    if survey_row:
        evidence_conflicts = [ec.to_dict() for ec in EvidenceConflict.query.filter_by(
            company_id=company_id, fiscal_year=survey_row.fiscal_year).order_by(EvidenceConflict.created_at).all()]

    return jsonify({
        'company': c.to_dict(),
        'has_survey_data': survey_row is not None,
        'has_director_data': len(director_rows) > 0,
        'has_committee_data': len(committees) > 0,
        'available_survey_years': available_survey_years,
        'period_label': period.period_label if period else None,
        'survey': survey_dict,
        'director_rows': director_rows,
        'has_survey_director_data': len(survey_director_rows) > 0,
        'survey_director_rows': survey_director_rows,
        'survey_director_benefits': [b for b in survey_director_benefits],
        'committees': committees,
        'source_documents': [d.to_dict(include_score=(current_role() == 'admin')) for d in SourceDocument.query.filter_by(company_id=company_id).all()],
        'evidence_conflicts': evidence_conflicts,
        'quality_by_year': quality_by_year,
    })


@app.route('/api/companies/<int:company_id>/committees', methods=['POST'])
@require_role('admin')
def save_committees(company_id):
    """Replace ALL committees (and their members) for this company's
    latest period with the posted list - same full-replace pattern as
    DirectorRemunerationRow's own save path, so a re-save never leaves
    stale rows from a previous edit mixed in with new ones. Body:
    {"committees": [{"name", "chairperson_name", "member_count",
    "meetings_held", "attendance_rate", "members": [{"director_name",
    "role_on_committee"}]}]}"""
    Company.query.get_or_404(company_id)
    periods = _ordered_periods(company_id)
    if not periods:
        return jsonify({'error': 'This company has no financial period yet - add one before adding committees.'}), 400
    period = periods[0]
    data = request.get_json(force=True) or {}
    committees_in = data.get('committees', [])

    Committee.query.filter_by(period_id=period.id).delete()
    db.session.flush()
    for idx, comm_in in enumerate(committees_in):
        comm = Committee(
            period_id=period.id,
            name=comm_in.get('name', '').strip(),
            chairperson_name=comm_in.get('chairperson_name') or None,
            member_count=comm_in.get('member_count'),
            meetings_held=comm_in.get('meetings_held'),
            attendance_rate=comm_in.get('attendance_rate'),
            order_index=idx,
        )
        if not comm.name:
            continue
        for midx, m_in in enumerate(comm_in.get('members', [])):
            name = (m_in.get('director_name') or '').strip()
            if not name:
                continue
            comm.members.append(CommitteeMember(
                director_name=name, role_on_committee=m_in.get('role_on_committee'), order_index=midx,
            ))
        db.session.add(comm)
    db.session.commit()
    log_activity(company_id, 'committees_updated', detail=f"{len(committees_in)} committee(s) saved")
    return jsonify({'committees': [comm.to_dict() for comm in Committee.query.filter_by(
        period_id=period.id).order_by(Committee.order_index).all()]})


@app.route('/api/survey/export/report.pdf', methods=['GET'])
@require_role('admin', 'user')
def export_survey_pdf():
    """Renders the same fiscal-year overview as /api/survey/overview,
    as a downloadable PDF (survey_report_pdf.py). ?fiscal_year=FY2025."""
    overview = build_survey_overview(request.args.get('fiscal_year'))
    if not overview.get('has_data'):
        return jsonify({'error': 'No survey data on file for this fiscal year yet.'}), 404

    buf = build_survey_pdf(overview)
    filename = f"Board_Remuneration_Survey_{_safe_filename(overview['fiscal_year'])}.pdf"
    return send_file(buf, as_attachment=True, download_name=filename, mimetype='application/pdf')


@app.route('/api/intelligence-layer/companies', methods=['GET'])
def intelligence_layer_companies():
    """Per-company rows (with an illustrative governance score) for one
    fiscal year - the Intelligence Layer's Benchmarking, Company
    Profiles, Company Rankings, and Governance & Compliance tabs all
    compare individual companies, which build_survey_overview() doesn't
    expose (it only aggregates). ?fiscal_year=FY2025."""
    return jsonify(build_company_rows(request.args.get('fiscal_year')))


@app.route('/api/intelligence-layer/historical-trends', methods=['GET'])
def intelligence_layer_historical_trends():
    """One build_survey_overview() call per fiscal year that actually
    has data - real multi-year series for the Historical Trends tab, no
    interpolation for years with nothing on file."""
    return jsonify(build_historical_trends())


@app.route('/api/survey/data-quality', methods=['GET'])
def survey_data_quality():
    """Portfolio-wide Complete/Needs Review/Missing field counts across
    every company's latest SurveyCompanyData row - used by Overview's
    Data Quality Overview panel. See build_portfolio_data_quality()'s
    docstring for exactly how each field is classified."""
    return jsonify(build_portfolio_data_quality())


@app.route('/api/companies/<int:company_id>/periods')
def list_periods(company_id):
    """Lightweight list of periods for a company - just enough to build a
    period selector. Full statement/line-item detail is fetched separately
    via /periods/<period_label> to avoid over-fetching every time."""
    Company.query.get_or_404(company_id)
    periods = FinancialPeriod.query.filter_by(company_id=company_id) \
        .order_by(FinancialPeriod.period_label.desc()).all()
    return jsonify([
        {
            'period_label': p.period_label,
            'period_type': p.period_type,
            'currency': p.currency,
            'statement_types': [s.statement_type for s in p.statements],
            'calculated_metrics': {m.metric_name: m.value for m in p.calculated_metrics},
        }
        for p in periods
    ])

@app.route('/api/companies/<int:company_id>/periods/<period_label>')
def get_period_detail(company_id, period_label):
    """Full nested breakdown for one period - statements, line items
    (with page/confidence), segments, KPIs, notes, calculated ratios."""
    Company.query.get_or_404(company_id)
    period = FinancialPeriod.query.filter_by(
        company_id=company_id, period_label=period_label
    ).first_or_404()
    return jsonify(period.to_dict())

def flat_statement_rows(period, statement_type):
    """label/normalized_name -> {'label', 'amount'} for every line item
    (top level and children) of one statement type in one period - keyed
    by normalized_name when set else the raw label text, so rows align
    across periods even if a filing's exact wording drifted year to
    year. Shared by get_period_detail_view (income_statement, 3-year
    comparison table) and the Intelligence Report endpoint (income
    statement AND cash_flow, single period) so both read the exact same
    way and can never disagree about what a filing's line items were."""
    if period is None:
        return {}
    stmt = period.statement(statement_type)
    if not stmt:
        return {}
    rows = {}
    def walk(items):
        for li in items:
            key = li.normalized_name or li.label
            rows[key] = {'label': li.label, 'amount': li.amount}
            walk(li.children)
    walk([li for li in stmt.line_items if li.parent_id is None])
    return rows

def build_comparison_rows(cols):
    """cols: a list of flat_statement_rows() dicts, latest period first.
    Returns ordered rows (order of first appearance, latest-first column)
    with a value per column and YoY% between columns 0 and 1 - the same
    reshaping get_period_detail_view already did for income_statement,
    generalized so the Intelligence Report can reuse it for cash_flow
    too without duplicating the logic."""
    seen_order = []
    for col in cols:
        for key in col:
            if key not in seen_order:
                seen_order.append(key)
    rows = []
    for key in seen_order:
        latest_cell = cols[0].get(key) if cols else None
        prior_cell = cols[1].get(key) if len(cols) > 1 else None
        rows.append({
            'label': (latest_cell or prior_cell or {}).get('label', key),
            'values': [c.get(key, {}).get('amount') if c else None for c in cols],
            'yoy_change_pct': pct_change(
                latest_cell['amount'] if latest_cell else None,
                prior_cell['amount'] if prior_cell else None
            ),
        })
    return rows

@app.route('/api/companies/<int:company_id>/periods/<period_label>/detail-view')
def get_period_detail_view(company_id, period_label):
    """Company Detail page data: the selected period's income statement
    with up to two prior fiscal years lined up alongside it (by
    normalized_name, so rows match even if label wording differs
    year to year) plus YoY% on the latest column, the period's
    calculated ratios, and its source document. Built from the same
    FinancialPeriod/CalculatedMetric rows get_period_detail and
    calculate_ratios use, so figures always agree with the rest of the
    app - this endpoint only reshapes them for the 3-year comparison
    table.
    """
    company = Company.query.get_or_404(company_id)
    period = FinancialPeriod.query.filter_by(
        company_id=company_id, period_label=period_label
    ).first_or_404()

    all_periods = FinancialPeriod.query.filter_by(company_id=company_id).all()
    ordered = sorted(
        all_periods,
        key=lambda p: (p.fiscal_year if p.fiscal_year is not None else extract_year(p.period_label) or 0),
        reverse=True
    )
    idx = next((i for i, p in enumerate(ordered) if p.id == period.id), 0)
    window = ordered[idx:idx + 3]  # selected period + up to 2 prior years
    while len(window) < 3:
        window.append(None)

    cols = [flat_statement_rows(p, 'income_statement') for p in window]
    income_rows = build_comparison_rows(cols)

    metrics = {m.metric_name: m.value for m in period.calculated_metrics}
    source_doc = None
    stmt = period.statement('income_statement')
    if stmt and stmt.source_document_id:
        doc = SourceDocument.query.get(stmt.source_document_id)
        source_doc = doc.to_dict(include_score=(current_role() == 'admin')) if doc else None

    return jsonify({
        'company': company.to_dict(),
        'period': period.to_dict(include_line_items=False),
        'period_columns': [p.period_label if p else None for p in window],
        'income_statement_rows': income_rows,
        'metrics': metrics,
        'source_document': source_doc,
    })

@app.route('/api/companies/<int:company_id>/periods/<period_label>/report.docx')
@require_role('admin', 'user')
def generate_period_report(company_id, period_label):
    """Word report for one FY, built off the same verified line items and
    calculated metrics as the rest of the app - build_report_context()
    is the only thing that reads the ORM here, report_docx.py just lays
    out what it's handed. Runs synchronously; revisit with a background
    job only if this starts taking long enough to matter.

    Pass ?narrative=ai to have the Executive Summary and Key Findings
    sections drafted by Claude from the same computed metrics the
    numbers-only template uses (see report_narrative.py) - every other
    number in the report is unaffected either way. Falls back to the
    numbers-only template automatically if no ANTHROPIC_API_KEY is set
    or the call fails, so this flag is always safe to pass."""
    Company.query.get_or_404(company_id)
    period = FinancialPeriod.query.filter_by(
        company_id=company_id, period_label=period_label
    ).first_or_404()

    ctx = build_report_context(period)
    narrative = generate_narrative(ctx) if request.args.get('narrative') == 'ai' else None

    export_dir = '/tmp/exports'
    os.makedirs(export_dir, exist_ok=True)
    filename = f"{_safe_filename(ctx['company']['name'])}_{period.period_label}_report.docx"
    filepath = os.path.join(export_dir, filename)
    generate_docx(ctx, filepath, narrative=narrative)

    return send_file(
        filepath, as_attachment=True, download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )

# ---------- EXPORTS ----------

def _safe_filename(name):
    return ''.join(c for c in (name or 'company') if c.isalnum() or c in (' ', '_', '-')).strip() or 'company'

@app.route('/api/companies/<int:company_id>/export/snapshot')
@require_role('admin', 'user')
def export_snapshot(company_id):
    """Single-company snapshot: one sheet, company info + all periods + KPIs."""
    company = Company.query.get_or_404(company_id)
    financials = Financials.query.filter_by(company_id=company_id).order_by(Financials.period.desc()).all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Snapshot'

    ws.append(['Company', company.name])
    ws.append(['Ticker', company.ticker])
    ws.append(['Exchange', company.exchange])
    ws.append(['Sector', company.sector])
    ws.append(['Country', company.country])
    ws.append([])
    ws.append(['Period', 'Currency', 'Revenue', 'Net Income', 'Total Assets',
               'Total Liabilities', 'Total Equity', 'Net Margin %', 'ROE %', 'Debt/Equity'])
    for f in financials:
        d = f.to_dict()
        ws.append([
            d['period'], d['currency'], d['revenue'], d['net_income'],
            d['total_assets'], d['total_liabilities'], d['total_equity'],
            round(d['net_margin'], 2) if d['net_margin'] is not None else None,
            round(d['roe'], 2) if d['roe'] is not None else None,
            round(d['debt_equity'], 2) if d['debt_equity'] is not None else None
        ])

    export_dir = '/tmp/exports'
    os.makedirs(export_dir, exist_ok=True)
    filepath = os.path.join(export_dir, f'{_safe_filename(company.name)}_snapshot.xlsx')
    wb.save(filepath)
    return send_file(filepath, as_attachment=True)

@app.route('/api/export/comparison')
@require_role('admin', 'user')
def export_comparison():
    """All companies, one sheet per company plus a summary ranking sheet."""
    companies = Company.query.order_by(Company.name).all()
    wb = openpyxl.Workbook()
    summary = wb.active
    summary.title = 'Summary'
    summary.append(['Company', 'Ticker', 'Period', 'Net Margin %', 'ROE %', 'Debt/Equity'])

    for c in companies:
        f = latest_financials(c.id)
        if f:
            d = f.to_dict()
            summary.append([
                c.name, c.ticker, d['period'],
                round(d['net_margin'], 2) if d['net_margin'] is not None else None,
                round(d['roe'], 2) if d['roe'] is not None else None,
                round(d['debt_equity'], 2) if d['debt_equity'] is not None else None
            ])
        else:
            summary.append([c.name, c.ticker, '-', None, None, None])

        sheet_name = _safe_filename(c.name)[:30] or f'Company{c.id}'
        ws = wb.create_sheet(title=sheet_name)
        ws.append(['Period', 'Currency', 'Revenue', 'Net Income', 'Total Assets',
                   'Total Liabilities', 'Total Equity'])
        rows = Financials.query.filter_by(company_id=c.id).order_by(Financials.period.desc()).all()
        for f in rows:
            ws.append([f.period, f.currency, f.revenue, f.net_income,
                       f.total_assets, f.total_liabilities, f.total_equity])

    export_dir = '/tmp/exports'
    os.makedirs(export_dir, exist_ok=True)
    filepath = os.path.join(export_dir, 'comparison_report.xlsx')
    wb.save(filepath)
    return send_file(filepath, as_attachment=True)

@app.route('/api/export/portfolio')
@require_role('admin', 'user')
def export_portfolio():
    """Full watchlist: one row per company with latest KPIs, single sheet."""
    companies = Company.query.order_by(Company.name).all()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Portfolio'
    ws.append(['Company', 'Ticker', 'Exchange', 'Sector', 'Country',
               'Latest Period', 'Revenue', 'Net Income', 'Net Margin %', 'ROE %', 'Debt/Equity'])
    for c in companies:
        f = latest_financials(c.id)
        if f:
            d = f.to_dict()
            ws.append([
                c.name, c.ticker, c.exchange, c.sector, c.country, d['period'],
                d['revenue'], d['net_income'],
                round(d['net_margin'], 2) if d['net_margin'] is not None else None,
                round(d['roe'], 2) if d['roe'] is not None else None,
                round(d['debt_equity'], 2) if d['debt_equity'] is not None else None
            ])
        else:
            ws.append([c.name, c.ticker, c.exchange, c.sector, c.country, '-', None, None, None, None, None])

    export_dir = '/tmp/exports'
    os.makedirs(export_dir, exist_ok=True)
    filepath = os.path.join(export_dir, 'portfolio_export.xlsx')
    wb.save(filepath)
    return send_file(filepath, as_attachment=True)

@app.route('/api/companies/<int:company_id>/export/raw.csv')
@require_role('admin', 'user')
def export_raw_csv(company_id):
    """Raw underlying financials rows, CSV, for external modeling."""
    company = Company.query.get_or_404(company_id)
    financials = Financials.query.filter_by(company_id=company_id).order_by(Financials.period.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['company', 'period', 'currency', 'revenue', 'net_income',
                      'total_assets', 'total_liabilities', 'total_equity', 'source'])
    for f in financials:
        writer.writerow([company.name, f.period, f.currency, f.revenue, f.net_income,
                          f.total_assets, f.total_liabilities, f.total_equity, f.source])

    export_dir = '/tmp/exports'
    os.makedirs(export_dir, exist_ok=True)
    filepath = os.path.join(export_dir, f'{_safe_filename(company.name)}_raw.csv')
    with open(filepath, 'w', newline='') as fh:
        fh.write(output.getvalue())
    return send_file(filepath, as_attachment=True, mimetype='text/csv')

@app.route('/api/system/status')
def system_status():
    """Live check of what's actually configured right now - not what the
    code supports, what's active in THIS running process - so the
    Settings page can show a real answer instead of generic advice."""
    dialect = db.engine.dialect.name  # 'sqlite', 'postgresql', etc.
    is_persistent = dialect != 'sqlite'
    return jsonify({
        'database_engine': dialect,
        'persistent': is_persistent,
        'warning': None if is_persistent else (
            "Running on SQLite. If this app is deployed (not just the dev "
            "editor), an Autoscale deployment's local disk is reset on "
            "every restart/redeploy - saved data will disappear. Set the "
            "DATABASE_URL environment variable to a persistent Postgres "
            "instance (Replit's built-in Database, under Tools) to fix this."
        ),
        'counts': {
            'companies': Company.query.count(),
            'periods': FinancialPeriod.query.count(),
            'line_items': FinancialLineItem.query.count(),
            'source_documents': SourceDocument.query.count(),
            'import_jobs': ImportJob.query.count(),
            'legacy_financials_rows': Financials.query.count(),
        }
    })

@app.route('/api/compare')
def compare_companies():
    """Side-by-side comparison for a chosen set of companies. Defaults to
    each company's own latest period (unchanged, so numbers still match
    the Companies/Rankings pages when no period is picked) - but if a
    'period' query param is given (e.g. ?ids=1,2,3&period=FY2024), every
    company is compared at THAT period label instead, so a batch upload
    that adds an older year for several companies at once can actually be
    compared at that year, not just whichever year is now "latest" for
    each of them.

    A company simply doesn't have every period every other company has
    (different fiscal year ends, one report skipped a year, etc.) - that
    company's 'latest' comes back null rather than silently falling back
    to a different period, so the frontend can show "No data for
    <period>" instead of quietly comparing mismatched years."""
    ids_param = request.args.get('ids', '')
    period = (request.args.get('period') or '').strip() or None
    try:
        ids = [int(x) for x in ids_param.split(',') if x.strip()]
    except ValueError:
        return jsonify({'error': 'ids must be a comma-separated list of integers'}), 400
    if not ids:
        return jsonify({'error': 'ids is required, e.g. ?ids=1,2,3'}), 400

    result = []
    for cid in ids:
        c = Company.query.get(cid)
        if c is None:
            continue
        d = c.to_dict()
        if period:
            f = Financials.query.filter_by(company_id=cid, period=period).first()
        else:
            f = latest_financials(c.id)
        d['latest'] = f.to_dict() if f else None
        result.append(d)

    return jsonify(result)

@app.route('/api/compare/periods')
def compare_periods():
    """Every distinct period label across a chosen set of companies, for
    building the period selector on the Compare page - union, not
    intersection, so a period only one of the selected companies has is
    still offered (that company will simply be the only one with data
    for it; the others show "No data for <period>" per compare_companies'
    docstring above, rather than the option being hidden entirely)."""
    ids_param = request.args.get('ids', '')
    try:
        ids = [int(x) for x in ids_param.split(',') if x.strip()]
    except ValueError:
        return jsonify({'error': 'ids must be a comma-separated list of integers'}), 400
    if not ids:
        return jsonify({'error': 'ids is required, e.g. ?ids=1,2,3'}), 400

    labels = {row.period for row in Financials.query.filter(Financials.company_id.in_(ids)).all() if row.period}
    return jsonify({'periods': sorted(labels, reverse=True)})

@app.route('/api/companies/<int:company_id>/intelligence-report')
def company_intelligence_report(company_id):
    """Everything the 8-tab Intelligence Report page needs for one company,
    in one call. Reads live from FinancialPeriod/FinancialLineItem/
    CalculatedMetric (via PeriodFinancialsView) - the same normalized data
    the Company Detail page and the PDF import pipeline use - so this
    reflects everything actually captured from an imported filing, not
    just the 5 fields the old flat Financials table tracked. Fields the
    schema genuinely has no data for (market cap, share price, dividend
    yield, 5-year risk-score history, forward-looking bull/base/bear
    scenario forecasts) are left as null/omitted rather than estimated -
    the frontend renders those as "Not available", never a guessed
    number."""
    c = Company.query.get_or_404(company_id)
    periods = _ordered_periods(company_id)
    rows = [PeriodFinancialsView(p) for p in periods]
    if not rows:
        return jsonify({'company': c.to_dict(), 'has_data': False, 'available_periods': []})

    period_label = request.args.get('period')
    idx = next((i for i, r in enumerate(rows) if r.period == period_label), 0) if period_label else 0
    latest = rows[idx]
    prior = rows[idx + 1] if idx + 1 < len(rows) else None
    prior2 = rows[idx + 2] if idx + 2 < len(rows) else None
    ld = latest.to_dict()
    pd_ = prior.to_dict() if prior else None

    def delta(cur, pri):
        return (cur - pri) if (cur is not None and pri is not None) else None

    # roa comes straight from CalculatedMetric (ratios.py already computes
    # it whenever total_assets is available) rather than being re-derived
    # here, so it can never disagree with what ratios.py/the Company
    # Detail page show for the same period.
    roa = ld['roa']
    prior_roa = pd_['roa'] if pd_ else None

    snapshot = {
        'revenue': ld['revenue'], 'revenue_yoy': pct_change(ld['revenue'], pd_['revenue']) if pd_ else None,
        'net_income': ld['net_income'], 'net_income_yoy': pct_change(ld['net_income'], pd_['net_income']) if pd_ else None,
        'operating_profit': ld['operating_profit'], 'operating_profit_yoy': pct_change(ld['operating_profit'], pd_['operating_profit']) if pd_ else None,
        'total_assets': ld['total_assets'], 'total_assets_yoy': pct_change(ld['total_assets'], pd_['total_assets']) if pd_ else None,
        'roe': ld['roe'], 'roe_delta': delta(ld['roe'], pd_['roe'] if pd_ else None),
        'net_margin': ld['net_margin'], 'net_margin_delta': delta(ld['net_margin'], pd_['net_margin'] if pd_ else None),
        'roa': roa, 'roa_delta': delta(roa, prior_roa),
        'eps': ld['eps'], 'eps_yoy': pct_change(ld['eps'], pd_['eps']) if pd_ else None,
        'debt_equity': ld['debt_equity'], 'current_ratio': ld['current_ratio'],
        'interest_coverage': ld['interest_coverage'], 'net_debt_to_ebitda': ld['net_debt_to_ebitda'],
        'gross_profit': ld['gross_profit'],
    }

    # Full trend series (up to 5 years, oldest first) with every ratio the
    # Performance Trends / Financial Analysis tabs chart - not just
    # revenue/net_income/net_margin as before.
    trend_rows = list(reversed(rows[:5]))
    trend = [{
        'period': r.period, 'revenue': r.revenue, 'net_income': r.net_income,
        'operating_profit': r.operating_profit, 'total_assets': r.total_assets,
        **{k: r.to_dict()[k] for k in (
            'net_margin', 'gross_margin', 'operating_margin', 'roe', 'roa', 'roic', 'eps'
        )},
    } for r in trend_rows]

    # 5-year summary table (Performance Trends) with simple CAGR where at
    # least 2 real data points exist - never an assumed/rounded growth
    # rate, just the actual compound rate between the first and last
    # period actually on record (which may be fewer than 5 years).
    def cagr(series_key):
        vals = [(t['period'], t[series_key]) for t in trend if t.get(series_key) is not None]
        if len(vals) < 2:
            return None
        (_, v0), (_, v1) = vals[0], vals[-1]
        years = len(vals) - 1
        if v0 in (None, 0) or v0 < 0 or years <= 0:
            return None
        return (((v1 / v0) ** (1 / years)) - 1) * 100

    five_year_summary = {
        'revenue_cagr': cagr('revenue'), 'net_income_cagr': cagr('net_income'),
        'operating_profit_cagr': cagr('operating_profit'), 'eps_cagr': cagr('eps'),
        'years_on_record': len(trend),
    }

    key_ratios = [
        {'metric': 'Net Profit Margin', 'current': ld['net_margin'], 'prior': pd_['net_margin'] if pd_ else None, 'suffix': '%'},
        {'metric': 'Gross Margin', 'current': ld['gross_margin'], 'prior': pd_['gross_margin'] if pd_ else None, 'suffix': '%'},
        {'metric': 'Operating Margin', 'current': ld['operating_margin'], 'prior': pd_['operating_margin'] if pd_ else None, 'suffix': '%'},
        {'metric': 'ROE', 'current': ld['roe'], 'prior': pd_['roe'] if pd_ else None, 'suffix': '%'},
        {'metric': 'ROA', 'current': roa, 'prior': prior_roa, 'suffix': '%'},
        {'metric': 'ROIC', 'current': ld['roic'], 'prior': pd_['roic'] if pd_ else None, 'suffix': '%'},
        {'metric': 'Debt to Equity', 'current': ld['debt_equity'], 'prior': pd_['debt_equity'] if pd_ else None, 'suffix': ''},
        {'metric': 'Current Ratio', 'current': ld['current_ratio'], 'prior': pd_['current_ratio'] if pd_ else None, 'suffix': ''},
        {'metric': 'Interest Coverage', 'current': ld['interest_coverage'], 'prior': pd_['interest_coverage'] if pd_ else None, 'suffix': ''},
        {'metric': 'Net Debt / EBITDA', 'current': ld['net_debt_to_ebitda'], 'prior': pd_['net_debt_to_ebitda'] if pd_ else None, 'suffix': ''},
        {'metric': 'EPS', 'current': ld['eps'], 'prior': pd_['eps'] if pd_ else None, 'suffix': ''},
    ]
    for r in key_ratios:
        r['change'] = delta(r['current'], r['prior'])

    # ---- Income statement + cash flow, real line items, current vs prior period ----
    period_obj = latest.period_obj
    prior_period_obj = prior.period_obj if prior else None
    income_statement_rows = build_comparison_rows([
        flat_statement_rows(period_obj, 'income_statement'),
        flat_statement_rows(prior_period_obj, 'income_statement'),
    ])
    cash_flow_rows = build_comparison_rows([
        flat_statement_rows(period_obj, 'cash_flow'),
        flat_statement_rows(prior_period_obj, 'cash_flow'),
    ])

    # ---- Risk Analysis: only 'financial' gets a real computed score;
    # every other category is listed with score=None ('Not available')
    # since scoring regulatory/competitive/technology/strategic/ESG risk
    # needs qualitative or external data this app has no source for. ----
    financial_risk_score = compute_financial_risk_score(ld, pd_)
    risk_categories = [{
        'key': 'financial', 'label': RISK_CATEGORY_LABELS['financial'],
        'score': financial_risk_score, 'band': _risk_band(financial_risk_score),
        'basis': 'Debt/Equity, Current Ratio, Interest Coverage, Net Margin trend' if financial_risk_score is not None else None,
        'qualitative': None,
    }]
    # Qualitative risk categories (Credit, Capital Adequacy, Technology &
    # Cybersecurity, Data Protection, Market, Operational, Fraud,
    # Compliance, AML/CFT/CPF, Climate, Strategic, Conduct, Reputational)
    # come from the filing's own "Management of Principal Risks" section
    # when it was extracted (see PrincipalRisk/extract_principal_risks) -
    # real disclosed categories with the company's own description and
    # mitigation text, but deliberately no numeric score: the filing
    # itself never scores these, so inventing a 0-100 number here would
    # be indistinguishable from a real figure while being pure fiction.
    principal_risks = PrincipalRisk.query.filter_by(period_id=latest.id).order_by(
        PrincipalRisk.order_index
    ).all()
    for pr in principal_risks:
        risk_categories.append({
            'key': pr.category.lower().replace(' ', '_').replace('/', '_'),
            'label': pr.category, 'score': None, 'band': None, 'basis': None,
            'qualitative': {'description': pr.description, 'mitigation': pr.mitigation, 'page': pr.page},
        })
    if not principal_risks:
        # Section not extracted for this filing (unsupported layout, or
        # not uploaded) - keep the tab's 6 generic placeholder categories
        # rather than showing nothing, but every one explicitly says why
        # it's empty rather than looking like a zero-risk score.
        for key in ('operational', 'regulatory', 'competitive', 'technology', 'strategic', 'esg'):
            risk_categories.append({
                'key': key, 'label': RISK_CATEGORY_LABELS[key],
                'score': None, 'band': None, 'basis': None, 'qualitative': None,
            })
    overall_risk_score = financial_risk_score  # only real scored component; qualitative categories are narrative-only, not blended in

    risk_exposures = []
    if ld['debt_equity'] is not None and ld['debt_equity'] > 2.0:
        risk_exposures.append({
            'risk': 'High Leverage', 'description': f"Debt/Equity of {round(ld['debt_equity'],2)}x",
            'metric': 'debt_equity', 'value': ld['debt_equity'],
        })
    if ld['current_ratio'] is not None and ld['current_ratio'] < 1.0:
        risk_exposures.append({
            'risk': 'Liquidity Strain', 'description': f"Current ratio of {round(ld['current_ratio'],2)}x (below 1.0)",
            'metric': 'current_ratio', 'value': ld['current_ratio'],
        })
    if ld['interest_coverage'] is not None and ld['interest_coverage'] < 2.0:
        risk_exposures.append({
            'risk': 'Weak Interest Coverage', 'description': f"Operating profit covers interest {round(ld['interest_coverage'],1)}x",
            'metric': 'interest_coverage', 'value': ld['interest_coverage'],
        })
    if pd_ and snapshot['net_margin_delta'] is not None and snapshot['net_margin_delta'] < -2:
        risk_exposures.append({
            'risk': 'Margin Compression', 'description': f"Net margin narrowed {round(abs(snapshot['net_margin_delta']),1)}pp vs prior period",
            'metric': 'net_margin_delta', 'value': snapshot['net_margin_delta'],
        })

    risk_candidates = assess_company_risks(c, latest, prior)
    opp = assess_company_opportunity(c, latest, prior)
    risks = [{'severity': 'High' if r[0] == 3 else 'Medium', 'title': r[1], 'metric': r[2]} for r in risk_candidates]
    opportunities = [{'title': o[0]} for o in ([opp] if opp else [])]

    strengths = []
    if snapshot['net_margin_delta'] is not None and snapshot['net_margin_delta'] > 0:
        strengths.append(f"Net margin improved {round(snapshot['net_margin_delta'], 1)}pp to {round(ld['net_margin'], 1)}%")
    if snapshot['roe_delta'] is not None and snapshot['roe_delta'] > 0:
        strengths.append(f"ROE improved {round(snapshot['roe_delta'], 1)}pp to {round(ld['roe'], 1)}%")
    if snapshot['revenue_yoy'] is not None and snapshot['revenue_yoy'] > 0:
        strengths.append(f"Revenue grew {round(snapshot['revenue_yoy'], 1)}% year over year")
    if snapshot['net_income_yoy'] is not None and snapshot['net_income_yoy'] > 0:
        strengths.append(f"Net profit grew {round(snapshot['net_income_yoy'], 1)}% year over year")
    if ld['score_band'] == 'strong':
        strengths.append(f"Financial health score of {ld['financial_score']}/100, rated Strong")
    if not opportunities and not risks and not strengths:
        strengths.append("No notable swings vs the prior period on record.")

    # ---- Peer comparison (same sector) ----
    sector = c.sector
    peer_rows = []
    if sector:
        for p in Company.query.filter_by(sector=sector).all():
            pf = latest_period_view(p.id)
            if pf:
                peer_rows.append({**pf.to_dict(), 'id': p.id, 'name': p.name})
    peer_count = len(peer_rows)

    def sector_avg(key):
        vals = [p[key] for p in peer_rows if p.get(key) is not None]
        return (sum(vals) / len(vals)) if vals else None

    def sector_top_val(key, reverse=True):
        vals = [p[key] for p in peer_rows if p.get(key) is not None]
        return (max(vals) if reverse else min(vals)) if vals else None

    def sector_top_name(key, reverse=True):
        vals = sorted([p for p in peer_rows if p.get(key) is not None], key=lambda p: p[key], reverse=reverse)
        return vals[0]['name'] if vals else None

    def rank_of(key, reverse=True):
        vals = sorted([p for p in peer_rows if p.get(key) is not None], key=lambda p: p[key], reverse=reverse)
        for i, p in enumerate(vals):
            if p['id'] == company_id:
                return i + 1
        return None

    peer_comparison = {
        'sector': sector, 'peer_count': peer_count,
        'peers_table': [{
            'id': p['id'], 'name': p['name'], 'revenue_yoy': None,
            'net_margin': p.get('net_margin'), 'roe': p.get('roe'),
            'debt_equity': p.get('debt_equity'), 'eps': p.get('eps'),
            'financial_score': p.get('financial_score'),
        } for p in sorted(peer_rows, key=lambda p: -(p.get('financial_score') or 0))],
        'metrics': [
            {'label': 'Revenue', 'company': ld['revenue'], 'sector_avg': sector_avg('revenue'), 'top': sector_top_val('revenue'), 'top_name': sector_top_name('revenue'), 'rank': rank_of('revenue'), 'suffix': ''},
            {'label': 'Net Profit Margin', 'company': ld['net_margin'], 'sector_avg': sector_avg('net_margin'), 'top': sector_top_val('net_margin'), 'top_name': sector_top_name('net_margin'), 'rank': rank_of('net_margin'), 'suffix': '%'},
            {'label': 'ROE', 'company': ld['roe'], 'sector_avg': sector_avg('roe'), 'top': sector_top_val('roe'), 'top_name': sector_top_name('roe'), 'rank': rank_of('roe'), 'suffix': '%'},
            {'label': 'Debt to Equity', 'company': ld['debt_equity'], 'sector_avg': sector_avg('debt_equity'), 'top': sector_top_val('debt_equity', reverse=False), 'top_name': sector_top_name('debt_equity', reverse=False), 'rank': rank_of('debt_equity', reverse=False), 'suffix': ''},
            {'label': 'EPS', 'company': ld['eps'], 'sector_avg': sector_avg('eps'), 'top': sector_top_val('eps'), 'top_name': sector_top_name('eps'), 'rank': rank_of('eps'), 'suffix': ''},
            {'label': 'Financial Health Score', 'company': ld['financial_score'], 'sector_avg': sector_avg('financial_score'), 'top': sector_top_val('financial_score'), 'top_name': sector_top_name('financial_score'), 'rank': rank_of('financial_score'), 'suffix': ''},
        ],
    }

    # ---- Source evidence ----
    source_docs = SourceDocument.query.filter_by(company_id=company_id).order_by(SourceDocument.uploaded_at.desc()).all()
    jobs = ImportJob.query.filter_by(company_id=company_id).order_by(ImportJob.started_at.desc()).limit(20).all()

    # ---- Outlook: only ever a Positive/Negative/Neutral LABEL derived by
    # counting real YoY signals, plus the same driver sentences already
    # shown elsewhere - never a numeric forward-looking forecast (no
    # bull/base/bear revenue range, no confidence score) since projecting
    # FY+1 figures isn't something historical filed data alone supports
    # without modeling assumptions this app has no basis to assert. ----
    signal_values = [snapshot['revenue_yoy'], snapshot['net_income_yoy'], snapshot['roe_delta'], snapshot['net_margin_delta']]
    pos_signals = sum(1 for x in signal_values if x is not None and x > 0)
    neg_signals = sum(1 for x in signal_values if x is not None and x < 0)
    if pos_signals >= 3:
        outlook_label = 'Positive'
    elif neg_signals >= 3:
        outlook_label = 'Negative'
    else:
        outlook_label = 'Neutral'

    score_rank = rank_of('financial_score')
    market_position = None
    if score_rank and peer_count:
        third = max(1, round(peer_count / 3))
        market_position = 'Outperformer' if score_rank <= third else ('Underperformer' if score_rank > peer_count - third else 'In-line')

    confidence = 'High' if prior2 else ('Medium' if prior else 'Low')

    outlook_drivers = []
    if snapshot['revenue_yoy'] is not None:
        outlook_drivers.append(f"Revenue {'grew' if snapshot['revenue_yoy'] >= 0 else 'declined'} {round(abs(snapshot['revenue_yoy']), 1)}% vs {pd_['period']}" if pd_ else '')
    if snapshot['net_margin_delta'] is not None:
        outlook_drivers.append(f"Net margin {'improved' if snapshot['net_margin_delta'] >= 0 else 'narrowed'} {round(abs(snapshot['net_margin_delta']), 1)}pp")
    if snapshot['roe_delta'] is not None:
        outlook_drivers.append(f"ROE {'improved' if snapshot['roe_delta'] >= 0 else 'declined'} {round(abs(snapshot['roe_delta']), 1)}pp")
    if market_position:
        outlook_drivers.append(f"{market_position} vs {peer_count - 1} other {sector or 'sector'} peer(s) on Financial Health Score")
    outlook_drivers = [d for d in outlook_drivers if d]

    # Scenario analysis (Bull/Base/Bear) - built only from the filing's
    # own printed Management Guidance range for next FY (see
    # ManagementGuidance/extract_management_guidance's docstring). Bear =
    # the guidance range's low end, Bull = its high end, Base = the
    # midpoint - a purely mechanical min/mid/max mapping of management's
    # own stated target range, never a modeled or estimated projection.
    # Returns [] (shown as "not disclosed in this filing") when no
    # guidance table was extracted, rather than falling back to a
    # historical-trend extrapolation that management didn't actually say.
    guidance_rows = ManagementGuidance.query.filter_by(period_id=latest.id).order_by(
        ManagementGuidance.order_index
    ).all()
    scenario_analysis = [{
        'metric_name': g.metric_name,
        'guidance_period_label': g.guidance_period_label,
        'current_value': g.current_value,
        'bear_case': g.guidance_low,
        'base_case': round((g.guidance_low + g.guidance_high) / 2, 2) if (g.guidance_low is not None and g.guidance_high is not None) else None,
        'bull_case': g.guidance_high,
        'commentary': g.commentary,
        'page': g.page,
    } for g in guidance_rows]

    # Market data (share price, market cap, dividend, shareholding
    # structure, total shareholder return) - only ever the filing's own
    # printed Investor Information figures (see MarketDataSnapshot /
    # extract_market_data's docstring). None/omitted per-field when the
    # filing didn't print it - never derived or estimated.
    market_data_row = MarketDataSnapshot.query.filter_by(period_id=latest.id).first()
    market_data = market_data_row.to_dict() if market_data_row else None

    return jsonify({
        'company': c.to_dict(),
        'has_data': True,
        'available_periods': [r.period for r in rows],
        'period': ld['period'], 'currency': ld['currency'],
        'financial_health': {
            'score': ld['financial_score'], 'band': ld['score_band'],
            'assessment': (ld['score_band'] or 'unknown').capitalize(),
            'outlook': outlook_label, 'market_position': market_position, 'confidence': confidence,
        },
        'snapshot': snapshot,
        'trend': trend,
        'five_year_summary': five_year_summary,
        'key_ratios': key_ratios,
        'income_statement_rows': income_statement_rows,
        'cash_flow_rows': cash_flow_rows,
        'strengths': strengths, 'risks': risks, 'opportunities': opportunities,
        'risk_categories': risk_categories, 'overall_risk_score': overall_risk_score,
        'overall_risk_band': _risk_band(overall_risk_score), 'risk_exposures': risk_exposures,
        'peer_comparison': peer_comparison,
        'source_documents': [d.to_dict(include_score=(current_role() == 'admin')) for d in source_docs],
        'import_jobs': [{**j.to_dict(), 'source_url': (SourceDocument.query.get(j.source_document_id).url if j.source_document_id else None)} for j in jobs],
        'legacy_source': latest.source,
        'outlook_drivers': outlook_drivers,
        'scenario_analysis': scenario_analysis,
        'market_data': market_data,
    })

@app.route('/api/companies/<int:company_id>/remuneration')
def company_remuneration(company_id):
    """Remuneration-tab data for one company: this company's OWN filed
    director remuneration totals (from OperationalMetric, one per period -
    see extract_director_remuneration in pdf_parse.py for how these get
    populated on upload) plus the full per-director breakdown from
    DirectorRemunerationRow. This is company-own-filing data only - for
    the cross-company market benchmark, see the Survey page
    (/api/survey/overview), which is a separate feature entirely."""
    Company.query.get_or_404(company_id)  # 404s early if the company doesn't exist

    own_remuneration = []
    periods = _ordered_periods(company_id)
    for p in periods:
        metric = OperationalMetric.query.filter_by(
            period_id=p.id, metric_name='total_director_remuneration'
        ).first()
        if metric:
            own_remuneration.append({
                'period_label': p.period_label, 'total': metric.value, 'unit': metric.unit,
            })

    # Per-director rows (NED grand total + each Executive Director's own
    # printed total, with the filing's own component breakdown) - see
    # DirectorRemunerationRow/extract_director_remuneration_detail's
    # docstrings. Independent of own_remuneration above (which comes
    # from a single-total-only extraction and may be empty for a filing
    # like this one that never prints one combined grand total).
    director_rows_by_period = {}
    for p in periods:
        rows = DirectorRemunerationRow.query.filter_by(period_id=p.id).order_by(
            DirectorRemunerationRow.order_index
        ).all()
        if rows:
            director_rows_by_period[p.period_label] = [r.to_dict() for r in rows]

    return jsonify({
        'own_remuneration': own_remuneration,
        'director_rows_by_period': director_rows_by_period,
    })

@app.route('/api/companies/<int:company_id>/trends')
def company_trends(company_id):
    """Every period's metrics for one company, oldest to newest, for the
    Financial Analytics trend charts. Reuses Financials.to_dict() so the
    ratios shown match everywhere else in the app exactly."""
    Company.query.get_or_404(company_id)
    rows = Financials.query.filter_by(company_id=company_id).order_by(Financials.period.asc()).all()
    return jsonify([r.to_dict() for r in rows])

@app.route('/api/import-jobs')
def import_jobs():
    """Audit trail of every scan->parse->save attempt (manual entries
    included, since those go through the same ImportJob row) - status,
    which company, which source PDF, when. Backs the Reports page."""
    jobs = ImportJob.query.order_by(ImportJob.started_at.desc()).limit(200).all()
    result = []
    for j in jobs:
        company = Company.query.get(j.company_id) if j.company_id else None
        doc = SourceDocument.query.get(j.source_document_id) if j.source_document_id else None
        row = j.to_dict()
        row['company_name'] = company.name if company else None
        row['source_url'] = doc.url if doc else None
        row['period_label'] = doc.period_label if doc else None
        # The dashboard may show a safe attention count to guests without
        # exposing the underlying numeric extraction score.
        row['needs_attention'] = bool(
            doc and doc.extraction_score is not None
            and doc.extraction_score < LOW_EXTRACTION_SCORE_THRESHOLD
        )
        if current_role() == 'admin' and doc:
            row['extraction_score'] = doc.extraction_score
            row['extraction_score_low'] = (
                doc.extraction_score is not None and doc.extraction_score < LOW_EXTRACTION_SCORE_THRESHOLD
            )
        result.append(row)
    return jsonify(result)

with app.app_context():
    db.create_all()

    # One-time seed admin, from env vars so no credential is hardcoded in
    # source. Only fires while zero admin accounts exist - after the
    # first login, every further admin is created from inside the app
    # (User Management panel), not from this env var again.
    _seed_email = os.environ.get('ADMIN_EMAIL')
    _seed_password = os.environ.get('ADMIN_PASSWORD')
    if _seed_email and _seed_password and not User.query.filter_by(role='admin').first():
        _seed_email = _seed_email.strip().lower()
        _existing = User.query.filter_by(email=_seed_email).first()
        if _existing:
            _existing.role = 'admin'
        else:
            _seed_user = User(name='Admin', email=_seed_email, role='admin')
            _seed_user.set_password(_seed_password)
            db.session.add(_seed_user)
        db.session.commit()

if __name__ == '__main__':
    # threaded=True: the live upload-progress SSE stream (GET, held open)
    # and the upload POST it's reporting on need to be served
    # concurrently - Flask's dev server is single-threaded by default,
    # which would silently block the SSE connection until the upload
    # request finished (defeating the whole point of a *live* progress
    # popup, even though nothing would error).
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False, threaded=True)