"""
Report context builder — Phase 6 step 1.

This is the ONLY place that reads FinancialPeriod/FinancialStatement/
FinancialLineItem/CalculatedMetric out of the database for reporting
purposes. Every generator (docx, html, xlsx, pptx, and later an AI
narrative pass) reads the plain dict this returns - none of them touch
the ORM directly. That's what keeps every number in every output format
traceable to one place and one query.

Nothing in here writes prose. It returns numbers, labels, and page/
confidence provenance exactly as stored. The "Not disclosed" decision
and all narrative sentences belong to the generators (see report_docx.py),
not here - this module never decides how something should read, only
what is or isn't in the database.
"""

from models import FinancialPeriod, FinancialStatement, CalculatedMetric


def _flatten_line_items(line_items, depth=0):
    """Flatten a line-item tree (top-level items with .children) into an
    ordered list of dicts, preserving depth so a generator can indent
    sub-items instead of losing the report's original structure."""
    flat = []
    for li in line_items:
        flat.append({
            'label': li.label,
            'normalized_name': li.normalized_name,
            'section': li.section,
            'amount': li.amount,
            'currency': li.currency,
            'page': li.page,
            'confidence': li.confidence,
            'depth': depth,
            'has_children': len(li.children) > 0,
        })
        if li.children:
            flat.extend(_flatten_line_items(li.children, depth + 1))
    return flat


def _statement_dict(statement: FinancialStatement):
    top_level = [li for li in statement.line_items if li.parent_id is None]
    return {
        'statement_type': statement.statement_type,
        'source_document_id': statement.source_document_id,
        'line_items': _flatten_line_items(top_level),
        # quick lookup so a generator can grab "revenue" or "net_income"
        # by normalized_name without re-walking the tree itself
        'by_normalized_name': {
            item['normalized_name']: item
            for item in _flatten_line_items(top_level)
            if item['normalized_name']
        },
    }


def _find_prior_period(period: FinancialPeriod):
    """Same company, same period_type, most recent fiscal_year strictly
    before this one - used for YoY figures. Returns None rather than
    guessing if there's no clean prior match."""
    if period.fiscal_year is None:
        return None
    candidates = [
        p for p in period.company.periods
        if p.id != period.id
        and p.period_type == period.period_type
        and p.fiscal_year is not None
        and p.fiscal_year < period.fiscal_year
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.fiscal_year)


def _yoy(current, prior):
    if current is None or prior is None or prior == 0:
        return None
    return round((current - prior) / abs(prior) * 100, 2)


def build_report_context(period: FinancialPeriod) -> dict:
    """The single function every report/export generator calls.

    Returns a plain, JSON-serializable dict - safe to hand to an AI
    narrative step later (see report_docx.py's module docstring) or to
    dump straight into an HTML/PDF template.
    """
    company = period.company

    statements = {stmt.statement_type: _statement_dict(stmt) for stmt in period.statements}

    metrics = {m.metric_name: {'value': m.value, 'formula': m.formula_description}
               for m in period.calculated_metrics}

    prior_period = _find_prior_period(period)
    prior_statements = {stmt.statement_type: _statement_dict(stmt) for stmt in prior_period.statements} if prior_period else {}

    def current_amount(stmt_type, norm_name):
        item = statements.get(stmt_type, {}).get('by_normalized_name', {}).get(norm_name)
        return item['amount'] if item else None

    def prior_amount(stmt_type, norm_name):
        item = prior_statements.get(stmt_type, {}).get('by_normalized_name', {}).get(norm_name)
        return item['amount'] if item else None

    yoy = {}
    for stmt_type, names in {
        'income_statement': ['revenue', 'net_income', 'profit_before_tax'],
        'balance_sheet': ['total_assets', 'total_equity'],
        'cash_flow': ['operating_cash_flow'],
    }.items():
        for name in names:
            cur = current_amount(stmt_type, name)
            pri = prior_amount(stmt_type, name)
            if cur is not None and pri is not None:
                yoy[name] = {'current': cur, 'prior': pri, 'change_pct': _yoy(cur, pri),
                             'prior_period_label': prior_period.period_label}

    return {
        'company': {
            'name': company.name,
            'ticker': company.ticker,
            'exchange': company.exchange,
            'sector': company.sector,
            'country': company.country,
        },
        'period': {
            'label': period.period_label,
            'period_type': period.period_type,
            'fiscal_year': period.fiscal_year,
            'start_date': period.start_date.isoformat() if period.start_date else None,
            'end_date': period.end_date.isoformat() if period.end_date else None,
            'currency': period.currency,
        },
        'statements': statements,
        'calculated_metrics': metrics,
        'yoy': yoy,
        'segments': [s.to_dict() for s in period.segments],
        'operational_metrics': [m.to_dict() for m in period.operational_metrics],
        'notes': [n.to_dict() for n in period.notes],
        'has_prior_period': prior_period is not None,
    }
