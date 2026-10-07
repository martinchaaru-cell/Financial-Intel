"""Adds company.slug (URL-friendly name) and fills it for every existing company.

Idempotent and safe on every boot (same pattern as v5-v9)."""
from sqlalchemy import inspect, text
from models import db


def run_migration():
    cols = {c['name'] for c in inspect(db.engine).get_columns('company')}
    if 'slug' not in cols:
        db.session.execute(text('ALTER TABLE company ADD COLUMN slug VARCHAR(140)'))
        db.session.commit()
    existing = {i['name'] for i in inspect(db.engine).get_indexes('company')}
    if 'ix_company_slug' not in existing:
        db.session.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_company_slug ON company (slug)'))
        db.session.commit()
    import public_pages
    public_pages.ensure_slugs()
