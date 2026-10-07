import pytest
from app import app as flask_app


@pytest.fixture()
def https_client(app, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'FORCE_HTTPS_IN_TESTS', True)
    return app.test_client()


def test_plain_http_on_a_public_host_is_redirected_to_https(https_client):
    r = https_client.get('/', base_url='http://financeintel.example.com')
    assert r.status_code == 301 and r.headers['Location'].startswith('https://financeintel.example.com')


def test_proxy_header_saying_https_is_trusted_and_gets_hsts(https_client):
    r = https_client.get('/', base_url='http://financeintel.example.com', headers={'X-Forwarded-Proto': 'https'})
    assert r.status_code == 200 and 'max-age' in r.headers['Strict-Transport-Security']


def test_health_check_and_localhost_are_never_redirected(https_client):
    assert https_client.get('/api/system/status', base_url='http://financeintel.example.com').status_code != 301
    assert https_client.get('/', base_url='http://localhost').status_code == 200


def test_security_headers_present(client):
    r = client.get('/')
    assert r.headers['X-Content-Type-Options'] == 'nosniff' and 'frame-ancestors' in r.headers['Content-Security-Policy']


def test_api_responses_are_noindex(client):
    assert 'noindex' in client.get('/api/overview').headers.get('X-Robots-Tag', '')


def test_robots_and_sitemap_use_the_configured_site_url(client, monkeypatch):
    monkeypatch.setenv('SITE_URL', 'https://financeintel.example.com/')
    robots = client.get('/robots.txt').get_data(as_text=True)
    assert 'Disallow: /api/' in robots and 'Sitemap: https://financeintel.example.com/sitemap.xml' in robots
    assert '<loc>https://financeintel.example.com/</loc>' in client.get('/sitemap.xml').get_data(as_text=True)


def test_home_page_has_seo_tags(client, monkeypatch):
    monkeypatch.setenv('SITE_URL', 'https://financeintel.example.com')
    html = client.get('/').get_data(as_text=True)
    for needle in ('<title>Finance Intel |', 'name="description"', 'rel="canonical" href="https://financeintel.example.com/"',
                   'og:image', 'application/ld+json', '<h1>Finance Intel</h1>'):
        assert needle in html
    assert client.get('/static/og-image.png').status_code == 200




def test_login_cookie_is_secure_only_over_https(client):
    body = {'name': 'T', 'email': 't@example.com', 'password': 'secret1', 'organization': 'O', 'job_title': 'J'}
    https = client.post('/api/register', json=body, headers={'X-Forwarded-Proto': 'https'})
    assert https.status_code == 201
    cookie = https.headers['Set-Cookie']
    assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=Lax' in cookie
    plain = client.post('/api/login', json={'email': 't@example.com', 'password': 'secret1'}, base_url='http://localhost')
    assert 'Secure' not in plain.headers['Set-Cookie']


# ---- account management is super-admin only ----

def _make(db, User, email, role, super_admin=False):
    u = User(name=email.split('@')[0], email=email, role=role, is_super_admin=super_admin)
    u.set_password('secret1')
    db.session.add(u); db.session.commit()
    return u.id


def _login(client, email):
    r = client.post('/api/login', json={'email': email, 'password': 'secret1'})
    assert r.status_code == 200


def test_only_super_admin_can_manage_accounts(client, app):
    from app import db, User
    sup = _make(db, User, 'sup@x.com', 'admin', True)
    reg = _make(db, User, 'adm@x.com', 'admin')
    usr = _make(db, User, 'usr@x.com', 'user')

    # regular admin: every account-management call is refused
    _login(client, 'adm@x.com')
    assert client.get('/api/users').status_code == 403
    assert client.post('/api/users', json={'name': 'n', 'email': 'n@x.com', 'password': 'secret1', 'organization': 'O', 'job_title': 'J'}).status_code == 403
    assert client.patch(f'/api/users/{usr}/role', json={'role': 'admin'}).status_code == 403
    assert client.post(f'/api/users/{usr}/reset-password', json={'password': 'newpass1'}).status_code == 403
    assert client.delete(f'/api/users/{usr}').status_code == 403
    client.post('/api/logout')

    # plain user: also refused
    _login(client, 'usr@x.com')
    assert client.get('/api/users').status_code == 403
    client.post('/api/logout')

    # super admin: allowed
    _login(client, 'sup@x.com')
    assert client.get('/api/users').status_code == 200
    assert client.post('/api/users', json={'name': 'n', 'email': 'n@x.com', 'password': 'secret1', 'organization': 'O', 'job_title': 'J'}).status_code == 201
    assert client.post(f'/api/users/{usr}/reset-password', json={'password': 'newpass1'}).status_code == 200
    assert client.patch(f'/api/users/{reg}/role', json={'role': 'user'}).status_code == 200
    assert client.delete(f'/api/users/{usr}').status_code == 200
    # cannot demote the last super admin
    assert client.patch(f'/api/users/{sup}/role', json={'role': 'user'}).status_code == 400


def test_public_registration_makes_members_and_can_be_closed(client, monkeypatch):
    monkeypatch.delenv('ALLOW_SELF_REGISTRATION', raising=False)
    assert client.get('/api/me').get_json()['self_registration'] is True
    body = {'name': 'T', 'email': 'z@example.com', 'password': 'secret1', 'organization': 'O', 'job_title': 'J', 'role': 'admin'}
    r = client.post('/api/register', json=body)
    assert r.status_code == 201 and r.get_json()['role'] == 'user'   # never an admin
    client.post('/api/logout')
    assert client.post('/api/register', json={**body, 'job_title': '', 'email': 'q@example.com'}).status_code == 400
    monkeypatch.setenv('ALLOW_SELF_REGISTRATION', 'false')
    assert client.post('/api/register', json={**body, 'email': 'y@example.com'}).status_code == 403
    assert client.get('/api/me').get_json()['self_registration'] is False


def test_profile_needs_a_real_name_and_super_admin_is_labelled(client, app):
    from app import db, User
    _make(db, User, 'sup@x.com', 'admin', True)
    _login(client, 'sup@x.com')
    me = client.get('/api/me').get_json()
    assert me['is_super_admin'] is True and not me.get('organization') and not me.get('job_title')
    base = {'organization': 'Acme', 'job_title': 'CFO', 'phone': ''}
    assert client.put('/api/profile', json={**base, 'name': 'Admin'}).status_code == 400
    assert client.put('/api/profile', json={**base, 'name': ''}).status_code == 400
    ok = client.put('/api/profile', json={**base, 'name': 'Martin Chaaru'})
    assert ok.status_code == 200 and ok.get_json()['name'] == 'Martin Chaaru'
    assert ok.get_json()['is_super_admin'] is True