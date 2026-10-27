"""Billing: plans, trials, invoices, payments, Paystack, plan-based access, seats and one sign-in per account."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta

import pytest

import billing
from migrate_v11_billing import seed_plans
from models import User
from models_billing import Invoice, Organisation, Payment, Plan, Subscription
from models_survey import ActivityLogEntry

MEMBER_PAGE = '/api/survey/overview'        # needs an account (not on the guest list)
EXPORT = '/api/export/portfolio'           # counts as a download


@pytest.fixture()
def plans(db):
    seed_plans()
    return {p.code: p for p in Plan.query.all()}


@pytest.fixture()
def enforced(monkeypatch):
    monkeypatch.setenv('BILLING_ENFORCED', 'true')
    monkeypatch.setenv('EXPORT_DAILY_LIMIT', '0')


@pytest.fixture()
def clock(monkeypatch):
    """Move billing's clock: clock.set(datetime) / clock.add(days=...)."""
    class Clock:
        t = datetime(2026, 11, 1, 9, 0, 0)

        def set(self, t):
            self.t = t

        def add(self, **kw):
            self.t = self.t + timedelta(**kw)
    c = Clock()
    monkeypatch.setattr(billing, 'now', lambda: c.t)
    return c


def make_user(db, email, role='user', super_admin=False, org=None, org_role=None, name='Person'):
    u = User(name=name, email=email, role=role, is_super_admin=super_admin,
             organization_id=org.id if org else None, org_role=org_role)
    u.set_password('secret123')
    db.session.add(u)
    db.session.commit()
    return u


def login(client, email):
    r = client.post('/api/login', json={'email': email, 'password': 'secret123'})
    assert r.status_code == 200, r.get_json()
    return client


@pytest.fixture()
def staff(app, db):
    make_user(db, 'boss@fi.example', role='admin', super_admin=True, name='Boss')
    return login(app.test_client(), 'boss@fi.example')


# ------------------------------------------------------------------ plans and the off switch
def test_default_plans_are_seeded_once_and_public(client, plans):
    assert {'professional_monthly', 'professional_annual', 'team_monthly', 'team_annual', 'enterprise_annual'} <= set(plans)
    assert plans['professional_annual'].price == 150000 and plans['team_annual'].seats == 5
    seed_plans()
    assert Plan.query.count() == 5
    r = client.get('/api/billing/plans')                    # guests can see prices
    assert r.status_code == 200 and len(r.get_json()['plans']) == 5


def test_nothing_is_blocked_while_billing_is_off(client, db, plans):
    make_user(db, 'm@x.example')
    login(client, 'm@x.example')
    assert client.get(MEMBER_PAGE).status_code != 402
    me = client.get('/api/billing/me').get_json()
    assert me['enforced'] is False and me['level'] == 'full'


# ------------------------------------------------------------------ the self-serve journey
def test_member_without_plan_is_asked_to_choose_one(client, db, plans, enforced):
    make_user(db, 'm@x.example')
    login(client, 'm@x.example')
    r = client.get(MEMBER_PAGE)
    assert r.status_code == 402 and r.get_json()['billing'] is True
    assert client.get('/api/companies').status_code != 402          # the public overview still works
    me = client.get('/api/billing/me').get_json()
    assert me['level'] == 'none' and 'Choose a plan' in me['banner']


def test_trial_then_payment_then_grace_then_read_only(client, db, plans, enforced, clock, staff):
    make_user(db, 'owner@acme.example')
    login(client, 'owner@acme.example')
    r = client.post('/api/billing/subscribe', json={'plan_code': 'professional_annual',
                                                     'organisation': {'name': 'Acme Ltd', 'kra_pin': 'P051234567X'}})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body['trial'] is True
    inv = body['invoice']
    assert inv['number'] == 'FI-2026-0001' and inv['total'] == 150000 and inv['status'] == 'open'
    org = Organisation.query.one()
    assert org.trial_used and org.billing_email == 'owner@acme.example'
    assert User.query.filter_by(email='owner@acme.example').one().org_role == 'owner'

    # trial: full access, with a countdown banner
    assert client.get(MEMBER_PAGE).status_code != 402
    assert '14 day' in client.get('/api/billing/me').get_json()['banner']

    # a second trial is not given to the same organisation
    r2 = client.post('/api/billing/subscribe', json={'plan_code': 'team_annual'})
    assert r2.get_json()['trial'] is False

    # trial ends unpaid -> no access
    client.post('/api/billing/subscribe', json={'plan_code': 'professional_annual'})  # back to the plan, pay first
    sub = Subscription.query.order_by(Subscription.id.desc()).first()
    inv_obj = Invoice.query.filter_by(subscription_id=sub.id).one()
    clock.add(days=15)
    trial_sub = Subscription.query.filter_by(status='trialing').first()
    if trial_sub is not None:
        assert billing.effective_status(trial_sub) == 'expired'
    assert client.get(MEMBER_PAGE).status_code == 402

    # the super admin records a bank transfer: a year of access starts
    r = staff.post(f'/api/admin/billing/invoices/{inv_obj.id}/payments',
                   json={'amount': inv_obj.total, 'method': 'bank_transfer', 'reference': 'EQ-778812'})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()['invoice']['status'] == 'paid'
    assert client.get(MEMBER_PAGE).status_code != 402
    sub = db.session.get(Subscription, sub.id)
    assert sub.status == 'active'
    end = sub.current_period_end

    # renewal unpaid: a week of grace with a warning, then read-only (view yes, export / change no)
    clock.set(end + timedelta(days=3))
    me = client.get('/api/billing/me').get_json()
    assert me['level'] == 'full' and 'overdue' in me['banner']
    clock.set(end + timedelta(days=8))
    assert client.get(MEMBER_PAGE).status_code != 402
    assert client.get(EXPORT).status_code == 402
    assert client.post('/api/companies', json={'name': 'X'}).status_code in (402, 403)
    assert client.get('/api/billing/me').get_json()['level'] == 'read_only'


def test_paying_during_the_trial_keeps_the_trial_days(db, plans, clock):
    org = Organisation(name='Early Payer')
    db.session.add(org)
    db.session.commit()
    sub, inv = billing.start_subscription(org, plans['professional_monthly'], mode='trial')
    trial_end = sub.trial_end
    billing.apply_payment(inv, inv.total, 'mpesa', 'QK12AB34')
    sub = db.session.get(Subscription, sub.id)
    assert sub.status == 'active' and sub.current_period_start == trial_end
    assert billing.effective_status(sub) == 'active'          # still covered between now and the trial end


def test_partial_payment_leaves_invoice_open(db, plans):
    org = Organisation(name='Halfway')
    db.session.add(org)
    db.session.commit()
    sub, inv = billing.start_subscription(org, plans['team_monthly'], mode='invoice')
    billing.apply_payment(inv, 20000, 'bank_transfer', 'A1')
    assert inv.status == 'open' and db.session.get(Subscription, sub.id).status == 'pending'
    billing.apply_payment(inv, 25000, 'bank_transfer', 'A2')
    assert inv.status == 'paid' and db.session.get(Subscription, sub.id).status == 'active'


def test_upgrade_replaces_the_old_plan_only_once_paid(db, plans, clock):
    org = Organisation(name='Growing Co')
    db.session.add(org)
    db.session.commit()
    old, _ = billing.start_subscription(org, plans['professional_annual'], mode='complimentary')
    new, inv = billing.start_subscription(org, plans['team_annual'], mode='invoice')
    assert db.session.get(Subscription, old.id).status == 'active'      # still working while unpaid
    billing.apply_payment(inv, inv.total, 'bank_transfer', 'UPG')
    assert db.session.get(Subscription, old.id).status == 'cancelled'
    best, st = billing.best_subscription(org.id)
    assert best.id == new.id and best.plan.code == 'team_annual' and st == 'active'


# ------------------------------------------------------------------ renewals
def test_sweep_raises_one_renewal_invoice_per_period(db, plans, clock):
    org = Organisation(name='Renewer')
    db.session.add(org)
    db.session.commit()
    sub, _ = billing.start_subscription(org, plans['professional_monthly'], mode='complimentary')
    assert billing.run_sweep()['renewal_invoices'] == 0                      # a month away
    clock.set(sub.current_period_end - timedelta(days=3))
    assert billing.run_sweep()['renewal_invoices'] == 1
    assert billing.run_sweep()['renewal_invoices'] == 0                      # not twice
    inv = Invoice.query.filter_by(kind='renewal').one()
    assert inv.period_start == sub.current_period_end and inv.due_at == sub.current_period_end
    billing.apply_payment(inv, inv.total, 'mpesa', 'RNW1')
    assert db.session.get(Subscription, sub.id).current_period_end == inv.period_end


def test_cancel_at_period_end_stops_renewal_and_access(client, db, plans, enforced, clock):
    org = Organisation(name='Leaver')
    db.session.add(org)
    db.session.commit()
    make_user(db, 'own@leaver.example', org=org, org_role='owner')
    sub, _ = billing.start_subscription(org, plans['professional_monthly'], mode='complimentary')
    login(client, 'own@leaver.example')
    assert client.post('/api/billing/cancel', json={}).status_code == 200
    clock.set(sub.current_period_end - timedelta(days=2))
    assert billing.run_sweep()['renewal_invoices'] == 0
    clock.set(sub.current_period_end + timedelta(days=1))
    assert client.get(MEMBER_PAGE).status_code == 402


# ------------------------------------------------------------------ seats
def test_seat_limits(client, db, plans, enforced):
    org = Organisation(name='Small Team')
    db.session.add(org)
    db.session.commit()
    make_user(db, 'own@small.example', org=org, org_role='owner')
    billing.start_subscription(org, plans['professional_annual'], mode='complimentary')     # 1 seat
    login(client, 'own@small.example')
    r = client.post('/api/billing/organisation/members',
                    json={'name': 'Colleague', 'email': 'col@small.example', 'password': 'secret123'})
    assert r.status_code == 409
    billing.start_subscription(org, plans['team_annual'], mode='complimentary')            # 5 seats
    r = client.post('/api/billing/organisation/members',
                    json={'name': 'Colleague', 'email': 'col@small.example', 'password': 'secret123'})
    assert r.status_code == 201
    col = User.query.filter_by(email='col@small.example').one()
    assert col.organization_id == org.id and col.org_role == 'member' and col.role == 'user'
    # squeeze back to one seat: the owner keeps it, the colleague loses access
    sub, _ = billing.best_subscription(org.id)
    sub.seats_override = 1
    db.session.commit()
    assert billing.access_for(col)['reason'] == 'no_seat'
    assert billing.access_for(User.query.filter_by(email='own@small.example').one())['level'] == 'full'
    # members cannot manage the organisation
    other = login(client.application.test_client(), 'col@small.example')
    assert other.post('/api/billing/organisation/members', json={}).status_code == 403


# ------------------------------------------------------------------ one sign-in per account
def test_second_sign_in_signs_out_the_first(app, db):
    make_user(db, 'shared@x.example')
    a, b = app.test_client(), app.test_client()
    login(a, 'shared@x.example')
    assert a.get('/api/me').get_json()['logged_in'] is True
    login(b, 'shared@x.example')
    r = a.get('/api/me')
    assert r.get_json()['logged_in'] is False and r.headers.get('X-Session-Ended') == 'replaced'
    assert b.get('/api/me').get_json()['logged_in'] is True


def test_admins_can_stay_signed_in_on_two_devices(app, db):
    make_user(db, 'staff@fi.example', role='admin')
    a, b = app.test_client(), app.test_client()
    login(a, 'staff@fi.example')
    login(b, 'staff@fi.example')
    assert a.get('/api/me').get_json()['logged_in'] is True


def test_single_session_can_be_switched_off(app, db, monkeypatch):
    monkeypatch.setenv('SINGLE_SESSION', 'false')
    make_user(db, 'two@x.example')
    a, b = app.test_client(), app.test_client()
    login(a, 'two@x.example')
    login(b, 'two@x.example')
    assert a.get('/api/me').get_json()['logged_in'] is True


# ------------------------------------------------------------------ downloads by kind
REPORT = '/api/companies/1/export/report.pdf'
DATA = '/api/companies/1/export/raw.csv'
BULK = '/api/export/portfolio'


def _downloads(db, user, path, n, when):
    for _ in range(n):
        db.session.add(ActivityLogEntry(action='get:download', user_id=user.id, category='Exports & Downloads',
                                        path=path, created_at=when))
    db.session.commit()


def _member_on(db, plans, code, email):
    org = Organisation(name=email.split('@')[1])
    db.session.add(org)
    db.session.commit()
    u = make_user(db, email, org=org, org_role='owner')
    billing.start_subscription(org, plans[code], mode='complimentary')
    return u


def test_download_kinds():
    k = billing.download_kind
    assert k('/api/companies/3/export/report.pdf') == 'report'
    assert k('/api/companies/3/periods/FY2025/report.docx') == 'report'
    assert k('/api/export/combined-report.pdf?fiscal_year=all') == 'report'
    assert k('/api/companies/3/export/raw.csv') == 'data'
    assert k('/api/companies/3/export/snapshot') == 'data'
    assert k('/api/manual/forms.zip?fiscal_year=FY2025') == 'bulk'
    assert k('/api/export/portfolio') == 'bulk' and k('/api/export/comparison') == 'bulk'


def test_data_downloads_have_a_monthly_allowance(client, db, plans, enforced, clock):
    u = _member_on(db, plans, 'professional_monthly', 'dl@data.example')                # 20 data files a month
    login(client, 'dl@data.example')
    _downloads(db, u, DATA, 19, clock.t)
    assert client.get(DATA).status_code not in (402, 429)
    _downloads(db, u, DATA, 1, clock.t)
    r = client.get(DATA)
    assert r.status_code == 429 and r.get_json()['reason'] == 'export_limit' and r.get_json()['kind'] == 'data'
    clock.set(datetime(2026, 12, 1, 8, 0))                                               # new month
    assert client.get(DATA).status_code not in (402, 429)


def test_reports_stay_unlimited_when_data_runs_out(client, db, plans, enforced, clock):
    u = _member_on(db, plans, 'professional_monthly', 'rep@x.example')
    login(client, 'rep@x.example')
    _downloads(db, u, DATA, 20, clock.t)
    _downloads(db, u, REPORT, 150, clock.t)
    assert client.get(DATA).status_code == 429
    assert client.get(REPORT).status_code not in (402, 429)


def test_bulk_and_zip_files_follow_the_package(client, db, plans, enforced, clock):
    pro = _member_on(db, plans, 'professional_annual', 'pro@bulk.example')                # bulk not included
    login(client, 'pro@bulk.example')
    r = client.get(BULK)
    assert r.status_code == 402 and r.get_json()['reason'] == 'not_in_plan' and 'bulk' in r.get_json()['error']

    team = _member_on(db, plans, 'team_annual', 'team@bulk.example')                    # 2 bulk files a month
    t = login(client.application.test_client(), 'team@bulk.example')
    assert t.get(BULK).status_code not in (402, 429)                                     # passes the plan check
    _downloads(db, team, '/api/manual/forms.zip', 2, clock.t)
    assert t.get(BULK).status_code == 429

    _member_on(db, plans, 'enterprise_annual', 'ent@bulk.example')                      # unlimited
    e = login(client.application.test_client(), 'ent@bulk.example')
    r = e.get(BULK)
    assert r.status_code not in (402, 429)


def test_plan_allowance_replaces_the_daily_cap(client, db, plans, enforced, clock, monkeypatch):
    monkeypatch.setenv('EXPORT_DAILY_LIMIT', '5')
    u = _member_on(db, plans, 'team_annual', 'busy@x.example')
    login(client, 'busy@x.example')
    from datetime import datetime as _dt
    _downloads(db, u, REPORT, 6, _dt.utcnow())                    # 6 today: over the old 5-a-day cap
    assert client.get(REPORT).status_code not in (402, 429)
    monkeypatch.setenv('BILLING_ENFORCED', 'false')               # billing off: the old daily cap applies again
    r = client.get(REPORT)
    assert r.status_code == 429 and 'Daily download limit' in r.get_json()['error']


def test_usage_is_shown_to_the_customer(client, db, plans, enforced, clock):
    u = _member_on(db, plans, 'professional_monthly', 'use@x.example')
    login(client, 'use@x.example')
    _downloads(db, u, DATA, 3, clock.t)
    _downloads(db, u, REPORT, 2, clock.t)
    d = client.get('/api/billing/me').get_json()['downloads']
    assert d['data'] == {'used': 3, 'limit': 20} and d['report'] == {'used': 2, 'limit': None}
    assert d['bulk'] == {'used': 0, 'limit': 0}


def test_existing_plans_get_allowances_when_upgrading(db, plans):
    from sqlalchemy import text
    import migrate_v12_download_limits as m12
    custom = Plan(code='old_custom', name='Old custom', price=1, exports_per_month=7)
    db.session.add(custom)
    db.session.commit()
    for c in ('report_downloads_per_month', 'data_downloads_per_month', 'bulk_downloads_per_month'):
        db.session.execute(text(f'ALTER TABLE billing_plans DROP COLUMN {c}'))
    db.session.commit()
    db.session.expire_all()
    m12.run_migration()
    db.session.expire_all()
    got = {p.code: (p.report_downloads_per_month, p.data_downloads_per_month, p.bulk_downloads_per_month)
           for p in Plan.query.all()}
    assert got['professional_annual'] == (None, 20, 0) and got['team_monthly'] == (None, 50, 2)
    assert got['enterprise_annual'] == (None, None, None) and got['old_custom'] == (None, 7, 0)
    m12.run_migration()                                            # second run changes nothing


def test_staff_can_set_allowances(client, db, plans, staff):
    pid = plans['team_annual'].id
    r = staff.patch(f'/api/admin/billing/plans/{pid}', json={'data_downloads_per_month': '', 'bulk_downloads_per_month': 5})
    assert r.get_json()['data_downloads_per_month'] is None and r.get_json()['bulk_downloads_per_month'] == 5


# ------------------------------------------------------------------ Paystack
class FakePaystack:
    def __init__(self):
        self.calls = []
        self.verify_data = None
        self.charge_status = 'success'

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == '/transaction/initialize':
            return {'status': True, 'data': {'authorization_url': 'https://checkout.paystack.com/abc'}}
        if path.startswith('/transaction/verify/'):
            return {'status': True, 'data': self.verify_data}
        if path == '/transaction/charge_authorization':
            return {'status': True, 'data': {'status': self.charge_status, 'reference': body['reference'],
                                             'amount': body['amount'], 'currency': body['currency'],
                                             'metadata': body['metadata'], 'channel': 'card'}}
        raise AssertionError(path)


def _charge(inv, ref='FI1-abc', reusable=True):
    return {'status': 'success', 'reference': ref, 'amount': inv.total * 100, 'currency': 'KES', 'channel': 'card',
            'metadata': {'invoice_id': inv.id}, 'customer': {'customer_code': 'CUS_1'},
            'authorization': {'authorization_code': 'AUTH_xyz', 'reusable': reusable, 'channel': 'card',
                              'card_type': 'visa ', 'last4': '4081'}}


def _signed(body):
    raw = json.dumps(body).encode()
    return raw, hmac.new(b'sk_test_123', raw, hashlib.sha512).hexdigest()


def test_pay_now_opens_paystack_checkout(client, db, plans, monkeypatch):
    monkeypatch.setenv('PAYSTACK_SECRET_KEY', 'sk_test_123')
    fake = FakePaystack()
    monkeypatch.setattr(billing, 'paystack_http', fake)
    make_user(db, 'payer@x.example')
    login(client, 'payer@x.example')
    inv = client.post('/api/billing/subscribe', json={'plan_code': 'team_monthly',
                                                       'organisation': {'name': 'Payer Ltd'}}).get_json()['invoice']
    r = client.post(f"/api/billing/invoices/{inv['id']}/pay")
    assert r.status_code == 200 and r.get_json()['authorization_url'].startswith('https://checkout.paystack.com')
    _, path, body = fake.calls[-1]
    assert body['amount'] == 45000 * 100 and body['currency'] == 'KES' and body['email'] == 'payer@x.example'
    assert body['metadata']['invoice_id'] == inv['id'] and body['callback_url'].endswith('/billing/paystack/callback')
    # someone else's invoice is off limits
    make_user(db, 'nosy@x.example')
    other = login(client.application.test_client(), 'nosy@x.example')
    assert other.post(f"/api/billing/invoices/{inv['id']}/pay").status_code == 404


def test_webhook_applies_payment_once_and_saves_the_card(client, db, plans, monkeypatch):
    monkeypatch.setenv('PAYSTACK_SECRET_KEY', 'sk_test_123')
    org = Organisation(name='Card Co', billing_email='fin@card.example')
    db.session.add(org)
    db.session.commit()
    sub, inv = billing.start_subscription(org, plans['team_monthly'], mode='invoice')
    raw, sig = _signed({'event': 'charge.success', 'data': _charge(inv)})
    assert client.post('/api/billing/paystack/webhook', data=raw, headers={'x-paystack-signature': 'bad'},
                       content_type='application/json').status_code == 401
    for _ in range(2):                                                 # Paystack may retry
        r = client.post('/api/billing/paystack/webhook', data=raw, headers={'x-paystack-signature': sig},
                        content_type='application/json')
        assert r.status_code == 200
    assert Payment.query.count() == 1
    sub = db.session.get(Subscription, sub.id)
    assert inv.status == 'paid' and sub.status == 'active'
    assert sub.paystack_authorization_code == 'AUTH_xyz' and sub.card_label == 'Visa ending 4081'


def test_return_page_verifies_and_redirects(client, db, plans, monkeypatch):
    monkeypatch.setenv('PAYSTACK_SECRET_KEY', 'sk_test_123')
    fake = FakePaystack()
    monkeypatch.setattr(billing, 'paystack_http', fake)
    org = Organisation(name='Return Co', billing_email='a@b.example')
    db.session.add(org)
    db.session.commit()
    _, inv = billing.start_subscription(org, plans['professional_monthly'], mode='invoice')
    fake.verify_data = _charge(inv, ref=f'FI{inv.id}-zz', reusable=False)
    r = client.get(f'/billing/paystack/callback?reference=FI{inv.id}-zz')
    assert r.status_code == 302 and r.headers['Location'].endswith('/app?billing=paid')
    assert db.session.get(Invoice, inv.id).status == 'paid'


def test_saved_card_is_charged_at_renewal(db, plans, clock, monkeypatch):
    monkeypatch.setenv('PAYSTACK_SECRET_KEY', 'sk_test_123')
    fake = FakePaystack()
    monkeypatch.setattr(billing, 'paystack_http', fake)
    org = Organisation(name='Auto Co', billing_email='a@auto.example')
    db.session.add(org)
    db.session.commit()
    sub, _ = billing.start_subscription(org, plans['professional_monthly'], mode='complimentary')
    sub.paystack_authorization_code = 'AUTH_1'
    db.session.commit()
    old_end = sub.current_period_end
    clock.set(old_end - timedelta(days=1))
    assert billing.run_sweep() == {'renewal_invoices': 1, 'cards_charged': 1}
    assert db.session.get(Subscription, sub.id).current_period_end > old_end


def test_declined_card_leaves_invoice_open(db, plans, clock, monkeypatch):
    monkeypatch.setenv('PAYSTACK_SECRET_KEY', 'sk_test_123')
    fake = FakePaystack()
    fake.charge_status = 'failed'
    monkeypatch.setattr(billing, 'paystack_http', fake)
    org = Organisation(name='Declined Co', billing_email='a@d.example')
    db.session.add(org)
    db.session.commit()
    sub, _ = billing.start_subscription(org, plans['professional_monthly'], mode='complimentary')
    sub.paystack_authorization_code = 'AUTH_1'
    db.session.commit()
    clock.set(sub.current_period_end - timedelta(days=1))
    assert billing.run_sweep() == {'renewal_invoices': 1, 'cards_charged': 0}
    assert Invoice.query.filter_by(kind='renewal').one().status == 'open'


def test_pay_now_without_paystack_key_explains(client, db, plans, monkeypatch):
    monkeypatch.delenv('PAYSTACK_SECRET_KEY', raising=False)
    make_user(db, 'p@x.example')
    login(client, 'p@x.example')
    inv = client.post('/api/billing/subscribe', json={'plan_code': 'professional_monthly',
                                                       'organisation': {'name': 'P'}}).get_json()['invoice']
    r = client.post(f"/api/billing/invoices/{inv['id']}/pay")
    assert r.status_code == 503 and 'bank transfer' in r.get_json()['error']


# ------------------------------------------------------------------ staff tools
def test_staff_tools_and_permissions(client, db, plans, staff):
    make_user(db, 'plainadmin@fi.example', role='admin')
    plain = login(client, 'plainadmin@fi.example')
    assert plain.get('/api/admin/billing/overview').status_code == 403

    org = staff.post('/api/admin/billing/orgs', json={'name': 'Bank of Somewhere', 'kra_pin': 'P0000000Z'}).get_json()
    make_user(db, 'cs@bank.example')
    r = staff.post(f"/api/admin/billing/orgs/{org['id']}/members", json={'email': 'cs@bank.example', 'org_role': 'owner'})
    assert r.status_code == 200 and r.get_json()['members'][0]['org_role'] == 'owner'

    r = staff.post(f"/api/admin/billing/orgs/{org['id']}/plan", json={'plan_code': 'enterprise_annual', 'mode': 'invoice',
                                                                        'seats_override': 12})
    assert r.status_code == 201 and r.get_json()['subscription']['seats'] == 12

    inv = staff.post(f"/api/admin/billing/orgs/{org['id']}/invoices",
                     json={'description': 'FY2025 Board Remuneration Report', 'amount': 45000, 'vat_rate': 16}).get_json()
    assert inv['vat_amount'] == 7200 and inv['total'] == 52200
    assert staff.patch(f"/api/admin/billing/invoices/{inv['id']}", json={'etims_number': 'KRAMW0123'}).get_json()['etims_number'] == 'KRAMW0123'
    assert staff.patch(f"/api/admin/billing/invoices/{inv['id']}", json={'status': 'void'}).get_json()['status'] == 'void'

    ov = staff.get('/api/admin/billing/overview').get_json()
    assert ov['organisations'][0]['name'] == 'Bank of Somewhere' and ov['outstanding']['KES'] == 1200000

    # price changes
    pid = plans['professional_annual'].id
    assert staff.patch(f'/api/admin/billing/plans/{pid}', json={'price': 160000}).get_json()['price'] == 160000
    r = staff.post('/api/admin/billing/plans', json={'code': 'Pro USD', 'name': 'Professional (USD)', 'price': 1200,
                                                      'currency': 'usd', 'interval_months': 12, 'seats': 1})
    assert r.status_code == 201 and r.get_json()['code'] == 'pro_usd' and r.get_json()['currency'] == 'USD'

    # invoice page: staff and the customer can open it, others cannot
    inv2 = Invoice.query.filter_by(kind='subscription').first()
    page = staff.get(f'/billing/invoice/{inv2.id}')
    assert page.status_code == 200 and b'Bank of Somewhere' in page.data and b'P0000000Z' in page.data
    stranger = client.application.test_client()
    assert stranger.get(f'/billing/invoice/{inv2.id}').status_code == 404


def test_enterprise_is_not_self_serve(client, db, plans):
    make_user(db, 'e@x.example')
    login(client, 'e@x.example')
    r = client.post('/api/billing/subscribe', json={'plan_code': 'enterprise_annual', 'organisation': {'name': 'E'}})
    assert r.status_code == 400 and 'quote' in r.get_json()['error']


def test_staff_are_never_billed(client, db, plans, enforced, staff):
    assert staff.get(MEMBER_PAGE).status_code != 402
    assert staff.get('/api/billing/me').get_json()['level'] == 'full'
