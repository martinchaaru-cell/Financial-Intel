"""
NSE Kenya company directory + name matching.

This used to also contain a scraper for the NSE 'Financial Results' web
page (fetch_nse_filings / fetch_nse_page / fetch_nse_filings_for_year).
That flow is gone: all financial statement PDFs are now uploaded manually
(see /api/import/upload-batch in app.py), so there is nothing left to
scan or fetch here. What's left is the one part still genuinely needed -
the static roster of NSE-listed companies and the fuzzy name matcher that
identifies which company an uploaded PDF belongs to.

Design notes:
- Matching is best-effort. It never writes to the database directly -
  callers decide what to do with a low-confidence or missing match.
"""

import re
import difflib

# ---------- HARDCODED COMPANY LIST ----------
# All 63 operating-company equities currently listed on the NSE (the two
# separately-quoted KPLC preference-share lines are the same issuer as
# KPLC common stock, so they're deliberately not listed as separate
# companies here), sourced from the exchange's live ticker table. A PDF
# for anything not in this list will still come back unmatched - if a new
# company lists, add it here.
NSE_COMPANIES = [
    # --- Agricultural ---
    {"name": "Eaagads", "ticker": "EGAD", "sector": "Agricultural"},
    {"name": "Kakuzi", "ticker": "KUKZ", "sector": "Agricultural"},
    {"name": "Kapchorua Tea Company", "ticker": "KAPC", "sector": "Agricultural"},
    {"name": "Limuru Tea Company", "ticker": "LIMT", "sector": "Agricultural"},
    {"name": "Sasini", "ticker": "SASN", "sector": "Agricultural",
     "aliases": ["Sasini Tea and Coffee"]},
    {"name": "Williamson Tea Kenya", "ticker": "WTK", "sector": "Agricultural"},
    {"name": "Kenya Orchards", "ticker": "ORCH", "sector": "Agricultural"},
    # --- Automobiles & Accessories ---
    {"name": "Car and General Kenya", "ticker": "CGEN", "sector": "Automobiles & Accessories"},
    {"name": "Sameer Africa", "ticker": "SMER", "sector": "Automobiles & Accessories"},
    # --- Banking ---
    {"name": "Absa Bank Kenya", "ticker": "ABSA", "sector": "Banking"},
    {"name": "Family Bank", "ticker": "FMLY", "sector": "Banking",
     "aliases": ["Family Bank Limited"]},
    {"name": "BK Group", "ticker": "BKG", "sector": "Banking"},
    {"name": "Co-operative Bank of Kenya", "ticker": "COOP", "sector": "Banking"},
    {"name": "Diamond Trust Bank Kenya", "ticker": "DTK", "sector": "Banking"},
    {"name": "Equity Group Holdings", "ticker": "EQTY", "sector": "Banking"},
    {"name": "HF Group", "ticker": "HFCK", "sector": "Banking"},
    {"name": "I&M Group", "ticker": "IMH", "sector": "Banking", "aliases": ["I&M Holdings"]},
    {"name": "KCB Group", "ticker": "KCB", "sector": "Banking"},
    {"name": "NCBA Group", "ticker": "NCBA", "sector": "Banking"},
    {"name": "Stanbic Holdings", "ticker": "SBIC", "sector": "Banking"},
    {"name": "Standard Chartered Bank Kenya", "ticker": "SCBK", "sector": "Banking"},
    # --- Commercial & Services ---
    {"name": "Deacons East Africa", "ticker": "DCON", "sector": "Commercial & Services"},
    {"name": "Eveready East Africa", "ticker": "EVRD", "sector": "Commercial & Services"},
    {"name": "Home Afrika", "ticker": "HAFR", "sector": "Commercial & Services"},
    {"name": "Homeboyz Entertainment", "ticker": "HBE", "sector": "Commercial & Services"},
    {"name": "Longhorn Publishers", "ticker": "LKL", "sector": "Commercial & Services"},
    {"name": "Nairobi Business Ventures", "ticker": "NBV", "sector": "Commercial & Services"},
    {"name": "Nation Media Group", "ticker": "NMG", "sector": "Commercial & Services"},
    {"name": "Standard Group", "ticker": "SGL", "sector": "Commercial & Services"},
    {"name": "WPP ScanGroup", "ticker": "SCAN", "sector": "Commercial & Services", "aliases": ["ScanGroup"]},
    {"name": "TPS Eastern Africa (Serena)", "ticker": "TPSE", "sector": "Commercial & Services"},
    {"name": "Uchumi Supermarket", "ticker": "UCHM", "sector": "Commercial & Services"},
    {"name": "Express Kenya", "ticker": "XPRS", "sector": "Commercial & Services"},
    # --- Construction & Allied ---
    {"name": "Bamburi Cement", "ticker": "BAMB", "sector": "Construction & Allied"},
    {"name": "Crown Paints Kenya", "ticker": "CRWN", "sector": "Construction & Allied"},
    {"name": "ARM Cement", "ticker": "ARM", "sector": "Construction & Allied"},
    {"name": "East African Portland Cement", "ticker": "PORT", "sector": "Construction & Allied"},
    # --- Energy & Petroleum ---
    {"name": "KenGen", "ticker": "KEGN", "sector": "Energy & Petroleum"},
    {"name": "Kenya Power and Lighting", "ticker": "KPLC", "sector": "Energy & Petroleum"},
    {"name": "Total Kenya", "ticker": "TOTL", "sector": "Energy & Petroleum"},
    {"name": "Umeme", "ticker": "UMME", "sector": "Energy & Petroleum"},
    # --- Insurance ---
    {"name": "Britam Holdings", "ticker": "BRIT", "sector": "Insurance"},
    {"name": "CIC Insurance Group", "ticker": "CIC", "sector": "Insurance"},
    {"name": "Jubilee Holdings", "ticker": "JUB", "sector": "Insurance"},
    {"name": "Kenya Re-Insurance Corporation", "ticker": "KNRE", "sector": "Insurance"},
    {"name": "Liberty Kenya Holdings", "ticker": "LBTY", "sector": "Insurance"},
    {"name": "Sanlam Kenya", "ticker": "SLAM", "sector": "Insurance"},
    # --- Investment ---
    {"name": "Centum Investment", "ticker": "CTUM", "sector": "Investment"},
    {"name": "Olympia Capital Holdings", "ticker": "OCH", "sector": "Investment"},
    {"name": "TransCentury", "ticker": "TCL", "sector": "Investment"},
    {"name": "Kurwitu Ventures", "ticker": "KURV", "sector": "Investment"},
    # --- Investment Services ---
    {"name": "Nairobi Securities Exchange", "ticker": "NSE", "sector": "Investment Services",
     "aliases": ["NSE Plc"]},
    # --- Manufacturing & Allied ---
    {"name": "British American Tobacco Kenya", "ticker": "BAT", "sector": "Manufacturing & Allied"},
    {"name": "BOC Kenya", "ticker": "BOC", "sector": "Manufacturing & Allied"},
    {"name": "Carbacid Investments", "ticker": "CARB", "sector": "Manufacturing & Allied"},
    {"name": "East African Breweries", "ticker": "EABL", "sector": "Manufacturing & Allied"},
    {"name": "East African Cables", "ticker": "CABL", "sector": "Manufacturing & Allied"},
    {"name": "Flame Tree Group Holdings", "ticker": "FTGH", "sector": "Manufacturing & Allied"},
    {"name": "Mumias Sugar Company", "ticker": "MSC", "sector": "Manufacturing & Allied"},
    {"name": "Unga Group", "ticker": "UNGA", "sector": "Manufacturing & Allied"},
    # --- Telecommunication & Technology ---
    {"name": "Safaricom", "ticker": "SCOM", "sector": "Telecommunication & Technology",
     "aliases": ["Safaricom Plc"]},
    # --- Real Estate Investment Trusts ---
    {"name": "Laptrust Imara Income-REIT", "ticker": "LAPR", "sector": "Real Estate (REIT)"},
    {"name": "Stanlib Fahari I-REIT", "ticker": "FAHR", "sector": "Real Estate (REIT)"},
    # --- Exchange Traded Funds ---
    {"name": "Absa NewGold ETF", "ticker": "GLD", "sector": "ETF"},
    # --- Transport ---
    {"name": "Kenya Airways", "ticker": "KQ", "sector": "Transport"},
]

_LEGAL_SUFFIXES = re.compile(
    r"\b(plc|ltd|limited|holdings|group|kenya|company|the)\b", re.IGNORECASE
)


def _normalize(name: str) -> str:
    """Lowercase, strip legal suffixes/punctuation for fairer matching."""
    name = name.replace("&amp;", "&")
    name = _LEGAL_SUFFIXES.sub("", name)
    name = re.sub(r"[^a-z0-9 ]", "", name.lower())
    return re.sub(r"\s+", " ", name).strip()


def extract_company_title(filing_title: str) -> str:
    """
    A detected company name (from a PDF cover page, or historically an NSE
    filing title) sometimes carries a trailing description after a dash,
    e.g. 'Equity Group Holdings Plc - Unaudited Financial Statements'.
    The company name is everything before the first ' - ' or ' – '.
    """
    parts = re.split(r"\s[-–]\s", filing_title, maxsplit=1)
    return parts[0].strip() if parts else filing_title.strip()


def match_company(filing_title: str, companies=None, min_score: float = 0.75):
    """
    Best-effort fuzzy match of a detected company name to a company in our
    hardcoded list. Returns (company_dict_or_None, score).

    min_score was 0.55 until a real false positive was found: "Family Bank
    Limited" (a company not yet in NSE_COMPANIES at the time) scored 0.6
    against "Absa Bank Kenya" - both are short "X Bank ..." strings, which
    inflates difflib's character-overlap ratio even though the names
    aren't a real match - and got silently saved into Absa's records.
    Genuine matches (including aliased/reworded titles) score at or near
    1.0 here because of the substring boost below, so 0.75 leaves a wide
    safety margin above that false positive while still accepting real
    matches. A title that now falls under 0.75 comes back as NO MATCH
    rather than guessing - which is the correct failure mode: an unmatched
    upload needs a human (or an NSE_COMPANIES addition), a wrongly-matched
    one silently corrupts another company's data.
    """
    companies = companies if companies is not None else NSE_COMPANIES
    candidate_name = _normalize(extract_company_title(filing_title))
    if not candidate_name:
        return None, 0.0

    best_match, best_score = None, 0.0
    for company in companies:
        names_to_try = [company["name"]] + company.get("aliases", [])
        for candidate_target in names_to_try:
            target = _normalize(candidate_target)
            score = difflib.SequenceMatcher(None, candidate_name, target).ratio()
            # Boost if one is a substring of the other (handles "Equity
            # Group Holdings" vs "Equity Group Holdings Plc" cleanly).
            if candidate_name in target or target in candidate_name:
                score = max(score, 0.9)
            if score > best_score:
                best_match, best_score = company, score

    if best_score >= min_score:
        return best_match, round(best_score, 2)
    return None, round(best_score, 2)
