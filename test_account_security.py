import time

import pytest

import account_security as sec
from models import db as _db, User


def _user(email='a@x.com', role='admin', password='secret1'):
    u = User(name='A', email=email, role=role, organization='O', job_title='J')
    u.set_password(password)
    _db.session.add(u)
    _db.session.commit()
    return u


def _login(client, email='a@x.com', password='secret1', **extra):
    return client.post('/api/login', json={'email': email, 'password': password, **extra})


def test_totp_matches_rfc6238_vector():
    secret = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'          # ASCII "12345678901234567890"
    assert sec.match_totp(secret, '287082', window=0, now=59) is not None
    assert sec.match_totp(secret, '000000', window=0, now=59) is None


def test_account_locks_after_repeated_failures(client, monkeypatch):
    monkeypatch.setenv('LOGIN_MAX_FAILURES', '3')
    _user()
    for _ in range(3):
        assert _login(client, password='wrong').status_code == 401
    r = _login(client)                                     # even the right password is refused now
    assert r.status_code == 429 and 'Try again' in r.get_json()['error']


def test_success_resets_the_failure_counter(client, monkeypatch):
    monkeypatch.setenv('LOGIN_MAX_FAILURES', '3')
    _user()
    _login(client, password='wrong'); _login(client, password='wrong')
    assert _login(client).status_code == 200
    _login(client, password='wrong'); _login(client, password='wrong')
    assert _login(client).status_code == 200


def test_mfa_enrol_login_and_replay(client):
    u = _user()
    assert _login(client).status_code == 200
    secret = client.post('/api/mfa/setup').get_json()['secret']
    code = sec._hotp(secret, int(time.time() // 30))
    assert client.post('/api/mfa/enable', json={'code': '000000'}).status_code == 400
    assert client.post('/api/mfa/enable', json={'code': code}).status_code == 200
    client.post('/api/logout')

    r = _login(client)                                      # password alone is no longer enough
    assert r.status_code == 401 and r.get_json()['mfa_required'] is True
    assert _login(client, code=code).status_code == 401     # the enrolment code cannot be replayed
    nxt = sec._hotp(secret, int(time.time() // 30) + 1)     # next step is inside the +/-1 window
    assert _login(client, code=nxt).status_code == 200


def test_super_admin_can_reset_mfa(client):
    boss = _user('boss@x.com'); boss.is_super_admin = True
    victim = _user('v@x.com', role='user')
    victim.mfa_enabled = True; victim.totp_secret = sec.new_secret()
    _db.session.commit()
    assert _login(client, 'v@x.com').status_code == 401
    assert client.post(f'/api/users/{victim.id}/reset-mfa').status_code == 403
    _login(client, 'boss@x.com')
    assert client.post(f'/api/users/{victim.id}/reset-mfa').status_code == 200
    client.post('/api/logout')
    assert _login(client, 'v@x.com').status_code == 200


def test_cross_site_origin_is_blocked(client):
    _user()
    r = client.post('/api/login', json={'email': 'a@x.com', 'password': 'secret1'},
                    headers={'Origin': 'https://evil.example.com'})
    assert r.status_code == 403
    ok = client.post('/api/login', json={'email': 'a@x.com', 'password': 'secret1'},
                     headers={'Origin': 'http://localhost'})
    assert ok.status_code == 200
