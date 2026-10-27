"""Billing: adds the organisation / sign-in columns to app_users and seeds the default price plans.

Idempotent and safe on every boot (same pattern as v5-v10): a column is only added if it is missing, and
plans are only seeded while the plan table is empty - once a super admin edits prices on the Billing page,
this never touches them again. The billing tables themselves are created by db.create_all() in app.py.
"""
from sqlalchemy import inspect, text

from models import db

_COLUMNS = {
    'organization_id': 'INTEGER',
    'org_role': 'VARCHAR(20)',
    'session_token': 'VARCHAR(64)',
}

# Starting prices (KES, before VAT). Change them on the Billing page, not here.
DEFAULT_PLANS = [
    dict(code='professional_monthly', name='Professional - monthly', price=15000, interval_months=1, seats=1,
         sort_order=10, self_serve=True, description='1 user, all years, sector and size filters.'),
    dict(code='professional_annual', name='Professional - annual', price=150000, interval_months=12, seats=1,
         sort_order=20, self_serve=True, description='1 user, all years, sector and size filters. Two months free.'),
    dict(code='team_monthly', name='Team - monthly', price=45000, interval_months=1, seats=5,
         sort_order=30, self_serve=True, description='Up to 5 users and custom peer groups.'),
    dict(code='team_annual', name='Team - annual', price=450000, interval_months=12, seats=5,
         sort_order=40, self_serve=True, description='Up to 5 users and custom peer groups. Two months free.'),
    dict(code='enterprise_annual', name='Enterprise', price=1200000, interval_months=12, seats=10,
         sort_order=50, self_serve=False,
         description='10+ users, all countries, full data access and custom analysis. Invoiced; priced per client.'),
]

# Downloads a month per plan: (reports, data files, bulk ZIP / whole-portfolio). None = unlimited, 0 = not included.
DEFAULT_DOWNLOADS = {
    'professional_monthly': (None, 20, 0),
    'professional_annual': (None, 20, 0),
    'team_monthly': (None, 50, 2),
    'team_annual': (None, 50, 2),
    'enterprise_annual': (None, None, None),
}


def run_migration():
    existing = {c['name'] for c in inspect(db.engine).get_columns('app_users')}
    for name, ddl in _COLUMNS.items():
        if name not in existing:
            db.session.execute(text(f'ALTER TABLE app_users ADD COLUMN {name} {ddl}'))
    db.session.commit()
    seed_plans()


def seed_plans():
    from models_billing import Plan
    # Raw count: works even before migrate_v12 has added its columns to an older billing_plans table.
    if db.session.execute(text('SELECT COUNT(*) FROM billing_plans')).scalar():
        return
    for p in DEFAULT_PLANS:
        r, d, b = DEFAULT_DOWNLOADS[p['code']]
        db.session.add(Plan(currency='KES', countries='all', active=True, tier=p['code'].split('_')[0], report_downloads_per_month=r,
                            data_downloads_per_month=d, bulk_downloads_per_month=b, **p))
    db.session.commit()
