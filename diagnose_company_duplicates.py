"""Read-only: shows where duplicate rows actually live for one company.
Usage: python diagnose_company_duplicates.py "Equity Group Holdings"
Changes nothing. Run it in the SAME environment/database as the app
you're looking at."""
import sys
from collections import Counter

from app import app
from models import (Company, SourceDocument, FinancialPeriod, FinancialStatement,
                    FinancialLineItem, DirectorRemunerationRow, Committee)
from models_survey import SurveyCompanyData, SurveyDirector, SurveyDirectorBenefit

name = sys.argv[1] if len(sys.argv) > 1 else 'Equity'
with app.app_context():
    for c in Company.query.filter(Company.name.ilike(f'%{name}%')).all():
        print(f'\n=== {c.name} (id={c.id}) ===')
        print('SourceDocuments:')
        for d in SourceDocument.query.filter_by(company_id=c.id).order_by(SourceDocument.id):
            print(f'  id={d.id} period={d.period_label} url={d.url} file={d.filename}')
        print('FinancialPeriods:')
        for p in FinancialPeriod.query.filter_by(company_id=c.id).order_by(FinancialPeriod.id):
            print(f'  id={p.id} label={p.period_label!r} fiscal_year={p.fiscal_year} type={p.period_type}')
            stmts = FinancialStatement.query.filter_by(period_id=p.id).all()
            types = Counter(s.statement_type for s in stmts)
            for s in stmts:
                items = FinancialLineItem.query.filter_by(statement_id=s.id).all()
                dup = {k: v for k, v in Counter((i.label, i.amount) for i in items).items() if v > 1}
                print(f'    stmt id={s.id} {s.statement_type} doc={s.source_document_id} '
                      f'items={len(items)} duplicate_label+amount={len(dup)}')
            extra = {t: n for t, n in types.items() if n > 1}
            if extra:
                print(f'    !! more than one statement of the same type: {extra}')
            rem = DirectorRemunerationRow.query.filter_by(period_id=p.id).count()
            com = Committee.query.filter_by(period_id=p.id).count()
            print(f'    remuneration_rows={rem} committees={com}')
        print('Survey side:')
        for r in SurveyCompanyData.query.filter_by(company_id=c.id):
            print(f'  SurveyCompanyData id={r.id} fiscal_year={r.fiscal_year!r}')
        dirs = Counter(d.fiscal_year for d in SurveyDirector.query.filter_by(company_id=c.id))
        bens = Counter(b.fiscal_year for b in SurveyDirectorBenefit.query.filter_by(company_id=c.id))
        print(f'  SurveyDirector rows by fiscal_year: {dict(dirs)}')
        print(f'  SurveyDirectorBenefit rows by fiscal_year: {dict(bens)}')
        names = Counter((d.fiscal_year, (d.director_name or '').strip().lower())
                        for d in SurveyDirector.query.filter_by(company_id=c.id))
        d2 = {k: v for k, v in names.items() if v > 1}
        print(f'  duplicate director names within a year: {d2 or "none"}')