"""
One-time migration - adds any UploadDraft columns that are missing from
an existing upload_drafts table.

Why this is needed: upload_drafts (models_ai.py) was created back when
UploadDraft had fewer columns. Fields added to the model afterward -
most recently source_document_id, which links a draft to the
SourceDocument its import produced - were never added to the live
table, because db.create_all() only CREATES tables that don't exist
yet; it never ALTERS an existing table to add new columns. That
mismatch is exactly what produces:

    psycopg2.errors.UndefinedColumn: column upload_drafts.source_document_id
    does not exist

This script brings an old upload_drafts table up to date with the
current model by adding whichever of its columns aren't there yet.
Safe to run more than once: each ALTER TABLE is guarded by a check
against the database's own information_schema, so a column that
already exists is skipped rather than erroring.

Run once, from the Flask app context:

    from app import app
    from migrate_v5_upload_drafts_columns import run_migration
    with app.app_context():
        run_migration()

Postgres only (this app no longer supports a SQLite fallback - see
app.py's DATABASE_URL check) - the ALTER TABLE syntax below assumes
Postgres's information_schema.
"""

from sqlalchemy import text
from models import db

# Every non-primary-key column UploadDraft (models_ai.py) currently defines,
# with the SQL type to use if it needs to be added.
_NEW_COLUMNS = [
    ('filename', 'VARCHAR(255)'),
    ('sha256', 'VARCHAR(64)'),
    ('size', 'INTEGER'),
    ('pdf_bytes', 'BYTEA'),
    ('forced_company_id', 'INTEGER'),
    ('status', 'VARCHAR(20)'),
    ('source_document_id', 'INTEGER'),
    ('preview_json', 'TEXT'),
    ('result_json', 'TEXT'),
    ('error', 'TEXT'),
    ('created_at', 'TIMESTAMP'),
    ('reviewed_at', 'TIMESTAMP'),
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
        if _column_exists('upload_drafts', column_name):
            skipped.append(column_name)
            continue
        db.session.execute(text(
            f'ALTER TABLE upload_drafts ADD COLUMN {column_name} {column_type}'
        ))
        added.append(column_name)

    # sha256 is queried via an index (models_ai.py: index=True) - add it if
    # the column itself was just created (a pre-existing column would
    # already have whatever index it has, or lack of one is not this
    # script's problem to fix).
    if 'sha256' in added:
        db.session.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_upload_drafts_sha256 ON upload_drafts (sha256)'
        ))

    db.session.commit()

    print(f"upload_drafts: added columns {added or '(none)'}, "
          f"already present {skipped or '(none)'}")


if __name__ == '__main__':
    print("Run this from the Flask app context - see this file's own docstring "
          "for the exact snippet. Not meant to be run as a bare script.")