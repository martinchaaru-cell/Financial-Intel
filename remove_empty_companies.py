"""
One-time cleanup for companies with no financial data attached - almost
certainly the ~65-company NSE roster that used to get bulk-created by the
now-removed "Seed all NSE companies" button/`/api/companies/seed-nse`
route. Companies are now only meant to come into existence when a report
is actually uploaded (or financials are manually entered) for them, so
any company with zero FinancialPeriod rows is stale.

This does NOT touch a company that has at least one FinancialPeriod, even
an empty/ghost one from before cleanup_empty_periods.py ran - only
companies with zero periods at all. Run cleanup_empty_periods.py first if
you haven't, so a company isn't kept alive by a ghost period that's about
to be deleted anyway.

Usage (from a Replit shell, with the app's environment/DATABASE_URL
already set):
    python remove_empty_companies.py            # dry run - just reports
    python remove_empty_companies.py --apply    # actually deletes them
"""

import sys

from app import app, db, Company, Financials


def main():
    apply_changes = '--apply' in sys.argv

    with app.app_context():
        companies = Company.query.all()
        empty = [c for c in companies if not c.periods]

        if not empty:
            print("No companies without financial data found. Nothing to do.")
            return

        for c in empty:
            print(f"{c.name!r} (ticker={c.ticker!r}, id={c.id}): 0 financial periods")
            if apply_changes:
                # Also clear out any leftover flat Financials rows (the
                # older dual-write table) for this company, so nothing
                # dangles behind after the Company row itself is gone.
                Financials.query.filter_by(company_id=c.id).delete()
                db.session.delete(c)

        if apply_changes:
            db.session.commit()
            print(f"\nDeleted {len(empty)} company(ies) with no financial data.")
        else:
            print(f"\nDRY RUN — would delete {len(empty)} company(ies) with no financial data. "
                  f"Re-run with --apply to do it.")


if __name__ == '__main__':
    main()
