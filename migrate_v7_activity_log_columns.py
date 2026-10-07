"""
One-time migration - brings an existing activity_log_entries table up to date for the
account-tagged activity log: adds user_id, actor_email, actor_role, category, http_method
and path, adds indexes on user_id / category / created_at, and makes company_id optional
(sign-ins, user management and profile edits belong to no company).

Same story as migrate_v5/v6: db.create_all() only creates missing tables, it never ALTERs
an existing one. Safe to run more than once - every step is guarded. Runs automatically on
boot from app.py (inside try/except), and can also be run by hand:

    from app import app
    from migrate_v7_activity_log_columns import run_migration
    with app.app_context():
        run_migration()

Postgres only (same as the other migrations).
"""

from sqlalchemy import text
from models import db

TABLE = 'activity_log_entries'

_NEW_COLUMNS = [
    ('user_id', 'INTEGER'),
    ('actor_email', 'VARCHAR(150)'),
    ('actor_role', 'VARCHAR(20)'),
    ('category', 'VARCHAR(40)'),
    ('http_method', 'VARCHAR(10)'),
    ('path', 'VARCHAR(200)'),
]
_INDEXES = [
    ('ix_activity_log_entries_user_id', 'user_id'),
    ('ix_activity_log_entries_category', 'category'),
    ('ix_activity_log_entries_created_at', 'created_at'),
]


def _table_exists(name):
    return db.session.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_name = :t"), {'t': name}).first() is not None


def _column_exists(table, column):
    return db.session.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
        {'t': table, 'c': column}).first() is not None


def run_migration():
    if not _table_exists(TABLE):
        return          # create_all() will build it with the full current shape
    added, skipped = [], []
    for col, ctype in _NEW_COLUMNS:
        if _column_exists(TABLE, col):
            skipped.append(col)
            continue
        db.session.execute(text(f'ALTER TABLE {TABLE} ADD COLUMN {col} {ctype}'))
        added.append(col)
    db.session.execute(text(f'ALTER TABLE {TABLE} ALTER COLUMN company_id DROP NOT NULL'))
    for name, col in _INDEXES:
        db.session.execute(text(f'CREATE INDEX IF NOT EXISTS {name} ON {TABLE} ({col})'))
    db.session.commit()
    print(f"{TABLE}: added columns {added or '(none)'}, already present {skipped or '(none)'}")
