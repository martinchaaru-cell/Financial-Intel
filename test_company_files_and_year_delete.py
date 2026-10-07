"""Settings > Companies: list every company's files by year, delete one year, and the bulk-approve used by the review page."""
import io

import extraction_report as er
from test_extraction_evidence import PAGES, _pdf


def _login(client, db):
    from app import User
    u = User(name='adm', email='adm@x.com', role='admin')
    u.set_password('secret1')
    db.session.add(u)
    db.session.commit()
    assert client.post('/api/login', json={'email': 'adm@x.com', 'password': 'secret1'}).status_code == 200


def _seed(db):
    from app import Financials
    from models import Company, FinancialPeriod, FinancialStatement, SourceDocument, ImportJob
    from models_survey import SurveyCompanyData, SurveyDirector
    a = Company(name='Alpha Ltd', ticker='ALP', sector='Banking')
    b = Company(name='Beta Plc', ticker='BET')
    db.session.add_all([a, b])
    db.session.commit()
    for fy, fn in (('FY2025', 'ke-alp-2025-ar-00.pdf'), ('FY2026', 'ke-alp-2026-ar-00.pdf')):
        db.session.add(SurveyCompanyData(company_id=a.id, fiscal_year=fy, source_filename=fn, board_size=9,
                                         field_confidence={'board_size': 0.9}, field_review_status={}))
        db.session.add(SurveyDirector(company_id=a.id, fiscal_year=fy, director_name='A Person'))
        doc = SourceDocument(company_id=a.id, filename=fn, period_label=fy, page_count=120)
        db.session.add(doc)
        db.session.flush()
        per = FinancialPeriod(company_id=a.id, period_label=fy, period_type='FY', fiscal_year=int(fy[2:]))
        db.session.add(per)
        db.session.flush()
        db.session.add(FinancialStatement(period_id=per.id, statement_type='income_statement', source_document_id=doc.id))
        db.session.add(ImportJob(company_id=a.id, source_document_id=doc.id, status='saved'))
        db.session.add(Financials(company_id=a.id, period=fy, revenue=1.0))
        er.record_import(a.id, fy, fn, doc.id, _pdf(PAGES), kind='diagnose')
    db.session.add(SurveyCompanyData(company_id=b.id, fiscal_year='FY2026', source_filename=None, board_size=5))
    db.session.commit()
    return a.id, b.id


def test_files_listing(app, client, db):
    a, b = _seed(db)
    _login(client, db)
    r = client.get('/api/companies/files')
    assert r.status_code == 200
    d = r.get_json()
    assert d['years'] == ['FY2026', 'FY2025']
    alpha = next(c for c in d['companies'] if c['name'] == 'Alpha Ltd')
    assert alpha['years'] == ['FY2026', 'FY2025'] and len(alpha['files']) == 2
    f = alpha['files'][0]
    assert f['filename'] == 'ke-alp-2026-ar-00.pdf' and f['fiscal_year'] == 'FY2026' and f['has_survey'] and f['has_financials']
    assert f['text_stored'] is True and f['scored_fields'] == 1 and f['reviewed_fields'] == 0 and f['pages'] == 120
    beta = next(c for c in d['companies'] if c['name'] == 'Beta Plc')
    assert beta['files'][0]['filename'] is None and beta['files'][0]['has_survey']   # typed in by hand


def test_files_listing_is_admin_only(app, client, db):
    _seed(db)
    assert client.get('/api/companies/files').status_code in (401, 403)


def test_delete_one_year_keeps_company_and_other_years(app, client, db):
    from app import Financials
    from models import Company, FinancialPeriod, SourceDocument, ImportJob
    from models_survey import SurveyCompanyData, SurveyDirector
    a, b = _seed(db)
    _login(client, db)
    r = client.post('/api/companies/delete-year', json={'fiscal_year': 'FY2025', 'ids': [a, b]})
    assert r.status_code == 200
    d = r.get_json()
    assert [x['id'] for x in d['deleted']] == [a] and d['skipped'] == [b] and d['failed'] == []
    assert d['deleted'][0]['counts']['survey_rows'] == 1 and d['deleted'][0]['counts']['files'] == 1
    # FY2025 is gone everywhere ...
    assert SurveyCompanyData.query.filter_by(company_id=a, fiscal_year='FY2025').count() == 0
    assert SurveyDirector.query.filter_by(company_id=a, fiscal_year='FY2025').count() == 0
    assert FinancialPeriod.query.filter_by(company_id=a, period_label='FY2025').count() == 0
    assert SourceDocument.query.filter_by(company_id=a, period_label='FY2025').count() == 0
    assert Financials.query.filter_by(company_id=a, period='FY2025').count() == 0
    assert er.load_pages(a, 'FY2025') is None and er.ExtractionTrace.query.filter_by(company_id=a, fiscal_year='FY2025').count() == 0
    # ... FY2026, the company and the other company are untouched
    assert SurveyCompanyData.query.filter_by(company_id=a, fiscal_year='FY2026').count() == 1
    assert FinancialPeriod.query.filter_by(company_id=a, period_label='FY2026').count() == 1
    assert SourceDocument.query.filter_by(company_id=a, period_label='FY2026').count() == 1
    assert ImportJob.query.filter_by(company_id=a).count() == 1
    assert er.load_pages(a, 'FY2026') is not None
    assert db.session.get(Company, a) is not None and SurveyCompanyData.query.filter_by(company_id=b).count() == 1


def test_delete_year_validation_and_all(app, client, db):
    a, b = _seed(db)
    _login(client, db)
    assert client.post('/api/companies/delete-year', json={'ids': [a]}).status_code == 400
    assert client.post('/api/companies/delete-year', json={'fiscal_year': 'FY2026'}).status_code == 400
    r = client.post('/api/companies/delete-year', json={'fiscal_year': 'FY2026', 'all': True})
    d = r.get_json()
    assert r.status_code == 200 and sorted(x['id'] for x in d['deleted']) == sorted([a, b])
    # a year nobody has -> nothing to do, not an error
    r = client.post('/api/companies/delete-year', json={'fiscal_year': 'FY1999', 'all': True})
    assert r.status_code == 200 and r.get_json()['deleted'] == []


def test_whole_company_delete_also_removes_stored_text(app, client, db):
    a, b = _seed(db)
    _login(client, db)
    assert client.delete(f'/api/companies/{a}').status_code == 200
    assert er.ExtractionPages.query.filter_by(company_id=a).count() == 0 and er.ExtractionTrace.query.filter_by(company_id=a).count() == 0


def test_bulk_approve(app, client, db):
    from models_survey import SurveyCompanyData
    a, _ = _seed(db)
    _login(client, db)
    row = SurveyCompanyData.query.filter_by(company_id=a, fiscal_year='FY2026').first()
    row.field_confidence = {'board_size': 0.9, 'turnover': 0.5}      # turnover has no value
    db.session.commit()
    r = client.post(f'/api/survey-data/{a}/FY2026/review-bulk', json={'fields': ['board_size', 'turnover', 'not_a_field']})
    d = r.get_json()
    assert r.status_code == 200 and d['approved'] == ['board_size'] and d['skipped'] == ['turnover', 'not_a_field']
    assert d['survey_data']['field_review_status'] == {'board_size': 'approved'}
    assert client.post(f'/api/survey-data/{a}/FY2026/review-bulk', json={'fields': []}).status_code == 400
