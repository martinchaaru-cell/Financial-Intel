"""Independent remuneration scaling. Missing pay unit follows existing survey policy: whole currency."""
import math

SCALES = {'units': 1, 'ones': 1, 'thousands': 1000, 'millions': 1000000, 'billions': 1000000000, 'trillions': 1000000000000}

def pay_scale(unit):
    return SCALES.get(str(unit or 'units').strip().lower(), 1)

def convert_pay(value, source, target):
    if value is None:
        return None
    source = str(source or '').strip().lower()
    if source not in SCALES:
        raise ValueError('Missing or unknown source pay unit')
    result = float(value) * SCALES[source] / pay_scale(target)
    if not math.isfinite(result):
        raise ValueError('Non-finite pay amount')
    return result

def audit_flags(total, unit, annual_ceo=None, turnover=None):
    if total is None:
        return []
    amount = float(total) * pay_scale(unit)
    flags = []
    if not math.isfinite(amount):
        return ['non_finite']
    if turnover and turnover > 0 and abs(amount) > turnover:
        flags.append('pay_exceeds_turnover')
    if annual_ceo and annual_ceo > 0 and abs(amount) > annual_ceo * 100:
        flags.append('pay_over_100x_ceo_cost')
    return flags
