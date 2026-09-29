"""
One-time migration - adds the new profile columns (organization, job_title,
phone) to an existing app_users table.

Why this is needed: same story as migrate_v5_upload_drafts_columns.py -
db.create_all() only CREATES tables that don't exist yet, it never ALTERs
an existing table to add new columns. Any app_users table created before
these fields were added to the User model (models.py) will be missing
them, which produces:

    psycopg2.errors.UndefinedColumn: column app_users.organization
    does not exist

This script brings an old app_users table up to date by adding whichever
of these columns aren't there yet. Safe to run more than once: each ALTER
TABLE is guarded by a check against the database's own information_schema,
so a column that already exists is skipped rather than erroring.

Run once, from the Flask app context:

    from app import app
    from migrate_v6_user_profile_columns import run_migration
    with app.app_context():
        run_migration()

Postgres only (this app no longer supports a SQLite fallback) - the ALTER
TABLE syntax below assumes Postgres's information_schema.
"""

from sqlalchemy import text
from models import db

_NEW_COLUMNS = [
    ('organization', 'VARCHAR(150)'),
    ('job_title', 'VARCHAR(120)'),
    ('phone', 'VARCHAR(30)'),
]


def _column_exists(table_name: str, column_name: str) -> bool:
    result = db.session.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :table_name AND column_name = :column_name"
    ), {'table_name': table_name, 'column_name': column_name})
    return result.first() is not None


def run_migration():
    added = []
    skipped = []

    for column_name, column_type in _NEW_COLUMNS:
        if _column_exists('app_users', column_name):
            skipped.append(column_name)
            continue
        db.session.execute(text(
            f'ALTER TABLE app_users ADD COLUMN {column_name} {column_type}'
        ))
        added.append(column_name)

    db.session.commit()

    print(f"app_users: added columns {added or '(none)'}, "
          f"already present {skipped or '(none)'}")


if __name__ == '__main__':
    print("Run this from the Flask app context - see this file's own docstring "
          "for the exact snippet. Not meant to be run as a bare script.")
