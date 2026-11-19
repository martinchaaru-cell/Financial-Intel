"""Read-only audit. Run from application root with the configured database environment."""
import csv
import sys
from monetary_units import pay_scale, audit_flags, SCALES
from pay_evidence import reconcile

def main():
    from app import app
    from models import Company, FinancialPeriod, DirectorRemunerationRow
    from models_survey import SurveyCompanyData
    writer = csv.writer(sys.stdout)
    writer.writerow(['company','year','row_id','director','raw_total','pay_unit','headline_unit','corrected_display','old_display','flags','source_document_id','page','unit_evidence'])
    with app.app_context():
        for survey, period, company in (SurveyCompanyData.query.join(FinancialPeriod, (FinancialPeriod.company_id == SurveyCompanyData.company_id) & (FinancialPeriod.period_label == SurveyCompanyData.fiscal_year)).join(Company, Company.id == SurveyCompanyData.company_id).with_entities(SurveyCompanyData, FinancialPeriod, Company).yield_per(100)):
            scale = pay_scale(survey.director_figures_unit)
            old_scale = pay_scale(survey.director_figures_unit or survey.unit)
            revenue = survey.turnover * pay_scale(survey.unit) if survey.turnover is not None else None
            ceo = survey.ceo_monthly_cost_of_employment * scale * 12 if survey.ceo_monthly_cost_of_employment is not None else None
            rows = DirectorRemunerationRow.query.filter_by(period_id=period.id).all()
            groups = {}
            for r in rows:
                if r.source_document_id and r.page and r.table_kind not in ('manual', 'unknown', None):
                    groups.setdefault((r.source_document_id, r.page, r.table_kind), []).append(r)
            mismatches = set()
            for group in groups.values():
                totals = [r for r in group if r.is_grand_total]
                named = [r for r in group if not r.is_grand_total and not r.is_total_row]
                if len(totals) == 1 and len({r.director_name for r in named}) == len(named) and survey.director_figures_unit in SCALES:
                    check = reconcile([r.total for r in named], totals[0].total, survey.director_figures_unit)
                    if check and not check['within_rounding']:
                        mismatches.add(totals[0].id)
            for r in rows:
                if r.total is None:
                    continue
                flags = audit_flags(r.total, survey.director_figures_unit, None if r.is_grand_total or r.is_total_row else ceo, revenue)
                if old_scale != scale:
                    flags.append('previous_display_scale_changed')
                if not survey.director_figures_unit:
                    flags.append('missing_explicit_pay_unit_verify_source')
                if r.id in mismatches:
                    flags.append('possible_table_total_mismatch_verify_completeness')
                if survey.director_figures_unit and survey.director_figures_unit not in SCALES:
                    flags.append('unknown_pay_unit_verify_source')
                if flags:
                    writer.writerow([company.name,survey.fiscal_year,r.id,r.director_name,r.total,survey.director_figures_unit,survey.unit,r.total*scale,r.total*old_scale,';'.join(flags),r.source_document_id,r.page,(survey.field_sources or {}).get('director_figures_unit')])

if __name__ == '__main__':
    main()
