import os
import csv
import io
from flask import Flask, render_template, request, jsonify, send_file
from datetime import datetime
import openpyxl

from models import (
    db, Company, FinancialPeriod, FinancialStatement, FinancialLineItem,
    SourceDocument, ImportJob,
)
from ratios import calculate_ratios
from nse_import import fetch_nse_filings, fetch_nse_page, NSE_FINANCIAL_RESULTS_URL
from nse_pdf_parse import fetch_and_parse_pdf, match_canonical_label

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

# Fix for hosts (e.g. Replit's provisioned Postgres) that hand back a
# "postgres://" URL — SQLAlchemy 1.4+ requires "postgresql://".
database_url = os.environ.get('DATABASE_URL', 'sqlite:///test.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

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

# ---------- PAGE ----------

@app.route('/')
def index():
    return render_template('index.html')

# ---------- API: OVERVIEW ----------

@app.route('/api/overview')
def overview_stats():
    companies = Company.query.all()
    total_companies = len(companies)

    margins, roes, revenues, net_incomes, scores = [], [], [], [], []
    companies_with_data = 0
    performers = []
    health_counts = {'strong': 0, 'moderate': 0, 'weak': 0}
    latest_updated = None

    for c in companies:
        f = latest_financials(c.id)
        if f:
            d = f.to_dict()
            companies_with_data += 1
            if d['net_margin'] is not None:
                margins.append(d['net_margin'])
            if d['roe'] is not None:
                roes.append(d['roe'])
            if d['revenue']:
                revenues.append(d['revenue'])
            if d['net_income']:
                net_incomes.append(d['net_income'])
            if d['financial_score'] is not None:
                scores.append(d['financial_score'])
                health_counts[d['score_band']] += 1
            performers.append({
                'id': c.id, 'name': c.name, 'ticker': c.ticker,
                'sector': c.sector, 'period': d['period'],
                'net_margin': d['net_margin'], 'roe': d['roe'],
                'financial_score': d['financial_score']
            })
            if f.updated_at and (latest_updated is None or f.updated_at > latest_updated):
                latest_updated = f.updated_at

    top_performers = sorted(
        [p for p in performers if p['financial_score'] is not None],
        key=lambda p: p['financial_score'], reverse=True
    )[:5]

    activity = []
    for c in companies:
        if c.created_at:
            activity.append({'type': 'company_added', 'label': f'{c.name} added to portfolio', 'at': c.created_at})
    recent_financials = Financials.query.order_by(Financials.updated_at.desc()).limit(10).all()
    for f in recent_financials:
        comp = Company.query.get(f.company_id)
        if comp:
            activity.append({'type': 'financials_added', 'label': f'{comp.name} {f.period} financials added', 'at': f.updated_at})
    activity.sort(key=lambda a: a['at'] or datetime.min, reverse=True)
    activity = activity[:8]
    for a in activity:
        a['at'] = a['at'].isoformat() if a['at'] else None

    return jsonify({
        'total_companies': total_companies,
        'companies_with_data': companies_with_data,
        'combined_revenue': sum(revenues) if revenues else None,
        'combined_net_income': sum(net_incomes) if net_incomes else None,
        'avg_net_margin': (sum(margins) / len(margins)) if margins else None,
        'avg_roe': (sum(roes) / len(roes)) if roes else None,
        'avg_financial_score': (sum(scores) / len(scores)) if scores else None,
        'health_distribution': health_counts,
        'top_performers': top_performers,
        'recent_activity': activity,
        'last_updated': latest_updated.isoformat() if latest_updated else None
    })

# ---------- API: COMPANIES ----------

@app.route('/api/companies', methods=['GET'])
def list_companies():
    sort = request.args.get('sort', 'name')
    companies = Company.query.all()
    result = []
    for c in companies:
        d = c.to_dict()
        f = latest_financials(c.id)
        d['latest'] = f.to_dict() if f else None
        result.append(d)

    if sort == 'score':
        result.sort(key=lambda r: (r['latest']['financial_score'] if r['latest'] and r['latest']['financial_score'] is not None else -1), reverse=True)
    elif sort == 'revenue':
        result.sort(key=lambda r: (r['latest']['revenue'] if r['latest'] and r['latest']['revenue'] is not None else -1), reverse=True)
    else:
        result.sort(key=lambda r: r['name'].lower())

    return jsonify(result)

@app.route('/api/companies', methods=['POST'])
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

# ---------- API: FINANCIALS ----------

@app.route('/api/companies/<int:company_id>/financials', methods=['POST'])
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

# ---------- NSE IMPORT ----------
# Three-step flow, each step reviewable before the next:
#   1. /scan   - list current NSE filings + best-guess company match
#   2. /parse  - download+parse one filing's PDF, return DRAFT numbers
#   3. /save   - human confirms the draft numbers, THEN they're written
# Nothing here auto-saves. PDF parsing is heuristic and needs a human
# glance before it lands in the database.

@app.route('/api/import/nse/scan')
def nse_scan():
    try:
        html = fetch_nse_page(NSE_FINANCIAL_RESULTS_URL)
    except Exception as e:
        return jsonify({'error': f'Could not reach NSE: {e}'}), 502

    filings = fetch_nse_filings(html)

    # Attach whether we already have this company in our own database,
    # since that determines whether "save" needs to create a company first.
    existing = {c.name.strip().lower(): c.id for c in Company.query.all()}
    for f in filings:
        mc = f.get('matched_company')
        f['existing_company_id'] = existing.get(mc['name'].strip().lower()) if mc else None

    return jsonify(filings)

@app.route('/api/import/nse/parse', methods=['POST'])
def nse_parse():
    data = request.get_json(force=True) or {}
    pdf_url = (data.get('pdf_url') or '').strip()
    if not pdf_url:
        return jsonify({'error': 'pdf_url is required'}), 400
    try:
        parsed = fetch_and_parse_pdf(pdf_url)
    except Exception as e:
        return jsonify({'error': f'Could not parse PDF: {e}'}), 502
    return jsonify({'pdf_url': pdf_url, 'parsed': parsed})

@app.route('/api/import/nse/save', methods=['POST'])
def nse_save():
    """Human has reviewed the parsed line items (and can have hand-edited
    them) - this saves them into the normalized schema: one FinancialPeriod,
    one FinancialStatement per statement type present, and a
    FinancialLineItem per reviewed line.

    Expected payload shape (matches what /parse returns, after review):
        {
          "company_id": 1,            # or company_name/ticker/sector to create one
          "period": "FY2025",
          "pdf_url": "https://...",   # optional, recorded as the SourceDocument
          "statements": {
            "income_statement": {"line_items": [{"label", "normalized_name",
                                                   "amount", "page", "confidence"}, ...]},
            "balance_sheet": {...}, "cash_flow": {...}, "equity": {...}
          }
        }

    Also writes a matching flat `Financials` row (source='nse_import') so
    existing routes that haven't been upgraded yet (overview stats, the
    older exports) keep working unchanged - see models.py's migration
    notes for why that table is being kept around for now.
    """
    data = request.get_json(force=True) or {}

    company_id = data.get('company_id')
    if not company_id:
        company_name = (data.get('company_name') or '').strip()
        if not company_name:
            return jsonify({'error': 'company_id or company_name is required'}), 400
        c = Company(
            name=company_name,
            ticker=(data.get('ticker') or '').strip(),
            exchange='NSE',
            sector=(data.get('sector') or '').strip(),
            country='Kenya'
        )
        db.session.add(c)
        db.session.commit()
        company_id = c.id
    else:
        Company.query.get_or_404(company_id)

    period_label = (data.get('period') or '').strip()
    if not period_label:
        return jsonify({'error': 'period is required'}), 400

    statements_payload = data.get('statements') or {}
    if not statements_payload:
        return jsonify({'error': 'statements (with at least one line item) is required'}), 400

    # Optional source document, so line items can point back to "which PDF,
    # which page" - only created if a pdf_url was actually supplied.
    source_doc_id = None
    pdf_url = (data.get('pdf_url') or '').strip()
    if pdf_url:
        doc = SourceDocument(
            company_id=company_id, url=pdf_url, period_label=period_label
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
        return jsonify({'error': f'Save failed: {e}'}), 500

    # Dual-write: keep the old flat Financials table populated too, so
    # /api/overview and the pre-Phase-6 export routes keep working exactly
    # as before while those get upgraded to read from FinancialPeriod
    # directly.
    f = Financials(
        company_id=company_id,
        period=period_label,
        currency='KES',
        revenue=flat.get('revenue', 0.0),
        net_income=flat.get('net_income', 0.0),
        total_assets=flat.get('total_assets', 0.0),
        total_liabilities=flat.get('total_liabilities', 0.0),
        total_equity=flat.get('total_equity', 0.0),
        source='nse_import'
    )
    db.session.add(f)
    db.session.commit()

    return jsonify({
        'company_id': company_id,
        'period': period.to_dict(),
        'financials': f.to_dict(),   # legacy shape, for any frontend code still reading it
    }), 201

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

# ---------- EXPORTS ----------

def _safe_filename(name):
    return ''.join(c for c in (name or 'company') if c.isalnum() or c in (' ', '_', '-')).strip() or 'company'

@app.route('/api/companies/<int:company_id>/export/snapshot')
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

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)