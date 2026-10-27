"""The member dashboard's "my company": billing_organisations.company_id (shared by the organisation's team) and
app_users.home_company_id (accounts with no organisation). Idempotent and safe on every boot: each column is added
only if it is missing."""
from sqlalchemy import inspect, text

from models import db


def _add(table, name, ddl):
    insp = inspect(db.engine)
    if table not in insp.get_table_names():
        return
    if name not in {c['name'] for c in insp.get_columns(table)}:
        db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
        db.session.commit()


def run_migration():
    _add('billing_organisations', 'company_id', 'INTEGER')
    _add('app_users', 'home_company_id', 'INTEGER')
