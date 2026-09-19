"""
One-time cleanup for data left behind by the old pdf_parse.py bug in
extract_director_remuneration_detail() (table detection could span the
whole PDF and sweep unrelated tables in as "director pay" rows), plus
the duplicate SourceDocument rows that piled up from repeated uploads.

WHY THE PREVIOUS VERSION OF THIS SCRIPT DELETED NOTHING
The old version selected rows with `source_document_id IN (this
company's documents)`. But no import route ever stamped
source_document_id onto DirectorRemunerationRow / SurveyDirector /
SurveyDirectorBenefit / SurveyBenefitCategory - it was always NULL - so
that filter matched zero rows on real data. This version scopes rows by
COMPANY + PERIOD/FISCAL YEAR instead (which is how the import routes
themselves replace rows on re-upload).

WHAT IT DOES
  1. Deletes DirectorRemunerationRow, SurveyDirectorBenefit and
     SurveyBenefitCategory rows for the company (optionally one fiscal
     year). Re-upload the PDF(s) afterwards so the fixed extractor
     repopulates them - raw PDFs are not kept on disk.
  2. --merge-duplicate-docs: SourceDocument rows for the same company +
     sha256 (or URL, when there is no hash) + period_label are exact duplicates. Keeps the newest,
     repoints every foreign key (statements, import jobs, remuneration
     rows, committees, survey rows...) at it, deletes the extras.
  3. Reports (never changes) the same filename filed under different
     period_labels - that's a human decision about which year is right.

NEVER TOUCHES: Committee/CommitteeMember rows, SurveyDirector rows,
FinancialPeriod rows, SurveyCompanyData rows.

USAGE (Replit shell). Dry run first, always:
    python Cleanup_reversed_remuneration_bug.py --company "Equity Group Holdings"
    python Cleanup_reversed_remuneration_bug.py --company "Equity Group Holdings" --merge-duplicate-docs
    python Cleanup_reversed_remuneration_bug.py --company "Equity Group Holdings" --merge-duplicate-docs --apply
    python Cleanup_reversed_remuneration_bug.py --company "Equity Group Holdings" --fiscal-year FY2025 --apply

IMPORTANT: run this against the SAME database your published app uses.
On Replit the workspace shell and the published deployment can point at
different databases - cleaning the workspace DB does not change what
the published site renders.
"""

import argparse

from app import app, db
from models import (Company, SourceDocument, FinancialPeriod, FinancialStatement,
                    ImportJob, MarketDataSnapshot, PrincipalRisk, ManagementGuidance,
                    DirectorRemunerationRow, Committee)
from models_survey import SurveyDirector, SurveyDirectorBenefit, SurveyBenefitCategory

# Every table with a source_document_id foreign key.
DOC_FK_MODELS = (FinancialStatement, ImportJob, MarketDataSnapshot, PrincipalRisk,
                 ManagementGuidance, DirectorRemunerationRow, Committee,
                 SurveyDirector, SurveyDirectorBenefit, SurveyBenefitCategory)


def _find_companies(name):
    if not name:
        return Company.query.all()
    return Company.query.filter(Company.name.ilike(f'%{name}%')).all()


def _norm(v):
    return (v or '').strip().upper()


def report_docs(company):
    docs = SourceDocument.query.filter_by(company_id=company.id).order_by(SourceDocument.id).all()
    print(f'\n{company.name} (id={company.id}) - {len(docs)} SourceDocument row(s):')
    for d in docs:
        print(f'  id={d.id}  period={d.period_label}  file={d.filename}  '
              f'sha={(d.sha256 or "-")[:10]}  uploaded={d.uploaded_at}  score={d.extraction_score}')
    by_name = {}
    for d in docs:
        by_name.setdefault(d.filename, []).append(d)
    for filename, rows in by_name.items():
        labels = {r.period_label for r in rows}
        if filename and len(rows) > 1 and len(labels) > 1:
            print(f'  [REVIEW NEEDED] "{filename}" filed under different years: '
                  + ', '.join(f'{r.period_label} (id={r.id})' for r in rows))


def merge_duplicate_docs(company, apply):
    groups = {}
    for d in SourceDocument.query.filter_by(company_id=company.id).all():
        key = d.sha256 or d.url   # NSE-scan documents have a URL but no hash
        if key:
            groups.setdefault((key, d.period_label), []).append(d)
    merged = 0
    for (sha, label), rows in groups.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: r.id)
        keep, extras = rows[-1], rows[:-1]
        for extra in extras:
            print(f'[{"MERGE" if apply else "WOULD MERGE"}] {company.name}: source_document '
                  f'id={extra.id} -> id={keep.id} ({label}, {keep.filename})')
            if apply:
                for Model in DOC_FK_MODELS:
                    Model.query.filter(Model.source_document_id == extra.id).update(
                        {Model.source_document_id: keep.id}, synchronize_session=False)
                db.session.delete(extra)
            merged += 1
    return merged


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--company', default=None, help='Company name substring. Omit for every company.')
    parser.add_argument('--fiscal-year', default=None, help='Only this year, e.g. FY2025.')
    parser.add_argument('--merge-duplicate-docs', action='store_true',
                        help='Also merge exact-duplicate SourceDocument rows.')
    parser.add_argument('--apply', action='store_true', help='Actually change data. Default is dry-run.')
    args = parser.parse_args()

    with app.app_context():
        companies = _find_companies(args.company)
        if args.company and not companies:
            print(f'No company matching "{args.company}" found.')
            return
        if args.company and len(companies) > 1:
            print(f'"{args.company}" matched {len(companies)} companies - narrow it down:')
            for c in companies:
                print(f'  - {c.name} (id={c.id})')
            return

        tag = 'DELETE' if args.apply else 'WOULD DELETE'
        fy = _norm(args.fiscal_year)
        t_rem = t_ben = t_cat = t_merge = 0

        for company in companies:
            report_docs(company)

            # --- DirectorRemunerationRow: scoped by the company's periods ---
            periods = FinancialPeriod.query.filter_by(company_id=company.id).all()
            if fy:
                periods = [p for p in periods if _norm(p.period_label) == fy]
            period_ids = [p.id for p in periods]
            rem_rows = DirectorRemunerationRow.query.filter(
                DirectorRemunerationRow.period_id.in_(period_ids)).all() if period_ids else []
            for r in rem_rows:
                print(f'[{tag}] DirectorRemunerationRow "{r.director_name}" '
                      f'(total={r.total}, page={r.page}, period_id={r.period_id})')

            # --- survey side: scoped by company (+ fiscal year) ---
            ben_q = SurveyDirectorBenefit.query.filter_by(company_id=company.id)
            cat_q = SurveyBenefitCategory.query.filter_by(company_id=company.id)
            ben_rows = [b for b in ben_q.all() if not fy or _norm(b.fiscal_year) == fy]
            cat_rows = [c for c in cat_q.all() if not fy or _norm(c.fiscal_year) == fy]
            for b in ben_rows:
                print(f'[{tag}] SurveyDirectorBenefit "{b.director_name}" '
                      f'(total_amount={b.total_amount}, fiscal_year={b.fiscal_year})')

            t_rem += len(rem_rows); t_ben += len(ben_rows); t_cat += len(cat_rows)

            if args.apply:
                for r in rem_rows + ben_rows + cat_rows:
                    db.session.delete(r)

            if args.merge_duplicate_docs:
                t_merge += merge_duplicate_docs(company, args.apply)

        if args.apply:
            db.session.commit()
        verb = 'Deleted' if args.apply else 'Dry run - would delete'
        print(f'\n{verb}: {t_rem} DirectorRemunerationRow, {t_ben} SurveyDirectorBenefit, '
              f'{t_cat} SurveyBenefitCategory; duplicate documents merged: {t_merge}.')
        if not args.apply:
            print('Re-run with --apply to make these changes.')
        else:
            print('Now re-upload the affected PDF(s) through the normal import UI.')


if __name__ == '__main__':
    main()