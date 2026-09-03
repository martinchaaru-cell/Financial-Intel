"""
One-time cleanup for "ghost" FinancialPeriod rows - periods that exist
(and so show up in the Financial Year selector) but have zero
FinancialStatement/FinancialLineItem rows attached to them, because
`_save_one_import()` used to accept a `statements` payload that was a
non-empty dict of statement-type keys whose `line_items` lists were
themselves empty (e.g. {"income_statement": {"line_items": []}}) -
truthy enough to pass the old `if not statements_payload:` check, but
nothing to actually save. That's fixed in app.py now (see
`_save_one_import`'s new `has_line_items` check), so this only needs to
run once to clear out whatever the bug already created - e.g. a company
showing a Financial Year like "30 June 2026" with "No income statement
line items" and blank ratios everywhere.

This does NOT touch periods that have at least one statement, even an
empty/near-empty one - only periods with zero statements at all.

Usage (from a Replit shell, with the app's environment/DATABASE_URL
already set):
    python cleanup_empty_periods.py            # dry run - just reports
    python cleanup_empty_periods.py --apply    # actually deletes them
"""

import sys

from app import app, db, FinancialPeriod


def main():
    apply_changes = '--apply' in sys.argv

    with app.app_context():
        periods = FinancialPeriod.query.all()
        empty = [p for p in periods if not p.statements]

        if not empty:
            print("No empty FinancialPeriod rows found. Nothing to do.")
            return

        for p in empty:
            company_name = p.company.name if p.company else f'company_id={p.company_id}'
            print(f"{company_name!r} — period {p.period_label!r} (id={p.id}): "
                  f"0 statements, 0 line items")
            if apply_changes:
                db.session.delete(p)

        if apply_changes:
            db.session.commit()
            print(f"\nDeleted {len(empty)} empty period(s).")
        else:
            print(f"\nDRY RUN — would delete {len(empty)} empty period(s). "
                  f"Re-run with --apply to do it.")


if __name__ == '__main__':
    main()