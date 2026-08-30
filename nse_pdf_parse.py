"""
Phase 3 — full statement-aware PDF extraction.

Upgrades the old "grab five numbers" parser into one that walks every
page, buckets lines under whichever financial statement heading they fall
under, and extracts EVERY numeric line on that statement - not just a
five-item allowlist - preserving the page number and (where we recognize
the label) a normalized_name for cross-company comparison.

Still explicitly heuristic and best-effort: results are DRAFT and must go
through human review before being saved (the /api/import/nse/scan ->
/parse -> review -> /save flow is unchanged, /parse and /save just carry
richer data now).
"""

import re
import io
import json
import requests
import pdfplumber

# ---------- STATEMENT HEADING DETECTION ----------

STATEMENT_HEADINGS = {
    'income_statement': [
        r"statement\s+of\s+comprehensive\s+income",
        r"statement\s+of\s+profit\s+or\s+loss",
        r"income\s+statement",
    ],
    'balance_sheet': [
        r"statement\s+of\s+financial\s+position",
        r"balance\s+sheet",
    ],
    'cash_flow': [
        r"statement\s+of\s+cash\s+flows?",
    ],
    'equity': [
        r"statement\s+of\s+changes\s+in\s+equity",
    ],
}
_HEADING_RES = {
    stmt: [re.compile(p, re.IGNORECASE) for p in patterns]
    for stmt, patterns in STATEMENT_HEADINGS.items()
}

# Lines that end a statement's numeric section even without hitting the
# next heading (notes sections, page furniture) - stop pulling line items
# once one of these appears, so we don't scrape footnote numbers.
_SECTION_STOP_RES = [
    re.compile(p, re.IGNORECASE) for p in [
        r"^\s*notes?\s+to\s+the\s+financial\s+statements",
        r"^\s*the\s+notes.*form\s+an\s+integral\s+part",
    ]
]

# ---------- CANONICAL LABEL -> normalized_name ----------
# Anything not matched here is still extracted, just with
# normalized_name=None (an "unmapped" line item) - the review UI lets a
# human assign one, or leave it as a raw extra line.

CANONICAL_LINE_ITEMS = {
    'income_statement': {
        r"total\s+operating\s+income": 'revenue',
        r"^\s*revenue\b": 'revenue',
        r"total\s+revenue": 'revenue',
        r"total\s+interest\s+and\s+similar\s+income": 'interest_income',
        r"interest\s+expense": 'interest_expense',
        r"cost\s+of\s+sales": 'cost_of_sales',
        r"gross\s+profit": 'gross_profit',
        r"employee\s+costs?": 'employee_costs',
        r"staff\s+costs?": 'employee_costs',
        r"depreciation": 'depreciation',
        r"amortisation|amortization": 'amortisation',
        r"finance\s+costs?": 'finance_costs',
        r"profit\s+before\s+tax": 'profit_before_tax',
        r"income\s+tax\s+expense": 'tax_expense',
        r"profit\s+for\s+the\s+(period|year)": 'net_income',
        r"profit\s+after\s+tax": 'net_income',
        r"net\s+profit": 'net_income',
        r"net\s+income": 'net_income',
        r"earnings\s+per\s+share": 'eps',
    },
    'balance_sheet': {
        r"total\s+assets": 'total_assets',
        r"total\s+liabilities": 'total_liabilities',
        r"total\s+equity": 'total_equity',
        r"total\s+shareholders.\s+funds": 'total_equity',
        r"cash\s+and\s+cash\s+equivalents": 'cash_and_equivalents',
        r"trade\s+and\s+other\s+receivables": 'receivables',
        r"trade\s+and\s+other\s+payables": 'payables',
        r"inventor(y|ies)": 'inventory',
        r"total\s+current\s+assets": 'current_assets',
        r"total\s+current\s+liabilities": 'current_liabilities',
        r"property,?\s+plant\s+and\s+equipment": 'ppe',
        r"borrowings": 'borrowings',
        r"share\s+capital": 'share_capital',
        r"retained\s+earnings": 'retained_earnings',
    },
    'cash_flow': {
        r"net\s+cash\s+(generated\s+from|from|used\s+in)\s+operating": 'operating_cash_flow',
        r"net\s+cash\s+(generated\s+from|from|used\s+in)\s+investing": 'investing_cash_flow',
        r"net\s+cash\s+(generated\s+from|from|used\s+in)\s+financing": 'financing_cash_flow',
        r"cash\s+and\s+cash\s+equivalents\s+at\s+(the\s+)?end": 'cash_end_of_period',
        r"purchase\s+of\s+property": 'capex',
    },
    'equity': {
        r"balance\s+at\s+(the\s+)?(start|beginning)": 'equity_opening_balance',
        r"balance\s+at\s+(the\s+)?end": 'equity_closing_balance',
        r"dividends?\s+paid": 'dividends_paid',
        r"total\s+comprehensive\s+income": 'total_comprehensive_income',
    },
}
_CANONICAL_RES = {
    stmt: [(re.compile(p, re.IGNORECASE), norm) for p, norm in mapping.items()]
    for stmt, mapping in CANONICAL_LINE_ITEMS.items()
}

# Figures that are genuinely allowed to be negative (a loss, a cash
# outflow) - everything else gets abs()'d even if parentheses-styled,
# since balance-sheet/income magnitudes shouldn't come out negative just
# because of accounting formatting.
_SIGNED_ALLOWED = {
    'net_income', 'operating_cash_flow', 'investing_cash_flow',
    'financing_cash_flow', 'total_comprehensive_income', 'eps',
    'dividends_paid',
}

_NUMBER_RE = re.compile(r"\(?-?\d[\d,]*\.?\d*\)?")
_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z ,&/()'-]*[A-Za-z)]")


def _parse_number(token: str):
    token = token.strip()
    negative = token.startswith("(") and token.endswith(")")
    token = token.strip("()").replace(",", "")
    try:
        value = float(token)
    except ValueError:
        return None
    return -value if negative else value


def _split_label_and_numbers(line: str):
    """A statement line typically looks like:
        'Total operating income          45,231,000   38,940,000'
    (current period first, then prior period). Split the label text off
    from the trailing numeric columns."""
    matches = list(_NUMBER_RE.finditer(line))
    if not matches:
        return None, []
    label = line[:matches[0].start()].strip(' .')
    numbers = [_parse_number(m.group()) for m in matches]
    numbers = [n for n in numbers if n is not None]
    return label, numbers


_JUNK_LABEL_RES = [
    re.compile(p, re.IGNORECASE) for p in [
        r"^k?shs\.?[`']?\s*$",                    # "Shs." / "Kshs.`" bare currency-unit fragments
        r"^k?shs\.?\s*['\u2018\u2019]?0*\s*$",     # "Shs '000" style unit-column headers
        r"^assets\s*k?shs\.?[`']?\s*$",            # "Assets Shs.`" - column header run-together
        r"^liabilities\s*k?shs\.?[`']?\s*$",
        r"^[a-h]\)\s*$",                           # bare lettered legend marker, e.g. "a)" alone
        r"^[a-h]\s+assets\s*$",                    # "A Assets" - lettered column header, not a
        r"^[a-h]\s+liabilities\s*$",               # real reported line item (compare "Total Assets",
        r"^assets\s*$",                            # which has a real qualifier word and is kept)
        r"^liabilities\s*$",
    ]
]


def _looks_like_label(label: str) -> bool:
    if not label or not _LABEL_RE.match(label):
        return False
    stripped = label.strip()
    if len(stripped) <= 2:
        return False
    # Reject currency/unit header fragments and bare lettered legend
    # markers that tabular banking-disclosure pages (e.g. NPL schedules
    # with an "a) ... g)" column legend and a "Shs. '000" unit header
    # running through the number columns) get misread as if they were
    # real reported line items.
    if any(rx.match(stripped) for rx in _JUNK_LABEL_RES):
        return False
    return True


def _classify_statement(line: str):
    for stmt, regexes in _HEADING_RES.items():
        for rx in regexes:
            if rx.search(line):
                return stmt
    return None


def match_canonical_label(statement_type, label):
    """Public entry point so callers outside this module (e.g. the manual
    report builder / NSE-review save path in app.py) can resolve a
    hand-typed label like "Revenue" or "Net income" to the same
    normalized_name the PDF parser would assign - so ratios.py can find it
    regardless of whether the figure came from a parsed PDF or was typed
    in by hand."""
    return _match_canonical(statement_type, label)


def _match_canonical(stmt_type, label):
    for rx, norm_name in _CANONICAL_RES.get(stmt_type, []):
        if rx.search(label):
            return norm_name
    return None


def parse_financials_text(pages_text) -> dict:
    """
    pages_text: iterable of (page_number, page_text), 1-indexed.

    Returns:
        {"statements": {
            "income_statement": {"line_items": [...]},
            "balance_sheet":    {"line_items": [...]},
            "cash_flow":        {"line_items": [...]},
            "equity":           {"line_items": [...]},
        }}

    Each line item: {label, normalized_name, amount, page, confidence, order_index}
    Statements with zero recognized lines are omitted entirely.
    """
    statements = {stmt: [] for stmt in STATEMENT_HEADINGS}
    current_stmt = None
    order_counters = {stmt: 0 for stmt in STATEMENT_HEADINGS}
    seen_normalized = {stmt: set() for stmt in STATEMENT_HEADINGS}
    seen_raw = {stmt: set() for stmt in STATEMENT_HEADINGS}

    for page_num, page_text in pages_text:
        for raw_line in page_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            heading_match = _classify_statement(line)
            if heading_match:
                current_stmt = heading_match
                continue

            if current_stmt is None:
                continue

            if any(rx.search(line) for rx in _SECTION_STOP_RES):
                current_stmt = None
                continue

            label, numbers = _split_label_and_numbers(line)
            if not numbers or not _looks_like_label(label):
                continue

            normalized = _match_canonical(current_stmt, label)
            amount = numbers[0]  # current-period column, by NSE convention

            if normalized not in _SIGNED_ALLOWED:
                amount = abs(amount)

            # Dedupe, two levels:
            # 1. Any exact (label, amount) repeat within a statement is
            #    dropped outright - this catches identical rows that
            #    appear more than once regardless of whether they were
            #    canonically recognized (e.g. an unmapped line repeated
            #    across a continuation page or a note cross-reference).
            raw_key = (label.strip().lower(), amount)
            if raw_key in seen_raw[current_stmt]:
                continue
            seen_raw[current_stmt].add(raw_key)

            # 2. For recognized (normalized) line items specifically, only
            #    the first occurrence is kept even if a later repeat has a
            #    different-looking label (subtotals sometimes repeat with
            #    slightly reworded text on continuation pages).
            if normalized:
                if normalized in seen_normalized[current_stmt]:
                    continue
                seen_normalized[current_stmt].add(normalized)

            order_counters[current_stmt] += 1
            statements[current_stmt].append({
                'label': label,
                'normalized_name': normalized,
                'amount': amount,
                'page': page_num,
                'confidence': 0.9 if normalized else 0.5,
                'order_index': order_counters[current_stmt],
            })

    return {
        'statements': {
            stmt: {'line_items': items} for stmt, items in statements.items() if items
        }
    }


def parse_financials_pdf(pdf_bytes: bytes) -> dict:
    """Extract text page-by-page (page number matters now, for provenance)
    and run the statement-aware parser over it."""
    pages_text = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            pages_text.append((i, page.extract_text() or ""))
    return parse_financials_text(pages_text)


def fetch_and_parse_pdf(pdf_url: str, timeout: int = 30) -> dict:
    """Download a filing PDF and parse it. Requires real internet access
    (works from Replit; will fail in a sandboxed environment)."""
    resp = requests.get(pdf_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    resp.raise_for_status()
    return parse_financials_pdf(resp.content)


if __name__ == "__main__":
    # Local smoke test with synthetic multi-statement text, to confirm the
    # section-tracking + extraction logic before ever touching a real PDF.
    sample_text = """
    STATEMENT OF COMPREHENSIVE INCOME
    Total operating income                          45,231,000   40,100,000
    Employee costs                                   (8,204,000)  (7,900,000)
    Depreciation                                     (3,102,000)  (2,850,000)
    Profit before tax                                15,900,000   13,200,000
    Income tax expense                               (2,995,500)  (2,500,000)
    Profit for the period                            12,904,500   10,700,000
    STATEMENT OF FINANCIAL POSITION
    Property, plant and equipment                    120,400,000  110,200,000
    Cash and cash equivalents                         45,000,000   38,000,000
    Total assets                                     512,400,000  480,100,000
    Trade and other payables                          22,100,000   19,800,000
    Total liabilities                               (410,100,000) (390,000,000)
    Total equity                                      102,300,000   90,100,000
    STATEMENT OF CASH FLOWS
    Net cash generated from operating activities       18,200,000   15,100,000
    Net cash used in investing activities              (9,400,000)  (8,200,000)
    NOTES TO THE FINANCIAL STATEMENTS
    1. Basis of preparation ...
    """
    result = parse_financials_text([(1, sample_text)])
    print(json.dumps(result, indent=2))