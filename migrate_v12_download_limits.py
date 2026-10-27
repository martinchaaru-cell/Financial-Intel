"""Download allowances by kind on billing_plans: reports (PDF/Word), data files (CSV/Excel) and bulk files
(ZIP bundles, whole-portfolio exports). None = unlimited, 0 = not included in the plan.

Idempotent and safe on every boot. When the columns are first added, existing plans are filled in: the five
starting plans get their standard allowances; any other plan keeps its old monthly export number as its data
allowance, unlimited reports and no bulk files. Plain SQL throughout, so it works before later columns exist.
"""
from sqlalchemy import inspect, text

from models import db

_COLUMNS = ('report_downloads_per_month', 'data_downloads_per_month', 'bulk_downloads_per_month')


def run_migration():
    insp = inspect(db.engine)
    if 'billing_plans' not in insp.get_table_names():
        return
    existing = {c['name'] for c in insp.get_columns('billing_plans')}
    missing = [c for c in _COLUMNS if c not in existing]
    if not missing:
        return
    for name in missing:
        db.session.execute(text(f'ALTER TABLE billing_plans ADD COLUMN {name} INTEGER'))
    db.session.commit()
    from migrate_v11_billing import DEFAULT_DOWNLOADS
    rows = db.session.execute(text('SELECT id, code, exports_per_month FROM billing_plans')).fetchall()
    for pid, code, old in rows:
        r, d, b = DEFAULT_DOWNLOADS.get(code, (None, old, 0))
        db.session.execute(text('UPDATE billing_plans SET report_downloads_per_month = :r, data_downloads_per_month = :d, '
                                'bulk_downloads_per_month = :b WHERE id = :id'), {'r': r, 'd': d, 'b': b, 'id': pid})
    db.session.commit()
