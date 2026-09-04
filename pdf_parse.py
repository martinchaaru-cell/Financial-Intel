"""
Full statement-aware PDF extraction, used by the manual upload flow
(/api/import/upload-batch in app.py - see there for the single adapter
every company's uploads go through, regardless of the report's own
format or layout).

Walks every relevant page, buckets lines under whichever financial
statement heading they fall under, and extracts EVERY numeric line on
that statement - not just a five-item allowlist - preserving the page
number and (where we recognize the label) a normalized_name for
cross-company comparison.

Still explicitly heuristic and best-effort: every saved line item carries
its own confidence score rather than being screened out before saving -
see the module docstring on /api/import/upload-batch in app.py for how
that's surfaced to the user (Source Evidence).
"""

import re
import io
import json
import pdfplumber

try:
    import pypdf
except ImportError:  # pragma: no cover - pypdf is in requirements.txt
    pypdf = None

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
# Section-title style notes markers - these need the same short/ALL-CAPS
# heading test _classify_statement uses (via _looks_like_heading_line),
# unlike the two patterns above which are meant to match ordinary
# boilerplate sentences. Without that guard, a running header repeated on
# every page of this document ("...STAKEHOLDERS NOTES OF THE BOD
# INDEX...", the site nav reused as a page banner) or a stray mid-
# sentence mention could trip these.
_SECTION_STOP_HEADING_RES = [
    re.compile(p, re.IGNORECASE) for p in [
        r"financial\s+statements\s*[-\u2013\u2014]\s*notes",
        r"^\s*notes\s*$",
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
        # A bank's income statement sums net interest income + net fee
        # income + other income into a single top-line subtotal labelled
        # "Total net income" (see e.g. Equity Group's format) - it plays
        # the same role "Total operating income" does for other banks,
        # NOT the bottom-line profit figure. Ordered before the generic
        # net_income patterns below so this specific subtotal always wins
        # that match first; without it, "net\s+income" (deliberately kept
        # broad, since plenty of filings really do just say "Net income")
        # would grab it and both misreport revenue as missing and net
        # income as the wrong (much larger, pre-expense) figure.
        r"total\s+net\s+income": 'revenue',
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


# A genuine statement heading in these filings is its own short, ALL-CAPS
# line ("CONSOLIDATED STATEMENT OF FINANCIAL POSITION") - never a mid-
# sentence mention. A long-form annual/integrated report runs 100-300+
# pages of narrative (highlights, strategy, sustainability, governance)
# before the actual statements, and that narrative routinely uses the
# same words in passing - "...strong balance sheet momentum and healthy
# liquidity...", "...give a true and fair view of the financial position
# of the Group...". Those lines have no digit on them either (so the
# TOC guard above doesn't catch them), and without this check one such
# sentence anywhere in the front matter flips current_stmt on early and
# every number on every page after it - for however many pages until the
# real heading is reached - gets vacuumed up as if it were that
# statement's own line items.
_HEADING_MAX_WORDS = 10

# A heading never trails off on a dangling connector - a genuine title is
# a complete noun phrase ("... Statement of Financial Position"), while a
# narrative sentence that got line-wrapped mid-clause ("...strong balance
# sheet momentum and healthy liquidity across the") almost always ends on
# one of these. This is what actually distinguishes the two, rather than
# capitalization: headings are set in ALL CAPS in some filings' PDFs but
# plain sentence-case ("Consolidated statement of ...", capital only on
# the first word) in others, so a capitalization-ratio check can't be
# relied on to hold across different filers/years.
_HEADING_BAD_ENDINGS = {
    'the', 'a', 'an', 'of', 'and', 'or', 'in', 'on', 'at', 'to', 'for',
    'by', 'with', 'that', 'this', 'is', 'are', 'was', 'were', 'as',
}

def _dedupe_word(word: str):
    if len(word) < 2 or len(word) % 2 != 0:
        return None
    even, odd = word[0::2], word[1::2]
    return even if even == odd else None

def _dedupe_char_doubled(line: str):
    """Some filings' PDFs render bold headings as every character drawn
    twice in the content stream - "CCoonnssoolliiddaatteedd" for
    "Consolidated" - which pdfplumber then extracts literally; the single
    space between words is untouched, so this has to de-duplicate word by
    word rather than treating the whole line as one even/odd-index split
    (a single un-doubled space anywhere shifts every following
    character's parity and breaks a whole-line split). Requires nearly
    every word in the line to fit the doubled pattern before returning
    anything, so an ordinary line - short numeric fragment included -
    is never mistaken for one."""
    words = line.split(' ')
    alpha_words = [w for w in words if any(ch.isalpha() for ch in w)]
    if len(alpha_words) < 2:
        return None
    deduped, hits = [], 0
    for w in words:
        dw = _dedupe_word(w) if w else w
        if dw is not None:
            if any(ch.isalpha() for ch in w):
                hits += 1
            deduped.append(dw)
        else:
            deduped.append(w)
    if hits < len(alpha_words) - 1:
        return None
    result = ' '.join(deduped)
    if sum(1 for ch in result if ch.isalpha()) < 8:
        return None
    return result

def _heading_candidate(line: str):
    """The text a heading check should actually run against, and whether
    it got there by collapsing a doubled-character bold heading -
    (candidate_text, was_doubled)."""
    deduped = _dedupe_char_doubled(line)
    return (deduped, True) if deduped is not None else (line, False)

def _looks_like_heading_line(line: str, was_doubled: bool = False) -> bool:
    text = re.sub(r'\(continued\)\s*$', '', line.strip(), flags=re.IGNORECASE).strip()
    words = text.split()
    if not words or len(words) > _HEADING_MAX_WORDS:
        return False
    last_word = re.sub(r'[^a-zA-Z]', '', words[-1]).lower()
    if last_word in _HEADING_BAD_ENDINGS:
        return False
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    # Surviving the doubled-character check above is itself strong
    # evidence this line is a specially-rendered (bold) heading, not body
    # text - ordinary prose in these PDFs is never emitted that way - so
    # a doubled line only needs the length/ending checks above. A normal
    # (non-doubled) line still needs to pass the stricter ALL-CAPS test:
    # that's what actually separates a real heading ("CONSOLIDATED
    # STATEMENT OF FINANCIAL POSITION") from a short, capitalized,
    # non-stopword-ending sentence START that just happens to mention the
    # same words in passing ("Balance sheet resilience defined this
    # year's results.") - which a length/ending/first-letter check alone
    # doesn't reliably catch, as a genuinely glossy report's narrative
    # front matter turned out to contain plenty of.
    if was_doubled:
        return True
    upper_ratio = sum(1 for ch in letters if ch.isupper()) / len(letters)
    return upper_ratio > 0.85


def _classify_statement(line: str):
    # A TOC/contents-page entry repeats the exact heading text followed by
    # a page number or page range on the same line - reject those so the
    # contents page never gets mistaken for the start of the real
    # statement (see _HEADING_HAS_DIGIT_RE's comment above).
    if _HEADING_HAS_DIGIT_RE.search(line):
        return None
    candidate, was_doubled = _heading_candidate(line)
    if not _looks_like_heading_line(candidate, was_doubled):
        return None
    for stmt, regexes in _HEADING_RES.items():
        for rx in regexes:
            if rx.search(candidate):
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
# An uploaded annual report PDF has no filing-listing title to lean on (no
# "Equity Group Holdings Plc - ... For the period ended 30 June 2026" to
# parse), so the fiscal year has to come out of the statement pages
# themselves - see detect_period_label() below.

_MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
           r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
           r"nov(?:ember)?|dec(?:ember)?)")

_PERIOD_END_RES = [
    re.compile(rf"for\s+the\s+year\s+ended\s+\d{{1,2}}\s+{_MONTHS}\s+(\d{{4}})", re.IGNORECASE),
    re.compile(rf"for\s+the\s+period\s+ended\s+\d{{1,2}}\s+{_MONTHS}\s+(\d{{4}})", re.IGNORECASE),
    # Same as the two above but without the leading "for the" - some
    # reports head statement columns with just "Year ended 31 December
    # 2025" (seen on Equity Group Holdings' 2025 integrated report,
    # amongst others). Anchored to the start of the line so it doesn't
    # also match "...for the year ended..." twice.
    re.compile(rf"^\s*year\s+ended\s+\d{{1,2}}\s+{_MONTHS}\s+(\d{{4}})", re.IGNORECASE),
    re.compile(rf"^\s*period\s+ended\s+\d{{1,2}}\s+{_MONTHS}\s+(\d{{4}})", re.IGNORECASE),
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

# Last-resort signal: a bare 4-digit year in the uploaded filename itself
# (e.g. "Equity-Group-Holdings-PLC-2024-Integrated-Report...pdf"). This is
# NOT a substitute for reading the statement pages - a file can be renamed
# or have no year in its name at all - but every comparative statement in
# a two-year report mentions the prior year almost as often as the current
# one (each line has a current-period column and a prior-period column),
# so raw occurrence-counting alone can be a near coin-flip. The filename
# year is used only to break that kind of tie, never to overrule a clear
# textual majority.
_FILENAME_YEAR_RE = re.compile(r"(20\d{2}|19\d{2})")


def _filename_year(filename: str | None) -> str | None:
    if not filename:
        return None
    years = _FILENAME_YEAR_RE.findall(filename)
    return years[-1] if years else None


def detect_period_label(pages_text, filename: str | None = None) -> str | None:
    """Determine the reporting fiscal year and return it as 'FY<year>'.

    Every page is scanned for statement period-end dates ('FOR THE YEAR
    ENDED 31 DECEMBER 2022', 'AT 31 DECEMBER 2022'). Two-year comparative
    statements mention both the current and prior year on almost every
    line (current-period column + prior-period column), so a naive
    "most frequent year wins" count is only a couple of matches away from
    picking the WRONG year on files where the two years are close in
    count - which is most of them. To avoid that:

      1. Count each year once per PAGE (its first match), not once per
         line. A page repeating "Year ended 31 December 2024" as a column
         header alongside a dozen "2023" comparative figures should only
         contribute one vote per year, not vote by line-item count.
      2. If that still leaves a tie or a margin of 1, prefer the higher
         year - a report's reporting year is never earlier than its
         comparative year, so on genuine ambiguity the later year is the
         better guess.
      3. If a filename year is available and matches one of the two
         leading candidates, prefer it as the final tie-breaker over the
         "prefer higher year" rule, since it's an independent signal from
         outside the document body.

    Falls back to the report's cover-page year (_REPORT_TITLE_YEAR_RE) if
    no statement-level date is found anywhere, then to the filename year.
    Returns None only if none of these signals are present at all -
    callers should treat that as 'ask the user', not guess further."""
    year_page_counts: dict[str, int] = {}
    for _, page_text in pages_text:
        years_on_this_page: set[str] = set()
        for raw_line in page_text.splitlines():
            line = raw_line.strip()
            for rx in _PERIOD_END_RES:
                m = rx.search(line)
                if m:
                    years_on_this_page.add(m.group(1))
        for y in years_on_this_page:
            year_page_counts[y] = year_page_counts.get(y, 0) + 1

    fname_year = _filename_year(filename)

    if year_page_counts:
        ranked = sorted(year_page_counts.items(), key=lambda kv: (-kv[1], -int(kv[0])))
        top_year, top_count = ranked[0]
        if len(ranked) > 1:
            second_year, second_count = ranked[1]
            if top_count - second_count <= 1:
                # Near-tie: let a filename year decide if it names one of
                # the two contenders; otherwise fall back to the higher
                # year, per the docstring above.
                if fname_year in (top_year, second_year):
                    return f"FY{fname_year}"
                top_year = max(top_year, second_year, key=int)
        return f"FY{top_year}"

    for _, page_text in pages_text:
        m = _REPORT_TITLE_YEAR_RE.search(page_text)
        if m:
            return f"FY{m.group(1)}"

    if fname_year:
        return f"FY{fname_year}"

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


def _filename_company_candidate(filename: str | None) -> str | None:
    """Best-effort company name straight out of the uploaded filename, e.g.
    'Equity-Group-Holdings-PLC-2024-Integrated-Report...pdf' ->
    'Equity Group Holdings PLC'. Strips the extension, replaces separators
    with spaces, and cuts the string at the first token that signals
    "the report metadata starts here, not the company name" - a 4-digit
    year, or the words that head every annual-report filename pattern seen
    in practice (Integrated/Annual Report, Financial Statement(s)). This is
    ONLY meant to be used as a tie-breaker/fallback alongside content-based
    detection (see detect_company_name) - a renamed file or one with no
    year/company in its name at all simply yields None here, which callers
    should treat the same as "no filename signal available"."""
    if not filename:
        return None
    stem = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    stem = re.sub(r"[_\-]+", " ", stem).strip()
    cut_re = re.compile(
        r"\b(?:19|20)\d{2}\b|\bintegrated\b|\bannual\b|\bfinancial\b", re.IGNORECASE
    )
    m = cut_re.search(stem)
    candidate = stem[:m.start()].strip() if m else stem
    return candidate or None


def detect_company_name(pages_text, max_pages: int = 400, filename: str | None = None) -> str | None:
    """Best-effort company name from the first few pages - most annual
    reports repeat 'X PLC INTEGRATED REPORT AND FINANCIAL STATEMENTS' or
    similar as a running header, which is a more reliable signal than the
    cover page's often stylised title text/graphics.

    Different reports lay out their first few pages very differently - a
    plain annual report usually has a clean all-caps company banner right
    on page 1, but a marketing-led "integrated report" (glossy cover,
    theme tagline, table of contents) often doesn't, and the frequency
    heuristic below can latch onto an unrelated all-caps heading instead
    (e.g. a table-of-contents entry like "A MESSAGE FROM OUR GROUP
    MANAGING DIRECTOR..." matching on the word "GROUP"). To guard against
    that, every candidate found is scored against the known NSE company
    roster (company_directory.match_company) and the best-scoring one
    wins over the merely most-frequent one, whenever any candidate scores
    highly enough to be a confident real match; only falls back to
    "most frequent candidate" when nothing scores well (e.g. a company
    not yet in the roster), same as before.

    If a filename is supplied and content-based detection finds nothing
    at all (no candidate name anywhere in the scanned pages), the
    filename's own leading text (see _filename_company_candidate) is used
    as a last-resort fallback rather than returning None outright. It is
    never used to override a content-based candidate - a company's own
    report text is a stronger signal than how the file happened to be
    named on the portal it was downloaded from.
    """
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
        fname_candidate = _filename_company_candidate(filename)
        return fname_candidate

    try:
        from company_directory import match_company
        best_candidate, best_candidate_score = None, 0.0
        for candidate in counts:
            _, score = match_company(candidate.title())
            if score > best_candidate_score:
                best_candidate, best_candidate_score = candidate, score
        if best_candidate is not None and best_candidate_score >= 0.75:
            return best_candidate.title()
    except ImportError:
        pass

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
            stop_candidate, stop_was_doubled = _heading_candidate(line)
            if _looks_like_heading_line(stop_candidate, stop_was_doubled) and any(rx.search(stop_candidate) for rx in _SECTION_STOP_HEADING_RES):
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


# A "different report, different shape" guard: a scanned/image-only PDF
# (no embedded text layer) will come back with empty or near-empty text on
# every page no matter which extractor is used - there's no OCR here, so
# that case can only be reported honestly, not silently "fixed".
_MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER = 20

# Hard ceiling so a genuinely pathological upload (thousands of pages,
# or a corrupt file that confuses page counting) fails fast with a clear
# message instead of hanging the request indefinitely.
_MAX_PAGES = 600


def _fast_prefilter_pages(pdf_bytes: bytes, always_include: int = 5):
    """First pass with pypdf - much faster than pdfplumber's extract_text()
    but doesn't reliably reconstruct table rows (see the note on
    extract_pdf_document below), so it's only used here to guess which
    page numbers are worth the slow, accurate pass. Returns a sorted list
    of 1-indexed candidate page numbers, or None if pypdf isn't available
    (callers should fall back to scanning every page in that case, rather
    than silently returning nothing).

    Always includes the first `always_include` pages regardless of
    whether a heading was found on them - detect_company_name() reads
    from the first few pages (the cover/running header), which usually
    has no statement heading of its own and would otherwise get dropped
    by the prefilter entirely.
    """
    if pypdf is None:
        return None
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        num_pages = len(reader.pages)
    except Exception:
        return None
    if num_pages == 0:
        return None

    candidates = set(range(1, min(always_include, num_pages) + 1))
    for i in range(num_pages):
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            # A single malformed page shouldn't sink the whole prefilter -
            # just treat it as having no heading and move on.
            continue
        for line in text.splitlines():
            if _classify_statement(line.strip()):
                page_num = i + 1
                # A statement's line items almost always run several pages
                # past its own heading page (continuation pages, notes-free
                # runs of numbers) - grab a generous window forward, plus
                # one page back in case the heading itself sits just before
                # the detected page due to text-extraction quirks.
                for p in range(max(1, page_num - 1), min(num_pages, page_num + 6) + 1):
                    candidates.add(p)
    return sorted(candidates)


def extract_pdf_document(pdf_bytes: bytes) -> dict:
    """The one adapter every uploaded PDF goes through, regardless of
    which company or report format it came from - a slim standalone
    financial-statements PDF, a scanned image PDF, or a dense 300-page
    integrated report all reach this same function, and it's the ONLY
    place that runs pdfplumber's slow extract_text() over an uploaded
    file. (Previously the upload route extracted page text once itself,
    for company/period detection, and then parse_financials_pdf()
    extracted it again from scratch for the actual statement parsing -
    two full slow passes over the same PDF. That doubling was the biggest
    single contributor to large uploads timing out; this function is the
    fix - one slow pass, whose output feeds both detection and parsing.)

    Returns {"pages_text": [(page_num, text), ...], "statements": {...}} -
    "pages_text" covers whichever pages were actually read (see the
    performance note below, not necessarily the full document) and is
    what detect_company_name()/detect_period_label() should be called
    with; "statements" is parse_financials_text()'s normal output.

    NOTE on performance: page-by-page pdfplumber.extract_text() is the
    slow part of an import - measured ~105s for a dense 298-page
    integrated report, almost entirely inside that one call. A PyMuPDF
    swap was tried and reverted: PyMuPDF's plain get_text() is ~55x
    faster but does NOT reconstruct table rows the way pdfplumber does -
    "Interest income 6 188,329 185,344" (one line, one row) comes back
    from PyMuPDF as four separate lines, one per column - which silently
    breaks _split_label_and_numbers() and guts extraction rather than
    just slowing it down.

    What IS done about it: pypdf (much faster, same row-splitting problem
    as PyMuPDF) runs a first pass over every page just to spot statement
    headings, then only that shortlist of candidate pages - typically a
    few dozen out of a few hundred, plus the first few pages for company
    detection - goes through the slow, accurate pdfplumber pass. A large
    integrated report's ~280 pages of narrative, governance and
    sustainability content never touch the slow extractor. If the fast
    pass can't find any headings (pypdf missing, or a layout it can't
    read at all), every page is scanned the slow way exactly as before -
    correctness never depends on the fast pass succeeding.

    This does NOT fully solve very large batches - moving big-PDF imports
    off the request/response cycle entirely (background worker + poll)
    is the real fix and hasn't been built; if a single file is still slow
    enough to hit a platform-level request timeout, that's a proxy/worker
    timeout setting to raise, not something this function can catch.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        num_pages = len(pdf.pages)
        if num_pages == 0:
            return {'pages_text': [], 'statements': {}}
        if num_pages > _MAX_PAGES:
            raise ValueError(
                f"PDF has {num_pages} pages, over the {_MAX_PAGES}-page limit for a single upload. "
                "Split it or upload the relevant section separately."
            )

        candidate_pages = _fast_prefilter_pages(pdf_bytes)
        page_numbers = candidate_pages if candidate_pages else range(1, num_pages + 1)

        pages_text = []
        pages_with_text = 0
        for i in page_numbers:
            try:
                text = pdf.pages[i - 1].extract_text() or ""
            except Exception:
                # One unreadable page (corrupt object, unsupported font)
                # shouldn't abort the whole import - skip it and keep going.
                text = ""
            pages_text.append((i, text))
            if len(text) >= _MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER:
                pages_with_text += 1

        if pages_with_text == 0:
            raise ValueError(
                "No extractable text found on any scanned page - this looks like a scanned "
                "image PDF with no embedded text layer, which isn't supported yet (no OCR)."
            )

    return {'pages_text': pages_text, 'statements': parse_financials_text(pages_text)['statements']}


def parse_financials_pdf(pdf_bytes: bytes) -> dict:
    """Thin wrapper over extract_pdf_document() that returns just the
    parsed statements - kept for the __main__ smoke test below and any
    other caller that only needs statements, not page text. Callers that
    also need company/period detection (i.e. the upload route) should
    call extract_pdf_document() directly instead, so the PDF is only
    read once - see its docstring.
    """
    return {'statements': extract_pdf_document(pdf_bytes)['statements']}


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