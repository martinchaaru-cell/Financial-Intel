"""
Ontology of search terms for every SurveyCompanyData field this app
knows about (models_survey.py / SURVEY_FORMAT_SPEC.md) - the thing
that lets intelligence_extractor.py find "which chunk talks about the
chairperson's retainer" without a company ever having to use FinSight's
own exact field names in its own filing.

Design choice: plain keyword lists, ranked by simple scoring - not
embeddings, not a vector store. For the volume of documents this app
actually processes (one or a handful of annual reports per company per
year, each a few hundred pages), ranking ~15-30 keyword sets against a
few hundred already-chunked text blocks is fast, fully deterministic,
and - crucially - auditable: a person can read ONTOLOGY and see
exactly why a given chunk scored the way it did, which a cosine
similarity over an embedding can't offer without extra tooling on top.

Each canonical field maps to a list of synonym phrases (lowercase,
matched case-insensitively against chunk heading + body text) and an
optional list of "section hint" phrases that, if found in a chunk's
heading, count extra - since a company's own section title ("Directors'
Remuneration Report") is a much stronger signal than the same words
merely appearing in body prose. An optional 'negative_terms' list
suppresses a match entirely when one of those phrases appears in the
SAME chunk - e.g. a field for an actual LTIP shouldn't score highly on
a chunk whose own text says "the Group does not operate an LTIP".
"""

# canonical field (SurveyCompanyData column name, or a
# RemunerationPolicy/GovernancePolicy column name) -> {
#   'synonyms': [phrase, ...],        # scored wherever found (heading or body)
#   'section_hints': [phrase, ...],   # extra weight if found in the chunk's HEADING specifically
#   'negative_terms': [phrase, ...],  # optional - presence anywhere in the chunk suppresses this field's score entirely
# }
ONTOLOGY = {
    # ---- Performance ----
    'turnover': {
        'synonyms': ['turnover', 'total revenue', 'group revenue', 'revenue'],
        'section_hints': ['financial highlights', 'statement of profit or loss', 'income statement'],
    },
    'net_profit': {
        'synonyms': ['profit for the year', 'net profit', 'profit after tax'],
        'section_hints': ['financial highlights', 'statement of profit or loss'],
    },
    'market_cap': {
        'synonyms': ['market capitalisation', 'market capitalization', 'market cap'],
        'section_hints': ['shareholder information', 'share performance'],
    },

    # ---- Board composition ----
    'board_size': {
        'synonyms': ['constitution of the board', 'board consists of', 'number of directors',
                     'board comprises', 'members of the board'],
        'section_hints': ['who governs us', 'board of directors', 'corporate governance'],
    },
    'board_meetings_per_year': {
        'synonyms': ['board meetings held', 'meetings of the board', 'number of board meetings'],
        'section_hints': ['board meetings', 'meeting attendance', 'attendance profile'],
    },
    'committees_per_board': {
        'synonyms': ['board committees', 'committees of the board'],
        'section_hints': ['board committees'],
    },
    'committee_meetings_per_year': {
        'synonyms': ['committee meetings held', 'number of committee meetings'],
        'section_hints': ['committee attendance', 'meeting attendance'],
    },
    'directors_female': {
        'synonyms': ['female directors', 'women on the board', 'gender diversity'],
        'section_hints': ['board diversity', 'diversity'],
    },
    'directors_male': {
        'synonyms': ['male directors', 'men on the board'],
        'section_hints': ['board diversity', 'diversity'],
    },
    'executive_directors_count': {
        'synonyms': ['executive director', 'executive directors'],
        'section_hints': ['board of directors', 'who governs us'],
    },
    'non_executive_directors_count': {
        'synonyms': ['non-executive director', 'non-executive directors'],
        'section_hints': ['board of directors', 'who governs us'],
    },
    'independent_neds_count': {
        'synonyms': ['independent non-executive director', 'independent director', 'independence of directors'],
        'section_hints': ['director independence', 'independent directors'],
    },
    'non_independent_neds_count': {
        'synonyms': ['non-independent director', 'non-independent non-executive'],
        'section_hints': ['director independence'],
    },
    'neds_kenyan_count': {
        'synonyms': ['kenyan director', 'kenyan national', 'nationality: kenyan'],
        'section_hints': ['board of directors', 'director nationality'],
    },
    'neds_non_kenyan_count': {
        'synonyms': ['non-kenyan director', 'foreign director', 'nationality:'],
        'section_hints': ['board of directors', 'director nationality'],
    },
    'avg_age_executive_directors': {
        'synonyms': ['average age', 'age of executive directors'],
        'section_hints': ['board of directors', 'average age'],
    },
    'avg_age_non_executive_directors': {
        'synonyms': ['average age', 'age of non-executive directors'],
        'section_hints': ['board of directors', 'average age'],
    },

    # ---- Director pay ----
    'chairperson_annual_retainer': {
        'synonyms': ['chairman fee', 'chairperson retainer', 'chair remuneration',
                     "chairman's annual fee", 'annual retainer fee for the chairman'],
        'section_hints': ["directors' remuneration report", 'remuneration for non-executive directors'],
    },
    'other_ned_annual_retainer': {
        'synonyms': ['director fee', 'non-executive fee', 'ned fee',
                     'annual retainer fee for the chairman and other non-executive',
                     'fees for non-executive directors'],
        'section_hints': ["directors' remuneration report", 'remuneration for non-executive directors'],
    },
    'chairperson_meeting_allowance': {
        'synonyms': ['sitting allowance', 'attendance fee', 'meeting fee', 'board meeting allowance'],
        'section_hints': ["directors' remuneration report"],
    },
    'other_ned_meeting_allowance': {
        'synonyms': ['sitting allowance', 'attendance fee', 'meeting fee'],
        'section_hints': ["directors' remuneration report"],
    },
    'executive_director_annual_retainer': {
        'synonyms': ["executive director's remuneration", 'executive director salary', 'as executives'],
        'section_hints': ["directors' remuneration report", "directors' emoluments"],
    },

    # ---- Pay components (benefit-level detail, not just totals) ----
    'pension_contribution': {
        'synonyms': ['pension contribution', 'retirement benefit', 'pension scheme contribution',
                     'defined contribution scheme', 'staff pension fund'],
        'section_hints': ["directors' remuneration report", "directors' emoluments", 'employee benefits'],
    },
    'gratuity': {
        'synonyms': ['gratuity', 'end of service benefit', 'terminal benefit'],
        'section_hints': ["directors' remuneration report", "directors' emoluments"],
    },
    'medical_cover': {
        'synonyms': ['medical insurance', 'medical cover', 'medical aid', 'health insurance cover'],
        'section_hints': ["directors' remuneration report", 'employee benefits'],
    },
    'vehicle_benefit': {
        'synonyms': ['car benefit', 'vehicle allowance', 'company car', 'car allowance'],
        'section_hints': ["directors' remuneration report", 'employee benefits'],
    },
    'housing_benefit': {
        'synonyms': ['housing allowance', 'housing benefit', 'accommodation allowance'],
        'section_hints': ["directors' remuneration report", 'employee benefits'],
    },
    'leave_allowance': {
        'synonyms': ['leave allowance', 'annual leave pay', 'leave pay'],
        'section_hints': ["directors' remuneration report", 'employee benefits'],
    },
    'bonus_or_performance_pay': {
        'synonyms': ['performance bonus', 'annual bonus', 'incentive pay', 'performance-based pay',
                     'short-term incentive', 'sti award'],
        'section_hints': ["directors' remuneration report", "directors' emoluments"],
    },

    # ---- Additional fixed-pay/committee terms (merged from a
    # community-contributed ontology draft - kept in the same tested
    # dict shape/scoring model as everything above, not the dataclass
    # shape that draft proposed, since score_chunk/rank_chunks/
    # extract_survey_schema all key off this dict shape directly) ----
    'retainer_fee': {
        'synonyms': ['annual retainer', 'monthly retainer', 'board retainer', 'retainer fee'],
        'section_hints': ["directors' remuneration report"],
    },
    'committee_chair_fee': {
        'synonyms': ['committee chair fee', 'committee chairman fee', 'committee chairperson fee'],
        'section_hints': ['committee remuneration'],
    },

    # ---- Long-term incentives (distinct from the has_ltip POLICY
    # yes/no field further below - these are for an actual disclosed
    # AMOUNT, when a filing states one) ----
    'share_award_value': {
        'synonyms': ['share award', 'share grant', 'equity award', 'restricted share award'],
        'section_hints': ["directors' remuneration report", 'long-term incentive plan'],
    },
    'stock_option_value': {
        'synonyms': ['stock option', 'share option grant', 'option grant value'],
        'section_hints': ["directors' remuneration report", 'long-term incentive plan'],
    },

    # ---- Totals (company-level, matches this filing's OWN stated
    # total rather than a sum this app computes itself - same
    # discipline as ceo_monthly_cost_of_employment above) ----
    'total_director_remuneration': {
        'synonyms': ['total directors remuneration', 'aggregate directors remuneration',
                     'total emoluments of directors'],
        'section_hints': ["directors' remuneration report", "directors' emoluments"],
    },
    'key_management_compensation_total': {
        'synonyms': ['key management personnel compensation', 'total key management compensation'],
        'section_hints': ['key management compensation', "directors' emoluments"],
    },

    # ---- Committee pay ----
    'committee_chair_annual_retainer': {
        'synonyms': ['committee chair fee', 'committee chairperson retainer'],
        'section_hints': ['committee remuneration'],
    },
    'committee_member_annual_retainer': {
        'synonyms': ['committee member fee', 'committee member retainer'],
        'section_hints': ['committee remuneration'],
    },

    # ---- CEO pay ----
    'ceo_monthly_salary': {
        'synonyms': ['chief executive officer salary', "ceo's remuneration", 'md salary',
                     'managing director salary', 'key management compensation', 'key management personnel'],
        'section_hints': ["directors' emoluments", 'key management compensation'],
    },
    'ceo_monthly_cost_of_employment': {
        'synonyms': ['total compensation', 'cost to company', 'total remuneration package'],
        'section_hints': ["directors' emoluments", 'key management compensation'],
    },

    # ---- Policy disclosures (yes/no + free text, not amounts) ----
    'has_share_option_scheme': {
        'synonyms': ['share option scheme', 'employee share ownership plan', 'esop',
                     'stock option plan', 'share-based payment scheme'],
        'section_hints': ["directors' remuneration report", 'remuneration policy'],
        # Real filings routinely state the ABSENCE of a scheme using
        # the same vocabulary as its presence (e.g. Eaagads Limited's
        # 2025 report: "The Company does not operate a share option
        # scheme for Directors") - without suppression this scores as
        # a strong positive match for a policy the filing says it
        # does NOT have.
        'negative_terms': ['does not operate a share option scheme', 'no share option scheme',
                            'not operate any share option scheme', 'does not have a share option scheme'],
    },
    'has_ltip': {
        'synonyms': ['long-term incentive plan', 'long term incentive', ' ltip ', ' ltip.', ' ltip,', '(ltip)'],
        'section_hints': ["directors' remuneration report", 'remuneration policy'],
        # Same failure mode as has_share_option_scheme above, and the
        # exact case the module docstring's own example describes -
        # confirmed against a real filing: Eaagads Limited's 2025
        # report states "There were no long-term incentives granted
        # to Non-Executive Directors", which shares every synonym
        # word with a genuine LTIP disclosure and would otherwise
        # score just as high as one.
        'negative_terms': ['no long-term incentive', 'no long term incentive', 'not operate a long-term incentive',
                            'does not operate a long-term incentive', 'no ltip'],
    },
    'remuneration_benchmark_percentile': {
        'synonyms': ['market percentile', 'percentile of the market', 'benchmarking exercise',
                     'salary survey', 'remuneration survey'],
        'section_hints': ["directors' remuneration report", 'remuneration policy'],
    },
    'remuneration_benchmark_provider': {
        'synonyms': ['pwc', 'deloitte', 'ey benchmarking', 'kpmg survey', 'mercer', 'korn ferry',
                     'benchmarking consultant', 'independent remuneration consultant'],
        'section_hints': ["directors' remuneration report", 'remuneration policy'],
    },
}


def _normalize_quotes(s: str) -> str:
    """Collapses typographic (curly) apostrophes/quotes to their plain
    ASCII equivalents. PDF text layers from InDesign, Word, and most
    professional publishing tools consistently emit U+2019 (') for an
    apostrophe rather than the plain U+0027 (') this ontology's phrases
    are written with - e.g. a real filing's own heading "DIRECTORS'
    REMUNERATION REPORT" uses U+2019, so an ontology phrase written as
    "directors' remuneration report" (U+0027) would otherwise never
    match it, silently, for every field whose synonyms or section_hints
    contain an apostrophe (the majority of this ontology). Normalizing
    both sides at comparison time fixes this for every phrase at once
    without having to rewrite each dict entry by hand."""
    return (
        s.replace('\u2019', "'").replace('\u2018', "'")
         .replace('\u201c', '"').replace('\u201d', '"')
    )


def score_chunk(chunk_text: str, chunk_heading: str, canonical_field: str) -> float:
    """Score in [0, 1]-ish range (not strictly capped - see below) for
    how well one chunk matches one canonical field's ontology entry.
    Heading hits count far more than body-text hits, since a company's
    own section title is the strongest available signal that a chunk
    is actually ABOUT this field rather than merely mentioning an
    adjacent word in passing (e.g. "director" appears constantly in a
    governance section without every paragraph being about pay)."""
    entry = ONTOLOGY.get(canonical_field)
    if not entry:
        return 0.0
    text_lower = _normalize_quotes((chunk_text or '').lower())
    heading_lower = _normalize_quotes((chunk_heading or '').lower())

    # A negative term anywhere in this chunk suppresses the field
    # entirely, before any synonym scoring - a chunk whose own text
    # states "the Group does not operate a Long-Term Incentive Plan"
    # should never rank as a good match for 'has_ltip', regardless of
    # how many times "LTIP" itself appears in that same sentence.
    for phrase in entry.get('negative_terms', []):
        if phrase in text_lower:
            return 0.0

    score = 0.0
    for phrase in entry.get('synonyms', []):
        if phrase in heading_lower:
            score += 0.5
        elif phrase in text_lower:
            score += 0.15
    for phrase in entry.get('section_hints', []):
        if phrase in heading_lower:
            score += 0.8
    return score


def rank_chunks(chunks, canonical_field: str, top_n: int = 5):
    """Returns the top_n DocumentChunks best matching canonical_field,
    highest score first, ties broken by page order. Chunks scoring 0
    are excluded entirely rather than padding the result - "nothing
    found" should look like nothing found, not like a weak guess."""
    scored = [
        (score_chunk(c.text, c.heading, canonical_field), c)
        for c in chunks
    ]
    scored = [(s, c) for s, c in scored if s > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1].page))
    return [c for _s, c in scored[:top_n]]
