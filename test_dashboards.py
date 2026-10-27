"""Super admin business dashboard and the member's "my company vs the market" dashboard."""
import dashboards
from test_security_seo import _make, _login


def _rows(fy):
    base = {'FY2025': [100, 200, 300, 400, 500], 'FY2024': [90, 210, 250, 380, 520]}[fy]
    return [{'company_id': i + 1, 'name': f'Co {i + 1}', 'sector': 'Banking' if i < 2 else 'Insurance', 'currency': 'KES',
             'chairperson_annual_retainer': v, 'directors_female': i, 'directors_male': 8 - i, 'board_size': 8,
             'independent_neds_count': 4} for i, v in enumerate(base)]


def test_compare_positions_a_company_against_the_market(monkeypatch):
    monkeypatch.setattr(dashboards, '_rows', _rows)
    monkeypatch.setattr(dashboards, '_years', lambda: ['FY2024', 'FY2025'])
    d = dashboards.compare(4, 'FY2025')
    chair = next(m for m in d['metrics'] if m['key'] == 'chairperson_annual_retainer')
    assert chair['value'] == 400 and chair['median'] == 300 and chair['rank'] == 2 and chair['percentile'] == 75
    assert chair['sector_median'] == 400 and round(chair['change_pct'], 1) == 5.3     # 380 -> 400
    women = next(m for m in d['metrics'] if m['key'] == 'women_on_board_pct')
    assert women['value'] == 37.5 and women['change_pts'] == 0.0
    assert d['previous_year'] == 'FY2024' and d['companies'] == 5 and d['sector_companies'] == 3


def test_headlines_compare_the_same_companies(monkeypatch):
    monkeypatch.setattr(dashboards, '_rows', _rows)
    monkeypatch.setattr(dashboards, '_years', lambda: ['FY2024', 'FY2025'])
    h = dashboards.headlines()
    chair = next(i for i in h['items'] if i['key'] == 'chairperson_annual_retainer')
    assert chair['median'] == 300 and chair['change_pct'] == 20.0 and chair['rose'] == 3 and chair['fell'] == 2


def test_business_dashboard_is_super_admin_only(client, app):
    from app import db, User
    _make(db, User, 'sup@x.com', 'admin', True)
    _make(db, User, 'adm@x.com', 'admin')
    _login(client, 'adm@x.com')
    assert client.get('/api/admin/dashboard').status_code == 403
    client.post('/api/logout')
    _login(client, 'sup@x.com')
    d = client.get('/api/admin/dashboard').get_json()
    assert 'customers' in d and 'revenue_by_month' in d and len(d['revenue_by_month']) == 12


def test_member_saves_their_company(client, app):
    from app import db, User, Company
    c = Company(name='Acme Holdings')
    db.session.add(c); db.session.commit()
    _make(db, User, 'm@x.com', 'user')
    _login(client, 'm@x.com')
    assert client.post('/api/home/my-company', json={'company_id': 999999}).status_code == 404
    r = client.post('/api/home/my-company', json={'company_id': c.id})
    assert r.status_code == 200 and r.get_json()['for'] == 'account'
    assert db.session.get(User, User.query.filter_by(email='m@x.com').first().id).home_company_id == c.id
