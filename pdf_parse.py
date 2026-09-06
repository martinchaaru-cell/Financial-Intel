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
        # \s* (not \s+) between "cash" and "flow(s)" - confirmed on a
        # real KCB filing that prints this heading as one word,
        # "Consolidated statement of cashflows", alongside other filings
        # that space it normally ("statement of cash flows").
        r"statement\s+of\s+cash\s*flows?",
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
# A numbered note heading ("1. Reporting entity", "2. Material accounting
# policies", "36. Deposits from customers") - confirmed on a real KCB
# filing to be the ONLY signal that the notes section has started on
# filings that never print a standalone "Notes to the Financial
# Statements" banner line at all (the numbered notes just start directly
# after the primary statements). Checked against every page of that
# filing's real statements (pages 73-78) with zero false positives - the
# numbering only starts exactly where the notes do. Still run through
# the same short-line heading guard as _SECTION_STOP_HEADING_RES so a
# numbered list item deep inside ordinary body prose (rare, but possible)
# doesn't trip it.
_NUMBERED_NOTE_HEADING_RE = re.compile(r"^\s*\d{1,2}\.\s+[A-Z]")


def _is_numbered_note_heading(line: str) -> bool:
    if not _NUMBERED_NOTE_HEADING_RE.match(line):
        return False
    candidate, was_doubled = _heading_candidate(line)
    return _looks_like_heading_line(candidate, was_doubled)


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
        # Anchored so a genuine revenue TOTAL line wins over a same-
        # statement component that also starts with "Revenue" (e.g.
        # "Revenue from contracts with customers" / "Revenue from other
        # sources" printed as their own lines above the real "Total
        # revenue" line, per IFRS 15 disclosure convention) - confirmed
        # on a real filing where the component line, appearing first,
        # otherwise won the seen_normalized dedupe ahead of the real
        # total a few lines later. A standalone "Revenue" total line has
        # nothing after the word itself but note refs/whitespace, so this
        # loses no genuine match.
        r"^\s*revenue\s*$": 'revenue',
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
        # "Operating profit" / "Profit from operations" - the pre-finance-
        # cost, pre-tax subtotal a P&L reports before "profit before tax"
        # (which is operating profit net of finance costs/income). Kept
        # distinct from profit_before_tax below - anchored with a
        # trailing \b so it doesn't also swallow "Operating profit margin"
        # commentary lines, and ordered before profit_before_tax since a
        # line literally reading "Operating profit before tax" (seen on
        # some filings) should still resolve to operating_profit, not be
        # shadowed by the narrower phrase.
        r"operating\s+profit\b": 'operating_profit',
        r"profit\s+from\s+operations": 'operating_profit',
        r"results?\s+from\s+operating\s+activities": 'operating_profit',
        r"profit\s+before\s+(income\s+)?tax": 'profit_before_tax',
        r"income\s+tax\s+expense": 'tax_expense',
        # Anchored against a trailing qualifier ("...from continuing
        # operations") since that's a P&L component subtotal, not the
        # true bottom line - confirmed on a real filing where "Profit
        # for the year from continuing operations" appears before the
        # real total "Profit for the period"/"Profit for the year" a few
        # lines later, and being first would otherwise win the
        # seen_normalized dedupe over the actual bottom-line figure.
        r"profit\s+for\s+the\s+(period|year)(?!\s+from\s+continuing)": 'net_income',
        r"profit\s+after\s+tax": 'net_income',
        # Anchored to the start of the line (allowing a leading bullet/
        # dash) - unanchored "net\s+profit"/"net\s+income" also matched
        # inside unrelated lines that merely contain that phrase, e.g.
        # "Share of net profit from associates accounted for using
        # equity method" (a P&L component, not the bottom-line total) -
        # confirmed on a real filing where that component line preceded
        # the real "Profit for the period" total and, being first, won
        # the seen_normalized dedupe, silently discarding the real
        # figure. A genuine net-profit/net-income total line always
        # leads with that phrase (label text before it is a note number
        # or bullet character at most), so anchoring loses no real match.
        r"^[\s\-\u2022]*net\s+profit\b": 'net_income',
        r"^[\s\-\u2022]*net\s+income\b": 'net_income',
        r"earnings\s+per\s+share": 'eps',
        # Weighted-average / basic shares outstanding - usually sits right
        # next to the EPS line in the P&L or its accompanying note, stated
        # as a share count rather than a currency amount. "Weighted
        # average number of (ordinary )?shares" is the IFRS-standard
        # phrasing; "shares in issue" is the more plain-language variant
        # some filings use for the same figure.
        r"weighted\s+average\s+number\s+of\s+(ordinary\s+)?shares": 'shares_outstanding',
        r"number\s+of\s+shares\s+in\s+issue": 'shares_outstanding',
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
        # "Net cash (flows )?(generated )?from/used in operating..." - the
        # optional "flows" covers filings (e.g. KCB) that write "Net cash
        # flows from operating activities" rather than "Net cash generated
        # from operating activities"; both phrasings are common across
        # NSE filings and refer to the same cash flow statement subtotal.
        r"net\s+cash\s+(flows\s+)?(generated\s+from|from|used\s+in)\s+operating": 'operating_cash_flow',
        r"net\s+cash\s+(flows\s+)?(generated\s+from|from|used\s+in)\s+investing": 'investing_cash_flow',
        r"net\s+cash\s+(flows\s+)?(generated\s+from|from|used\s+in)\s+financing": 'financing_cash_flow',
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
    'dividends_paid', 'operating_profit',
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
    # a doubled line only needs the length/ending checks above.
    if was_doubled:
        return True
    # NOTE: an ALL-CAPS line is a strong positive signal but is NOT
    # required - confirmed on real filings (KCB 2024/2025) that print
    # their genuine statement headings in plain sentence-case
    # ("Consolidated statement of comprehensive income", capital only on
    # the first word). Requiring upper_ratio > 0.85 here silently
    # dropped every statement in those filings down to the fast
    # prefilter's 5-page fallback. The length + bad-ending checks above
    # are deliberately NOT enough on their own to call this a heading
    # (a narrative fragment like "Balance sheet resilience defined this
    # year's results." also passes them) - callers that go on to treat a
    # match as the start of a real statement section (parse_financials_text)
    # must additionally corroborate with _has_nearby_note_column() before
    # trusting it; the fast prefilter (_fast_prefilter_pages) doesn't need
    # that corroboration since it only produces a candidate-page shortlist,
    # not the final parse.
    return True


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


# A real audited statement's heading is followed within a line or two by
# a "Note" column header (alongside the year columns) - confirmed on
# every real KCB/NSE-convention filing checked. A pre-statement summary
# table that reuses the SAME heading wording (e.g. a "Five-Year Review"
# page in the front matter, confirmed on a real KCB filing: "Consolidated
# statement of financial position" sitting over a 5-year, no-Note-column
# KPI table, not the actual balance sheet) never has this column, because
# it isn't tied to numbered notes to the accounts at all. This is what
# lets _looks_like_heading_line() safely accept sentence-case headings
# without reopening that false positive - see its docstring note.
# A real statement's column-header line names the "Note" column on its
# own (optionally alongside a currency-unit column, e.g. " Note Kshs
# million Kshs million") - confirmed across every real statement page
# checked (KCB 2024/2025). This must NOT match a bare cross-reference
# like "(Note 46)" inside ordinary notes prose (confirmed on a real KCB
# filing: "Off balance sheet letters of credit and guarantees (Note 46)"
# sits a few lines under an unrelated "(d) Off balance sheet items"
# sub-heading that otherwise false-matches the balance_sheet pattern -
# see _has_nearby_note_column's docstring). The distinction: a genuine
# column-header line is SHORT (just column labels, no sentence content)
# and starts with "Note" as its own word, rather than merely containing
# "Note" anywhere in a longer sentence.
_NOTE_COLUMN_RE = re.compile(r"^\s*notes?\b", re.IGNORECASE)


def _has_nearby_note_column(lines: list, heading_idx: int, lookahead: int = 7) -> bool:
    for line in lines[heading_idx + 1: heading_idx + 1 + lookahead]:
        stripped = line.strip()
        # Column-header line only - short (a handful of column labels,
        # never a full sentence) and starting with "Note" as its own
        # word. The word-count cap is what rules out a "(Note 46)"
        # cross-reference sitting inside an otherwise-long sentence.
        if _NOTE_COLUMN_RE.match(stripped) and len(stripped.split()) <= 6:
            return True
    return False


# "statement of changes in equity" is specific enough on its own -
# confirmed across real filings checked, this exact phrase never turns
# up loosely in narrative/notes prose the way "balance sheet", "income
# statement" or "statement of cash flows" do (see _has_nearby_note_column's
# docstring for those). Its own real heading also often has no Note
# column in the next few lines (the statement's column headers are share
# capital/premium/retained earnings/etc, not a notes reference) so
# requiring corroboration here would create false NEGATIVES instead -
# missing the real section - without meaningfully reducing false
# positives, since there's essentially no false-positive risk for this
# heading in the first place.
_HEADINGS_NOT_REQUIRING_CORROBORATION = {'equity'}


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
        page_lines = [l.strip() for l in page_text.splitlines()]
        for line_idx, line in enumerate(page_lines):
            if not line:
                continue

            heading_match = _classify_statement(line)
            if heading_match:
                # Corroborate with a nearby "Note" column before trusting
                # this as the real statement's start - see
                # _has_nearby_note_column's docstring for why (a
                # front-matter summary table, or a narrative sentence
                # that happens to say e.g. "balance sheet", can otherwise
                # match now that sentence-case headings are accepted -
                # see _looks_like_heading_line's note). Without that
                # corroboration, just skip this line rather than starting
                # (or ending) a section on a false positive - a real
                # statement heading appears again, correctly corroborated,
                # once the actual statement page is reached. 'equity' is
                # exempt - see _HEADINGS_NOT_REQUIRING_CORROBORATION.
                if (heading_match in _HEADINGS_NOT_REQUIRING_CORROBORATION
                        or _has_nearby_note_column(page_lines, line_idx)):
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
            if _is_numbered_note_heading(line):
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
                page = pdf.pages[i - 1]
                # Some reports (seen in the wild: a landscape "integrated
                # report" layout, roughly double a normal portrait
                # page's width) print two independent logical report
                # pages side by side on one physical PDF page - plain
                # extract_text() reads across both halves line-by-line,
                # interleaving e.g. "Consolidated statement of profit or
                # loss" with an unrelated statement printed next to it,
                # which silently corrupts every numeric row that follows
                # (still non-empty text, so this fails silently rather
                # than raising - the only symptom is wrong/missing
                # numbers reaching parse_financials_text). Detect that
                # layout via aspect ratio and reconstruct each half
                # separately by word x-position instead of reading the
                # page as one block; a normal single-column page is
                # unaffected and costs nothing extra (extract_words() is
                # only called for pages that fail the aspect-ratio check).
                halves = _split_double_page_text(page)
                for half_text in halves:
                    pages_text.append((i, half_text))
                combined_len = sum(len(h) for h in halves)
            except Exception:
                # One unreadable page (corrupt object, unsupported font)
                # shouldn't abort the whole import - skip it and keep going.
                pages_text.append((i, ""))
                combined_len = 0
            if combined_len >= _MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER:
                pages_with_text += 1

        if pages_with_text == 0:
            raise ValueError(
                "No extractable text found on any scanned page - this looks like a scanned "
                "image PDF with no embedded text layer, which isn't supported yet (no OCR)."
            )

    return {'pages_text': pages_text, 'statements': parse_financials_text(pages_text)['statements']}


def _split_double_page_text(page) -> list:
    """Returns [text] for a normal single-column page, or [left_text,
    right_text] for a double-wide page whose two logical halves need
    reconstructing independently (see extract_pdf_document's docstring
    on why plain extract_text() silently corrupts these). Both halves
    are attributed to the same physical page number by the caller -
    that's still correct provenance, since they really are on that one
    PDF page.

    Aspect ratio (width/height) is the detection signal: a genuine
    two-logical-pages-per-sheet layout is landscape and roughly double a
    normal portrait page's proportions (confirmed on a real filing at
    1417x850 - almost exactly 2x a standard 708x850 portrait half). A
    merely-wide-but-still-single-content landscape page (a wide table,
    a chart) would need a much higher threshold to false-positive on,
    so 1.4 leaves comfortable room above normal portrait (~0.77) and
    normal landscape (~1.3) aspect ratios without also catching this
    genuinely-two-page layout's 1.67.
    """
    width, height = page.width, page.height
    if height == 0 or width / height < 1.4:
        return [page.extract_text() or '']
    words = page.extract_words()
    if not words:
        return [page.extract_text() or '']
    midpoint = width / 2
    left_words = [w for w in words if w['x0'] < midpoint]
    right_words = [w for w in words if w['x0'] >= midpoint]
    if not left_words or not right_words:
        # words all fell on one side (e.g. a full-width table spanning
        # the midpoint) - this isn't actually a two-page layout, don't
        # force a split that would just cut a real table in half
        return [page.extract_text() or '']

    def _reconstruct(ws):
        ws = sorted(ws, key=lambda w: (round(w['top'] / 3), w['x0']))
        lines = []
        cur_top = None
        cur_line = []
        for w in ws:
            if cur_top is None or abs(w['top'] - cur_top) < 3:
                cur_line.append(w)
                cur_top = w['top'] if cur_top is None else cur_top
            else:
                lines.append(' '.join(x['text'] for x in sorted(cur_line, key=lambda x: x['x0'])))
                cur_line = [w]
                cur_top = w['top']
        if cur_line:
            lines.append(' '.join(x['text'] for x in sorted(cur_line, key=lambda x: x['x0'])))
        return '\n'.join(lines)

    return [_reconstruct(left_words), _reconstruct(right_words)]


# ---------- DIRECTOR REMUNERATION (totals-only) ----------
#
# Deliberately scoped to ONE figure: the report's own printed grand total
# for director/executive remuneration for the period, taken only when the
# filing prints that total explicitly as its own row. Some filings (seen
# in the wild: NSE's own report) split remuneration across multiple
# tables/subtotals with no single printed grand total - reconstructing
# one there would mean deciding which subtotals to add, which is a
# judgement call this function deliberately does NOT make silently, since
# a wrong guess here would look just as authoritative as a real filed
# number. Those filings simply return no total - "not available", never
# a computed guess presented as if the company printed it.
#
# Also deliberately does NOT attempt per-director rows: names in this
# section come out individually letter-reversed on at least one real
# filing (Equity Group) in a way that doesn't reliably self-correct
# (foreign/Kenyan surnames aren't in any general English wordlist), so
# a per-director breakdown would risk showing a boardroom name garbled -
# worse than not showing it. The totals row's numbers are unaffected by
# that corruption either way (see _REMUNERATION_HEADING_RES below), so
# this is a purely lower-risk scope than the per-director table would be.
_REMUNERATION_HEADING_RES = [
    re.compile(p, re.IGNORECASE) for p in [
        r"directors?\W{0,3}remuneration\s+report",
        r"single\s+figure\s+remuneration",
    ]
]

# Some filings (confirmed on a real Equity Group filing) render this
# specific section's text with every word doubled-then-reversed by a
# pdfplumber-specific font/encoding quirk - "Directors' remuneration
# report" comes out as "’’ssrroottcceerriiDD ttrrooppeerr
# nnooiittaarreennuummeerr". pypdf reads the SAME page's text layer
# correctly (confirmed against that filing), so it's tried first here
# specifically for this section; pdfplumber is only a fallback for
# environments without pypdf installed, or pages pypdf can't read at all.
# (extract_pdf_document(), by contrast, has to use pdfplumber for the
# main statements because pypdf doesn't preserve table row structure -
# see that function's docstring - so this is a deliberate, narrow
# exception just for this section, not a wider reader swap.)
_TOTAL_ROW_LABEL_RE = re.compile(r"^\s*(?:grand\s+)?total\b", re.IGNORECASE)


def _normalize_currency_hint(raw: str) -> str:
    """'Kshs'/'Ksh'/'Shs' all mean KES - normalize to that. Previously
    did .replace('KSH','KES').replace('SHS','KES') on the upper-cased
    match, which left 'KSHS' (from 'Kshs') as 'KESS' - the first
    .replace only strips the 'KSH' prefix, and the trailing 'S' from
    the plural survives untouched. An explicit membership check avoids
    that partial-replace bug entirely."""
    up = raw.upper()
    if up in ('KSHS', 'KSH', 'SHS'):
        return 'KES'
    return up


def _looks_like_labeled_total_row(line: str) -> bool:
    """True if `line` starts with an explicit 'Total' / 'Grand Total'
    label followed by several numbers - e.g. 'GRAND TOTAL 39,165 54,510
    6,835 2,736 103,246' (confirmed on a real KCB filing). This is a
    STRONGER signal than the bare-numeric-row heuristic below - the
    filing is telling us directly which row is the total, so this is
    checked first and, when found, wins outright over any bare numeric
    row further down the page."""
    if not _TOTAL_ROW_LABEL_RE.search(line):
        return False
    numbers = [_parse_amount_token(t) for t in line.split()]
    return sum(1 for n in numbers if n is not None) >= 3


def _looks_like_all_numeric_row(line: str, min_numbers: int = 5) -> bool:
    """True if `line` is (almost) entirely numbers/dashes/currency
    punctuation - the shape of a table data row with no name attached,
    which on every real total row seen is exactly what's left once the
    row has no director name in it. min_numbers defaults to 5 (not just
    "more than one or two") specifically to avoid matching a table of
    contents' bare page numbers/ranges ("128", "129 - 132") as if they
    were a totals row - a real remuneration total row has one number per
    disclosed component (salary, fees, pension, bonus, ...) which is
    never fewer than about 5 columns on any real filing checked."""
    tokens = line.split()
    if not tokens:
        return False
    numeric_like = 0
    for t in tokens:
        stripped = t.strip(",-\u2013()")
        if stripped == '' or stripped == '-':
            numeric_like += 1
            continue
        if re.fullmatch(r"[\d,.\u2019']+", stripped):
            numeric_like += 1
    return numeric_like >= min_numbers and numeric_like == len(tokens)


def _parse_amount_token(tok: str):
    cleaned = re.sub(r"[^\d.]", "", tok)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_director_remuneration(pdf_bytes: bytes) -> dict | None:
    """Best-effort: the filing's OWN printed grand total for director/
    executive remuneration for its reporting period, from a "Directors'
    remuneration report" / "single figure remuneration" section.

    Returns {'total': float, 'currency_hint': str|None, 'page': int} or
    None if no such section, or no explicit total row within it, was
    found - callers should show that as "not available in this filing",
    never fall back to summing rows themselves (see module note above
    for why).
    """
    reader = None
    if pypdf is not None:
        try:
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        except Exception:
            reader = None

    page_texts = []
    if reader is not None:
        for i, page in enumerate(reader.pages):
            try:
                page_texts.append((i + 1, page.extract_text() or ""))
            except Exception:
                page_texts.append((i + 1, ""))
    else:
        # Fallback path - only reached if pypdf isn't installed at all.
        # Will NOT recover the reversed-text case described above, but
        # still works for filings (like NSE's) with no such corruption.
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for i, page in enumerate(pdf.pages):
                page_texts.append((i + 1, page.extract_text() or ""))

    heading_pages = []
    for pg, text in page_texts:
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            if _HEADING_HAS_DIGIT_RE.search(line):
                continue  # e.g. a same-line TOC entry with a trailing page range
            if not any(rx.search(line) for rx in _REMUNERATION_HEADING_RES):
                continue
            # Guard against a TOC layout where the heading and its page
            # range are on separate lines (confirmed on a real filing:
            # "Directors' remuneration report" \n " 125 - 127") - if the
            # next non-empty line is just a bare number/page-range, this
            # is a contents entry, not the section itself.
            next_line = next((l for l in lines[idx + 1:idx + 2]), "")
            if re.fullmatch(r"\s*\d+\s*(-\s*\d+\s*)?", next_line):
                continue
            heading_pages.append(pg)
            break
    heading_pages = sorted(set(heading_pages))
    if not heading_pages:
        return None

    # Group heading hits into clusters of genuinely adjacent pages (the
    # real section) rather than treating every hit as its own independent
    # +3-page search - a passing mention of "Directors' remuneration
    # report" on an unrelated page (confirmed on a real KCB filing: an
    # auditor's-report sentence a few pages after the real table) must
    # NOT extend the scan into pages that have nothing to do with the
    # section, which is exactly how an earlier version of this function
    # picked up an unrelated statement-of-changes-in-equity row as if it
    # were the remuneration total. A gap of more than 1 page between
    # consecutive heading hits starts a new cluster.
    clusters = []
    current = [heading_pages[0]]
    for pg in heading_pages[1:]:
        if pg - current[-1] <= 1:
            current.append(pg)
        else:
            clusters.append(current)
            current = [pg]
    clusters.append(current)

    text_by_page = dict(page_texts)
    best = None
    for cluster in clusters:
        # Scan the cluster's own pages plus one page past the end, to
        # cover a table that starts right at the bottom of the heading
        # page and finishes just past it - not a wide, drift-prone
        # forward window from every individual hit.
        scan_pages = list(range(cluster[0], min(cluster[-1] + 1, len(page_texts)) + 1))

        # First, count labeled-total rows across the WHOLE cluster (not
        # just one page at a time) - a split-table filing (confirmed on
        # both a real NSE and a real KCB filing) prints a Non-Executive
        # Directors total on one page and a separate Executive Directors
        # total on the very next page, so a same-page-only check misses
        # that split. More than one labeled total anywhere in this
        # cluster means picking any single one of them would silently
        # under-report the real combined figure - exactly the ambiguity
        # this function declines to resolve on its own (see module note
        # above) - so the whole cluster yields no total rather than a
        # partial one.
        cluster_labeled_rows = []
        for pg in scan_pages:
            text = text_by_page.get(pg, "")
            if not text:
                continue
            for line in (l.strip() for l in text.splitlines() if l.strip()):
                if _looks_like_labeled_total_row(line):
                    cluster_labeled_rows.append((pg, line))
        if len(cluster_labeled_rows) > 1:
            continue
        if len(cluster_labeled_rows) == 1:
            pg, line = cluster_labeled_rows[0]
            lines = [l.strip() for l in text_by_page.get(pg, "").splitlines() if l.strip()]
            numbers = [_parse_amount_token(t) for t in line.split()]
            numbers = [n for n in numbers if n is not None]
            total_value = max(numbers)
            currency_hint = None
            idx_in_lines = lines.index(line)
            for j in range(max(0, idx_in_lines - 12), idx_in_lines):
                m = re.search(r"(Shs|KES|Kshs|USD|Ksh)\W*(?:\d|0{2,3})", lines[j], re.IGNORECASE)
                if m:
                    currency_hint = _normalize_currency_hint(m.group(1))
                    break
            best = {'total': total_value, 'currency_hint': currency_hint, 'page': pg}
            break

        # No labeled total row anywhere in this cluster - fall back to
        # the bare-numeric-row heuristic (see _looks_like_all_numeric_row's
        # docstring) on each page in the cluster.
        for pg in scan_pages:
            text = text_by_page.get(pg, "")
            if not text:
                continue
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            for i in range(len(lines) - 1, -1, -1):
                if not _looks_like_all_numeric_row(lines[i]):
                    continue
                is_boundary = (i == len(lines) - 1) or not _looks_like_all_numeric_row(lines[i + 1])
                if not is_boundary:
                    continue
                # A subtotal (e.g. "all Non-Executive Directors" summed,
                # before the Executive Directors' own rows) has this exact
                # same shape - all-numeric, immediately followed by a
                # named row rather than another numeric row - so the
                # boundary check above alone can't tell a subtotal from
                # the real grand total (confirmed on a real NSE filing: a
                # NED subtotal is followed by two more named executive
                # rows, not narrative). Guard against that: if a
                # currency-unit row (e.g. "Kshs Kshs Kshs Kshs Kshs")
                # appears within the next few lines, the table is still
                # going - a real total row is always the LAST thing
                # before the table's closing narrative/footnote, with no
                # more currency-column marker after it.
                lookahead = lines[i + 1:i + 5]
                still_in_table = any(
                    re.fullmatch(r"(?:kshs|shs\W*000|kes)(?:\s*(?:kshs|shs\W*000|kes))*", l, re.IGNORECASE)
                    for l in lookahead
                )
                if still_in_table:
                    continue
                numbers = [_parse_amount_token(t) for t in lines[i].split()]
                numbers = [n for n in numbers if n is not None]
                if not numbers:
                    continue
                total_value = max(numbers)  # the Total column is always the largest figure in its own row
                currency_hint = None
                for j in range(max(0, i - 12), i):
                    m = re.search(r"(Shs|KES|Kshs|USD|Ksh)\W*\W*0{2,3}", lines[j], re.IGNORECASE)
                    if m:
                        currency_hint = _normalize_currency_hint(m.group(1))
                        break
                best = {'total': total_value, 'currency_hint': currency_hint, 'page': pg}
                break  # first (bottom-most) qualifying row on this page is the one we want
            if best:
                break
        if best:
            break
    return best


def parse_financials_pdf(pdf_bytes: bytes) -> dict:
    """Thin wrapper over extract_pdf_document() that returns just the
    parsed statements - kept for the __main__ smoke test below and any
    other caller that only needs statements, not page text. Callers that
    also need company/period detection (i.e. the upload route) should
    call extract_pdf_document() directly instead, so the PDF is only
    read once - see its docstring.
    """
    return {'statements': extract_pdf_document(pdf_bytes)['statements']}



# ---------- MARKET DATA / PRINCIPAL RISKS / MANAGEMENT GUIDANCE ----------
# Extraction for the Intelligence Report's Peer Comparison, Risk Analysis,
# and Outlook tabs. Each function returns None (or an empty list) when its
# section isn't found in a given filing - callers show that as "not
# disclosed in this filing", never fall back to a guessed figure.
#
# These target the layout of KCB's 2025 Integrated Report specifically
# (confirmed against the real filing during development). Report layout
# varies year to year and company to company - see extract_director_
# remuneration's heading-search pattern above for the general approach
# this should grow towards as more filing formats are added, once a
# standardized input format is settled on.

def _pypdf_or_pdfplumber_page_texts(pdf_bytes: bytes):
    """Shared page-text extraction, same fallback order as
    extract_director_remuneration above."""
    reader = None
    if pypdf is not None:
        try:
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        except Exception:
            reader = None
    if reader is not None:
        out = []
        for i, page in enumerate(reader.pages):
            try:
                out.append((i + 1, page.extract_text() or ""))
            except Exception:
                out.append((i + 1, ""))
        return out
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [(i + 1, p.extract_text() or "") for i, p in enumerate(pdf.pages)]


def extract_market_data(pdf_bytes: bytes) -> dict | None:
    """Own-share market data from the filing's Investor Information /
    'KCB Share Information' table (share price, market cap, shareholder
    count, free float, shareholding by category), plus dividend-per-share
    and total shareholder return pulled from the statutory 'Report of the
    Directors > Dividend' section and the Outlook/Recap narrative. Every
    field is exactly what the filing prints - nothing here is derived or
    estimated. Returns None if none of this was found."""
    page_texts = _pypdf_or_pdfplumber_page_texts(pdf_bytes)
    result = {}
    # No trailing \s* here - it would swallow the separator before an
    # optional second (prior-year) number, leaving nothing for the outer
    # "\s+" between the two numbers to match against.
    num = r'(-?[\d,]+\.?\d*)%?'

    label_map = {
        r'Number of issued shares': 'shares_issued',
        # pypdf sometimes renders "Total" with a stray space after the
        # "T" (kerning/ligature artifact) - tolerate an optional space
        r'T\s?otal number of authorized shares': 'shares_authorized',
        r'Number of shareholders': 'shareholder_count',
        r'Free float': 'free_float_pct',
        r'End of period share price.*?\(Ksh\.?\)': 'share_price',
        r'Market capitali[sz]ation.*?\(Ksh\.?\s*billion\)': 'market_cap',
        r'Local Institutional Investors': 'local_institutional_pct',
        r'Local Individual Investors': 'local_individual_pct',
        r'Foreign Investors': 'foreign_investor_pct',
    }
    for pg, text in page_texts:
        if 'KCB Share Information' not in text and 'Investor Information' not in text:
            continue
        for line in text.splitlines():
            for pat, field in label_map.items():
                m = re.search(pat + r'\s+' + num + r'(?:\s+' + num + r')?', line)
                if m:
                    val = m.group(1)
                    prior = m.group(2) if m.lastindex and m.lastindex >= 2 else None
                    try:
                        result[field] = float(val.replace(',', ''))
                        if field == 'share_price' and prior:
                            result['prior_share_price'] = float(prior.replace(',', ''))
                        if field == 'shareholder_count' and prior:
                            result['prior_shareholder_count'] = float(prior.replace(',', ''))
                        result['page'] = pg
                    except ValueError:
                        pass

    # Dividend components - statutory "Report of the Directors > Dividend"
    # section. Column-wrapped text means the phrase and its number can be
    # separated by unrelated interleaved sentences from the neighbouring
    # column, so these use a wide non-greedy gap tolerance rather than
    # requiring the phrase to be contiguous.
    for pg, text in page_texts:
        if 'report of the directors' not in text.lower() or 'dividend' not in text.lower():
            continue
        blob = ' '.join(text.splitlines())
        found_any = False
        m = re.search(r'final dividend.{0,250}?of\s+K[Ss]hs?\.?\s*([\d.]+)\s*per\s*(?:ordinary\s*)?share', blob, re.I | re.S)
        if m:
            result['final_dividend_per_share'] = float(m.group(1)); found_any = True
        m = re.search(r'interim\s+dividend.{0,250}?of\s+K[Ss]hs?\.?\s*([\d.]+)\s*per\s*(?:ordinary\s*)?share', blob, re.I | re.S)
        if m:
            result['interim_dividend_per_share'] = float(m.group(1)); found_any = True
        m = re.search(r'special dividend of\s+K[Ss]hs?\.?\s*([\d.]+)\s*per\s*(?:ordinary\s*)?share', blob, re.I)
        if m:
            result['special_dividend_per_share'] = float(m.group(1)); found_any = True
        m = re.search(r'total dividends?\s+for\s+the\s+year\s+to\s+K[Ss]hs?\.?\s*([\d.]+)\s*per\s*(?:ordinary\s*)?share', blob, re.I)
        if m:
            result['dividend_per_share'] = float(m.group(1)); found_any = True
        if found_any:
            result['page'] = pg
            break
    if 'dividend_per_share' not in result:
        parts = [result.get('final_dividend_per_share'), result.get('interim_dividend_per_share'),
                  result.get('special_dividend_per_share')]
        parts = [p for p in parts if p is not None]
        if parts:
            # explicitly a sum of the filing's own printed components, not
            # a modeled/estimated total - only used when the filing itself
            # doesn't also print one combined figure
            result['dividend_per_share'] = round(sum(parts), 2)

    # Total shareholder return - only stored if the filing states the
    # number directly (never computed from share price + dividend here,
    # to avoid quietly disagreeing with the filing's own stated figure
    # if their methodology differs, e.g. compounding or timing basis).
    for pg, text in page_texts:
        m = re.search(r'total shareholder returns?\s+of\s+([\d.]+)%', text, re.I)
        if m:
            result['total_shareholder_return'] = float(m.group(1))
            break
        m = re.search(r'([\d.]+)%\s*\n?Total shareholder returns?\s+in\s+\d{4}', text, re.I)
        if m:
            result['total_shareholder_return'] = float(m.group(1))
            break

    return result if result else None


_GUIDANCE_METRICS = [
    'Non funded income ratio', 'Cost-to-income ratio', 'NPL ratio', 'Cost of risk',
    'Cost of funds', 'Net interest margin', 'Asset yield', 'Loan growth',
    'Deposit growth', 'Return on equity',
]
_GUIDANCE_HEADER_RE = re.compile(r'Key performance indicator\s+(\d{4})\s+performance\s+(\d{4})\s+guidance')
_GUIDANCE_ROW_RE = re.compile(r'(-?[\d.]+)%\*?\s+(-?[\d.]+)%\s*-\s*(-?[\d.]+)%\s*:?\s*(.*)')


def extract_management_guidance(pdf_bytes: bytes) -> list:
    """Forward-looking KPI guidance from the filing's own Outlook section
    table ('Key performance indicator | <year> performance | <next year>
    guidance'). guidance_low/guidance_high are the filing's own printed
    range - never a modeled projection. Returns [] if no such table is
    found (e.g. an interim or a filing without one)."""
    page_texts = _pypdf_or_pdfplumber_page_texts(pdf_bytes)
    result = []
    for pg, text in page_texts:
        header_m = _GUIDANCE_HEADER_RE.search(text)
        if not header_m:
            continue
        guidance_year = header_m.group(2)
        for order, line in enumerate(text.splitlines()):
            for metric in _GUIDANCE_METRICS:
                if not line.startswith(metric):
                    continue
                rest = line[len(metric):].strip()
                m = _GUIDANCE_ROW_RE.match(rest)
                if m:
                    result.append({
                        'metric_name': metric,
                        'guidance_period_label': f'FY{guidance_year}',
                        'current_value': float(m.group(1)),
                        'guidance_low': float(m.group(2)),
                        'guidance_high': float(m.group(3)),
                        'commentary': m.group(4).strip(),
                        'order_index': order,
                        'page': pg,
                    })
        if result:
            break
    return result


_ROMAN_HEADING_RE = re.compile(r'^(X{0,3}(?:IX|IV|V?I{0,3}))\.\s+(.+)$')
_KNOWN_RISK_CATEGORIES = {
    'Credit Risk', 'Capital Adequacy', 'Technology and Cybersecurity', 'Data Protection',
    'Market Risk', 'Operational Risk', 'Fraud Risk', 'Compliance Risk',
    'AML/CFT/CPF Compliance', 'Climate Risk', 'Strategic Risk', 'Conduct Risk',
    'Reputational Risk',
}


def _reconstruct_columns(page, header_footer_top_cutoff: float = 60):
    """Multi-column report pages (the 'Management of Principal Risks'
    section prints 2-4 risk write-ups side by side) interleave text
    line-by-line under plain extract_text(), scrambling one risk
    category's sentences with its neighbour's. This clusters words by
    x-position into column bands, then reconstructs each column's own
    text in correct top-to-bottom reading order."""
    words = page.extract_words()
    body_words = [w for w in words if w['top'] > header_footer_top_cutoff]
    if not body_words:
        return []
    xs = sorted(w['x0'] for w in body_words)
    groups = []
    cur = [xs[0]]
    for x in xs[1:]:
        if x - cur[-1] > 15:
            groups.append(cur)
            cur = [x]
        else:
            cur.append(x)
    groups.append(cur)
    bounds = sorted(set(round(min(g)) for g in groups))
    bounds.append(page.width + 1)
    merged = [bounds[0]]
    for b in bounds[1:]:
        if b - merged[-1] < 100:
            continue
        merged.append(b)
    bounds = merged

    col_words = {}
    for w in body_words:
        for i in range(len(bounds) - 1):
            if bounds[i] <= w['x0'] < bounds[i + 1]:
                col_words.setdefault(i, []).append(w)
                break

    columns = []
    for i in sorted(col_words.keys()):
        ws = sorted(col_words[i], key=lambda w: (round(w['top'] / 3), w['x0']))
        lines = []
        cur_top = None
        cur_line = []
        for w in ws:
            if cur_top is None or abs(w['top'] - cur_top) < 3:
                cur_line.append(w)
                cur_top = w['top'] if cur_top is None else cur_top
            else:
                lines.append(' '.join(x['text'] for x in sorted(cur_line, key=lambda x: x['x0'])))
                cur_line = [w]
                cur_top = w['top']
        if cur_line:
            lines.append(' '.join(x['text'] for x in sorted(cur_line, key=lambda x: x['x0'])))
        columns.append('\n'.join(lines))
    return columns


def extract_principal_risks(pdf_bytes: bytes) -> list:
    """The filing's own named, qualitative principal-risk categories
    (Credit Risk, Technology and Cybersecurity, Compliance Risk, Climate
    Risk, etc.) from its 'Management of Principal Risks' section, each
    with the company's own description and 'Our Mitigations' text -
    never a numeric score, since filed reports don't publish one for
    these categories. Returns [] if this section isn't found (e.g. it
    only matches the FY2025-style roman-numeral, multi-column layout
    confirmed during development - a different layout, like a two-column
    'Principal Risk | Mitigation Measures' table, returns [] rather than
    guessing at a mismatched structure)."""
    # Locate the section via the fast pypdf-based text pass first (~5x
    # faster than pdfplumber's per-page extract_text at this page count -
    # confirmed during development on a 138-page filing). pdfplumber is
    # only opened afterwards, and only for the narrow page range actually
    # needed, since its slower word-position data is what column
    # reconstruction below requires.
    page_texts = _pypdf_or_pdfplumber_page_texts(pdf_bytes)
    section_pages = [pg for pg, text in page_texts if 'management of principal risks' in text.lower()]
    if not section_pages:
        return []
    start_pg = section_pages[0]
    total_pages = len(page_texts)

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # scan the heading page plus a bounded run of following pages -
        # stop once two consecutive pages contribute no recognized
        # category, rather than scanning the whole document
        scan_range = range(start_pg, min(start_pg + 8, total_pages + 1))

        raw_entries = []
        for pg in scan_range:
            page = pdf.pages[pg - 1]
            cols = _reconstruct_columns(page)
            for col_text in cols:
                lines = col_text.splitlines()
                current = None
                for line in lines:
                    m = _ROMAN_HEADING_RE.match(line.strip())
                    if m and len(m.group(1)) <= 4:
                        if current:
                            raw_entries.append(current)
                        current = {'roman': m.group(1), 'category': m.group(2).strip(), 'lines': [], 'page': pg}
                    elif current is not None:
                        current['lines'].append(line)
                if current:
                    raw_entries.append(current)

        results = []
        seen_categories = set()
        for entry in raw_entries:
            category = entry['category'].rstrip('.')
            # only keep entries matching a real risk category name - this
            # is what filters out the section's own process-explanation
            # sub-headings ("I. Risk Identification, Assessment, and
            # Management") which also happen to use roman numerals
            if category not in _KNOWN_RISK_CATEGORIES:
                continue
            if category in seen_categories:
                continue
            seen_categories.add(category)
            full_text = '\n'.join(entry['lines']).replace('Ksh\n', '').strip()
            if 'Our Mitigations:' in full_text:
                description, mitigation = full_text.split('Our Mitigations:', 1)
            elif 'Mitigations:' in full_text:
                description, mitigation = full_text.split('Mitigations:', 1)
            else:
                description, mitigation = full_text, ''
            results.append({
                'category': category,
                'order_index': len(results),
                'description': re.sub(r'\s+', ' ', description).strip(),
                'mitigation': re.sub(r'\s+', ' ', mitigation).strip(),
                'page': entry['page'],
            })
        # A real "Management of Principal Risks" section discloses many
        # categories together, never just one or two - a low count here
        # means this filing uses a different layout (e.g. a two-column
        # "Principal Risk | Mitigation Measures" table) that this
        # roman-numeral/4-column parser doesn't understand, not that the
        # filing genuinely only disclosed a couple of risks. Returning a
        # partial list in that case would look like an accurate, complete
        # count when it isn't - so return [] and let the caller show
        # "not extracted from this filing" instead of a misleadingly
        # short real-looking list.
        if len(results) < 5:
            return []
        return results

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


_DIRECTOR_REM_SUBHEADING_RE = re.compile(
    r'(?:^|\n)\s*(?:i|ii|iii|iv|v)\.\s*(non-executive directors|executive directors)'
    r'.{0,120}?for the year ended\s+\d{1,2}\s+\w+\s+(\d{4})',
    re.I | re.S,
)
_DIRECTOR_REM_NED_TOTAL_RE = re.compile(
    r'GRAND TOTAL\s*(?:\(\d+\))?\s+([\d,.\-]+)\s+([\d,.\-]+)\s+([\d,.\-]+)\s+([\d,.\-]+)\s+([\d,.\-]+)',
    re.I,
)
_DIRECTOR_REM_ED_ROW_RE = re.compile(
    r'^(Mr\.|Mrs\.|Ms\.|Dr\.)\s+([A-Za-z.\s]+?)\s+([\d,.\-]+(?:\s+[\d,.\-]+){5,6})$'
)


def _num_or_none(s: str):
    s = s.strip()
    return None if s == '-' else float(s.replace(',', ''))


def extract_director_remuneration_detail(pdf_bytes: bytes, target_period_label: str | None = None) -> list:
    """Per-director remuneration rows from the filing's own "Directors'
    Remuneration Report" - the Non-Executive Directors' fees table's own
    printed GRAND TOTAL row, and each Executive Director's own printed
    Total row. Every figure is exactly what the filing prints in Ksh
    '000 - this app never sums individual NED rows itself to invent a
    total (see extract_director_remuneration's docstring above for why),
    it only reads a total the filing already states.

    This table always prints the current year's figures AND the prior
    year's as a second, separately-headed sub-table on the same page
    (e.g. "iii. Executive Directors' Remuneration for the Year Ended 31
    December 2025" immediately followed by "iv. ... for the Year Ended
    31 December 2024"). Pass target_period_label (e.g. "FY2025") to keep
    only that year's own sub-table - the other year's figures belong to
    that other filing's own upload instead, and keeping both here would
    double-count/conflict once both years are uploaded separately.
    Returns [] if this section isn't found (e.g. an interim filing, or a
    layout this parser doesn't recognize)."""
    page_texts = _pypdf_or_pdfplumber_page_texts(pdf_bytes)
    section_pages = [pg for pg, text in page_texts
                      if 'remuneration report' in text.lower()
                      and ('non-executive' in text.lower() or 'executive director' in text.lower())]
    if not section_pages:
        return []
    start_pg, end_pg = min(section_pages), max(section_pages) + 1

    results = []
    order = 0
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pg in range(start_pg, min(end_pg + 1, len(pdf.pages) + 1)):
            page = pdf.pages[pg - 1]
            for half_text in _split_double_page_text(page):
                lower = half_text.lower()
                if 'non-executive director' not in lower and 'executive director' not in lower:
                    continue
                sub_matches = list(_DIRECTOR_REM_SUBHEADING_RE.finditer(half_text))
                if not sub_matches:
                    continue
                for idx, sm in enumerate(sub_matches):
                    chunk_start = sm.end()
                    chunk_end = sub_matches[idx + 1].start() if idx + 1 < len(sub_matches) else len(half_text)
                    chunk = half_text[chunk_start:chunk_end]
                    section_kind = sm.group(1).lower()
                    year = sm.group(2)
                    if target_period_label and f'FY{year}' != target_period_label:
                        continue

                    if 'non-executive' in section_kind:
                        m = _DIRECTOR_REM_NED_TOTAL_RE.search(chunk)
                        if m:
                            results.append({
                                'director_name': 'GRAND TOTAL - Non-Executive Directors',
                                'role': 'non_executive', 'is_grand_total': True,
                                'total': _num_or_none(m.group(5)), 'fiscal_year': year,
                                'components': {
                                    "directors_fees_ksh'000": _num_or_none(m.group(1)),
                                    "sitting_allowance_ksh'000": _num_or_none(m.group(2)),
                                    "other_allowances_ksh'000": _num_or_none(m.group(3)),
                                    "non_cash_benefit_ksh'000": _num_or_none(m.group(4)),
                                },
                                'order_index': order, 'page': pg,
                            })
                            order += 1
                    else:
                        for line in chunk.splitlines():
                            line = line.strip()
                            rm = _DIRECTOR_REM_ED_ROW_RE.match(line)
                            if not rm:
                                continue
                            title, name, nums_str = rm.groups()
                            nums = [n.strip() for n in nums_str.split()]
                            if len(nums) not in (6, 7):
                                continue
                            components = {
                                "salary_ksh'000": _num_or_none(nums[0]),
                                "bonus_cash_ksh'000": _num_or_none(nums[1]),
                                "bonus_deferred_ksh'000": _num_or_none(nums[2]),
                                "allowances_ksh'000": _num_or_none(nums[3]),
                            }
                            if len(nums) == 7:
                                components["gratuity_ksh'000"] = _num_or_none(nums[4])
                                components["non_cash_benefit_ksh'000"] = _num_or_none(nums[5])
                            else:
                                components["gratuity_ksh'000"] = _num_or_none(nums[4])
                            results.append({
                                'director_name': f'{title} {name.strip()}',
                                'role': 'executive', 'is_grand_total': False,
                                'total': _num_or_none(nums[-1]), 'fiscal_year': year,
                                'components': components,
                                'order_index': order, 'page': pg,
                            })
                            order += 1
    return results


# ---------- STANDARD CONDENSED FORMAT (v1) ----------
# A plain-text alternative to a raw PDF, purpose-built to sidestep every
# layout problem the PDF extractors above have to work around (double-wide
# pages, Group/Company column ordering, "Note" vs "Notes" wording, roman-
# numeral multi-column risk sections, etc.). See condensed_format/SPEC.md
# for the full grammar. One file per company per fiscal year;
# ===SECTION=== markers, "Label: value" or "Label: current, prior" lines,
# "#"-prefixed comments ignored.
#
# This is deliberately NOT reusing extract_pdf_document's heading-
# detection/column-splitting machinery - that machinery exists to cope
# with messy real-world PDF text; a condensed file has none of that
# messiness by construction, so a much simpler line-based parser is both
# sufficient and less likely to misfire on this format's own edge cases
# (e.g. a still-valid negative cash-flow number shouldn't trip the PDF
# parser's parenthesized-negative heuristics meant for OCR'd statement
# text).

_CONDENSED_SECTION_RE = re.compile(r'^===([A-Z_]+)===$')
_CONDENSED_FIELD_RE = re.compile(r'^([A-Za-z][A-Za-z]*)\s*:\s*(.*)$')
_CONDENSED_RISK_HEADING_RE = re.compile(r'^\[(.+?)\]$')
_CONDENSED_REM_HEADING_RE = re.compile(r'^\[(.+?)\]\s*Total\s*:\s*([\-\d.,]+)\s*(?:;\s*(.+))?$')
_CONDENSED_GUIDANCE_RE = re.compile(
    r'^([A-Za-z][A-Za-z0-9]*)\s*:\s*current\s*=\s*([\-\d.]+)\s*,\s*low\s*=\s*([\-\d.]+)\s*,\s*high\s*=\s*([\-\d.]+)'
    r'(?:\s*,\s*commentary\s*=\s*(.+))?\s*$'
)

# Field label (as written in the condensed file) -> this app's
# normalized_name, one dict per statement section. Kept separate from
# CANONICAL_LINE_ITEMS above (which matches free-form PDF text via
# regex) since condensed field labels are an exact, fixed vocabulary by
# design - a plain dict lookup is the right tool, not another regex table.
_CONDENSED_INCOME_FIELDS = {
    'Revenue': 'revenue', 'InterestIncome': 'interest_income', 'InterestExpense': 'interest_expense',
    'CostOfSales': 'cost_of_sales', 'GrossProfit': 'gross_profit', 'EmployeeCosts': 'employee_costs',
    'Depreciation': 'depreciation', 'Amortisation': 'amortisation', 'FinanceCosts': 'finance_costs',
    'OperatingProfit': 'operating_profit', 'ProfitBeforeTax': 'profit_before_tax',
    'TaxExpense': 'tax_expense', 'NetIncome': 'net_income', 'EPS': 'eps',
    'SharesOutstanding': 'shares_outstanding',
}
_CONDENSED_BALANCE_FIELDS = {
    'TotalAssets': 'total_assets', 'TotalLiabilities': 'total_liabilities', 'TotalEquity': 'total_equity',
    'CashAndEquivalents': 'cash_and_equivalents', 'Receivables': 'receivables', 'Payables': 'payables',
    'Inventory': 'inventory', 'CurrentAssets': 'current_assets', 'CurrentLiabilities': 'current_liabilities',
    'PPE': 'ppe', 'Borrowings': 'borrowings', 'ShareCapital': 'share_capital',
    'RetainedEarnings': 'retained_earnings',
}
_CONDENSED_CASHFLOW_FIELDS = {
    'OperatingCashFlow': 'operating_cash_flow', 'InvestingCashFlow': 'investing_cash_flow',
    'FinancingCashFlow': 'financing_cash_flow', 'CashEndOfPeriod': 'cash_end_of_period',
    'Capex': 'capex',
}
_CONDENSED_STATEMENT_SECTIONS = {
    'INCOME_STATEMENT': ('income_statement', _CONDENSED_INCOME_FIELDS),
    'BALANCE_SHEET': ('balance_sheet', _CONDENSED_BALANCE_FIELDS),
    'CASH_FLOW': ('cash_flow', _CONDENSED_CASHFLOW_FIELDS),
}
_CONDENSED_MARKET_DATA_FIELDS = {
    'SharePrice': ('share_price', 'prior_share_price'),
    'MarketCap': ('market_cap', None),
    'SharesIssued': ('shares_issued', None),
    'SharesAuthorized': ('shares_authorized', None),
    'ShareholderCount': ('shareholder_count', 'prior_shareholder_count'),
    'FreeFloatPct': ('free_float_pct', None),
    'DividendPerShare': ('dividend_per_share', None),
    'InterimDividendPerShare': ('interim_dividend_per_share', None),
    'FinalDividendPerShare': ('final_dividend_per_share', None),
    'SpecialDividendPerShare': ('special_dividend_per_share', None),
    'DividendYieldPct': ('dividend_yield', None),
    'TotalShareholderReturnPct': ('total_shareholder_return', None),
    'LocalInstitutionalPct': ('local_institutional_pct', None),
    'LocalIndividualPct': ('local_individual_pct', None),
    'ForeignInvestorPct': ('foreign_investor_pct', None),
}


def _condensed_parse_numbers(raw: str):
    """'349447.2, 310904.8' -> (349447.2, 310904.8); '4.25' -> (4.25, None).
    Returns (None, None) for an empty/unparseable value rather than
    raising, so one malformed line doesn't abort the whole file - the
    caller decides whether to skip that field."""
    raw = raw.strip()
    if not raw:
        return None, None
    parts = [p.strip() for p in raw.split(',')]
    try:
        first = float(parts[0].replace(',', '')) if parts[0] not in ('', '-') else None
    except ValueError:
        return None, None
    second = None
    if len(parts) > 1 and parts[1] not in ('', '-'):
        try:
            second = float(parts[1].replace(',', ''))
        except ValueError:
            second = None
    return first, second


def is_condensed_format(text: str) -> bool:
    """True if this looks like our own condensed format rather than raw
    PDF-extracted text - checked before falling back to the PDF pipeline,
    so a condensed .txt upload never gets mistakenly run through the
    much slower/fuzzier PDF statement parser."""
    return bool(re.search(r'^===COMPANY===\s*$', text, re.MULTILINE))


def parse_condensed_filing(text: str) -> dict:
    """Parse one standard condensed filing (see condensed_format/SPEC.md).
    Returns a dict with keys: company, period, statements (same shape
    parse_financials_text's 'statements' value has, so this can feed the
    exact same save path app.py already uses for PDF uploads),
    market_data, management_guidance, principal_risks,
    director_remuneration - each None/[] if that section was omitted,
    exactly mirroring how a real filing missing that disclosure behaves.
    Raises ValueError with a line number if the file is malformed enough
    that guessing would be worse than telling the person exactly what to
    fix."""
    lines = text.splitlines()
    sections: dict = {}
    current_section = None
    current_lines: list = []

    def flush():
        if current_section is not None:
            sections.setdefault(current_section, []).extend(current_lines)

    for line in lines:
        if line.strip().startswith('#') or not line.strip():
            continue
        m = _CONDENSED_SECTION_RE.match(line.strip())
        if m:
            flush()
            current_section = m.group(1)
            current_lines = []
            continue
        current_lines.append(line.rstrip())
    flush()

    if 'COMPANY' not in sections:
        raise ValueError('Missing required ===COMPANY=== section.')
    if 'PERIOD' not in sections:
        raise ValueError('Missing required ===PERIOD=== section.')

    def parse_fields(section_lines):
        out = {}
        for line in section_lines:
            m = _CONDENSED_FIELD_RE.match(line.strip())
            if m:
                out[m.group(1)] = m.group(2).strip()
        return out

    company_fields = parse_fields(sections.get('COMPANY', []))
    period_fields = parse_fields(sections.get('PERIOD', []))
    if not company_fields.get('Name'):
        raise ValueError('===COMPANY=== section is missing required field "Name".')
    if not period_fields.get('Label'):
        raise ValueError('===PERIOD=== section is missing required field "Label".')

    company = {
        'name': company_fields.get('Name'), 'ticker': company_fields.get('Ticker'),
        'sector': company_fields.get('Sector'), 'exchange': company_fields.get('Exchange'),
        'currency': company_fields.get('Currency'), 'unit': company_fields.get('Unit'),
    }
    period = {
        'label': period_fields.get('Label'), 'fiscal_year_end': period_fields.get('FiscalYearEnd'),
        'prior_label': period_fields.get('PriorLabel'),
    }

    statements = {}
    for section_name, (stmt_type, field_map) in _CONDENSED_STATEMENT_SECTIONS.items():
        if section_name not in sections:
            continue
        fields = parse_fields(sections[section_name])
        line_items = []
        for order, (condensed_label, value) in enumerate(fields.items()):
            normalized = field_map.get(condensed_label)
            if normalized is None:
                continue  # unrecognized field label in this section - skip rather than guess
            amount, prior_amount = _condensed_parse_numbers(value)
            if amount is None:
                continue
            line_items.append({
                'label': condensed_label, 'normalized_name': normalized,
                'amount': amount, 'prior_amount': prior_amount,
                'page': None, 'confidence': 1.0,  # condensed file's own stated figure - not a parser guess
                'order_index': order,
            })
        if line_items:
            statements[stmt_type] = {'line_items': line_items}

    market_data = None
    if 'MARKET_DATA' in sections:
        fields = parse_fields(sections['MARKET_DATA'])
        md = {}
        for condensed_label, (field_name, prior_field_name) in _CONDENSED_MARKET_DATA_FIELDS.items():
            if condensed_label not in fields:
                continue
            amount, prior = _condensed_parse_numbers(fields[condensed_label])
            if amount is not None:
                md[field_name] = amount
            if prior is not None and prior_field_name:
                md[prior_field_name] = prior
        market_data = md if md else None

    management_guidance = []
    if 'MANAGEMENT_GUIDANCE' in sections:
        guidance_period_label = None
        order = 0
        for line in sections['MANAGEMENT_GUIDANCE']:
            stripped = line.strip()
            gp = _CONDENSED_FIELD_RE.match(stripped)
            if gp and gp.group(1) == 'GuidancePeriod':
                guidance_period_label = gp.group(2).strip()
                continue
            m = _CONDENSED_GUIDANCE_RE.match(stripped)
            if not m:
                continue
            metric_name, current, low, high, commentary = m.groups()
            management_guidance.append({
                'metric_name': metric_name, 'guidance_period_label': guidance_period_label,
                'current_value': float(current), 'guidance_low': float(low), 'guidance_high': float(high),
                'commentary': commentary.strip() if commentary else None, 'order_index': order, 'page': None,
            })
            order += 1

    principal_risks = []
    if 'PRINCIPAL_RISKS' in sections:
        current = None
        for order, line in enumerate(sections['PRINCIPAL_RISKS']):
            m = _CONDENSED_RISK_HEADING_RE.match(line.strip())
            if m:
                if current:
                    principal_risks.append(current)
                current = {'category': m.group(1).strip(), 'description': '', 'mitigation': '',
                           'order_index': len(principal_risks), 'page': None}
                continue
            if current is None:
                continue
            fm = _CONDENSED_FIELD_RE.match(line.strip())
            if fm and fm.group(1) in ('Description', 'Mitigation'):
                current[fm.group(1).lower()] = fm.group(2).strip()
        if current:
            principal_risks.append(current)

    director_remuneration = []
    if 'DIRECTOR_REMUNERATION' in sections:
        for order, line in enumerate(sections['DIRECTOR_REMUNERATION']):
            m = _CONDENSED_REM_HEADING_RE.match(line.strip())
            if not m:
                continue
            name_and_role, total_str, components_str = m.groups()
            role = 'non_executive'
            director_name = name_and_role.strip()
            if ',' in name_and_role:
                name_part, role_part = name_and_role.rsplit(',', 1)
                director_name = name_part.strip()
                role = 'executive' if 'executive' in role_part.lower() and 'non' not in role_part.lower() else 'non_executive'
            try:
                total = float(total_str.strip().replace(',', ''))
            except ValueError:
                continue
            # Optional breakdown after a ";" on the same line - e.g.
            # "Fees=141157, Salaries=259203, Bonuses=77033, Non-cash
            # benefits=5853" - so a condensed file can still carry the
            # filing's own component breakdown (as the real PDF path's
            # extract_director_remuneration_detail does) instead of
            # collapsing straight to just the total. Optional and
            # order-preserving (a plain dict, insertion order matches
            # the file) - a file with no ";" breakdown still parses
            # exactly as before, so this is fully backward compatible.
            components = None
            if components_str:
                components = {}
                for part in components_str.split(','):
                    if '=' not in part:
                        continue
                    key, val = part.split('=', 1)
                    key = key.strip()
                    try:
                        components[key] = float(val.strip().replace(',', ''))
                    except ValueError:
                        continue
                if not components:
                    components = None
            director_remuneration.append({
                'director_name': director_name, 'role': role,
                'is_grand_total': 'GRAND TOTAL' in director_name.upper(),
                'total': total, 'components': components, 'order_index': order, 'page': None,
            })

    return {
        'company': company, 'period': period, 'statements': statements,
        'market_data': market_data, 'management_guidance': management_guidance,
        'principal_risks': principal_risks, 'director_remuneration': director_remuneration,
    }