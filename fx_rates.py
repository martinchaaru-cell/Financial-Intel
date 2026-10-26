"""Exchange rates used to bring companies that report in another currency (BK Group in RWF, Umeme in UGX)
into the KES survey averages.

Rate = Kenya shillings per ONE unit of the foreign currency, for the calendar year the fiscal year ends in.
Source: World Bank "Official exchange rate (LCU per US$, period average)" (indicator PA.NUS.FCRF), annual
averages for Kenya, Rwanda and Uganda, crossed through the US dollar:
    KES per RWF = (KES per USD) / (RWF per USD)
A year-average rate is used because turnover, profit and pay are earned through the year.

To add a year or a currency, add a line below. A currency/year with no rate is left out of the averages
(and listed as such in the report) rather than guessed.
"""

# World Bank PA.NUS.FCRF, LCU per USD, period average (retrieved 13 Oct 2026)
_PER_USD = {
    'KES': {2021: 109.637746592367, 2022: 117.865989201682, 2023: 139.846383759616,
            2024: 134.822483279332, 2025: 129.302572099377},
    'RWF': {2021: 988.624806551768, 2022: 1030.30829974206, 2023: 1160.09869425,
            2024: 1318.12835733333, 2025: 1428.47827558333},
    'UGX': {2021: 3587.05170733908, 2022: 3689.81738566862, 2023: 3726.14045997085,
            2024: 3757.2625354215, 2025: 3602.98994206813},
}
SOURCE = 'World Bank official exchange rates (annual average, via USD)'


def _year(fiscal_year):
    digits = ''.join(ch for ch in str(fiscal_year or '') if ch.isdigit())
    return int(digits[:4]) if len(digits) >= 4 else None


def rate_to(target, currency, fiscal_year):
    """Units of `target` per one unit of `currency` for that fiscal year; 1.0 when they are the same;
    None when no rate is on file (callers then leave the figure out, never guess)."""
    target = (target or 'KES').strip().upper()
    currency = (currency or 'KES').strip().upper()
    if currency == target:
        return 1.0
    y = _year(fiscal_year)
    t, c = _PER_USD.get(target, {}).get(y), _PER_USD.get(currency, {}).get(y)
    if not t or not c:
        return None
    return t / c