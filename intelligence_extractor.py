"""
Turns a raw filing PDF into the same dict shape survey_data_parse.py's
parse_survey_data() produces - {SurveyCompanyData column: value, ...}
plus 'field_sources' (column -> "page N: <sentence>") and a parallel
'field_confidence' (column -> float) - so the save route in app.py can
treat a PDF-extracted field exactly like a hand-typed Survey Data file
field: same model, same upsert code, same "missing means None, never a
guess" discipline.

Pipeline: document_chunk.chunk_pdf() -> remuneration_ontology.rank_chunks()
picks candidate chunks per field -> this module's per-field extractors
pull an actual value out of the best candidate's text/table, together
with the sentence it came from and a confidence score. Nothing here
ever fabricates a number: if no candidate chunk contains something
that parses as this field's expected type, the field is left out of
the result dict entirely, same as an undisclosed field in a hand-typed
Survey Data file.

Confidence bands (mirrors what a person manually reviewing this file's
SOURCE section already does for every figure they type in):
  >= 0.75  "high"    - a section-hint heading match AND a clean single
                        number/count found near the matched phrase
  0.45-0.75 "medium"  - either the heading match OR the number match is
                        weak (e.g. found only in body text, or more
                        than one plausible number nearby)
  <  0.45  not returned at all - too uncertain to surface as a value;
                        this is BELOW the parser's own return threshold,
                        so these never appear in the result dict at all,
                        not even as a low-confidence guess a UI might
                        render then hide
"""

import re
from typing import Optional
from document_chunk import chunk_pdf, DocumentChunk
from remuneration_ontology import ONTOLOGY, rank_chunks, score_chunk

# Country -> the ONE local currency this app is willing to infer for
# that country when nothing in the document itself states a currency.
# Deliberately small and explicit rather than a general country/
# currency lookup table - this app only has good reason to trust an
# inference for a country whose currency is used unambiguously (a
# country with multiple commonly-reported currencies, or where a
# company might consolidate in a different currency than its home
# country's, should NOT be added here without the same care).
_COUNTRY_DEFAULT_CURRENCY = {
    'kenya': 'KES',
}

# Currency markers this module can recognize directly IN THE FILING'S
# OWN TEXT - checked before any company-country inference is even
# considered. A filing that states its own currency always wins over
# an inference, including when that stated currency differs from what
# the company's country would suggest (e.g. a Kenyan company reporting
# a USD-denominated subsidiary note).
_CURRENCY_MARKERS = {
    'KES': [r'kenya\s+shillings?', r'\bkshs?\.?\b', r'\bkes\b', r"\bshs['’]?\b"],
    'USD': [r'us\s+dollars?', r'\busd\b', r'\$'],
    'GBP': [r'pound\s+sterling', r'\bgbp\b', r'£'],
    'EUR': [r'\beur\b', r'€'],
    'TZS': [r'tanzania\s+shillings?', r'\btzs\b'],
    'UGX': [r'uganda\s+shillings?', r'\bugx\b'],
    'ZAR': [r'south\s+african\s+rand', r'\bzar\b'],
    'NGN': [r'nigerian\s+naira', r'\bngn\b'],
}


def detect_document_currency(chunks, company_country: str = None):
    """Returns (currency_code, confidence, source_note) for the whole
    document - checked ONCE up front and reused for every amount field,
    rather than re-detected per chunk, since a filing states its
    reporting currency in one place (usually the accounting policies
    note or a "figures are stated in..." line) and that applies
    throughout, not per-paragraph.

    Priority order:
      1. An explicit currency marker found in the document's own text
         (see _CURRENCY_MARKERS) - highest confidence, this IS what the
         filing states, not an inference.
      2. If nothing explicit is found anywhere, AND the company's own
         country (from Company.country) maps to a currency this app is
         willing to infer (_COUNTRY_DEFAULT_CURRENCY) - a low-confidence
         fallback, clearly labeled as inferred rather than stated. Never
         silently promoted to the same confidence as an explicit marker.
      3. Neither - returns (None, 0.0, None); every amount field is then
         left without a currency-scale conversion rather than guessed.
    """
    marker_counts = {code: 0 for code in _CURRENCY_MARKERS}
    for chunk in chunks:
        text_lower = (chunk.text or '').lower()
        for code, patterns in _CURRENCY_MARKERS.items():
            for pat in patterns:
                marker_counts[code] += len(re.findall(pat, text_lower, re.IGNORECASE))

    found = {code: n for code, n in marker_counts.items() if n > 0}
    if found:
        # Most frequently-cited currency wins - a filing mentioning USD
        # once in a footnote about a foreign subsidiary shouldn't
        # override KES appearing throughout the main statements.
        best_code = max(found, key=found.get)
        # Confidence scales with how dominant the winning currency's
        # mentions are relative to everything else found, capped high
        # but never absolute certainty (multiple currencies genuinely
        # can appear in one filing, e.g. group vs company figures).
        total = sum(found.values())
        dominance = found[best_code] / total
        confidence = min(0.95, 0.6 + 0.35 * dominance)
        return best_code, round(confidence, 2), (
            f"document states amounts in {best_code} "
            f"({found[best_code]} currency-marker mention(s) found)"
        )

    if company_country:
        fallback = _COUNTRY_DEFAULT_CURRENCY.get(company_country.strip().lower())
        if fallback:
            return fallback, 0.5, (
                f"INFERRED (not stated in the document) - defaulted to {fallback} "
                f"because the company's own country on file is {company_country!r}; "
                f"no explicit currency marker was found anywhere in the pages scanned"
            )

    return None, 0.0, None


# Real filings can genuinely state DIFFERENT scales in different
# sections of the same document (confirmed: KCB Group Plc's FY2025
# report states headline turnover/profit "in Ksh billion" but its
# director remuneration tables use "Ksh '000'" / thousands headers) -
# so unit detection is intentionally scope-aware: it can be asked to
# only look at chunks whose heading or text plausibly relates to
# director/remuneration content, rather than always scanning the whole
# document and picking whichever scale marker is simply most frequent
# (which would wrongly let a document-wide "billion" mention win over
# a table-local "'000'" header, or vice versa).
_UNIT_MARKERS = {
    'thousands': [r"[’']000[’']?", r'\bin\s+thousands\b', r"shs\s*['’]000", r"\(\s*(?:kshs?|kes|ksh)?\s*['’]?000['’]?\s*\)"],
    'millions': [r'\bin\s+millions?\b', r"\(\s*(?:kshs?|kes|ksh)?\s*m(?:illion)?s?\s*\)", r"amounts?\s+(?:are\s+)?(?:stated|expressed|shown)\s+in\s+millions?"],
    'billions': [r'\bin\s+billions?\b', r"\(\s*(?:kshs?|kes|ksh)?\s*(?:bn|billions?)\s*\)", r"amounts?\s+(?:are\s+)?(?:stated|expressed|shown)\s+in\s+billions?"],
}
# Deliberately NOT matching a bare "\bmillion\b"/"\bbillion\b" anywhere
# in free text - confirmed on a real filing (KCB Group Plc's 2025
# Integrated Report) that "million" alone appears hundreds of times in
# ordinary prose unrelated to a unit declaration (customer counts,
# various one-off figures), completely swamping the real scale signal;
# only a genuine unit-DECLARATION phrase ("in millions", "(Ksh
# billion)", "amounts are stated in...") counts as a marker.
_REMUNERATION_SCOPE_RE = re.compile(
    r'remuneration|director.?s?\s+fees|director.?s?\s+emolu|non-executive\s+director',
    re.IGNORECASE,
)


def detect_document_unit(chunks, scope: str = 'document'):
    """Returns (unit, confidence, source_note) the same shape as
    detect_document_currency(). scope='document' scans every chunk
    (for the company's headline `unit` - turnover/net_profit/
    market_cap, which are usually stated once near the front of a
    filing); scope='remuneration' restricts the scan to chunks whose
    heading or own text plausibly relates to director/remuneration
    content (for `director_figures_unit`), so a document-wide
    "billion" headline doesn't outvote a table-local "'000'" header
    that applies specifically to the director-pay figures, or vice
    versa - see the real KCB Group Plc case in the module note above.
    Returns (None, 0.0, None) when nothing in the scanned scope has a
    clear unit marker - the caller should then leave the field NULL
    (falling back to the company's headline `unit`) rather than guess.
    """
    marker_counts = {u: 0 for u in _UNIT_MARKERS}
    scanned_any = False
    for chunk in chunks:
        if scope == 'remuneration':
            heading = chunk.heading or ''
            if not (_REMUNERATION_SCOPE_RE.search(heading) or _REMUNERATION_SCOPE_RE.search(chunk.text[:200])):
                continue
        scanned_any = True
        text_lower = (chunk.text or '').lower()
        for unit, patterns in _UNIT_MARKERS.items():
            for pat in patterns:
                marker_counts[unit] += len(re.findall(pat, text_lower, re.IGNORECASE))

    if not scanned_any:
        return None, 0.0, None

    found = {u: n for u, n in marker_counts.items() if n > 0}
    if not found:
        return None, 0.0, None

    best_unit = max(found, key=found.get)
    total = sum(found.values())
    dominance = found[best_unit] / total
    confidence = min(0.95, 0.55 + 0.35 * dominance)
    scope_label = 'the director/remuneration section' if scope == 'remuneration' else 'the document'
    return best_unit, round(confidence, 2), (
        f"{scope_label} states figures in {best_unit} "
        f"({found[best_unit]} marker mention(s) found)"
    )


# Same INT_FIELDS/FLOAT_FIELDS split as survey_data_parse.py, so a
# value extracted here is coerced the same way a hand-typed file's
# value would be before it ever reaches SurveyCompanyData.
INT_FIELDS = {
    'board_size', 'board_meetings_per_year', 'committees_per_board',
    'committee_meetings_per_year', 'directors_female', 'directors_male',
    'executive_directors_count', 'non_executive_directors_count',
    'independent_neds_count', 'non_independent_neds_count',
    'neds_kenyan_count', 'neds_non_kenyan_count',
}

# A currency/plain number token: "8,500,000", "8.5", "45,757.2" -
# optionally preceded by a currency-ish word/symbol the caller strips
# before parsing. Deliberately does NOT match a bare year-like 4-digit
# token near a date (see _extract_number's exclusion list) so "31
# March 2025" doesn't get mistaken for a retainer figure.
_NUMBER_RE = re.compile(r'(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)')

# Simple integer-count phrasing: "is 11 Directors", "comprises 10
# non-executive", "the board comprises of 9 members" - a number
# followed WITHIN A FEW WORDS by a role word. Kept as a single tight
# pattern (rather than a separate "is/are/comprises + bare number,
# check role word in a wide window afterward" fallback, which this
# module used to also have) because that separate fallback was
# responsible for two different false positives before it was retired:
# once matching "is 30%" as a headcount, and once matching "is 11" and
# then finding an unrelated LATER number's role word ("1 Executive
# Director") inside its wide look-around window and wrongly treating
# that as confirmation for the "11" it actually matched. Requiring the
# role word within a handful of words of the SAME number closes both
# holes at once, at the cost of not catching a role word that's much
# further away in the sentence - a real but rare miss, and a much
# safer default than the false positives it replaces.
_COUNT_NEAR_KEYWORD_RE = re.compile(
    r'(\d{1,3})\s*(?:of\s+)?(?:non-executive\s+)?(?:\w+\s+){0,2}?(directors?|members?|non-executive|executive)',
    re.IGNORECASE
)


# Per-field requirement: the matched sentence/window must contain at
# least one of these terms, or the match is discarded outright - this
# is what stops "11 Directors" (the board's total constitutional size)
# from being mistaken for non_executive_directors_count just because
# both happen to be phrased as "<number> director(s)". Board_size is
# deliberately absent here - it's the one count field that SHOULD
# match the bare "N Directors" phrasing.
_FIELD_REQUIRED_TERMS = {
    'executive_directors_count': ['executive director'],
    'non_executive_directors_count': ['non-executive director', 'non executive director', 'ned'],
    'independent_neds_count': ['independent'],
    'non_independent_neds_count': ['non-independent', 'non independent'],
    'directors_female': ['female', 'women', 'woman'],
    'directors_male': ['male', 'men'],
    'neds_kenyan_count': ['kenyan'],
    'neds_non_kenyan_count': ['non-kenyan', 'foreign', 'other nationalit'],
    'board_meetings_per_year': ['meeting'],
    'committees_per_board': ['committee'],
    'committee_meetings_per_year': ['committee', 'meeting'],
}

# A required term can be satisfied by a substring of the OPPOSITE
# concept ("executive director" is a plain substring of "non-executive
# director") even with a leading \b, since a hyphen is a non-word
# character and creates a valid word boundary right before "executive"
# either way. Fields listed here additionally reject a match whose
# text contains any of these disqualifying phrases, even though the
# required term above textually matched.
_FIELD_EXCLUDED_TERMS = {
    'executive_directors_count': ['non-executive', 'non executive'],
    'directors_male': ['female'],
}


def _looks_like_year_or_date(text: str, match_start: int, match_end: int) -> bool:
    """True if the matched number sits inside what looks like a date
    ('31 March 2025', 'FY2025') rather than a standalone figure -
    checked so a year never gets mistaken for a count or amount."""
    window = text[max(0, match_start - 15):min(len(text), match_end + 15)]
    return bool(re.search(r'(january|february|march|april|may|june|july|august|'
                           r'september|october|november|december|FY\d)', window, re.IGNORECASE))


# A numbered board-roster caption, common in annual reports as a photo
# spread ("1. Jane Doe 2. John Smith ... Independent Non-Executive
# Director"), states composition through WHO is listed and how each
# person is tagged, not through a stated "N Directors" sentence -
# _extract_count's phrase patterns above find nothing on a page like
# this. _extract_board_roster_counts is a second, independent strategy
# that counts entries in exactly this shape instead.
_NUMBERED_NAME_RE = re.compile(r'(\d{1,2})\.\s*[A-Z][^\d.]*?(?=\d{1,2}\.[A-Z]|\s*$|\n)')


def _extract_board_roster_counts(chunk: DocumentChunk) -> Optional[dict]:
    """Looks for a numbered-name-list board roster in this chunk (see
    _NUMBERED_NAME_RE) and, if found, returns counts derived from
    role-tag KEYWORDS that appear anywhere in the same chunk's text -
    board_size (total numbered entries) and, IF the corresponding role
    keyword appears at least once anywhere in the chunk, a same-count
    signal for executive_directors_count/non_executive_directors_count.
    This is deliberately coarse: it doesn't try to associate a specific
    role tag with a specific numbered name (the two are often not
    adjacent in reading order once a caption's layout is flattened to
    plain text by PDF extraction) - it only produces board_size
    directly, plus a role-based split ONLY when the executive/
    non-executive tag counts on the page are unambiguous relative to
    the total (see the caller in extract_survey_schema, which discards
    a split that doesn't sum to the total exactly). Returns None if no
    numbered-name pattern is found at all, so a normal prose page never
    goes through this path."""
    names = list(_NUMBERED_NAME_RE.finditer(chunk.text))
    if len(names) < 3:   # a handful of numbered names elsewhere (a footnote list, a references list) isn't a board roster
        return None

    # Reject a COMMITTEE membership roster - "Audit Committee",
    # "Members as at 31 March 2025 / 1. Winnie Ouko (Chairperson)..."
    # has the exact same numbered-name-with-role-tag shape as a full
    # board roster, but is a SUBSET of the board (2-6 people), not the
    # board itself. Checked against the chunk's own heading AND the
    # text immediately before the first numbered name, since a
    # committee roster is reliably introduced by "Committee" or
    # "Members" wording in one of those two places, unlike a genuine
    # board-wide roster page.
    committee_context = ((chunk.heading or '') + ' ' + chunk.text[:names[0].start()]).lower()
    if re.search(r'committee|members? as at', committee_context):
        return None
    numbers = sorted(set(int(m.group(1)) for m in names))
    # A genuine roster is numbered 1..N with no big gaps - guards
    # against accidentally matching an unrelated numbered list (e.g.
    # footnote markers 1, 4, 9 scattered through a paragraph).
    if numbers[0] != 1 or numbers[-1] - numbers[0] + 1 > len(numbers) + 2:
        return None
    board_size = numbers[-1]
    if board_size < 3 or board_size > 25:   # sanity bound for a board specifically
        return None

    # The decisive check: a genuine board roster caption has an actual
    # director-role title (Director/Chairman/Chairperson/CEO/Company
    # Secretary/etc.) attached to most of its numbered entries -
    # "1. Ahmed Mohamud ... Independent Non-Executive Director". A
    # numbered PROSE list ("1. Held investor briefings...", "1. The
    # Companies Act...") has capitalized words after each number
    # (sentence starts, Act names) but no director-role vocabulary
    # anywhere near it. Without this check, board_size fired on both
    # a numbered policy-clause list and a numbered bullet list of
    # unrelated activities - this is what tells a name list apart
    # from any other numbered list, which the numbering pattern alone
    # cannot do.
    role_title_re = re.compile(
        r'director|chairman|chairperson|chief executive|company secretary|ceo\b',
        re.IGNORECASE,
    )
    role_hits = sum(
        1 for m in names
        if role_title_re.search(chunk.text[m.end():m.end() + 150])
    )
    if role_hits < max(2, len(names) * 0.5):
        return None   # not enough of the numbered entries carry an actual director-role title nearby - not a board roster

    # A genuine name is short - "Ahmed Mohamud" or "FCS Dr. Joseph
    # Kinyua, EGH", not a run-on sentence. This page's own numbered
    # list ("1. Joseph Kinyua (Chairman) However, as part of the
    # transition to the current framework, some non-executive
    # directors appointed...") passed the role-title check above only
    # because "Chairman"/"Director" happen to appear in the POLICY
    # PROSE that follows each numbered name on this particular page,
    # not because the number is genuinely tagged with a role the way
    # a caption tags each numbered photo. Capping the matched "name"
    # text length catches this: a real name-only entry is well under
    # 60 characters; a name run into a full sentence of policy prose
    # is not.
    if any(len(m.group(0)) > 60 for m in names):
        return None

    text_lower = chunk.text.lower()
    exec_count = len(re.findall(r'\bexecutive director\b(?!\s*of)', text_lower)) - \
        len(re.findall(r'non-?executive director', text_lower))
    non_exec_count = len(re.findall(r'non-?executive director', text_lower))
    result = {'board_size': board_size}
    # Only offer the exec/non-exec split if it EXACTLY sums to the
    # roster total found above - a mismatch means this page's role
    # tags don't cleanly correspond 1:1 with the numbered names (e.g.
    # a tag repeated in a legend/caption), and guessing which of the
    # two counts to trust would be exactly the kind of silent error
    # this pipeline is built to avoid.
    if exec_count >= 0 and exec_count + non_exec_count == board_size:
        result['executive_directors_count'] = exec_count
        result['non_executive_directors_count'] = non_exec_count
    return result


def _extract_count(chunk: DocumentChunk, canonical_field: str):
    """For an integer-count field (board_size, directors_female, ...):
    look for a "<number> <role word>" pattern (see
    _COUNT_NEAR_KEYWORD_RE) in the chunk text, and check THIS FIELD's
    own required distinguishing term (see _FIELD_REQUIRED_TERMS)
    against the MATCHED TEXT ITSELF, not a wider surrounding window -
    that is deliberate and important: a wide-window check can find a
    different number's role word elsewhere in the same sentence and
    wrongly treat it as confirmation for the number actually matched
    (this happened in practice - "...is 11 Directors, and 1 Executive
    Director..." let "11" pass the check for
    executive_directors_count purely because "Executive Director"
    happened to describe the unrelated "1" later in the sentence).
    Requiring the role word to be part of the SAME match closes that
    hole. Returns (value, source_sentence) or (None, None) if nothing
    matches cleanly. Deliberately narrow - this is meant for a
    handful of well-known count phrasings, not a general "grab any
    number" fallback, since a wrong count silently saved is worse than
    a field left blank."""
    text = chunk.text
    required_terms = _FIELD_REQUIRED_TERMS.get(canonical_field)
    for m in _COUNT_NEAR_KEYWORD_RE.finditer(text):
        if _looks_like_year_or_date(text, m.start(), m.end()):
            continue
        value = int(m.group(1))
        if value == 0 or value > 200:   # sanity bound - a board isn't 0 or 200+ people
            continue

        match_text = m.group(0).lower()
        if required_terms and not any(
            re.search(r'\b' + re.escape(term), match_text) for term in required_terms
        ):
            continue   # this number's own matched role word doesn't satisfy what THIS field needs - not a match

        excluded_terms = _FIELD_EXCLUDED_TERMS.get(canonical_field)
        if excluded_terms and any(term in match_text for term in excluded_terms):
            continue   # the required term only matched as a substring of a disqualifying opposite phrase

        start = text.rfind('\n', 0, m.start()) + 1
        end = text.find('\n', m.end())
        end = end if end != -1 else len(text)
        sentence = text[start:end].strip()
        return value, sentence
    return None, None


# When a filing repeats the same label under more than one internal
# sub-heading (e.g. Note 32(e)'s "As executives304" for EGH PLC's own
# directors AND a separate "As executives1,397" for subsidiary
# directors), the RIGHT figure for a company-level survey field is the
# one under the PARENT/holding-company scope, not a subsidiary. These
# phrases mark that a preceding sub-header refers to the parent
# company itself rather than a subsidiary or the wider Group - used to
# disambiguate which of several same-label matches to keep, never to
# introduce a number that wasn't already an unambiguous label match.
_PARENT_SCOPE_HINTS = [
    'plc', 'holding company', 'the company', 'parent company', 'directors of egh',
]
_SUBSIDIARY_SCOPE_HINTS = [
    'subsidiar', 'group entities', 'other group companies',
]


def _nearest_preceding_heading_line(text: str, position: int) -> str:
    """Returns the nearest line before `position` that looks like a
    sub-header (short, ends without a number, e.g. 'Directors of EGH
    PLC' or 'Directors of subsidiaries who are not directors of EGH
    PLC:') - used only to decide which of several same-label matches
    belongs to the parent company. Looks back up to 5 lines; returns
    '' if nothing heading-like is found in that span."""
    before = text[:position]
    lines = before.split('\n')
    for line in reversed(lines[-6:]):
        line = line.strip()
        if line and len(line) < 100 and not re.search(r'\d{3,}', line):
            return line.lower()
    return ''


def _extract_amount(chunk: DocumentChunk, canonical_field: str, document_currency_known: bool):
    """For a currency-amount field (chairperson_annual_retainer, etc.):
    look for a clean amount in the chunk text, and return
    (value, source_sentence) or (None, None). Two matching strategies,
    tried in order:

    1. A number immediately preceded by an explicit currency marker in
       THIS chunk (Kshs, KES, Shs, $, etc.) - the strongest signal,
       used regardless of whether the document-wide currency was
       already established, since a marker right next to a specific
       number can override the document default (e.g. a USD-
       denominated subsidiary note inside an otherwise-KES filing).
       Requires exactly one such marked amount in the whole chunk -
       with a currency symbol right there, a second one elsewhere in
       the chunk is genuinely ambiguous (no per-field label to
       disambiguate by), so this strategy still refuses to pick
       between two marked candidates.
    2. If no chunk-local marker is found but detect_document_currency()
       already established a currency for the WHOLE document (passed
       in as document_currency_known), a LABELED bare number is
       accepted - specifically, a number immediately glued to a label
       word/phrase (the shape PDF table extraction produces when
       columns collapse: "As executives304", "Fees for non-executive
       directors77") where that label matches THIS field's own
       ontology synonyms. This is what makes a figure like "Fees for
       non-executive directors77" extractable as
       other_ned_annual_retainer specifically, even in a chunk that
       ALSO contains "As executives304" (a different field's figure) -
       the two are disambiguated by their own attached labels, not by
       "there's only one number in the whole chunk," which would be
       far too easily defeated by any table with more than one row.
    """
    text = chunk.text
    text_lower = text.lower()
    marked_amount_re = re.compile(
        r'(?:kshs?\.?|kes|shs\.?|\$|usd)\s*([\d,]+(?:\.\d+)?)\s*(million|mn|m\b|billion|bn)?',
        re.IGNORECASE,
    )
    matches = list(marked_amount_re.finditer(text))
    if len(matches) == 1:
        # Even the SOLE marked amount in a chunk can genuinely be
        # about something else entirely if the chunk is long enough to
        # contain more than one topic (confirmed on a real filing:
        # KCB Group Plc's "Related party transactions" note correctly
        # scored highly for ceo_monthly_salary's ontology entry
        # because it legitimately contains a "key management
        # personnel compensation" table - but that same chunk/note ALSO
        # mentions an unrelated "Ksh 3.9Bn" related-party BORROWING
        # figure a few sentences away, which was the chunk's only
        # currency-marked amount and got taken as the salary value).
        #
        # Fix: require that at least one of this field's own ontology
        # synonym/section-hint terms appears within a short window of
        # characters around the amount - UNLESS the chunk's own HEADING
        # already matches one of this field's section_hints, in which
        # case the whole chunk is trusted (a heading match is the
        # strongest signal this code has - see score_chunk's own 0.8
        # weight for a heading hit vs 0.15-0.5 for a body-text one -
        # and an amount can legitimately sit in a section-headed chunk
        # without repeating the section's own title words right next
        # to it). This still catches the real bug case: KCB's actual
        # winning chunk's heading was "39. Related party transactions
        # (continued)" - a generic note title that is NOT itself one of
        # ceo_monthly_salary's section_hints (which are "directors'
        # emoluments" / "key management compensation") - so the
        # heading-trust exemption correctly does NOT apply there, and
        # the body-text proximity check (correctly) rejects the loan
        # figure.
        entry_for_proximity = ONTOLOGY.get(canonical_field, {})
        section_hints = entry_for_proximity.get('section_hints', [])
        heading_lower = (chunk.heading or '').lower()
        heading_is_trusted = any(hint in heading_lower for hint in section_hints)
        if not heading_is_trusted:
            proximity_terms = entry_for_proximity.get('synonyms', []) + section_hints
            if proximity_terms:
                window_start = max(0, matches[0].start() - 200)
                window_end = min(len(text), matches[0].end() + 200)
                window_lower = text_lower[window_start:window_end]
                if not any(term in window_lower for term in proximity_terms):
                    return None, None   # neither the heading nor nearby body text ties this amount to the field - likely a different figure that happens to share the chunk
        return _finish_amount_match(text, matches[0])
    if len(matches) > 1:
        return None, None   # multiple marked amounts in one chunk - ambiguous, don't guess

    if not document_currency_known:
        return None, None   # no chunk-local marker AND no document-wide currency to fall back on

    entry = ONTOLOGY.get(canonical_field, {})
    label_terms = entry.get('synonyms', [])
    if not label_terms:
        return None, None

    # A label immediately followed by digits, e.g. "executives304" -
    # the label itself can be multiple words with spaces (PDF text
    # extraction keeps internal spaces even when it drops the space
    # right before the number), so this searches for each known label
    # phrase followed directly (no separating space) by 2+ digits.
    candidates = []
    for label in label_terms:
        for m in re.finditer(re.escape(label) + r'(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\b', text_lower):
            if len(m.group(1).replace(',', '').replace('.', '')) < 2:
                continue   # a single stray digit glued to a label (e.g. a footnote marker) isn't a real amount
            candidates.append(m)
    if len(candidates) != 1:
        # More than one match for this field's label - before giving
        # up entirely, check whether exactly one of them sits under a
        # PARENT-company sub-heading while the others sit under a
        # SUBSIDIARY one (see _PARENT_SCOPE_HINTS/_SUBSIDIARY_SCOPE_HINTS).
        # A company-level survey field should reflect the parent
        # company's own directors, not a subsidiary's - so if that
        # split is clean, the parent-scoped match is used; if it
        # isn't (e.g. two matches both look parent-scoped, or none
        # do), this still refuses to guess.
        if len(candidates) > 1:
            parent_matches = []
            for m in candidates:
                heading_line = _nearest_preceding_heading_line(text_lower, m.start())
                is_parent = any(hint in heading_line for hint in _PARENT_SCOPE_HINTS)
                is_subsidiary = any(hint in heading_line for hint in _SUBSIDIARY_SCOPE_HINTS)
                if is_parent and not is_subsidiary:
                    parent_matches.append(m)
            if len(parent_matches) == 1:
                return _finish_amount_match(text, parent_matches[0], group_index=1)
        return None, None   # this field's own label wasn't found exactly once - don't guess between 0 or several
    return _finish_amount_match(text, candidates[0], group_index=1)


# A table/section-wide scale header - "Shs' millions", "KES billions",
# "amounts in thousands" - stated ONCE for a whole table rather than
# repeated next to every number in it (the same problem currency
# markers have, and the same fix: detect it once from context, apply
# it to every bare number pulled from that context, rather than
# requiring it glued to each individual figure).
_TABLE_SCALE_RE = re.compile(
    r"(?:shs|kshs?|kes)['’]?\s*(thousand|million|mn|billion|bn)s?"
    r"|(?:in|amounts? in)\s*(thousand|million|billion)s?\b",
    re.IGNORECASE,
)


def _detect_chunk_scale(text: str):
    """Returns the multiplier (1, 1_000, 1_000_000, or 1_000_000_000)
    implied by a table-wide scale header found anywhere in this
    chunk's text (e.g. "Shs' millions" as a column header), or None if
    no such header is present. Only ever used as a FALLBACK when the
    matched number itself carries no scale word of its own - a number
    with its own explicit "77 million" always uses that, regardless of
    what a table header elsewhere says, since the specific figure's
    own wording is always more trustworthy than a shared table
    caption."""
    m = _TABLE_SCALE_RE.search(text)
    if not m:
        return None
    word = (m.group(1) or m.group(2) or '').lower()
    return {'thousand': 1_000, 'million': 1_000_000, 'mn': 1_000_000,
            'billion': 1_000_000_000, 'bn': 1_000_000_000}.get(word)


def _finish_amount_match(text: str, m, group_index: int = 1):
    """Shared tail end of _extract_amount's two matching strategies -
    turns a regex Match into (value, source_sentence), applying any
    million/billion scale word found in the SAME match (group 2) if
    present, else falling back to a table-wide scale header detected
    anywhere in the chunk (see _detect_chunk_scale) - and trims the
    surrounding line for display."""
    raw = m.group(group_index).replace(',', '')
    try:
        value = float(raw)
    except ValueError:
        return None, None
    scale = ''
    if m.lastindex and m.lastindex >= 2 and m.group(2):
        scale = m.group(2).lower()
    if scale in ('million', 'mn', 'm'):
        value *= 1_000_000
    elif scale in ('billion', 'bn'):
        value *= 1_000_000_000
    else:
        # No scale word on this specific match - fall back to a
        # table-wide header if this chunk has one (e.g. "Shs' millions"
        # as a column caption applying to every figure in the table).
        chunk_scale = _detect_chunk_scale(text)
        if chunk_scale:
            value *= chunk_scale
    start = text.rfind('\n', 0, m.start()) + 1
    end = text.find('\n', m.end())
    end = end if end != -1 else len(text)
    sentence = text[start:end].strip()
    return value, sentence


def extract_survey_schema(pdf_path: str, start_page: int = 1, end_page=None,
                           company_country: str = None) -> dict:
    """Runs the full extraction pipeline against one PDF and returns a
    dict shaped exactly like survey_data_parse.parse_survey_data()'s
    return value: flat {column: value}, plus 'field_sources' (column
    -> 'page N: <sentence>') and 'field_confidence' (column -> float,
    0-1). Does NOT set company_name/fiscal_year/sector - those need a
    person or a separate targeted lookup (===COMPANY===/===PERIOD===
    equivalents), since this module is about pulling BOARD/PAY/
    PERFORMANCE figures out of body content, not re-deriving identity
    fields a caller should already know from the upload context.

    company_country (optional, e.g. "Kenya" - typically the caller's
    already-on-file Company.country) is used ONLY as a last-resort
    currency inference when the document itself states no currency
    anywhere - see detect_document_currency(). It has no other effect
    and is never used to infer anything besides currency.

    A field with no candidate chunk scoring above 0, or whose best
    candidate's text doesn't yield a clean single value via
    _extract_count/_extract_amount, is left out of the result
    entirely - exactly like an undisclosed field in a hand-typed
    Survey Data file. Nothing is filled with a best-effort guess.
    """
    chunks = chunk_pdf(pdf_path, start_page=start_page, end_page=end_page)

    currency_code, currency_confidence, currency_note = detect_document_currency(chunks, company_country)
    doc_unit, doc_unit_confidence, doc_unit_note = detect_document_unit(chunks, scope='document')
    rem_unit, rem_unit_confidence, rem_unit_note = detect_document_unit(chunks, scope='remuneration')

    result = {}
    field_sources = {}
    field_confidence = {}

    if currency_code:
        result['currency'] = currency_code
        field_sources['currency'] = currency_note
        field_confidence['currency'] = currency_confidence

    if doc_unit:
        result['unit'] = doc_unit
        field_sources['unit'] = doc_unit_note
        field_confidence['unit'] = doc_unit_confidence
    # director_figures_unit is only SET when it's genuinely DIFFERENT
    # from the document's own headline unit - see models_survey.py's
    # SurveyCompanyData.director_figures_unit docstring. Leaving it
    # NULL for the (much more common) single-scale filing means a
    # caller's fallback-to-`unit` logic is exercised, rather than this
    # extractor redundantly writing the same value into both columns
    # every time.
    if rem_unit and rem_unit != doc_unit:
        result['director_figures_unit'] = rem_unit
        field_sources['director_figures_unit'] = rem_unit_note
        field_confidence['director_figures_unit'] = rem_unit_confidence

    # Board-roster scan: a numbered photo-caption listing ("1. Jane Doe
    # ... Independent Non-Executive Director") states composition
    # through who's listed, not through a stated "N Directors"
    # sentence - run this BEFORE the per-field ontology loop below, on
    # every chunk (not just chunks the ontology already ranked highly
    # for board_size, since a roster caption's own text rarely
    # contains the word "constitution" or "comprises" that ranking
    # rewards). Confidence is fixed and high (0.8) rather than
    # computed from a keyword match, because this is a direct count of
    # named, individually-tagged people, not an inference from prose -
    # more reliable than most of what the ontology-ranked loop below
    # can produce, so it's allowed to set board_size/executive_
    # directors_count/non_executive_directors_count directly and the
    # loop below only fills those specific fields in if this scan
    # found nothing.
    roster_fields_found = set()
    for chunk in chunks:
        roster = _extract_board_roster_counts(chunk)
        if not roster:
            continue
        for field, value in roster.items():
            if field in result:
                continue   # first roster chunk found wins; don't let a second, unrelated numbered list override it
            result[field] = value
            field_sources[field] = f"page {chunk.page}: numbered board roster listing ({value} entries matching)"
            field_confidence[field] = 0.8
            roster_fields_found.add(field)

    for canonical_field in ONTOLOGY:
        if canonical_field in roster_fields_found:
            continue   # already set directly from the roster scan above - a fuzzier keyword-ranked match shouldn't override it
        candidates = rank_chunks(chunks, canonical_field, top_n=3)
        if not candidates:
            continue

        is_int_field = canonical_field in INT_FIELDS
        best_value = None
        best_sentence = None
        best_chunk = None
        best_heading_score = 0.0

        for chunk in candidates:
            if is_int_field:
                value, sentence = _extract_count(chunk, canonical_field)
                extraction_is_precise = value is not None   # count extraction is always this specific
            else:
                value, sentence = _extract_amount(chunk, canonical_field, document_currency_known=bool(currency_code))
                extraction_is_precise = value is not None   # _extract_amount only ever returns a value when it found an unambiguous, specifically-labeled match
            if value is None:
                continue
            heading_score = score_chunk(chunk.text, chunk.heading, canonical_field)
            if best_value is None or heading_score > best_heading_score:
                best_value, best_sentence, best_chunk, best_heading_score = value, sentence, chunk, heading_score

        if best_value is None:
            continue   # every candidate chunk scored on keywords, but none contained an extractable value

        # Confidence has two independent ingredients, taken as a
        # weighted MAX rather than purely multiplied together:
        #   - retrieval confidence: how strongly the chunk's heading/
        #     text matched this field's ontology entry (the
        #     "found the right neighborhood" signal)
        #   - extraction confidence: whether the actual value came
        #     from an unambiguous, specifically-labeled match (the
        #     "found the right number, and know why" signal) -
        #     _extract_count/_extract_amount ONLY ever return a value
        #     when they found exactly one qualifying match (after
        #     required-term filtering, cross-checking, and - for
        #     amounts - parent/subsidiary scope disambiguation), so a
        #     returned value is already a high-precision result on its
        #     own, even from a chunk whose generic keyword score was
        #     unremarkable (e.g. a heading-less table-note chunk).
        # Retrieval score alone would wrongly bury a precise,
        # well-disambiguated figure just because the CHUNK it lives in
        # didn't also happen to have an on-topic heading.
        retrieval_confidence = min(1.0, best_heading_score / 1.3)
        extraction_confidence = 0.7 if extraction_is_precise else 0.0
        normalized = max(retrieval_confidence, extraction_confidence)
        confidence = normalized if is_int_field else normalized * 0.9
        if not is_int_field and currency_code:
            confidence = min(confidence, currency_confidence)

        if confidence < 0.45:
            continue   # below the surfacing threshold - treat as not found

        result[canonical_field] = int(best_value) if is_int_field else best_value
        field_sources[canonical_field] = f"page {best_chunk.page}: {best_sentence}"
        field_confidence[canonical_field] = round(confidence, 2)

    # Cross-field safeguard: if two DIFFERENT fields' best evidence
    # traces back to the exact same source sentence, that's a strong
    # sign the count-extraction regex matched the same generic phrase
    # for both (e.g. "11 Directors" satisfying both
    # executive_directors_count and non_executive_directors_count,
    # which cannot both be true of the same 11 people) rather than two
    # independently-confirmed facts. Both are dropped rather than
    # guessing which one (if either) is actually correct.
    sentence_to_fields = {}
    for f, src in field_sources.items():
        sentence_to_fields.setdefault(src, []).append(f)
    for src, fields_sharing in sentence_to_fields.items():
        if len(fields_sharing) > 1:
            for f in fields_sharing:
                result.pop(f, None)
                field_sources.pop(f, None)
                field_confidence.pop(f, None)

    if field_sources:
        result['field_sources'] = field_sources
    if field_confidence:
        result['field_confidence'] = field_confidence

    return result


# Policy fields (RemunerationPolicy) are yes/no facts, not amounts or
# counts - "has_share_option_scheme", "has_ltip", "has_performance_bonus".
# A negation word within a short window of the matched phrase flips the
# result to False; otherwise a match is treated as True. No match at
# all leaves the field out entirely (None/not-addressed), never
# defaulted to False - a filing that never mentions LTIP at all is a
# genuinely different, weaker claim than one that explicitly says "the
# Group does not operate an LTIP."
_POLICY_FIELDS = {
    'has_share_option_scheme': 'has_share_option_scheme',
    'has_ltip': 'has_ltip',
    'has_performance_bonus': 'bonus_or_performance_pay',   # ontology key differs from the RemunerationPolicy column name
}
_NEGATION_RE = re.compile(
    r'\b(no|not|none|never|does not|do not|doesn\'t|don\'t|no longer|discontinued|'
    r'has not|have not)\b',
    re.IGNORECASE,
)


def extract_policy_disclosures(pdf_path: str, start_page: int = 1, end_page=None) -> dict:
    """Runs a SEPARATE, smaller pipeline for RemunerationPolicy's
    yes/no fields - reuses chunk_pdf + the ontology's ranking, but the
    extraction step is presence-of-negation-near-keyword, not
    number-parsing, so it doesn't share extract_survey_schema's
    INT_FIELDS/amount-extraction machinery. Returns a dict shaped like
    RemunerationPolicy's own columns: {'has_share_option_scheme': True,
    'field_sources': {...}, 'field_confidence': {...}}. A field with no
    matching chunk at all is left out of the result entirely, per the
    same "missing means not addressed" discipline as everywhere else
    in this module."""
    chunks = chunk_pdf(pdf_path, start_page=start_page, end_page=end_page)

    result = {}
    field_sources = {}
    field_confidence = {}

    for policy_field, ontology_key in _POLICY_FIELDS.items():
        candidates = rank_chunks(chunks, ontology_key, top_n=3)
        if not candidates:
            continue
        best_chunk = candidates[0]
        best_score = score_chunk(best_chunk.text, best_chunk.heading, ontology_key)

        # Find the specific sentence the matched synonym appeared in,
        # so the negation check runs on THAT sentence rather than the
        # whole (possibly long) chunk - a negation elsewhere in the
        # chunk talking about something else shouldn't flip this
        # field's own answer.
        entry_synonyms = ONTOLOGY.get(ontology_key, {}).get('synonyms', [])
        text_lower = best_chunk.text.lower()
        sentence = best_chunk.text
        for phrase in entry_synonyms:
            idx = text_lower.find(phrase)
            if idx != -1:
                start = best_chunk.text.rfind('\n', 0, idx) + 1
                end = best_chunk.text.find('\n', idx)
                end = end if end != -1 else len(best_chunk.text)
                sentence = best_chunk.text[start:end].strip()
                break

        is_negated = bool(_NEGATION_RE.search(sentence))
        result[policy_field] = not is_negated
        field_sources[policy_field] = f"page {best_chunk.page}: {sentence}"
        confidence = min(0.85, 0.5 + 0.35 * min(1.0, best_score / 1.3))
        field_confidence[policy_field] = round(confidence, 2)

    if field_sources:
        result['field_sources'] = field_sources
    if field_confidence:
        result['field_confidence'] = field_confidence

    return result
