"""Plan tier on billing_plans (professional / team / enterprise). The tier decides which pages a plan unlocks
(billing.TIER_FEATURES). Idempotent and safe on every boot: adds the column if missing, then gives any plan
without a tier one from its code ('team_annual' -> team; anything else -> professional). Plain SQL, so it
works whatever other columns the table has. Change a plan's tier on the Billing page > Plans.
"""
from sqlalchemy import inspect, text

from models import db


def run_migration():
    insp = inspect(db.engine)
    if 'billing_plans' not in insp.get_table_names():
        return
    if 'tier' not in {c['name'] for c in insp.get_columns('billing_plans')}:
        db.session.execute(text('ALTER TABLE billing_plans ADD COLUMN tier VARCHAR(20)'))
        db.session.commit()
    from billing import infer_tier
    for pid, code in db.session.execute(text('SELECT id, code FROM billing_plans WHERE tier IS NULL')).fetchall():
        db.session.execute(text('UPDATE billing_plans SET tier = :t WHERE id = :id'), {'t': infer_tier(code), 'id': pid})
    db.session.commit()
