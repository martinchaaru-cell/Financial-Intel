"""One-off: mark BK Group's survey years as RWF and Umeme's as UGX, so the survey report stops
averaging their figures as if they were Kenya shillings.

Run once from the Replit Shell, in the Financial-Intel folder:
    python set_currency.py
It only changes the currency label on those two companies' survey years; no figures are altered.
"""
from app import app
from models import db, Company
from models_survey import SurveyCompanyData

TARGETS = {'BK Group': 'RWF', 'Umeme': 'UGX'}

with app.app_context():
    for name, cur in TARGETS.items():
        co = Company.query.filter(Company.name.ilike(name)).first()
        if co is None:
            print(f'{name}: company not found - nothing changed')
            continue
        rows = SurveyCompanyData.query.filter_by(company_id=co.id).all()
        for r in rows:
            print(f'{name} {r.fiscal_year}: currency {r.currency or "(blank = KES)"} -> {cur}')
            r.currency = cur
    db.session.commit()
    print('Done.')