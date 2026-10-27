"""Public website, the /app entrance, package-based pages and the members' home summary."""
import pytest

import billing
from migrate_v11_billing import seed_plans
from models import User
from models_billing import Organisation, Plan

INTEL = '/api/intelligence-layer/companies'      # Team and above
DQ = '/api/survey/data-quality-average'          # Team and above
ANALYTICS = '/api/survey/overview'               # Professional and above


@pytest.fixture()
def plans(db):
    seed_plans()
    return {p.code: p for p in Plan.query.all()}


@pytest.fixture()
def enforced(monkeypatch):
    monkeypatch.setenv('BILLING_ENFORCED', 'true')
    monkeypatch.setenv('EXPORT_DAILY_LIMIT', '0')


def member(app, db, email, plan=None, plans=None):
    org = None
    if plan:
        org = Organisation(name=email.split('@')[1])
        db.session.add(org)
        db.session.commit()
    u = User(name='Jane Wanjiru', email=email, role='user', organization_id=org.id if org else None,
             org_role='owner' if org else None)
    u.set_password('secret123')
    db.session.add(u)
    db.session.commit()
    if plan:
        billing.start_subscription(org, plans[plan], mode='complimentary')
    c = app.test_client()
    assert c.post('/api/login', json={'email': email, 'password': 'secret123'}).status_code == 200
    return c


# ---------------------------------------------------------------- website
@pytest.mark.parametrize('path', ['/', '/about', '/features', '/pricing', '/contact', '/login', '/register'])
def test_website_pages_load_for_guests(client, plans, path):
    r = client.get(path)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'Financial Intel' in html and '<nav class="nav"' in html


def test_pricing_lists_the_plans(client, plans):
    html = client.get('/pricing').get_data(as_text=True)
    assert 'KES 150,000' in html and 'KES 450,000' in html and 'Contact us for a quote' in html


def test_guests_are_sent_to_sign_in_for_the_app(client):
    r = client.get('/app')
    assert r.status_code == 302 and r.headers['Location'].startswith('/login?next=/app')


def test_signed_in_users_get_the_app_and_skip_the_sign_in_page(app, db, plans):
    c = member(app, db, 'm@x.example')
    assert c.get('/app').status_code == 200
    assert c.get('/login').status_code == 302
    assert 'Open dashboard' in c.get('/').get_data(as_text=True)


def test_sign_in_page_only_returns_to_this_site(client):
    html = client.get('/login?next=https://evil.example/steal').get_data(as_text=True)
    assert 'evil.example' not in html.split('const NEXT')[1][:60]


def test_closed_registration_shows_invitation_note(client, monkeypatch):
    monkeypatch.setenv('ALLOW_SELF_REGISTRATION', 'false')
    html = client.get('/register').get_data(as_text=True)
    assert 'Accounts by invitation' in html and 'Get started' not in html


# ---------------------------------------------------------------- packages
def test_free_account_gets_dashboard_and_plans_only(app, db, plans, enforced):
    c = member(app, db, 'free@x.example')
    me = c.get('/api/billing/me').get_json()
    assert me['tier'] == 'free' and set(me['features']) == {'home', 'billing'}
    assert c.get('/api/home/summary').status_code == 200
    assert c.get(ANALYTICS).status_code == 402


def test_professional_opens_reports_but_not_intelligence(app, db, plans, enforced):
    c = member(app, db, 'pro@x.example', 'professional_annual', plans)
    me = c.get('/api/billing/me').get_json()
    assert me['tier'] == 'professional' and 'reports' in me['features'] and 'intelligence' not in me['features']
    assert c.get(ANALYTICS).status_code == 200
    r = c.get(INTEL)
    assert r.status_code == 402 and r.get_json()['reason'] == 'upgrade' and r.get_json()['needs'] == 'Team'
    assert c.get(DQ).status_code == 402


def test_team_and_enterprise_open_everything(app, db, plans, enforced):
    for code, email in (('team_monthly', 't@team.example'), ('enterprise_annual', 'e@ent.example')):
        c = member(app, db, email, code, plans)
        assert c.get(INTEL).status_code != 402
        assert c.get(DQ).status_code != 402
        assert 'intelligence' in c.get('/api/billing/me').get_json()['features']


def test_tier_comes_from_the_plan_not_its_name(app, db, plans, enforced, ):
    custom = Plan(code='board_pack', name='Board pack', price=1, tier='team')
    db.session.add(custom)
    db.session.commit()
    plans['board_pack'] = custom
    c = member(app, db, 'b@pack.example', 'board_pack', plans)
    assert c.get('/api/billing/me').get_json()['tier'] == 'team'
    assert billing.infer_tier('enterprise_usd') == 'enterprise' and billing.infer_tier('custom') == 'professional'


def test_everyone_keeps_full_member_pages_while_billing_is_off(app, db, plans):
    c = member(app, db, 'off@x.example')
    me = c.get('/api/billing/me').get_json()
    assert me['tier'] == 'team' and c.get(INTEL).status_code != 402


def test_staff_see_everything(app, db, plans, enforced):
    u = User(name='Boss', email='boss@fi.example', role='admin', is_super_admin=True)
    u.set_password('secret123')
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    c.post('/api/login', json={'email': 'boss@fi.example', 'password': 'secret123'})
    me = c.get('/api/billing/me').get_json()
    assert me['tier'] == 'staff' and {'intelligence', 'data_quality', 'reports'} <= set(me['features'])


def test_upgrading_unlocks_pages(app, db, plans, enforced):
    c = member(app, db, 'grow@grow.example', 'professional_monthly', plans)
    assert c.get(INTEL).status_code == 402
    org = Organisation.query.filter_by(name='grow.example').one()
    billing.start_subscription(org, plans['team_monthly'], mode='complimentary')
    assert c.get(INTEL).status_code != 402


def test_home_summary_needs_sign_in_and_counts_coverage(client, app, db, plans):
    assert client.get('/api/home/summary').status_code in (401, 403)
    c = member(app, db, 'h@x.example')
    d = c.get('/api/home/summary').get_json()
    assert d['companies'] == 0 and d['has_data'] is False


def test_staff_can_set_a_plan_tier(app, db, plans, enforced):
    u = User(name='Boss', email='boss2@fi.example', role='admin', is_super_admin=True)
    u.set_password('secret123')
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    c.post('/api/login', json={'email': 'boss2@fi.example', 'password': 'secret123'})
    pid = plans['professional_annual'].id
    assert c.patch(f'/api/admin/billing/plans/{pid}', json={'tier': 'team'}).get_json()['tier'] == 'team'
    assert c.patch(f'/api/admin/billing/plans/{pid}', json={'tier': 'gold'}).status_code == 400


def test_existing_plans_get_a_tier_when_upgrading(db, plans):
    from sqlalchemy import text
    import migrate_v13_plan_tier as m13
    db.session.execute(text('ALTER TABLE billing_plans DROP COLUMN tier'))
    db.session.commit()
    db.session.expire_all()
    m13.run_migration()
    db.session.expire_all()
    got = {p.code: p.tier for p in Plan.query.all()}
    assert got['team_annual'] == 'team' and got['enterprise_annual'] == 'enterprise' and got['professional_monthly'] == 'professional'
