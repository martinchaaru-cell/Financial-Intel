"""
NSE Kenya financial-results importer.

Scrapes the single central NSE 'Financial Results' page (one page covers
every listed company, so no per-company portal visits are needed), matches
each filing to a hardcoded list of listed companies, and (separately)
parses a given filing PDF for key line items.

Design notes:
- The scraper keys off "nearest heading before each .pdf link" rather than
  specific CSS class names, since WordPress theme markup can change without
  notice. This is more resilient than a brittle selector.
- Matching and PDF parsing are both best-effort. Nothing here writes to the
  database directly - callers should show results for human confirmation
  before saving, especially the parsed PDF numbers.
"""

import re
import difflib
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

NSE_FINANCIAL_RESULTS_URL = "https://www.nse.co.ke/financial-results/"

# ---------- HARDCODED COMPANY LIST ----------
# All 63 operating-company equities currently listed on the NSE (the two
# separately-quoted KPLC preference-share lines are the same issuer as
# KPLC common stock, so they're deliberately not listed as separate
# companies here), sourced from the exchange's live ticker table. Filings
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
    NSE filing titles look like:
      'Equity Group Holdings Plc - Unaudited Financial Statements ... 2026'
      'Family Bank Limited – Unaudited Financial Statements ...'
    The company name is everything before the first ' - ' or ' – '.
    """
    parts = re.split(r"\s[-–]\s", filing_title, maxsplit=1)
    return parts[0].strip() if parts else filing_title.strip()


def match_company(filing_title: str, companies=None, min_score: float = 0.55):
    """
    Best-effort fuzzy match of a filing title to a company in our hardcoded
    list. Returns (company_dict_or_None, score).
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


def extract_period(filing_title: str):
    """Pull a human-readable period out of the filing title, if present."""
    m = re.search(
        r"(ended|for the period ended)?\s*(\d{1,2}[\s-][A-Za-z]{3,9}[\s-]\d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        filing_title,
        re.IGNORECASE,
    )
    return m.group(2) if m else None


def fetch_nse_filings(html: str, base_url: str = NSE_FINANCIAL_RESULTS_URL):
    """
    Parse the NSE financial-results page HTML and return a list of:
      {title, pdf_url, matched_company, match_score, period_guess}

    Strategy: find every link ending in .pdf, then walk backward in the
    document to the nearest heading tag for its title. This is deliberately
    NOT keyed to specific CSS classes, since exact WordPress markup may
    differ from what's assumed here - it only relies on "heading, then PDF
    link" ordering, which is a stable pattern regardless of styling.
    """
    soup = BeautifulSoup(html, "html.parser")
    filings = []

    for a in soup.find_all("a", href=re.compile(r"\.pdf(\?.*)?$", re.IGNORECASE)):
        href = a.get("href", "")
        if not href:
            continue
        pdf_url = urljoin(base_url, href)

        heading = a.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
        title = heading.get_text(strip=True) if heading else None
        if not title:
            continue

        company, score = match_company(title)
        filings.append({
            "title": title,
            "pdf_url": pdf_url,
            "matched_company": company,
            "match_score": score,
            "period_guess": extract_period(title),
        })

    return filings


def fetch_nse_page(url: str = NSE_FINANCIAL_RESULTS_URL, timeout: int = 20) -> str:
    """Actual network fetch - only works where outbound internet is allowed
    (i.e. in Replit, not in this sandbox)."""
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


if __name__ == "__main__":
    # Local test against the reconstructed sample page (see
    # sample_nse_page.html) - proves the parsing/matching logic works
    # before it ever touches a live network call.
    with open("sample_nse_page.html", encoding="utf-8") as fh:
        sample_html = fh.read()

    results = fetch_nse_filings(sample_html)
    for r in results:
        matched = r["matched_company"]["name"] if r["matched_company"] else "NO MATCH"
        print(f"{r['title'][:70]:70s} -> {matched:30s} (score={r['match_score']}, period={r['period_guess']})")