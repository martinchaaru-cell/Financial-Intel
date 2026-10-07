"""Adds the account-lockout and two-step-verification columns to app_users.

Idempotent and safe on every boot (same pattern as v5-v8): each column is only added if it is missing."""
from sqlalchemy import inspect, text
from models import db

_COLUMNS = {
    'failed_login_count': 'INTEGER DEFAULT 0',
    'locked_until': 'TIMESTAMP',
    'totp_secret': 'VARCHAR(64)',
    'mfa_enabled': 'BOOLEAN DEFAULT FALSE',
    'mfa_last_step': 'BIGINT',
}


def run_migration():
    existing = {c['name'] for c in inspect(db.engine).get_columns('app_users')}
    for name, ddl in _COLUMNS.items():
        if name not in existing:
            db.session.execute(text(f'ALTER TABLE app_users ADD COLUMN {name} {ddl}'))
    db.session.commit()
