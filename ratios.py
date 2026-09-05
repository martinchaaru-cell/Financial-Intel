"""
Phase 5 preview — calculate ratios from normalized_name line items and
store them as CalculatedMetric rows, kept separate from reported figures.

This only needs normalized_name to be populated consistently by the parser
(Phase 3) - it doesn't care what the original label text was, or how deep
in the line-item tree the number sits, as long as normalized_name is set
on the item that holds the figure.

Start with the ratios your current score already uses; add more as the
parser starts extracting the additional line items (gross profit, EBITDA,
current assets/liabilities, etc.) that the richer ratios need.
"""

from models import db, FinancialPeriod, FinancialLineItem, CalculatedMetric


def _find_amount(period, normalized_name):
    """Search every line item (any depth, any statement) in this period for
    a given normalized_name and return its amount."""
    for stmt in period.statements:
        for li in stmt.line_items:
            found = _search_tree(li, normalized_name)
            if found is not None:
                return found
    return None


def _search_tree(line_item, normalized_name):
    if line_item.normalized_name == normalized_name:
        return line_item.amount
    for child in line_item.children:
        found = _search_tree(child, normalized_name)
        if found is not None:
            return found
    return None


def _upsert_metric(period, name, value, formula):
    if value is None:
        return
    existing = CalculatedMetric.query.filter_by(period_id=period.id, metric_name=name).first()
    if existing:
        existing.value = value
    else:
        db.session.add(CalculatedMetric(
            period_id=period.id, metric_name=name, value=value, formula_description=formula
        ))


def calculate_ratios(period: FinancialPeriod):
    """Recompute the ratio set for one period and upsert into
    CalculatedMetric. Call after any save that touches this period's line
    items (import save, manual edit, etc.)."""
    revenue = _find_amount(period, 'revenue')
    net_income = _find_amount(period, 'net_income')
    total_assets = _find_amount(period, 'total_assets')
    total_liabilities = _find_amount(period, 'total_liabilities')
    total_equity = _find_amount(period, 'total_equity')
    gross_profit = _find_amount(period, 'gross_profit')
    operating_profit = _find_amount(period, 'operating_profit')
    eps = _find_amount(period, 'eps')
    shares_outstanding = _find_amount(period, 'shares_outstanding')

    if revenue:
        _upsert_metric(period, 'net_margin', (net_income / revenue * 100) if net_income is not None else None,
                        'net_income / revenue * 100')
        # Only computed when the filing actually reported a gross_profit /
        # operating_profit subtotal line - never derived by subtracting
        # unrelated line items, since not every filing breaks out COGS or
        # an operating-profit subtotal the same way.
        _upsert_metric(period, 'gross_margin', (gross_profit / revenue * 100) if gross_profit is not None else None,
                        'gross_profit / revenue * 100')
        _upsert_metric(period, 'operating_margin', (operating_profit / revenue * 100) if operating_profit is not None else None,
                        'operating_profit / revenue * 100')
    # EPS: prefer the figure the filing reports directly (eps) - it's the
    # audited number and may reflect diluted shares, buybacks mid-year, or
    # other adjustments a simple net_income/shares_outstanding recompute
    # would miss. Only fall back to computing it when the filing gave a
    # shares count but no EPS line of its own.
    if eps is not None:
        _upsert_metric(period, 'eps', eps, 'as reported')
    elif shares_outstanding:
        _upsert_metric(period, 'eps', (net_income / shares_outstanding) if net_income is not None else None,
                        'net_income / shares_outstanding')
    if total_equity:
        _upsert_metric(period, 'roe', (net_income / total_equity * 100) if net_income is not None else None,
                        'net_income / total_equity * 100')
        _upsert_metric(period, 'debt_equity',
                        (total_liabilities / total_equity) if total_liabilities is not None else None,
                        'total_liabilities / total_equity')
    if total_assets:
        _upsert_metric(period, 'roa', (net_income / total_assets * 100) if net_income is not None else None,
                        'net_income / total_assets * 100')
        _upsert_metric(period, 'debt_assets',
                        (total_liabilities / total_assets) if total_liabilities is not None else None,
                        'total_liabilities / total_assets')

    db.session.commit()