"""
One-time migration - adds the two extraction-quality-score columns to
source_documents, introduced alongside the admin-only per-document
accuracy score (see LOW_EXTRACTION_SCORE_THRESHOLD and
SourceDocument.extraction_score in models.py, and
compute_extraction_score() in app.py).

Why a migration script at all, when this app usually relies on
db.create_all() at startup: create_all() only CREATES tables that don't
exist yet - it never ALTERS an existing table to add new columns. On a
fresh database, db.create_all() alone is enough (source_documents is
brand-new too, so it picks up extraction_score/extraction_score_computed_at
as ordinary columns from the start). On a database that already has a
source_documents table from before this change, those two columns need
an explicit ALTER TABLE - this script does exactly that, and nothing
else.

Safe to run more than once: each ALTER TABLE is guarded by a check
against the database's own information_schema, so a column that
already exists is skipped rather than erroring.

Run once, from the Flask app context:

    from app import app
    from migrate_v4_extraction_score import run_migration
    with app.app_context():
        run_migration()

Postgres only (this app no longer supports a SQLite fallback - see
app.py's DATABASE_URL check) - the ALTER TABLE syntax below assumes
Postgres's information_schema.
"""

from sqlalchemy import text
from models import db

_NEW_COLUMNS = [
    ('extraction_score', 'FLOAT'),
    ('extraction_score_computed_at', 'TIMESTAMP'),
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
        if _column_exists('source_documents', column_name):
            skipped.append(column_name)
            continue
        db.session.execute(text(
            f'ALTER TABLE source_documents ADD COLUMN {column_name} {column_type}'
        ))
        added.append(column_name)

    db.session.commit()

    print(f"source_documents: added columns {added or '(none)'}, "
          f"already present {skipped or '(none)'}")


if __name__ == '__main__':
    print("Run this from the Flask app context - see this file's own docstring "
          "for the exact snippet. Not meant to be run as a bare script.")
