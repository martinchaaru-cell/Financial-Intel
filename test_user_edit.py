"""A super admin can edit another account's details, including its sign-in email."""
from test_security_seo import _make, _login


def test_super_admin_edits_account_email(client, app):
    from app import db, User
    _make(db, User, 'sup@x.com', 'admin', True)
    _make(db, User, 'adm@x.com', 'admin')
    jane = _make(db, User, 'jane@x.com', 'user')

    _login(client, 'adm@x.com')          # a regular admin can't
    assert client.patch(f'/api/users/{jane}', json={'name': 'Jane', 'email': 'j@y.com'}).status_code == 403
    client.post('/api/logout')

    _login(client, 'sup@x.com')
    assert client.patch(f'/api/users/{jane}', json={'name': 'Jane', 'email': 'kithela'}).status_code == 400
    assert client.patch(f'/api/users/{jane}', json={'name': 'Jane', 'email': 'ADM@x.com'}).status_code == 409
    r = client.patch(f'/api/users/{jane}', json={'name': 'Jane Kithela', 'email': ' Jane.K@New.com ', 'organization': 'KCB',
                                                 'job_title': 'Analyst', 'phone': '0700'})
    assert r.status_code == 200 and r.get_json()['email'] == 'jane.k@new.com' and r.get_json()['organization'] == 'KCB'
    client.post('/api/logout')

    # she signs in with the new email and the same password
    assert client.post('/api/login', json={'email': 'jane.k@new.com', 'password': 'secret1'}).status_code == 200
