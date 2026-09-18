"""
One-time migration — adds the evidence/review-workflow columns to
survey_company_data, and creates the three new tables (remuneration_policy,
governance_policy, evidence_conflict) introduced alongside the PDF
intelligence-extraction layer (see intelligence_extractor.py,
models_survey.py).

Why a migration script at all, when this app usually relies on
db.create_all() at startup: create_all() only CREATES tables that don't
exist yet - it never ALTERS an existing table to add new columns. On a
fresh database, db.create_all() alone is enough (it will pick up
field_confidence/field_review_status/field_review_corrections on
survey_company_data as brand-new columns because the table itself is
brand-new too). On a database that already has a survey_company_data
table from before this change, those new columns need an explicit
ALTER TABLE - this script does exactly that, and nothing else.

Safe to run more than once: every ALTER TABLE is guarded by a check
against the database's own information_schema, so a column that
already exists is skipped rather than erroring. The three new tables
are created via SQLAlchemy's own create_all() scoped to just those
three model classes, which is already idempotent.

Run once, from the Flask app context:

    from app import app
    from migrate_v3_evidence_review import run_migration
    with app.app_context():
        run_migration()

Postgres only (this app no longer supports a SQLite fallback - see
app.py's DATABASE_URL check) - the ALTER TABLE syntax below assumes
Postgres's information_schema.
"""

from sqlalchemy import text
from models import db
from models_survey import RemunerationPolicy, GovernancePolicy, EvidenceConflict

# (column_name, Postgres column type) - matches the db.Column
# definitions in models_survey.py exactly. Kept as a plain list here
# (rather than introspecting the model) so this script's SQL is
# self-contained and reviewable without cross-referencing the model
# file to know what it's about to run against a live database.
_NEW_SURVEY_COLUMNS = [
    ('field_confidence', 'JSON'),
    ('field_review_status', 'JSON'),
    ('field_review_corrections', 'JSON'),
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

    for column_name, column_type in _NEW_SURVEY_COLUMNS:
        if _column_exists('survey_company_data', column_name):
            skipped.append(column_name)
            continue
        db.session.execute(text(
            f'ALTER TABLE survey_company_data ADD COLUMN {column_name} {column_type}'
        ))
        added.append(column_name)

    db.session.commit()

    # New tables - create_all() scoped to just these three via their
    # own __table__.create(), each with checkfirst=True (skip if the
    # table already exists) so this is safe to re-run.
    created_tables = []
    for model in (RemunerationPolicy, GovernancePolicy, EvidenceConflict):
        if not db.inspect(db.engine).has_table(model.__tablename__):
            model.__table__.create(db.engine, checkfirst=True)
            created_tables.append(model.__tablename__)

    print(f"survey_company_data: added columns {added or '(none)'}, "
          f"already present {skipped or '(none)'}")
    print(f"new tables created: {created_tables or '(none - all already existed)'}")


if __name__ == '__main__':
    print("Run this from the Flask app context - see this file's own docstring "
          "for the exact snippet. Not meant to be run as a bare script.")
