"""A sign-in ends when the person leaves the app (no check-in for SESSION_AWAY_MINUTES) or after SESSION_MAX_HOURS."""
import time

from test_security_seo import _make, _login


def _signed_in(client):
    from app import db, User
    _make(db, User, 'm@x.com', 'user')
    _login(client, 'm@x.com')
    assert client.get('/api/me').get_json()['logged_in'] is True


def test_cookie_is_a_browser_session_cookie(client, app):
    from app import db, User
    _make(db, User, 'm@x.com', 'user')
    r = client.post('/api/login', json={'email': 'm@x.com', 'password': 'secret1'})
    assert 'Expires=' not in r.headers['Set-Cookie']           # removed when the browser closes


def test_still_signed_in_while_the_app_checks_in(client, app):
    _signed_in(client)
    with client.session_transaction() as s:
        s['seen'] = time.time() - 120                            # 2 minutes ago: within the 3-minute allowance
    assert client.get('/api/me').get_json()['logged_in'] is True


def test_signed_out_after_leaving_the_app(client, app):
    _signed_in(client)
    with client.session_transaction() as s:
        s['seen'] = time.time() - 4 * 60
    r = client.get('/api/me')
    assert r.get_json()['logged_in'] is False and r.headers['X-Session-Ended'] == 'away'
    page = client.get('/app')
    assert page.status_code == 302 and '/login' in page.headers['Location']


def test_signed_out_after_the_maximum_length(client, app, monkeypatch):
    monkeypatch.setenv('SESSION_MAX_HOURS', '12')
    _signed_in(client)
    with client.session_transaction() as s:
        s['login_at'] = time.time() - 13 * 3600
    r = client.get('/api/me')
    assert r.get_json()['logged_in'] is False and r.headers['X-Session-Ended'] == 'expired'


def test_app_page_sends_a_left_user_to_sign_in_with_the_reason(client, app):
    _signed_in(client)
    with client.session_transaction() as s:
        s['seen'] = time.time() - 10 * 60
    r = client.get('/app')
    assert r.status_code == 302 and 'ended=away' in r.headers['Location']
    assert 'you left the app' in client.get('/login?ended=away').get_data(as_text=True)
