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

# A real statement heading is just the heading text (optionally with
# "(Continued)") - nothing else on the line. A table-of-contents entry
# has the SAME heading text but with trailing page numbers tacked on
# ("Consolidated Statement of Financial Position 75 - 76"), which is
# what makes the TOC page get misread as the start of the real
# statement, pulling every front-matter line after it in as if it were
# balance-sheet data. Any digit on a heading-matching line is that
# signal - a genuine heading line never contains one.
_HEADING_HAS_DIGIT_RE = re.compile(r"\d")

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
        # NSE's own statement labels its top line "Total income" (a sum of
        # transaction levies/listing fees/data-vending income, not a
        # conventional "Revenue" line) - anchored to match only the exact
        # subtotal line, not "Total comprehensive income" or "Total other
        # comprehensive income" which also contain "income" but are a
        # different figure entirely.
        r"^total\s+income$": 'revenue',
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

# A thousands-grouped number: 1,234 or 1,234,567 - every comma-separated
# group after the first is exactly 3 digits. Used to tell a real amount
# ("6,047") apart from a comma-joined note list ("12,14").
_THOUSANDS_RE = re.compile(r"^\(?-?\d{1,3}(,\d{3})+(\.\d+)?\)?$")

# A single note-reference token: 1-3 bare digits, optionally with an
# "(a)" or "(b)(iii)" style sub-reference suffix. Deliberately does not
# match anything comma-grouped on its own - that's handled by the
# thousands-vs-note-list check in _strip_note_reference below.
_NOTE_TOKEN_RE = re.compile(r"^\d{1,3}(\([a-z]+\)(\([ivx]+\))?)?")


def _parse_number(token: str):
    token = token.strip()
    negative = token.startswith("(") and token.endswith(")")
    token = token.strip("()").replace(",", "")
    try:
        value = float(token)
    except ValueError:
        return None
    return -value if negative else value


def _strip_note_reference(remainder: str) -> str:
    """NSE-style statements insert a Notes column between the label and
    the actual amounts, e.g.:
        'Staff costs 8 177,359 171,841 177,359 171,841'
        'Depreciation and amortization 12,14 48,512 54,915 ...'
        "Directors' emoluments 32(a) 47,289 43,606 ..."
    Without this, the parser reads the note number itself (8, or the
    first of "12,14") as if it were the reported figure. Peel off a
    single leading note-reference token - which may itself be a
    comma-joined list of note numbers like "12,14" - before number
    extraction runs. A real thousands-grouped amount ("6,047") is left
    untouched: the distinguishing signal is that a genuine amount's
    post-comma chunk is always exactly 3 digits, checked via
    _THOUSANDS_RE, whereas a note list's chunks are 1-3 digits with no
    thousands-grouping meaning."""
    remainder = remainder.lstrip()
    m = _NOTE_TOKEN_RE.match(remainder)
    if not m:
        return remainder
    after = remainder[m.end():]
    if after[:1] != ',':
        return after.lstrip()

    next_space = remainder.find(' ')
    first_chunk = remainder[:next_space] if next_space != -1 else remainder
    if _THOUSANDS_RE.match(first_chunk):
        return remainder  # genuine thousands-grouped amount - don't strip

    # Comma-joined note list ("12,14", "6, 9, 14a") - consume each
    # further ", <note>" segment.
    rest = after
    while rest.startswith(','):
        m2 = re.match(r",\s*\d{1,3}(\([a-z]+\))?", rest)
        if not m2:
            break
        rest = rest[m2.end():]
    return rest.lstrip()


def _split_label_and_numbers(line: str):
    """A statement line typically looks like:
        'Total operating income     6   45,231,000   38,940,000'
    label, an optional note-reference token, then the amount column(s)
    (by NSE convention: Group current, Group prior, [Company current,
    Company prior] where applicable). Splits the label off, strips any
    note reference, and returns the remaining numbers in report order."""
    matches = list(_NUMBER_RE.finditer(line))
    if not matches:
        return None, []
    label = line[:matches[0].start()].strip(' .')
    remainder = _strip_note_reference(line[matches[0].start():])
    numbers = [_parse_number(m.group()) for m in _NUMBER_RE.finditer(remainder)]
    numbers = [n for n in numbers if n is not None]
    return label, numbers


_JUNK_LABEL_RES = [
    re.compile(p, re.IGNORECASE) for p in [
        r"^k?shs\.?[`']?\s*$",                    # "Shs." / "Kshs.`" bare currency-unit fragments
        r"^k?shs\.?\s*['\u2018\u2019]?0*\s*$",     # "Shs '000" style unit-column headers
        r"^sh\.?\s*['\u2018\u2019]?0*\s*$",        # "Sh'" / "Sh '000'" - same as above without the "k"
        r"^assets\s*k?shs\.?[`']?\s*$",            # "Assets Shs.`" - column header run-together
        r"^liabilities\s*k?shs\.?[`']?\s*$",
        r"^[a-h]\)\s*$",                           # bare lettered legend marker, e.g. "a)" alone
        r"^[a-h]\s+assets\s*$",                    # "A Assets" - lettered column header, not a
        r"^[a-h]\s+liabilities\s*$",               # real reported line item (compare "Total Assets",
        r"^assets\s*$",                            # which has a real qualifier word and is kept)
        r"^liabilities\s*$",
        r"^for\s+the\s+(year|period)\s+ended\b",   # statement period-end heading, e.g. "FOR THE
                                                    # YEAR ENDED 31 DECEMBER 2022" - the date it
                                                    # contains gets misread as an amount otherwise
        r"^at\s+\d",                               # balance-sheet-style "AT 31 DECEMBER 2022" heading
        r"^notes?\s*$",                            # bare "Notes" / "Note" column header
        r"^group\s+company\s*$",                   # "Group Company" - Bank/Company/Group column
        r"^group\s*$",                             # super-header row, and its bare single-word
        r"^company\s*$",                           # forms when it wraps onto its own line
        r"^bank\s*$",
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
    # A TOC/contents-page entry repeats the exact heading text followed by
    # a page number or page range on the same line - reject those so the
    # contents page never gets mistaken for the start of the real
    # statement (see _HEADING_HAS_DIGIT_RE's comment above).
    if _HEADING_HAS_DIGIT_RE.search(line):
        return None
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


# ---------- PERIOD DETECTION (for uploaded PDFs with no filing-title metadata) ----------
# nse_import.py's extract_period() works off an NSE filing-page title like
# "Equity Group Holdings Plc - ... For the period ended 30 June 2026" - a
# locally-uploaded annual report PDF has no such title, so the year has to
# come out of the statement pages themselves.

_MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
           r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
           r"nov(?:ember)?|dec(?:ember)?)")

_PERIOD_END_RES = [
    re.compile(rf"for\s+the\s+year\s+ended\s+\d{{1,2}}\s+{_MONTHS}\s+(\d{{4}})", re.IGNORECASE),
    re.compile(rf"for\s+the\s+period\s+ended\s+\d{{1,2}}\s+{_MONTHS}\s+(\d{{4}})", re.IGNORECASE),
    # Balance-sheet-style "AT 31 DECEMBER 2022" heading - anchored to the
    # WHOLE line (not just "ends with", which .search() doesn't enforce on
    # its own) so it can't match "...at 2022" buried inside a wrapped
    # narrative paragraph elsewhere in the report.
    re.compile(rf"^at\s+\d{{1,2}}\s+{_MONTHS}\s+(\d{{4}})$", re.IGNORECASE),
]
# Fallback: the report's own running header, e.g. "... INTEGRATED REPORT AND
# FINANCIAL STATEMENTS 2022" repeated on nearly every page - a much weaker
# signal (it's the *report's* cover year, not necessarily every statement's
# period end) so it's only used if nothing above matched anywhere.
_REPORT_TITLE_YEAR_RE = re.compile(
    r"(?:annual\s+report|financial\s+statements|integrated\s+report)\D{0,20}(\d{4})",
    re.IGNORECASE,
)


def detect_period_label(pages_text) -> str | None:
    """Scan every page for a statement period-end date ('FOR THE YEAR ENDED
    31 DECEMBER 2022', 'AT 31 DECEMBER 2022') and return the most common
    year found as 'FY<year>'. Falls back to the report's cover-page year if
    no statement-level date is found. Returns None if neither is present -
    callers should treat that as 'ask the user', not guess further."""
    year_counts = {}
    for _, page_text in pages_text:
        for raw_line in page_text.splitlines():
            line = raw_line.strip()
            for rx in _PERIOD_END_RES:
                m = rx.search(line)
                if m:
                    year_counts[m.group(1)] = year_counts.get(m.group(1), 0) + 1

    if year_counts:
        best_year = max(year_counts, key=year_counts.get)
        return f"FY{best_year}"

    for _, page_text in pages_text:
        m = _REPORT_TITLE_YEAR_RE.search(page_text)
        if m:
            return f"FY{m.group(1)}"

    return None


def detect_prior_period_label(period_label: str | None) -> str | None:
    """The comparative column a two-column statement carries (NSE's
    'amount'/'prior_amount' pair - see parse_financials_text's docstring)
    is always the fiscal year immediately before the detected one: annual
    reports show exactly one prior year of comparatives, never further
    back, and NSE's own reports mark that column with a footnote ("*
    Change in presentation of comparatives") confirming it's the prior
    FY, not an arbitrary earlier year. Only handles the 'FY<year>' shape
    detect_period_label() produces - a period label detect_period_label
    never returns (quarterly, half-year) has no defined prior label here,
    so callers should treat None as 'don't attempt a second save'."""
    if not period_label or not period_label.startswith('FY'):
        return None
    try:
        year = int(period_label[2:])
    except ValueError:
        return None
    return f"FY{year - 1}"


def detect_company_name(pages_text, max_pages: int = 5) -> str | None:
    """Best-effort company name from the first few pages - most annual
    reports repeat 'X PLC INTEGRATED REPORT AND FINANCIAL STATEMENTS' or
    similar as a running header, which is a more reliable signal than the
    cover page's often stylised title text/graphics."""
    name_re = re.compile(
        r"([A-Z][A-Z .&'\-]{3,60}?(?:PLC|LIMITED|LTD|GROUP|HOLDINGS))\b",
    )
    counts = {}
    for page_num, page_text in pages_text:
        if page_num > max_pages:
            break
        for line in page_text.splitlines():
            m = name_re.search(line)
            if m:
                name = m.group(1).strip()
                counts[name] = counts.get(name, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get).title()


def _derive_total_liabilities(items: list) -> dict | None:
    """NSE's own statement layout never states 'Total Liabilities' as its
    own labeled line - it only gives 'TOTAL ASSETS' and 'Total equity' /
    'TOTAL SHAREHOLDERS' FUNDS AND LIABILITIES' (equity+liabilities
    combined), leaving debt_equity/debt_assets with nothing to compute
    from downstream (ratios.py, Financial Health Score, Debt/Equity on
    every Intelligence Report tab). Where a direct 'Total Liabilities'
    line genuinely wasn't found, derive it the only way the accounting
    identity allows: total_assets - total_equity. Returns a dict with
    'amount' and/or 'prior_amount' (only the columns that were
    computable), or None if total_assets/total_equity themselves weren't
    both found for a given column."""
    by_name = {li['normalized_name']: li for li in items if li.get('normalized_name')}
    if 'total_liabilities' in by_name:
        return None  # a real line was found - never override it with a derived one

    assets = by_name.get('total_assets')
    equity = by_name.get('total_equity')
    if not assets or not equity:
        return None

    derived = {}
    if assets.get('amount') is not None and equity.get('amount') is not None:
        derived['amount'] = assets['amount'] - equity['amount']
    if assets.get('prior_amount') is not None and equity.get('prior_amount') is not None:
        derived['prior_amount'] = assets['prior_amount'] - equity['prior_amount']
    return derived or None


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

    Each line item: {label, normalized_name, amount, prior_amount, page,
    confidence, order_index}. `amount` is the current-period (first)
    column; `prior_amount` is the comparative prior-period column when
    the statement's layout has one (NSE's convention: current period,
    then prior period, per Group/Company block) - None if only one
    column was present on the line. Callers that only care about the
    current period can keep reading `amount` exactly as before;
    `prior_amount` is purely additive.

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
            prior_amount = numbers[1] if len(numbers) > 1 else None

            if normalized not in _SIGNED_ALLOWED:
                amount = abs(amount)
                if prior_amount is not None:
                    prior_amount = abs(prior_amount)

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
                'prior_amount': prior_amount,
                'page': page_num,
                'confidence': 0.9 if normalized else 0.5,
                'order_index': order_counters[current_stmt],
            })

    # Derived total_liabilities (see _derive_total_liabilities docstring) -
    # balance sheet only, added as its own synthetic line item so it flows
    # through _save_one_import's normalized_name -> flat[] pickup exactly
    # like a directly-parsed line would, just with a lower confidence
    # score and a label that says plainly it's computed, not read off the
    # page.
    bs_items = statements.get('balance_sheet', [])
    derived = _derive_total_liabilities(bs_items)
    if derived:
        order_counters['balance_sheet'] += 1
        bs_items.append({
            'label': 'Total Liabilities (derived: Total Assets − Total Equity)',
            'normalized_name': 'total_liabilities',
            'amount': derived.get('amount'),
            'prior_amount': derived.get('prior_amount'),
            'page': None,
            'confidence': 0.6,
            'order_index': order_counters['balance_sheet'],
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