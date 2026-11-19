"""Conservative pay header evidence; never infer a scale from amount magnitude."""
import re
from decimal import Decimal
from monetary_units import SCALES

CURRENCY = r'(?:kshs?|shs?|kes|ksh|usd|frw|ugx|tzs)'
SCALED = re.compile(r'(?<!\w)' + CURRENCY + r"\.?\s*['’‘`]?\s*(000\b|thousands?\b|millions?\b|mn\b|m\b|billions?\b|bn\b)", re.I)
BARE = re.compile(r'^\s*(?:(?:' + CURRENCY + r')\.?[\s|,;/]*)+\s*$', re.I)

def header_evidence(text):
    hits = []
    for line in (text or '').splitlines():
        line = line.strip()
        if len(line) > 240: continue
        remainder = SCALED.sub('', line)
        remainder = BARE.sub('', remainder)
        remainder = re.sub(r'\b(?:salary|fees?|total|allowances?|benefits?|bonus|retainer|pension|gratuity|cash|non|sitting|directors?|remuneration|year)\b', '', remainder, flags=re.I)
        if re.search(r'[A-Za-z0-9]', remainder):
            if not BARE.fullmatch(line): continue
        for match in SCALED.finditer(line):
            token = match.group(1).lower()
            unit = 'thousands' if token == '000' or token.startswith('thousand') else 'billions' if token.startswith('billion') or token == 'bn' else 'millions'
            hits.append((unit, line))
        if BARE.fullmatch(line): hits.append(('units', line))
    return hits

def resolve_headers(texts):
    hits = [hit for text in texts for hit in header_evidence(text)]
    units = {u for u, _ in hits}
    return (next(iter(units)) if len(units) == 1 else None, hits)

def reconcile(named, disclosed, unit):
    """Only call with a single complete table, excluding subtotals and duplicate rows."""
    if unit not in SCALES or disclosed is None or not named or any(v is None for v in named): return None
    values = [Decimal(str(v)) for v in named]
    target = Decimal(str(disclosed))
    if not all(v.is_finite() for v in values + [target]): return None
    delta = (sum(values) - target) * SCALES[unit]
    tolerance = Decimal(len(values) + 1) / 2 * SCALES[unit]
    return {'difference_currency': str(delta), 'within_rounding': abs(delta) <= tolerance}
