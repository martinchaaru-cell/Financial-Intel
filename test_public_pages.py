import pytest

import public_pages as pp
from models import db as _db, Company
from models_survey import SurveyCompanyData


@pytest.fixture(autouse=True)
def _pages_on(monkeypatch):
    monkeypatch.setenv('PUBLIC_COMPANY_PAGES', '1')      # switched off by default; these tests cover the pages themselves


def test_pages_are_off_by_default_and_send_visitors_home(client, monkeypatch):
    monkeypatch.delenv('PUBLIC_COMPANY_PAGES')
    r = client.get('/companies')
    assert r.status_code == 301 and r.headers['Location'].endswith('/')


@pytest.fixture()
def seeded(app):
    kcb = Company(name='KCB Group Plc', ticker='KCB', sector='Banking')
    empty = Company(name='No Data Ltd')
    dup = Company(name='KCB Group Limited')
    _db.session.add_all([kcb, empty, dup]); _db.session.commit()
    _db.session.add_all([
        SurveyCompanyData(company_id=kcb.id, fiscal_year='FY2025', board_size=12, executive_directors_count=2,
                          non_executive_directors_count=10, independent_neds_count=7, directors_female=4,
                          board_meetings_per_year=6, ceo_monthly_salary=9999999, chairperson_annual_retainer=7777777),
        SurveyCompanyData(company_id=kcb.id, fiscal_year='FY2024', board_size=11),
        SurveyCompanyData(company_id=kcb.id, fiscal_year='H1 2025', board_size=99),   # interim: must be ignored
    ])
    _db.session.commit()
    return kcb, empty, dup


def test_slugs_are_readable_and_unique(seeded):
    kcb, empty, dup = seeded
    pp.ensure_slugs()
    assert kcb.slug == 'kcb-group' and dup.slug == f'kcb-group-{dup.id}'
    assert pp.make_slug('Stanbic Holdings & Co') == 'stanbic-holdings-and'


def test_company_page_has_unique_title_description_canonical(client, seeded):
    r = client.get('/company/kcb-group')
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert '<title>KCB Group Plc (KCB) Board Composition &amp; Governance FY2025 | Finance Intel</title>' in html
    assert '12 directors' in html and 'rel="canonical"' in html and '/company/kcb-group"' in html
    assert 'index, follow' in html and 'BreadcrumbList' in html
    assert 'H1 2025' not in html and '99' not in html          # interim rows never shown


def test_pay_figures_are_never_on_a_public_page(client, seeded):
    html = client.get('/company/kcb-group').get_data(as_text=True) + client.get('/companies').get_data(as_text=True)
    assert '9999999' not in html and '7777777' not in html and '9,999,999' not in html


def test_company_without_data_is_noindex_and_not_in_sitemap(client, seeded):
    _, empty, _ = seeded
    pp.ensure_slugs()
    assert 'noindex' in client.get(f'/company/{empty.slug}').get_data(as_text=True)
    assert 'no-data' not in client.get('/sitemap.xml').get_data(as_text=True)


def test_unknown_slug_404_and_id_redirects(client, seeded):
    kcb, _, _ = seeded
    assert client.get('/company/nope').status_code == 404
    r = client.get(f'/company/{kcb.id}')
    assert r.status_code == 301 and r.headers['Location'].endswith('/company/kcb-group')


def test_sitemap_lists_directory_and_company_pages(client, seeded):
    xml = client.get('/sitemap.xml').get_data(as_text=True)
    assert '/companies</loc>' in xml and '/company/kcb-group</loc>' in xml and '<lastmod>' in xml


def test_switch_off(client, seeded, monkeypatch):
    monkeypatch.setenv('PUBLIC_COMPANY_PAGES', '0')
    assert client.get('/company/kcb-group').status_code == 301           # sent to the home page
    assert '/company/' not in client.get('/sitemap.xml').get_data(as_text=True)
