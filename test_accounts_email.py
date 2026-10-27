"""Sign-up email confirmation, forgotten password, invitations and contact enquiries (accounts_email.py)."""
import re
from datetime import datetime, timedelta

from test_security_seo import _make, _login

REG = {'name': 'Jane Kithela', 'email': 'jane@example.com', 'password': 'goodpass1', 'organization': 'Acme', 'job_title': 'CFO'}


def _last_link(kind):
    from models_auth import EmailLog
    m = EmailLog.query.filter_by(kind=kind).order_by(EmailLog.id.desc()).first()
    assert m is not None, f'no {kind} email'
    return re.search(r'https?://\S+', m.body_text).group(0)


def _path(url):
    return url.split('localhost', 1)[-1]


def test_signup_needs_the_emailed_link_before_sign_in(client, app):
    r = client.post('/api/register', json=REG)
    assert r.status_code == 201 and r.get_json()['verification_sent'] is True
    assert client.get('/api/me').get_json()['logged_in'] is False          # not signed in yet
    bad = client.post('/api/login', json={'email': REG['email'], 'password': REG['password']})
    assert bad.status_code == 403 and bad.get_json()['needs_verification'] is True
    wrong = client.post('/api/login', json={'email': REG['email'], 'password': 'nope-nope'})
    assert wrong.status_code == 401 and 'needs_verification' not in wrong.get_json()   # never revealed without the password
    link = _last_link('verify')
    r = client.get(_path(link))
    assert r.status_code == 302 and '/app' in r.headers['Location']
    assert client.get('/api/me').get_json()['email'] == REG['email']
    assert client.get(_path(link)).status_code == 400                       # used once only


def test_resend_and_invalid_link(client, app):
    client.post('/api/register', json=REG)
    first = _last_link('verify')
    assert client.post('/api/verify/resend', json={'email': REG['email']}).status_code == 200
    assert client.get(_path(first)).status_code == 400                      # replaced by the new link
    assert client.get('/verify-email?token=rubbish').status_code == 400
    assert client.post('/api/verify/resend', json={'email': 'nobody@example.com'}).status_code == 200   # same answer


def test_forgot_password_flow(client, app):
    from app import db, User
    _make(db, User, 'm@x.com', 'user')
    unknown = client.post('/api/password/forgot', json={'email': 'ghost@x.com'})
    known = client.post('/api/password/forgot', json={'email': 'm@x.com'})
    assert unknown.status_code == known.status_code == 200
    assert unknown.get_json()['message'] == known.get_json()['message']     # does not reveal who has an account
    link = _last_link('reset')
    assert client.get(_path(link)).status_code == 200
    token = link.split('token=')[1]
    assert client.post('/api/password/reset', json={'token': token, 'password': 'short'}).status_code == 400
    r = client.post('/api/password/reset', json={'token': token, 'password': 'brand-new-pass'})
    assert r.status_code == 200
    from models_auth import EmailLog
    assert EmailLog.query.filter_by(kind='password_changed').count() == 1
    client.post('/api/logout')
    assert client.post('/api/login', json={'email': 'm@x.com', 'password': 'secret1'}).status_code == 401
    assert client.post('/api/login', json={'email': 'm@x.com', 'password': 'brand-new-pass'}).status_code == 200
    assert client.post('/api/password/reset', json={'token': token, 'password': 'another-pass'}).status_code == 400


def test_reset_link_expires(client, app):
    from app import db, User
    from models_auth import AuthToken
    _make(db, User, 'm@x.com', 'user')
    client.post('/api/password/forgot', json={'email': 'm@x.com'})
    token = _last_link('reset').split('token=')[1]
    t = AuthToken.query.filter_by(purpose='reset').first()
    t.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.session.commit()
    r = client.post('/api/password/reset', json={'token': token, 'password': 'brand-new-pass'})
    assert r.status_code == 400 and r.get_json()['reason'] == 'expired'


def test_admin_invites_without_a_password(client, app):
    from app import db, User
    _make(db, User, 'sup@x.com', 'admin', True)
    _login(client, 'sup@x.com')
    r = client.post('/api/users', json={'name': 'New Person', 'email': 'new@x.com', 'organization': 'O', 'job_title': 'J'})
    assert r.status_code == 201 and r.get_json()['invite']['status'] == 'not_configured' and r.get_json()['invite']['link']
    assert r.get_json()['email_verified'] is False
    client.post('/api/logout')
    token = r.get_json()['invite']['link'].split('token=')[1]
    assert client.get('/set-password?token=' + token).status_code == 200
    assert client.post('/api/password/reset', json={'token': token, 'password': 'my-own-pass'}).status_code == 200
    assert client.get('/api/me').get_json()['email'] == 'new@x.com'


def test_contact_enquiry_is_saved_emailed_and_acknowledged(client, app, monkeypatch):
    monkeypatch.setenv('CONTACT_EMAIL', 'info@fi.example')
    from models_auth import ContactEnquiry, EmailLog
    assert client.post('/api/contact', json={'name': 'A', 'email': 'bad', 'message': 'hi'}).status_code == 400
    r = client.post('/api/contact', json={'name': 'Ann Buyer', 'email': 'ann@corp.example', 'topic': 'Enterprise quote',
                                          'message': 'We need 20 seats.'})
    assert r.status_code == 200
    assert ContactEnquiry.query.count() == 1
    assert EmailLog.query.filter_by(kind='enquiry', to_address='info@fi.example').count() == 1
    assert EmailLog.query.filter_by(kind='enquiry_ack', to_address='ann@corp.example').count() == 1
    client.post('/api/contact', json={'name': 'Bot', 'email': 'b@b.example', 'message': 'spam', 'website': 'x'})
    assert ContactEnquiry.query.count() == 1                                 # the hidden field catches robots


def test_enquiries_and_mail_log_are_super_admin_only(client, app):
    from app import db, User
    _make(db, User, 'adm@x.com', 'admin')
    _login(client, 'adm@x.com')
    assert client.get('/api/admin/enquiries').status_code == 403
    assert client.get('/api/admin/mail').status_code == 403
