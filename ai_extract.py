"""AI extraction module: reads the report pages that carry the board, committee
and director-pay content with a Claude model, then VERIFIES every value against
the page text before anything can be applied.

Why it exists: rule-based readers (pdf_parse.py, board_extract.py) need one
parser per report layout. Seven real reports gave six different layouts, so a
new company usually means a new parser. A model reads any layout - but a model
can also invent a plausible number, so nothing it returns is trusted until it
passes the grounding checks below:

  * every item carries the page it came from and a short verbatim snippet;
  * the snippet, the person's name and each amount must literally occur on that
    page's text (amount digits are compared without separators);
  * director pay rows are reconciled against the table's own printed total;
  * items that fail are kept but marked unverified and are NOT applied.

Cost control: only the relevant pages are sent (about 12 per task, three tasks
per report), requests go through the Message Batches API at half price, and the
run is started by a person clicking a button after seeing the estimate.

The module never touches the database; ai_routes in app.py stores/reads items.
"""
import difflib
import io
import json
import os
import re

TASKS = ('board', 'committees', 'pay')

# USD per million tokens (input, output) - Anthropic pricing page, September 2026
PRICES = {
    'claude-haiku-4-5-20251001': (1.0, 5.0),
    'claude-sonnet-5': (2.0, 10.0),
    'claude-opus-5': (5.0, 25.0),
    'claude-fable-5-1': (10.0, 50.0),
}
DEFAULT_MODEL = os.environ.get('FINSIGHT_AI_MODEL', 'claude-sonnet-5')
BATCH_DISCOUNT = 0.5
# Newer Claude tokenizers produce ~30% more tokens than a 4-chars/token rule: budget conservatively
CHARS_PER_TOKEN = 3.0
EXPECTED_OUTPUT_TOKENS = {'board': 3500, 'committees': 3000, 'pay': 5500}

# ---------------------------------------------------------------- page finder
_KEYWORDS = {
    'board': [(r'appointed', 2, 6), (r'nationality', 4, 4), (r'independent', 1, 6), (r'non[\s\-]?executive', 1, 6),
              (r'board of directors', 2, 3), (r'profiles?', 2, 3), (r'date of appointment', 4, 3), (r'gender', 2, 3),
              (r'market capitali[sz]ation', 4, 2), (r'board (?:composition|meetings?)', 2, 3), (r'\bage\s*:', 3, 3),
              (r'\bchairman|chairperson\b', 1, 4)],
    'committees': [(r'attendance', 4, 3), (r'committee', 1, 12), (r'scheduled meetings?', 4, 2), (r'attended', 2, 4),
                   (r'meetings? held', 3, 3), (r'members?', 1, 6), (r'chair', 1, 4), (r'\d\s*/\s*\d|\d\s*\(\s*\d\s*\)', 1, 10)],
    'pay': [(r'remuneration', 1, 6), (r'emoluments?', 4, 3), (r'\bfees\b', 2, 6), (r'sitting allowances?', 4, 3),
            (r'retainer', 4, 3), (r'single figure', 5, 2), (r'directors.{0,3}\s+remuneration', 3, 3),
            (r'medical|insurance|indemnity|club membership', 1, 6), (r"(?:kshs?|shs?)\s*[\u2019']?\s*(?:000|million)", 1, 4),
            (r'non[\s\-]?executive directors?', 1, 6), (r'\btotal\b', 1, 6)],
}
_PAGE_LIMITS = {'board': (10, 55000), 'committees': (8, 45000), 'pay': (12, 60000)}


def score_page(text: str, task: str) -> float:
    low = (text or '').lower()
    score = 0.0
    for pattern, weight, cap in _KEYWORDS[task]:
        score += weight * min(len(re.findall(pattern, low)), cap)
    return score


def select_pages(pages, task: str, min_score: float = 12.0):
    """pages = [(page_number, text)]. Returns the page numbers to send for
    `task`: the best-scoring pages, plus the page after a strong table page
    (pay and attendance tables run over), capped by page count and characters."""
    max_pages, max_chars = _PAGE_LIMITS[task]
    scored = sorted(((score_page(t, task), pn) for pn, t in pages), reverse=True)
    top = [(s, pn) for s, pn in scored if s >= min_score][:max_pages]
    chosen = {pn for _, pn in top}
    if task in ('pay', 'committees'):
        by_pn = dict(pages)
        for s, pn in top[:4]:
            nxt = pn + 1
            if nxt in by_pn and score_page(by_pn[nxt], task) >= min_score / 2:
                chosen.add(nxt)
    ordered, chars = [], 0
    text_by = dict(pages)
    for pn in sorted(chosen):
        chars += len(text_by.get(pn, ''))
        if chars > max_chars and ordered:
            break
        ordered.append(pn)
    return ordered


def layout_texts(pdf_bytes: bytes, page_numbers):
    """Layout-preserving text (columns kept aligned) for just the chosen pages;
    a wide page holding two facing pages is split into halves so the columns of
    one table are not interleaved with the other page's text."""
    import pdfplumber
    out = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pn in page_numbers:
            page = pdf.pages[pn - 1]
            parts = []
            if page.width > 1.5 * page.height:
                halves = [page.crop((0, 0, page.width / 2, page.height)),
                          page.crop((page.width / 2, 0, page.width, page.height))]
                for i, half in enumerate(halves):
                    parts.append(f"[{'left' if i == 0 else 'right'} half of page]\n" +
                                 (half.extract_text(layout=True, x_density=4.5, y_density=13) or ''))
            else:
                parts.append(page.extract_text(layout=True, x_density=4.5, y_density=13) or '')
            text = "\n".join(re.sub(r"[ \t]+$", "", line) for p in parts for line in p.splitlines() if line.strip())
            out[pn] = text
    return out


# ------------------------------------------------------------------ prompts
_SYSTEM = """You extract structured facts from pages of a company's annual report for a board and director-pay survey.

Rules - they matter more than completeness:
1. Report ONLY what the pages state. If a value is not stated, leave it out (or null). Never estimate, sum, convert or guess.
2. Every item needs the page number it came from (the '=== PAGE n ===' marker) and an `evidence` snippet: 4-15 words copied EXACTLY from that page next to the value. Items without a copyable snippet must be omitted.
3. Use the CURRENT fiscal year only. Ignore prior-year comparative columns and prior-year tables (a table 'for the year ended ... 2023' is not current when the report is for 2024).
4. Copy names as printed, minus honorifics and credentials (Dr., Prof., CPA, EBS, CBS...). Do not merge two different people.
5. Numbers exactly as printed in the table's own unit; do not rescale. State the unit of each table (units / thousands / millions) from its column header.
6. Gender only if the page states it (a Gender column, or Mr./Mrs./Ms./Miss before the name). Independent only if the page states it. Role only from the person's stated title.
7. Directors are people who sit on the board. Exclude the company secretary and non-director executives, unless they are also listed as directors.
Call the `record_findings` tool exactly once."""

_TASK_INSTRUCTIONS = {
    'board': ("Find (a) every director on the board for the fiscal year, with role, independence, gender, nationality, "
              "appointment date and age where stated; (b) board-level facts stated in a sentence or table: board size, "
              "number of board meetings held in the year, number of executive / non-executive / independent / female / male "
              "directors; (c) the company's own market capitalisation if stated (not the stock market's)."),
    'committees': ("Find the board committees and the attendance table: for each committee its name, chair, members, the "
                   "number of meetings held in the year, and each member's meetings attended and meetings eligible to attend "
                   "if shown. Also the number of Board meetings held in the year."),
    'pay': ("Find (a) the directors' remuneration table(s) for the CURRENT year: one row per named director with each "
            "printed component and the row's printed total, and the table's own printed grand total; (b) the policy for "
            "non-executive directors: chairperson and other NED annual retainer / fee, per-meeting sitting allowance, "
            "committee chair and member retainers and allowances, executive director retainer, each with its period "
            "(annual / monthly / per meeting); (c) which benefits NEDs are stated to receive or not receive "
            "(medical cover, indemnity / D&O insurance, travel and accommodation, telephone, transport, meals, club "
            "membership, duty-day allowance, group personal accident, share scheme); (d) the remuneration of the Chief "
            "Executive Officer / Managing Director for the CURRENT year as printed (salary, allowances, bonus, benefits, "
            "pension, total), stating whether the figures are annual."),
}


def _ev():
    return {'page': {'type': 'integer'}, 'evidence': {'type': 'string', 'description': '4-15 words copied exactly from the page'}}


def _obj(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required)}


def _value_item(extra=None):
    props = {'value': {'type': 'number'}, **_ev()}
    props.update(extra or {})
    return _obj(props, ('value', 'page', 'evidence'))


_TOOL_SCHEMAS = {
    'board': _obj({
        'directors': {'type': 'array', 'items': _obj({
            'name': {'type': 'string'}, 'title_as_printed': {'type': 'string'},
            'role': {'type': 'string', 'enum': ['executive', 'non_executive', 'unknown']},
            'independent': {'type': ['boolean', 'null']}, 'gender': {'type': ['string', 'null'], 'enum': ['Male', 'Female', None]},
            'nationality': {'type': ['string', 'null']}, 'appointed': {'type': ['string', 'null']},
            'age': {'type': ['integer', 'null']}, **_ev()}, ('name', 'role', 'page', 'evidence'))},
        'facts': {'type': 'array', 'items': _obj({
            'field': {'type': 'string', 'enum': ['board_size', 'board_meetings_held', 'executive_directors_count',
                                                 'non_executive_directors_count', 'independent_neds_count',
                                                 'directors_female', 'directors_male']},
            'value': {'type': 'integer'}, **_ev()}, ('field', 'value', 'page', 'evidence'))},
        'market_cap': _obj({'value': {'type': 'number'}, 'unit': {'type': 'string', 'enum': ['units', 'thousands', 'millions', 'billions', 'trillions']},
                            'as_of': {'type': ['string', 'null']}, **_ev()}, ('value', 'unit', 'page', 'evidence')),
    }, ('directors', 'facts')),
    'committees': _obj({
        'board_meetings_held': _value_item(),
        'committees': {'type': 'array', 'items': _obj({
            'name': {'type': 'string'}, 'chair': {'type': ['string', 'null']}, 'meetings_held': {'type': ['integer', 'null']},
            'members': {'type': 'array', 'items': _obj({
                'name': {'type': 'string'}, 'attended': {'type': ['integer', 'null']}, 'eligible': {'type': ['integer', 'null']}}, ('name',))},
            **_ev()}, ('name', 'page', 'evidence'))},
    }, ('committees',)),
    'pay': _obj({
        'tables': {'type': 'array', 'items': _obj({
            'title': {'type': 'string'}, 'unit': {'type': 'string', 'enum': ['units', 'thousands', 'millions']},
            'fiscal_year_of_table': {'type': ['string', 'null'], 'description': 'e.g. 2024 - the year this table reports'},
            'is_current_year': {'type': 'boolean'},
            'rows': {'type': 'array', 'items': _obj({
                'name': {'type': 'string'}, 'role': {'type': 'string', 'enum': ['executive', 'non_executive', 'unknown']},
                'components': {'type': 'object', 'additionalProperties': {'type': ['number', 'null']}},
                'total': {'type': ['number', 'null']}, **_ev()}, ('name', 'total', 'page', 'evidence'))},
            'printed_total': {'type': ['number', 'null']}, 'printed_total_page': {'type': ['integer', 'null']},
        }, ('title', 'unit', 'is_current_year', 'rows'))},
        'policy': {'type': 'array', 'items': _obj({
            'field': {'type': 'string', 'enum': [
                'chairperson_annual_retainer', 'other_ned_annual_retainer', 'chairperson_meeting_allowance',
                'other_ned_meeting_allowance', 'executive_director_annual_retainer', 'executive_director_meeting_allowance',
                'committee_chair_annual_retainer', 'committee_member_annual_retainer',
                'committee_chair_meeting_allowance', 'committee_member_meeting_allowance']},
            'value': {'type': 'number'}, 'unit': {'type': 'string', 'enum': ['units', 'thousands', 'millions']}, **_ev()},
            ('field', 'value', 'unit', 'page', 'evidence'))},
        'ned_benefits': {'type': 'array', 'items': _obj({
            'key': {'type': 'string', 'enum': ['MedicalCover', 'IndemnityInsurance', 'TravelAccommodation', 'TelephoneAllowance',
                                               'TransportAllowance', 'MealAllowance', 'ClubMembership', 'DutyDayAllowance',
                                               'GroupPersonalAccident', 'ShareSchemeParticipation']},
            'provided': {'type': 'boolean'}, 'detail': {'type': ['string', 'null']}, **_ev()},
            ('key', 'provided', 'page', 'evidence'))},
        'ceo': _obj({
            'name': {'type': 'string'}, 'title_as_printed': {'type': ['string', 'null']},
            'unit': {'type': 'string', 'enum': ['units', 'thousands', 'millions']},
            'is_annual': {'type': 'boolean', 'description': 'true when the printed figures are for the whole year'},
            'salary': {'type': ['number', 'null']}, 'allowances': {'type': ['number', 'null']},
            'bonus': {'type': ['number', 'null']}, 'non_cash_benefits': {'type': ['number', 'null']},
            'pension': {'type': ['number', 'null']}, 'total': {'type': ['number', 'null']}, **_ev()},
            ('name', 'unit', 'is_annual', 'page', 'evidence')),
    }, ('tables',)),
}


_GAP_HELP = {
    'register': 'the list of directors (names, roles, independence, gender, nationality, appointment, age)',
    'roles': 'the role (executive / non-executive) and independence of each listed director',
    'board_size': 'board size', 'board_meetings_per_year': 'number of Board meetings held',
    'executive_directors_count': 'number of executive directors', 'non_executive_directors_count': 'number of non-executive directors',
    'independent_neds_count': 'number of independent non-executive directors', 'directors_female': 'number of female directors',
    'directors_male': 'number of male directors', 'market_cap': "the company's market capitalisation",
    'committees': 'the committees with members, chair and meetings held', 'committee_meetings': 'meetings held by each committee',
    'pay_rows': "the directors' remuneration table(s) for the current year", 'pay_reconcile': "the directors' remuneration table(s) - the rows found earlier did not add up to the printed total",
    'ned_policy': 'the non-executive director fee policy (retainers and sitting allowances)', 'ceo_pay': "the CEO / Managing Director's pay",
    'ned_benefits': 'the benefits non-executive directors do or do not receive',
}


# A gap only needs the pages that can answer it: "independent director count" needs pages that say
# "independent", not the whole board section. None = the gap needs every selected page (a list / table).
_GAP_PAGE_RX = {
    'board_size': r'board|directors', 'board_meetings_per_year': r'meetings?', 'executive_directors_count': r'executive',
    'non_executive_directors_count': r'non[\s\-]?executive', 'independent_neds_count': r'independen',
    'directors_female': r'female|women|gender', 'market_cap': r'market capitali', 'committees': r'committee',
    'committee_meetings': r'committee', 'ned_policy': r'retainer|sitting|annual fee|per meeting|monthly fee|allowance',
    'ceo_pay': r'chief executive|managing director', 'ned_benefits': r'medical|indemnity|insurance|club|travell?ing|allowance',
}


def narrow_pages(page_texts: dict, gaps) -> dict:
    """Keep only the pages that mention what the gaps ask for. A register / pay-table gap needs its
    whole page set, so any such gap keeps everything; if no page matches, keep all (never send nothing)."""
    if not gaps or any(g not in _GAP_PAGE_RX for g in gaps):
        return page_texts
    rx = re.compile("|".join(f"(?:{_GAP_PAGE_RX[g]})" for g in gaps), re.I)
    kept = {pn: t for pn, t in page_texts.items() if rx.search(t or '')}
    return kept or page_texts


def build_request(task: str, page_texts: dict, company: str, fiscal_year: str, model: str | None = None, gaps=None) -> dict:
    """Parameters for one Messages API call (also the shape a batch item needs)."""
    page_texts = narrow_pages(page_texts, gaps)
    pages_block = "\n\n".join(f"=== PAGE {pn} ===\n{txt}" for pn, txt in sorted(page_texts.items()))
    only = ""
    if gaps:
        wanted = "; ".join(_GAP_HELP.get(g, g) for g in gaps)
        only = ("\n\nIMPORTANT: everything else has already been read by other means. Extract ONLY these missing items: "
                f"{wanted}. Leave every other section empty (empty arrays / omit optional objects).")
    user = (f"Company: {company}\nFiscal period of this report: {fiscal_year}\n\nTask: {_TASK_INSTRUCTIONS[task]}{only}\n\n"
            f"<pages>\n{pages_block}\n</pages>")
    return {
        'model': model or DEFAULT_MODEL,
        'max_tokens': 8000,
        'system': _SYSTEM,
        'messages': [{'role': 'user', 'content': user}],
        'tools': [{'name': 'record_findings', 'description': 'Record the facts found on the pages.',
                   'input_schema': _TOOL_SCHEMAS[task]}],
        'tool_choice': {'type': 'tool', 'name': 'record_findings'},
    }


# ---------------------------------------------------------------- cost / usage
def estimate_tokens(params: dict) -> int:
    body = params['system'] + json.dumps(params['messages']) + json.dumps(params['tools'])
    return int(len(body) / CHARS_PER_TOKEN)


def estimate_cost(params_list, task_list=None, model: str | None = None, batch: bool = True, gap_counts=None) -> dict:
    model = model or (params_list[0]['model'] if params_list else DEFAULT_MODEL)
    pin, pout = PRICES.get(model, PRICES['claude-sonnet-5'])
    tin = sum(estimate_tokens(p) for p in params_list)
    tasks = task_list or ['pay'] * len(params_list)
    counts = gap_counts or [None] * len(tasks)
    # the answer is only the gaps: ~500 tokens of overhead + ~450 per gap, never above the task's full-answer size
    tout = sum(min(EXPECTED_OUTPUT_TOKENS.get(t, 4000), 500 + 450 * n) if n else EXPECTED_OUTPUT_TOKENS.get(t, 4000)
               for t, n in zip(tasks, counts))
    usd = (tin * pin + tout * pout) / 1e6 * (BATCH_DISCOUNT if batch else 1.0)
    return {'model': model, 'requests': len(params_list), 'input_tokens': tin, 'output_tokens': tout,
            'usd': round(usd, 4), 'batch': batch}


def actual_cost(usage: dict, model: str, batch: bool) -> float:
    pin, pout = PRICES.get(model, PRICES['claude-sonnet-5'])
    usd = ((usage.get('input_tokens') or 0) * pin + (usage.get('output_tokens') or 0) * pout) / 1e6
    return round(usd * (BATCH_DISCOUNT if batch else 1.0), 5)


# ------------------------------------------------------------------- client
_client_factory = None


def set_client_factory(fn):
    """Tests (and alternative transports) inject their own client."""
    global _client_factory
    _client_factory = fn


def api_key_configured() -> bool:
    return bool(os.environ.get('ANTHROPIC_API_KEY')) or _client_factory is not None


def get_client():
    if _client_factory is not None:
        return _client_factory()
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        raise RuntimeError('ANTHROPIC_API_KEY is not set - add it in the project secrets to use AI extraction.')
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise RuntimeError('The anthropic package is not installed (pip install anthropic).') from e
    return anthropic.Anthropic(api_key=key)


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def parse_message(message) -> tuple:
    """(tool_input_dict | None, usage_dict) from a Messages API response object or dict."""
    usage = _get(message, 'usage') or {}
    usage = {'input_tokens': _get(usage, 'input_tokens', 0) or 0, 'output_tokens': _get(usage, 'output_tokens', 0) or 0}
    for block in _get(message, 'content', []) or []:
        if _get(block, 'type') == 'tool_use' and _get(block, 'name') == 'record_findings':
            data = _get(block, 'input')
            return (dict(data) if data is not None else None), usage
    return None, usage


def run_direct(params: dict) -> tuple:
    """One synchronous call (full price). Returns (tool_input | None, usage)."""
    return parse_message(get_client().messages.create(**params))


def submit_batch(items) -> str:
    """items = [(custom_id, params)] -> batch id. One batch holds up to 100,000
    requests / 256 MB; text-only requests for 300 reports are ~60-110 MB."""
    client = get_client()
    batch = client.messages.batches.create(requests=[{'custom_id': cid, 'params': params} for cid, params in items])
    return _get(batch, 'id')


def poll_batch(batch_id: str) -> dict:
    """{'status': 'in_progress'|'ended'|..., 'counts': {...}, 'results': {custom_id: {...}}} - results only once ended."""
    client = get_client()
    batch = client.messages.batches.retrieve(batch_id)
    status = _get(batch, 'processing_status')
    counts = _get(batch, 'request_counts')
    counts = {k: _get(counts, k, 0) for k in ('processing', 'succeeded', 'errored', 'canceled', 'expired')} if counts else {}
    out = {'status': status, 'counts': counts, 'results': {}}
    if status != 'ended':
        return out
    for entry in client.messages.batches.results(batch_id):
        cid = _get(entry, 'custom_id')
        result = _get(entry, 'result')
        rtype = _get(result, 'type')
        if rtype == 'succeeded':
            data, usage = parse_message(_get(result, 'message'))
            out['results'][cid] = {'ok': data is not None, 'data': data, 'usage': usage,
                                   'error': None if data is not None else 'no tool call in the response'}
        else:
            err = _get(_get(result, 'error'), 'message') if rtype == 'errored' else rtype
            out['results'][cid] = {'ok': False, 'data': None, 'usage': {}, 'error': str(err)}
    return out


# ------------------------------------------------------------- verification
def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or '').lower().replace('\u2019', "'")).strip()


def _digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text or "")


def _tokens(text: str) -> set:
    return set(_norm(text).split())


def _evidence_ok(evidence: str, page_norm: str, page_tokens: set) -> bool:
    ev = _norm(evidence)
    if not ev:
        return False
    if ev in page_norm:
        return True
    toks = ev.split()
    hit = sum(1 for t in toks if t in page_tokens)
    return len(toks) >= 4 and hit / len(toks) >= 0.9          # wrapped lines / dropped punctuation


def _name_ok(name: str, page_tokens: set, page_norm: str) -> bool:
    toks = [t for t in _norm(name).split() if len(t) > 1]
    if not toks:
        return False
    ok = 0
    for t in toks:
        if t in page_tokens or any(difflib.SequenceMatcher(None, t, p).ratio() >= 0.85 for p in page_tokens if abs(len(p) - len(t)) <= 2):
            ok += 1
    return ok == len(toks)


def _amount_ok(value, page_text: str) -> bool:
    """The printed digits of `value` appear in the page (any separator style)."""
    if value is None:
        return True
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    s = f"{abs(v):.2f}".rstrip('0').rstrip('.') if v != int(v) else str(int(abs(v)))
    d_page = re.sub(r"[,\s\u00a0'\u2019]", "", page_text or "")
    return s.replace(',', '') in d_page or s in (page_text or '')


def _page_index(page_texts: dict):
    return {pn: (_norm(t), _tokens(t), t) for pn, t in page_texts.items()}


def _check_item(item: dict, idx: dict, name_key: str | None = None, amount_keys=()) -> list:
    """Reasons an item is NOT grounded ([] = verified)."""
    reasons = []
    pn = item.get('page')
    entry = idx.get(pn)
    if entry is None:
        return [f'cited page {pn} was not among the pages sent']
    page_norm, page_tokens, page_raw = entry
    if not _evidence_ok(item.get('evidence', ''), page_norm, page_tokens):
        reasons.append('evidence snippet not found on the cited page')
    if name_key and item.get(name_key) and not _name_ok(item[name_key], page_tokens, page_norm):
        reasons.append(f"name '{item[name_key]}' not found on the cited page")
    for key in amount_keys:
        if item.get(key) is not None and not _amount_ok(item[key], page_raw):
            reasons.append(f'{key} {item[key]} does not appear on the cited page')
    return reasons


def verify_result(task: str, data: dict, page_texts: dict) -> dict:
    """Marks each extracted item verified / unverified (with reasons) and runs
    arithmetic checks. Returns {'data': data-with-flags, 'summary': {...}, 'checks': [...]}."""
    idx = _page_index(page_texts)
    checks, n_items, n_ok = [], 0, 0

    def mark(item, reasons):
        nonlocal n_items, n_ok
        n_items += 1
        item['verified'] = not reasons
        item['problems'] = reasons
        n_ok += 0 if reasons else 1

    if task == 'board':
        for d in data.get('directors', []) or []:
            mark(d, _check_item(d, idx, 'name', ('age',)))
        for f in data.get('facts', []) or []:
            mark(f, _check_item(f, idx))
        mc = data.get('market_cap')
        if mc:
            mark(mc, _check_item(mc, idx, None, ('value',)))
        # counts stated vs listed
        sizes = [f['value'] for f in data.get('facts', []) or [] if f.get('field') == 'board_size' and f.get('verified')]
        listed = [d for d in data.get('directors', []) or [] if d.get('verified')]
        if sizes and listed and sizes[0] != len(listed):
            checks.append({'check': 'board_size_vs_listed', 'ok': False,
                           'detail': f'report states {sizes[0]} directors but {len(listed)} verified names were listed'})
    elif task == 'committees':
        bm = data.get('board_meetings_held')
        if bm:
            mark(bm, _check_item(bm, idx, None, ('value',)))
        for c in data.get('committees', []) or []:
            mark(c, _check_item(c, idx, 'name'))
            for m in c.get('members', []) or []:
                if m.get('eligible') is not None and c.get('meetings_held') and m['eligible'] > c['meetings_held']:
                    checks.append({'check': 'eligible_le_meetings', 'ok': False,
                                   'detail': f"{m['name']} eligible {m['eligible']} > committee meetings {c['meetings_held']}"})
    elif task == 'pay':
        for t in data.get('tables', []) or []:
            persons = []
            for r in t.get('rows', []) or []:
                mark(r, _check_item(r, idx, 'name', ('total',)))
                if r.get('verified') and not re.fullmatch(r"(?i)\s*(grand\s+)?total.*", r.get('name', '')) and r.get('total') is not None:
                    persons.append(r['total'])
            ptotal = t.get('printed_total')
            if t.get('is_current_year') is False:
                checks.append({'check': 'prior_year_table', 'ok': True, 'detail': f"'{t.get('title')}' is a prior-year table - excluded"})
            if ptotal is not None and persons:
                ok = abs(sum(persons) - ptotal) <= max(1.0, len(persons))
                checks.append({'check': 'rows_sum_to_printed_total', 'ok': ok, 'table': t.get('title'),
                               'detail': f"rows add to {sum(persons):,.0f}; printed total {ptotal:,.0f}"})
                t['reconciled'] = ok
        for p in data.get('policy', []) or []:
            mark(p, _check_item(p, idx, None, ('value',)))
        for b in data.get('ned_benefits', []) or []:
            mark(b, _check_item(b, idx))
        ceo = data.get('ceo')
        if ceo:
            mark(ceo, _check_item(ceo, idx, 'name', ('salary', 'allowances', 'bonus', 'non_cash_benefits', 'pension', 'total')))
    return {'data': data, 'summary': {'items': n_items, 'verified': n_ok,
                                      'unverified': n_items - n_ok,
                                      'checks_failed': sum(1 for c in checks if not c['ok'])},
            'checks': checks}
