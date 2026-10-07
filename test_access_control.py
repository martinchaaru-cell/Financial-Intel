"""Tests for access_control.py: guest allow-list, admin-only bulk exports, daily export cap, rate limits."""
import pytest

import activity_log
from models import User
from models_survey import ActivityLogEntry


def _make_user(db, email, role='user'):
    u = User(name=email.split('@')[0].title(), email=email, role=role)
    u.set_password('secret123')
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, email):
    r = client.post('/api/login', json={'email': email, 'password': 'secret123'})
    assert r.status_code == 200
    return r


# ---------------------------------------------------------------- guest default-deny
def test_guest_can_read_only_what_the_overview_needs(client):
    for path in ('/api/me', '/api/companies', '/api/survey/companies', '/api/survey/data-quality', '/api/import-jobs'):
        assert client.get(path).status_code == 200, path


@pytest.mark.parametrize('path', [
    '/api/survey/overview',
    '/api/survey/data-quality-average',
    '/api/survey-report/company-ranking',
    '/api/intelligence-layer/companies',
    '/api/companies/1/survey-report',
    '/api/companies/1/remuneration',
    '/api/survey/export/report.pdf',
    '/api/export/portfolio',
    '/api/activity',
])
def test_guest_is_refused_everything_else(client, path):
    r = client.get(path)
    assert r.status_code == 403, path
    assert 'sign in' in r.get_json()['error'].lower() or "permission" in r.get_json()['error'].lower()


def test_signed_in_member_is_not_refused_by_the_guest_rule(client, db):
    _make_user(db, 'member@example.com')
    _login(client, 'member@example.com')
    assert client.get('/api/survey/overview').status_code == 200
    assert client.get('/api/survey/data-quality-average').status_code == 200


def test_non_get_routes_are_untouched_by_the_guest_rule(client):
    # still handled (and refused) by each route's own require_role, not by the allow-list
    assert client.post('/api/survey-data/import', json={}).status_code == 403
    assert client.post('/api/login', json={'email': 'nobody@example.com', 'password': 'x'}).status_code == 401


# ---------------------------------------------------------------- bulk exports are admin only
def test_whole_portfolio_exports_are_admin_only(client, db):
    _make_user(db, 'member@example.com')
    _make_user(db, 'boss@example.com', role='admin')
    _login(client, 'member@example.com')
    assert client.get('/api/export/portfolio').status_code == 403
    assert client.get('/api/export/comparison').status_code == 403
    client.post('/api/logout')
    _login(client, 'boss@example.com')
    assert client.get('/api/export/portfolio').status_code == 200


# ---------------------------------------------------------------- daily export cap
def _log_exports(db, user, n):
    for _ in range(n):
        db.session.add(ActivityLogEntry(user_id=user.id, actor=user.name, actor_email=user.email,
                                        actor_role=user.role, action='get:export/x', category=activity_log.CAT_EXPORT,
                                        http_method='GET', path='/api/export/x'))
    db.session.commit()


def test_member_is_stopped_after_the_daily_download_limit(client, db, monkeypatch):
    monkeypatch.setenv('EXPORT_DAILY_LIMIT', '3')
    member = _make_user(db, 'member@example.com')
    _login(client, 'member@example.com')
    _log_exports(db, member, 2)
    assert client.get('/api/export/combined-report.pdf').status_code != 429    # 2 used of 3
    _log_exports(db, member, 1)
    r = client.get('/api/export/combined-report.pdf')                           # 3 used of 3
    assert r.status_code == 429
    assert 'Daily download limit' in r.get_json()['error']
    assert client.get('/api/survey/overview').status_code == 200                # ordinary reads are not capped


def test_admin_is_exempt_and_the_cap_can_be_switched_off(client, db, monkeypatch):
    monkeypatch.setenv('EXPORT_DAILY_LIMIT', '1')
    boss = _make_user(db, 'boss@example.com', role='admin')
    _log_exports(db, boss, 5)
    _login(client, 'boss@example.com')
    assert client.get('/api/export/combined-report.pdf').status_code != 429
    client.post('/api/logout')
    member = _make_user(db, 'member@example.com')
    _log_exports(db, member, 5)
    _login(client, 'member@example.com')
    assert client.get('/api/export/combined-report.pdf').status_code == 429
    monkeypatch.setenv('EXPORT_DAILY_LIMIT', '0')
    assert client.get('/api/export/combined-report.pdf').status_code != 429


# ---------------------------------------------------------------- rate limits
@pytest.fixture()
def limiter_on(app):
    lim = app.extensions.get('access_control_limiter')
    if lim is None:
        pytest.skip('Flask-Limiter is not installed')
    lim.enabled = True
    lim.reset()
    yield lim
    lim.reset()
    lim.enabled = False


def test_sign_in_is_throttled_per_ip(client, limiter_on):
    codes = [client.post('/api/login', json={'email': 'a@example.com', 'password': 'wrong'}).status_code
             for _ in range(12)]
    assert codes[:10] == [401] * 10
    assert 429 in codes[10:]
    r = client.post('/api/login', json={'email': 'a@example.com', 'password': 'wrong'})
    assert r.status_code == 429 and 'Too many requests' in r.get_json()['error']


def test_general_api_limit_applies_but_overview_probes_are_exempt(client, limiter_on, monkeypatch):
    # /api/me and /api/events never count, however often they are called
    assert all(client.get('/api/me').status_code == 200 for _ in range(260))
    codes = [client.get('/api/companies').status_code for _ in range(205)]
    assert codes[:200] == [200] * 200
    assert 429 in codes[200:]
