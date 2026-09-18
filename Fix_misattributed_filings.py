"""
One-time repair for filings that got saved under the WRONG company by the
old, too-permissive `match_company()` (min_score was 0.55 - see
nse_import.py's updated docstring for the concrete false positive this
caused: "Family Bank Limited" scored 0.6 against "Absa Bank Kenya" purely
from short-string character overlap, so its financial statements got
saved into Absa's records instead of its own).

Because a FinancialPeriod only ever has ONE FinancialStatement row per
statement_type (see the uq_period_statement constraint in models.py),
a second filing for the same period/statement_type doesn't create a
separate, cleanly-removable row - `_save_one_import()` finds the existing
statement and appends the new filing's line items straight into it. That
means a misattributed filing's numbers are interleaved, item-by-item,
with the correct company's own numbers inside the same statement - there
is no reliable way to pull just the bad rows back out after the fact.
So the fix here is a full purge, not a surgical delete: any
FinancialPeriod that has so much as one statement sourced from a
misattributed document gets removed entirely, along with the
misattributed SourceDocument row(s) themselves. Nothing is re-created by
this script - re-run your normal NSE scan/import (or single-file upload)
afterward for every company this reports, now that nse_import.py's
company list/threshold won't misattribute them again.

How a filing is judged "misattributed": each SourceDocument's `url` (or,
for a direct upload with no URL, its `filename`) still has the original
filing's company name embedded in it (e.g.
".../Family-Bank-Limited---Unaudited-Financial-Statements-...pdf"). This
re-derives that name and re-runs match_company() against it (using
today's NSE_COMPANIES + the corrected 0.75 threshold) - if the result is
a confident match for a DIFFERENT company than the one the document is
actually attached to, it's flagged.

Usage (from a Replit shell, with the app's environment/DATABASE_URL
already set):
    python fix_misattributed_filings.py            # dry run - just reports
    python fix_misattributed_filings.py --apply    # actually deletes
"""

import re
import sys

from app import app, db, SourceDocument, FinancialStatement, Company
from company_directory import match_company, NSE_COMPANIES


def _claimed_company_name(doc: SourceDocument):
    """Best-effort recovery of the company name a SourceDocument's own
    url/filename implies, from the NSE filename convention seen in
    practice: '<Company-Name-With-Dashes>---<rest of the filing title
    with dashes>.pdf'. Falls back to the whole filename (dashes ->
    spaces) if there's no '---' separator, on the theory that
    match_company()'s own fuzzy matching handles the extra noise fine -
    it already works directly on messy NSE filing titles."""
    source = (doc.url or doc.filename or '').rsplit('/', 1)[-1]
    source = re.sub(r'\.pdf$', '', source, flags=re.IGNORECASE)
    if not source:
        return None
    head = source.split('---', 1)[0]
    return head.replace('-', ' ').strip() or None


def main():
    apply_changes = '--apply' in sys.argv

    with app.app_context():
        docs = SourceDocument.query.all()
        flagged = []  # (doc, correct_company_name, score)

        for doc in docs:
            claimed_name = _claimed_company_name(doc)
            if not claimed_name:
                continue
            matched, score = match_company(claimed_name)
            if matched is None:
                continue  # can't tell either way - leave it alone
            actual_name = (doc.company.name if doc.company else '').strip().lower()
            if matched['name'].strip().lower() != actual_name:
                flagged.append((doc, matched['name'], score))

        if not flagged:
            print("No misattributed source documents found.")
            periods_to_delete = {}
        else:
            # Collect the distinct periods that need to be purged: any
            # period with at least one statement sourced from a flagged
            # document.
            periods_to_delete = {}  # period_id -> FinancialPeriod
            for doc, correct_name, score in flagged:
                wrong_company = doc.company.name if doc.company else f'company_id={doc.company_id}'
                print(f"MISATTRIBUTED: {doc.url or doc.filename!r}")
                print(f"  currently attached to: {wrong_company!r}")
                print(f"  filing is actually for: {correct_name!r} (match score {score})")

            flagged_doc_ids = {doc.id for doc, _, _ in flagged}
            for stmt in FinancialStatement.query.filter(
                    FinancialStatement.source_document_id.in_(flagged_doc_ids)).all():
                periods_to_delete[stmt.period_id] = stmt.period

            print(f"\n{len(flagged)} misattributed document(s) found, "
                  f"affecting {len(periods_to_delete)} period(s):")
        for period in periods_to_delete.values():
            company_name = period.company.name if period.company else f'company_id={period.company_id}'
            print(f"  - {company_name!r}, period {period.period_label!r} (id={period.id}) - will be deleted whole")

        if apply_changes:
            for period in periods_to_delete.values():
                db.session.delete(period)
            for doc, _, _ in flagged:
                db.session.delete(doc)
            db.session.commit()
            print(f"\nDeleted {len(periods_to_delete)} period(s) and {len(flagged)} "
                  f"misattributed document(s). Re-import each company listed above "
                  f"(NSE scan/save or single-file upload) to get clean data.")
        else:
            print(f"\nDRY RUN - would delete {len(periods_to_delete)} period(s) and "
                  f"{len(flagged)} document(s). Re-run with --apply to do it.")

        # ---- Second, unrelated pass: backfill missing sectors ----
        # A separate bug from the same session - saving a filing for an
        # EXISTING company (the common case) passes company_id straight
        # through and never touches sector at all, so any Company row
        # created before sector-matching existed (or from a match that
        # didn't carry a sector) is stuck with a blank sector forever,
        # even though nse_import.NSE_COMPANIES has always known it - this
        # shows up as "This company has no sector assigned, so peer
        # comparison is unavailable" on an otherwise fully-populated
        # company page. Ticker is the reliable join key here (unlike
        # name, which can drift in casing/punctuation).
        by_ticker = {c['ticker'].lower(): c['sector'] for c in NSE_COMPANIES}
        blank_sector = Company.query.filter(
            db.or_(Company.sector.is_(None), Company.sector == '')
        ).all()
        to_backfill = [
            (c, by_ticker[c.ticker.strip().lower()])
            for c in blank_sector
            if c.ticker and c.ticker.strip().lower() in by_ticker
        ]
        if to_backfill:
            print(f"\n{len(to_backfill)} compan(y/ies) with a blank sector "
                  f"can be backfilled from NSE_COMPANIES:")
            for c, sector in to_backfill:
                print(f"  - {c.name!r} (ticker {c.ticker}) -> sector {sector!r}")
            if apply_changes:
                for c, sector in to_backfill:
                    c.sector = sector
                db.session.commit()
                print(f"Backfilled {len(to_backfill)} compan(y/ies).")
            else:
                print("(dry run - re-run with --apply to write these)")


if __name__ == '__main__':
    main()