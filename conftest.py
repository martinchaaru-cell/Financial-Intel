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
