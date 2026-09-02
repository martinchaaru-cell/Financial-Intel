"""
One-time cleanup for legacy `Financials` rows that were duplicated by the
bug in `_save_one_import()` (fixed in app.py - see `_upsert_legacy_financials`).

Before that fix, every re-save of the same company+period (a re-upload, a
re-run batch NSE parse-and-save, an auto re-import) INSERTED a brand new
Financials row instead of updating the existing one. That's why a
company's Financial Year dropdown could show the same date several times,
and why the Intelligence Report could pick a mostly-empty duplicate
instead of the complete one.

The code fix stops NEW duplicates from being created. This script cleans
up duplicates that already exist in your database from before the fix.
It does NOT touch the newer FinancialPeriod/FinancialStatement/
FinancialLineItem tables - those already had a unique constraint on
(company_id, period_label) and were never able to duplicate this way.

For each (company_id, period) group with more than one row, this keeps
ONE row - the "most complete" one (most non-null/non-zero fields; ties
broken by most recently updated) - and deletes the rest.

Usage (from a Replit shell, with the app's environment/DATABASE_URL
already set):
    python dedupe_financials.py            # dry run - just reports what it would do
    python dedupe_financials.py --apply    # actually deletes the extra rows
"""

import sys
from collections import defaultdict

from app import app, db, Financials  # noqa: E402  (import after path is set up by running from the project root)


def _completeness(f: Financials) -> tuple:
    """Higher is 'more complete'. Counts populated numeric fields, then
    falls back to updated_at as a tiebreaker so the most recent save wins
    among equally-complete duplicates."""
    fields = [f.revenue, f.net_income, f.total_assets, f.total_liabilities, f.total_equity]
    populated = sum(1 for v in fields if v not in (None, 0, 0.0))
    updated_ts = f.updated_at.timestamp() if f.updated_at else 0
    return (populated, updated_ts)


def main():
    apply_changes = '--apply' in sys.argv

    with app.app_context():
        rows = Financials.query.order_by(Financials.company_id, Financials.period).all()
        groups = defaultdict(list)
        for r in rows:
            groups[(r.company_id, r.period)].append(r)

        dupe_groups = {k: v for k, v in groups.items() if len(v) > 1}

        if not dupe_groups:
            print("No duplicate (company_id, period) rows found. Nothing to do.")
            return

        total_to_delete = 0
        for (company_id, period), group in dupe_groups.items():
            group_sorted = sorted(group, key=_completeness, reverse=True)
            keep = group_sorted[0]
            drop = group_sorted[1:]
            total_to_delete += len(drop)
            print(f"company_id={company_id} period={period!r}: "
                  f"{len(group)} rows -> keeping id={keep.id} "
                  f"(revenue={keep.revenue}, net_income={keep.net_income}), "
                  f"dropping ids={[d.id for d in drop]}")
            if apply_changes:
                for d in drop:
                    db.session.delete(d)

        if apply_changes:
            db.session.commit()
            print(f"\nDeleted {total_to_delete} duplicate row(s) across {len(dupe_groups)} "
                  f"(company, period) group(s).")
        else:
            print(f"\nDRY RUN - would delete {total_to_delete} duplicate row(s) across "
                  f"{len(dupe_groups)} (company, period) group(s). Re-run with --apply to do it.")


if __name__ == '__main__':
    main()