"""Adds app_users.is_super_admin and makes sure one super admin exists.

Idempotent and safe on every boot (same pattern as v5-v7). If no super admin exists yet, the
EARLIEST-created admin account becomes the super admin, so the account that has always run the
app keeps working with no manual step. More can be designated afterwards by a super admin
(PATCH /api/users/<id>/super-admin)."""
from sqlalchemy import inspect, text
from models import db, User


def run_migration():
    cols = [c['name'] for c in inspect(db.engine).get_columns('app_users')]
    if 'is_super_admin' not in cols:
        db.session.execute(text('ALTER TABLE app_users ADD COLUMN is_super_admin BOOLEAN DEFAULT FALSE'))
        db.session.commit()
    if not User.query.filter_by(is_super_admin=True).first():
        first = User.query.filter_by(role='admin').order_by(User.id.asc()).first()
        if first:
            first.is_super_admin = True
            db.session.commit()
