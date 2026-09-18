"""
calculate_ratios() is the kind of function worth locking down with
tests first: it's pure business logic (numbers in, CalculatedMetric
rows out), it already drives the scoring shown on the dashboard, and
a silent regression here (e.g. a percentage stops being multiplied by
100) would be very easy to miss by eye on a live company page.
"""
from models import Company, FinancialPeriod, FinancialStatement, FinancialLineItem
from ratios import calculate_ratios


def _make_period(db, **line_items):
    """Creates one company/period/income-statement+balance-sheet with the
    given {normalized_name: amount} line items spread across both
    statements (the ratio calculator searches every statement in the
    period, so which statement a figure lives on doesn't matter here)."""
    company = Company(name="Test Co", ticker="TST")
    db.session.add(company)
    db.session.flush()

    period = FinancialPeriod(company_id=company.id, period_label="FY2025", fiscal_year=2025)
    db.session.add(period)
    db.session.flush()

    stmt = FinancialStatement(period_id=period.id, statement_type="income_statement")
    db.session.add(stmt)
    db.session.flush()

    for i, (name, amount) in enumerate(line_items.items()):
        db.session.add(FinancialLineItem(
            statement_id=stmt.id, label=name, normalized_name=name,
            amount=amount, order_index=i,
        ))
    db.session.commit()
    db.session.refresh(period)
    return period


def test_net_margin_is_percentage(db, app):
    period = _make_period(db, revenue=200_000_000, net_income=40_000_000)
    calculate_ratios(period)
    db.session.refresh(period)
    metrics = {m.metric_name: m.value for m in period.calculated_metrics}
    assert metrics["net_margin"] == 20.0  # 40M / 200M * 100


def test_net_margin_skipped_without_revenue(db, app):
    period = _make_period(db, net_income=40_000_000)
    calculate_ratios(period)
    db.session.refresh(period)
    metrics = {m.metric_name: m.value for m in period.calculated_metrics}
    assert "net_margin" not in metrics


def test_net_margin_none_without_net_income(db, app):
    """A revenue-only filing should report 'not enough data' (None),
    never a guessed 0% — matches the module's own stated convention."""
    period = _make_period(db, revenue=200_000_000)
    calculate_ratios(period)
    db.session.refresh(period)
    metrics = {m.metric_name: m.value for m in period.calculated_metrics}
    assert metrics.get("net_margin") is None
