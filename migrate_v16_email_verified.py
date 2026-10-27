"""app_users.email_verified for sign-up email confirmation (accounts_email.py). Every account that already exists is
treated as confirmed (the column defaults to true). Idempotent and safe on every boot."""
from sqlalchemy import inspect, text

from models import db


def run_migration():
    insp = inspect(db.engine)
    if 'app_users' not in insp.get_table_names():
        return
    if 'email_verified' not in {c['name'] for c in insp.get_columns('app_users')}:
        true = '1' if db.engine.dialect.name == 'sqlite' else 'TRUE'
        db.session.execute(text(f'ALTER TABLE app_users ADD COLUMN email_verified BOOLEAN DEFAULT {true}'))
        db.session.execute(text(f'UPDATE app_users SET email_verified = {true} WHERE email_verified IS NULL'))
        db.session.commit()
