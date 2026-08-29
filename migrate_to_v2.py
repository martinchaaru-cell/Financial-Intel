"""
Phase 2 — one-time migration: existing Company + Financials rows -> the new
FinancialPeriod / FinancialStatement / FinancialLineItem structure.

Nothing is deleted. Your old `Financials` table stays exactly as-is (keep
it in app.py for as long as you want a rollback path); this script only
*adds* rows to the new tables, one FinancialPeriod per existing Financials
row, with the five numbers you already track re-expressed as top-level
line items on the appropriate statement.

Run once, from the Flask app context:

    from app import app
    from migrate_to_v2 import run_migration
    with app.app_context():
        run_migration()

Safe to re-run: it skips any (company_id, period_label) pair that already
has a FinancialPeriod (see the UniqueConstraint in models.py).
"""

from models import (
    db, Company, FinancialPeriod, FinancialStatement, FinancialLineItem,
)

# Map each of the five old flat fields to (statement_type, label, normalized_name)
FIELD_MAP = {
    'revenue':            ('income_statement', 'Revenue',            'revenue'),
    'net_income':         ('income_statement', 'Net income',         'net_income'),
    'total_assets':       ('balance_sheet',    'Total assets',       'total_assets'),
    'total_liabilities':  ('balance_sheet',    'Total liabilities',  'total_liabilities'),
    'total_equity':       ('balance_sheet',    'Total equity',       'total_equity'),
}


def _get_or_create_statement(period, statement_type):
    stmt = period.statement(statement_type)
    if stmt is None:
        stmt = FinancialStatement(period_id=period.id, statement_type=statement_type)
        db.session.add(stmt)
        db.session.flush()  # get stmt.id without a full commit
    return stmt


def migrate_one(old_financials_row):
    """old_financials_row: an instance of your existing Financials model
    (imported lazily by the caller, since it still lives in app.py)."""
    company_id = old_financials_row.company_id
    period_label = old_financials_row.period

    existing = FinancialPeriod.query.filter_by(
        company_id=company_id, period_label=period_label
    ).first()
    if existing:
        return existing, False   # already migrated - skip

    period = FinancialPeriod(
        company_id=company_id,
        period_label=period_label,
        period_type='FY' if 'Q' not in (period_label or '') else period_label[:2],
        currency=old_financials_row.currency or 'KES',
    )
    db.session.add(period)
    db.session.flush()

    for field, (stmt_type, label, norm_name) in FIELD_MAP.items():
        value = getattr(old_financials_row, field, None)
        if value is None:
            continue
        stmt = _get_or_create_statement(period, stmt_type)
        db.session.add(FinancialLineItem(
            statement_id=stmt.id,
            label=label,
            normalized_name=norm_name,
            section='top_level',
            amount=value,
            currency=old_financials_row.currency or 'KES',
            confidence=1.0 if old_financials_row.source == 'manual' else 0.7,
            order_index=0,
        ))

    return period, True


def run_migration(OldFinancials):
    """OldFinancials: pass in your existing Financials class from app.py
    (kept as a separate import so this module has no hard dependency on
    app.py's structure beyond what it needs)."""
    rows = OldFinancials.query.all()
    migrated, skipped = 0, 0
    for row in rows:
        _, created = migrate_one(row)
        migrated += 1 if created else 0
        skipped += 0 if created else 1
    db.session.commit()
    print(f'Migration complete: {migrated} periods created, {skipped} already existed.')


if __name__ == '__main__':
    print(
        "Run this from within your Flask app context, passing in your "
        "existing Financials model, e.g.:\n\n"
        "    from app import app, Financials\n"
        "    from migrate_to_v2 import run_migration\n"
        "    with app.app_context():\n"
        "        run_migration(Financials)\n"
    )