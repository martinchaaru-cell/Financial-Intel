"""
Shared fixtures. The app under test is imported as-is (app.py has no
app-factory pattern — `app` and `db` are module-level objects) so we
override its config *before* any request is made, then create a fresh
in-memory SQLite schema per test. That's a deliberate deviation from
production (Postgres) for test speed; keep the docker-compose Postgres
service around for anything that depends on Postgres-only behavior
(e.g. an ALTER TABLE migration script) and cover that separately.
"""
import os
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SESSION_SECRET", "test-secret")
# Rate limits would trip over the many requests a test run makes from one address; test_access_control.py turns them back on.
os.environ.setdefault("RATELIMIT_ENABLED", "false")

from app import app as flask_app, db as _db  # noqa: E402  (env vars must be set first)


@pytest.fixture()
def app():
    flask_app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    with flask_app.app_context():
        _db.create_all()
        yield flask_app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    return _db


@pytest.fixture()
def signed_in(client, db):
    """The test client, signed in as an ordinary Member. Guests may only read what the Dashboard
    Overview needs (see access_control.py), so tests that read other data endpoints use this."""
    from models import User
    user = User(name="Test Member", email="member@test.example", role="user")
    user.set_password("secret123")
    db.session.add(user)
    db.session.commit()
    resp = client.post("/api/login", json={"email": "member@test.example", "password": "secret123"})
    assert resp.status_code == 200
    return client
