"""dynamic_extract.py - layout-independent extraction of the Survey fields.

WHY THIS EXISTS
    The older readers (board_extract.py, policy_extract.py, pdf_parse.py) each recognise ONE layout.
    A report that prints its board as cards, its pay table rotated 90 degrees, or its diversity data as
    a stat block ("7 Male / 2 Female") fell straight through every one of them, so a typical report
    filled about 10% of the 40 survey fields.

    This module does not look for a layout. It looks for STRUCTURAL SIGNALS that survive any layout:

      1. stated sentences         "the Board comprised nine members", "held six Board meetings"
      2. stat blocks              "7 Male", "2 Female", "4 Kenyans" printed as big numbers with a label
      3. a director roster        assembled from EVERY list that names directors (attendance cards or
                                  tables, the directors' report table with nationality, profile
                                  pages with "Boards: Independent Non-Executive Director ...") and
                                  joined by surname + first initial, so no single list has to be complete
      4. committee blocks         a committee title followed by member attendance ratios (3/5)
      5. pay tables               columns classified by the WORDS in the header (salary, fees, pension,
                                  bonus ...), rows read as "name then the values that follow it" - works
                                  for one-line rows, one-value-per-line rows and rotated tables; a row
                                  that lost a dash is re-aligned using the table's own Total column
      6. rate statements          "Chairman retainer of KShs 1.2 million", "sitting allowance of
                                  KShs 20,000 per meeting"

    Every value carries the page, the exact text it came from, and a confidence, and values are
    cross-checked against each other (male + female must equal the board size, and so on) before they
    are trusted. Nothing is guessed: a field the report does not state stays empty.

    The module is pure (no Flask, no database). app.py decides what to write and never overwrites a
    value a person typed or a higher-confidence source produced.
"""
import re
import statistics
from collections import Counter

# ----------------------------------------------------------------------------- page loading

def load_pages(pdf_bytes):
    """One plain-text string per page, in reading order. PyMuPDF first (fast, and it keeps a rotated
    table's rows together); pdfplumber as a fallback."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype='pdf')
        try:
            return [p.get_text('text') for p in doc]
        finally:
            doc.close()
    except Exception:
        pass
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [(p.extract_text() or '') for p in pdf.pages]


# ----------------------------------------------------------------------------- small helpers

_WORD_NUM = {
    'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8,
    'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
    'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20, 'twenty-one': 21,
    'twenty-two': 22, 'twenty-three': 23, 'twenty-four': 24, 'twenty-five': 25,
}
_NUM_TOKEN = r'(?:\d{1,3}|' + '|'.join(sorted(_WORD_NUM, key=len, reverse=True)) + r')'


def _to_int(tok):
    tok = tok.strip().lower().replace('\u2011', '-').replace('\u2010', '-')
    if tok.isdigit():
        return int(tok)
    return _WORD_NUM.get(tok)


def _clean(s):
    return re.sub(r'\s+', ' ', (s or '').replace('\u2019', "'").replace('\u2018', "'")).strip()


def _norm_line(s):
    return (s or '').replace('\u2019', "'").replace('\u2011', '-').replace('\u00a0', ' ').strip()


def _lines(page_text):
    return [_norm_line(l) for l in (page_text or '').split('\n')]


_HON = r'(?:Mr|Mrs|Ms|Miss|Mdm|Madam|Dr|Prof|Professor|Eng|Hon|Amb|Ambassador|Rev|Sir|Capt|Gen|Col|Maj|CPA|Justice|Judge|Sen|Rtd|Lt)'
_NAME_RE = re.compile(r'^(?P<hon>' + _HON + r')\.?\s+(?P<name>[A-Za-z][A-Za-z.\'\-]*(?:\s+[A-Za-z][A-Za-z.\'\-]*){0,5})(?P<star>\s*\*{1,3})?')
_NUM_RE = re.compile(r'^\(?-?\d[\d,]*(?:\.\d+)?\)?$')
_DASH_TOKENS = {'-', '\u2013', '\u2014', 'nil', 'Nil', 'NIL'}


def _is_value_token(t):
    return t in _DASH_TOKENS or bool(_NUM_RE.match(t))


def _value(t):
    if t in _DASH_TOKENS:
        return 0.0
    neg = t.startswith('(') and t.endswith(')')
    v = float(t.strip('()').replace(',', ''))
    return -v if neg else v


def _surname_key(name):
    """('baiya', 'e') - surname + first initial, for joining the same director across lists."""
    n = _NAME_RE.match(name.strip()) if _NAME_RE.match(name.strip()) else None
    raw = n.group('name') if n else name
    raw = re.split(r'[,(]', raw)[0]
    raw = re.sub(_HON + r'\.?\s+', '', raw.strip(), flags=re.I)
    toks = [t.lower().strip('.').replace("'", '') for t in raw.replace('-', ' - ').split() if t.strip('.')]
    toks = [t for t in toks if t and t != '-']
    if not toks:
        return None
    return toks[-1], toks[0][0]


def _same_person(a, b):
    ka, kb = _surname_key(a), _surname_key(b)
    if not ka or not kb:
        return False
    return ka == kb


def _strip_honorific(name):
    m = _NAME_RE.match(name.strip())
    if m:
        return _clean(m.group('name'))
    return _clean(re.sub(r'^' + _HON + r'\.?\s+', '', name.strip(), flags=re.I))


_DEMONYMS = (
    'Kenyan', 'Ugandan', 'Tanzanian', 'Rwandan', 'Burundian', 'Ethiopian', 'Somali', 'South Sudanese', 'Sudanese',
    'Congolese', 'Zambian', 'Zimbabwean', 'Malawian', 'Mozambican', 'South African', 'Namibian', 'Botswana',
    'Nigerian', 'Ghanaian', 'Egyptian', 'Moroccan', 'Mauritian', 'Seychellois', 'Cameroonian', 'Ivorian',
    'British', 'American', 'Canadian', 'Australian', 'Irish', 'French', 'German', 'Dutch', 'Belgian', 'Swiss',
    'Swedish', 'Norwegian', 'Danish', 'Italian', 'Spanish', 'Indian', 'Pakistani', 'Chinese', 'Japanese',
    'Singaporean', 'Emirati', 'Lebanese', 'Israeli', 'Turkish', 'Korean', 'New Zealander', 'Scottish', 'Welsh',
)
_DEMONYM_RE = re.compile(r'^(?:' + '|'.join(sorted((re.escape(d) for d in _DEMONYMS), key=len, reverse=True)) + r')s?$', re.I)
_TRAILING_DEMONYM_RE = re.compile(r'\b(' + '|'.join(sorted((re.escape(d) for d in _DEMONYMS), key=len, reverse=True)) + r')\s*$', re.I)


def _is_kenyan(nat):
    return bool(nat) and nat.strip().lower().startswith('kenya')


# ----------------------------------------------------------------------------- result container

class _Result:
    def __init__(self):
        self.fields = {}        # col -> {'value','page','text','confidence','method'}
        self.notes = []
        self.rejections = []    # [{'field','reason','value','page','text'}] - candidates found but not used (extraction report)

    def reject(self, col, reason, value=None, page=None, text=''):
        self.rejections.append({'field': col, 'reason': reason, 'value': value, 'page': page, 'text': _clean(text or '')[:160]})

    def offer(self, col, value, page, text, confidence, method):
        if value is None:
            return
        cur = self.fields.get(col)
        if cur is None or confidence > cur['confidence']:
            if cur is not None:
                self.reject(col, f"an earlier candidate (page {cur['page']}, {cur['method']}, confidence {cur['confidence']}) "
                                 f"was replaced by a better one", cur['value'], cur['page'], cur['text'])
            self.fields[col] = {'value': value, 'page': page, 'text': _clean(text)[:220],
                                'confidence': round(confidence, 2), 'method': method}
        else:
            self.reject(col, f"candidate from {method} (confidence {round(confidence, 2)}) lost to the kept value "
                             f"(page {cur['page']}, {cur['method']}, confidence {cur['confidence']})", value, page, text)


# ----------------------------------------------------------------------------- page relevance

def _is_governance_page(t):
    tl = t.lower()
    return ('board' in tl or 'directors' in tl) and not re.search(r'\bnotes? to the (?:consolidated )?financial statements\b', tl[:600])


# ----------------------------------------------------------------------------- 1. stated sentences

_SENT_SPLIT = re.compile(r'(?<=[.;!?])\s+|\n\s*\n')


def _sentences(page_text):
    flat = re.sub(r'[ \t]*\n[ \t]*', ' ', page_text or '')
    return [s.strip() for s in _SENT_SPLIT.split(flat) if s.strip()]


def _extract_stated(pages, res):
    for pi, t in enumerate(pages, start=1):
        if not _is_governance_page(t):
            continue
        for s in _sentences(t):
            sl = s.lower()
            if len(s) > 500:
                continue
            # board size
            m = re.search(r'\b(?:board|group board)\b[^.]{0,40}?\b(?:comprised|comprises|consisted of|consists of|is composed of|was composed of|has|had|is made up of)\s+(' + _NUM_TOKEN + r')\s+(?:\w+\s+){0,2}?(?:members|directors)\b', s, re.I)
            if m and 'committee' not in sl[:m.start(1)] and _to_int(m.group(1)) and 3 <= _to_int(m.group(1)) <= 25:
                res.offer('board_size', _to_int(m.group(1)), pi, s, 0.85, 'stated sentence')
            # executive / non-executive counts
            m = re.search(r'(' + _NUM_TOKEN + r')\s+(?:independent\s+)?non-?\s?executive\s+directors?', s, re.I)
            if m and re.search(r'\bboard\b', sl) and not re.search(r'committee', sl):
                n = _to_int(m.group(1))
                e = re.search(r'(' + _NUM_TOKEN + r')\s+executive\s+directors?', re.sub(r'non-?\s?executive', 'NX', s, flags=re.I), re.I)
                if n is not None and e and _to_int(e.group(1)) is not None:
                    res.offer('non_executive_directors_count', n, pi, s, 0.85, 'stated sentence')
                    res.offer('executive_directors_count', _to_int(e.group(1)), pi, s, 0.85, 'stated sentence')
            m = re.search(r'(' + _NUM_TOKEN + r')\s+(?:of\s+(?:whom|them)\s+(?:are|were)\s+)?independent\s+non-?\s?executive', s, re.I)
            if m and re.search(r'\bboard\b', sl) and not re.search(r'committee', sl):
                n = _to_int(m.group(1))
                if n is not None:
                    res.offer('independent_neds_count', n, pi, s, 0.8, 'stated sentence')
            # board meetings
            m = re.search(r'\b(?:board|directors)\b[^.]{0,60}?\b(?:held|had|met)\b[^.]{0,25}?\b(' + _NUM_TOKEN + r')\s+(?:scheduled\s+|regular\s+|full\s+|ordinary\s+)?(?:board\s+)?meetings', s, re.I)
            if m and 'committee' not in sl and _to_int(m.group(1)) is not None and 1 <= _to_int(m.group(1)) <= 40:
                res.offer('board_meetings_per_year', _to_int(m.group(1)), pi, s, 0.85, 'stated sentence')
            # number of committees
            m = re.search(r'\b(' + _NUM_TOKEN + r')\s+(?:standing\s+|principal\s+|main\s+|board\s+)?(?:board\s+)?committees\b', s, re.I)
            if m and re.search(r'\bboard\b', sl) and _to_int(m.group(1)) is not None and 2 <= _to_int(m.group(1)) <= 12 \
                    and re.search(r'\b(?:has|have|had|established|constituted|operates|maintains|comprises|supported by)\b', sl):
                res.offer('committees_per_board', _to_int(m.group(1)), pi, s, 0.7, 'stated sentence')
            # average age
            m = re.search(r'average age\b[^.]{0,60}?\b(?:is|was|of|stood at)\s+(\d{2}(?:\.\d)?)', s, re.I)
            if m and re.search(r'director|board', sl):
                age = float(m.group(1))
                if 25 <= age <= 90:
                    if re.search(r'independent', sl):
                        res.offer('avg_age_independent_neds', age, pi, s, 0.7, 'stated sentence')
                    elif re.search(r'non-?\s?executive', sl):
                        res.offer('avg_age_non_executive_directors', age, pi, s, 0.7, 'stated sentence')
                    elif re.search(r'\bexecutive', sl):
                        res.offer('avg_age_executive_directors', age, pi, s, 0.7, 'stated sentence')


# ----------------------------------------------------------------------------- 2. stat blocks

def _stat_pairs(text):
    """Every '<number> <label>' pair, whether on one line ('7 Male'), split over two lines ('7' / 'Male'),
    or written label-first ('Male: 7')."""
    pairs = []
    flat = _norm_line(text)
    for m in re.finditer(r'(?<![\w/.%])(\d{1,3})\s*(?:\n|\s)\s*([A-Za-z][A-Za-z\- ]{2,40}?)(?=\s*(?:\n|$|\d|\|))', flat):
        pairs.append((int(m.group(1)), _clean(m.group(2)), m.start()))
    for m in re.finditer(r'\b([A-Za-z][A-Za-z\- ]{2,30}?)\s*[:\-\u2013]\s*(\d{1,3})\b(?!\s*%)', flat):
        pairs.append((int(m.group(2)), _clean(m.group(1)), m.start()))
    return pairs


def _extract_stat_blocks(pages, res):
    for pi, t in enumerate(pages, start=1):
        tl = t.lower()
        if not re.search(r'board|directors', tl):
            continue
        if not re.search(r'\bdemographic|\bdiversity|\bgender|\bnationalit|\bindependen|\bboard composition', tl):
            continue
        # blocks are anchored at their heading so a workforce chart on the same page is not read
        for hm in re.finditer(r'(board demographics|board diversity|gender diversity|gender\b|board composition|nationalit(?:y|ies)|independence)', t, re.I):
            seg = t[hm.start(): hm.start() + 450]
            if re.search(r'employee|staff|workforce|management team|senior management', seg[:200], re.I):
                continue
            male = female = None
            for n, label, pos in _stat_pairs(seg):
                ll = label.lower()
                if re.fullmatch(r'(?:males?|men)', ll):
                    male = (n, pos)
                elif re.fullmatch(r'(?:females?|women)', ll):
                    female = (n, pos)
            if male and female and 3 <= male[0] + female[0] <= 30:
                res.offer('directors_male', male[0], pi, f'{male[0]} Male / {female[0]} Female (board demographics)', 0.85, 'stat block')
                res.offer('directors_female', female[0], pi, f'{male[0]} Male / {female[0]} Female (board demographics)', 0.85, 'stat block')
        # nationalities block: counts per nationality (used to cross-check the roster, not written directly)
        nm = re.search(r'NATIONALIT(?:Y|IES)\s*\n((?:\s*\d{1,2}\s+[A-Za-z][A-Za-z ]{2,20}\s*\n){2,12})', t, re.I)
        if nm:
            rows = re.findall(r'(\d{1,2})\s+([A-Za-z][A-Za-z ]{2,20})', nm.group(1))
            res.notes.append(('nationality_block', pi, [(int(n), _clean(l)) for n, l in rows]))


# ----------------------------------------------------------------------------- 3. roster

_STATUS_RE = re.compile(r'^(CHAIR(?:MAN|PERSON|WOMAN)?|MEMBER|RETIRED|RESIGNED|DECEASED|INVITEE|SECRETARY|ALTERNATE|VICE[- ]CHAIR(?:MAN|PERSON)?)\b[^\d]*?(?:[\u2022\u00b7\-|,]\s*)?(\d{1,2})\s+MEETINGS?', re.I)
_ATT_RATIO_RE = re.compile(r'^(\d{1,2})\s*/\s*(\d{1,2})$')


def _attendance_cards(pages):
    """'Prof. Isaac Macharia / CHAIRPERSON . 6 MEETINGS / 5/6' - one card per director."""
    cards = []
    for pi, t in enumerate(pages, start=1):
        if not re.search(r'meeting', t, re.I):
            continue
        L = _lines(t)
        for i, line in enumerate(L):
            nm = _NAME_RE.match(line)
            if not nm or i + 1 >= len(L):
                continue
            st = _STATUS_RE.match(L[i + 1])
            if not st:
                continue
            ratio = None
            if i + 2 < len(L):
                rm = _ATT_RATIO_RE.match(L[i + 2])
                if rm:
                    ratio = (int(rm.group(1)), int(rm.group(2)))
            cards.append({'name': line.strip(' *'), 'status': st.group(1).upper(), 'meetings': int(st.group(2)),
                          'attended': ratio[0] if ratio else None, 'page': pi})
    return cards


def _directors_table(pages):
    """The directors' report list: Name / role text / (Retired June 2025) / Nationality."""
    out = []
    for pi, t in enumerate(pages, start=1):
        L = _lines(t)
        head = next((i for i, l in enumerate(L) if re.fullmatch(r'nationality', l, re.I)), None)
        if head is None:
            continue
        cur = None
        exec_note = any(re.match(r'^\*+\s*executive directors?', l, re.I) for l in L)
        for l in L[head + 1:]:
            if not l:
                continue
            if re.match(r'^\*+\s*executive directors?', l, re.I) or re.match(r'^(?:impact|directors[\u2019\']? report)', l, re.I):
                break
            nm = _NAME_RE.match(l)
            if nm:
                if cur:
                    out.append(cur)
                cur = {'name': l.strip(' *'), 'exec_marked': bool(nm.group('star')) and exec_note, 'retired': False,
                       'role_text': '', 'nationality': None, 'page': pi}
                tail = _TRAILING_DEMONYM_RE.search(l)
                if tail and not _DEMONYM_RE.match(l):
                    cur['nationality'] = tail.group(1).title()
                continue
            if cur is None:
                continue
            if re.match(r'^\((?:retired|resigned|deceased|ceased|left)', l, re.I):
                cur['retired'] = True
                continue
            if re.match(r'^\((?:appointed|effective|elected)', l, re.I):
                continue
            if _DEMONYM_RE.match(l):
                cur['nationality'] = l.strip().title()
                continue
            if len(l) < 90 and not re.search(r'\d{3,}', l):
                cur['role_text'] = (cur['role_text'] + ' ' + l).strip()
        if cur:
            out.append(cur)
        if len(out) >= 3:
            break
        out = []
    return out


def _profiles(pages, company_name=None):
    """'DR. LAKSHMI SHYAM-SUNDER / Boards: Independent Non-Executive Director, Equity Group Holdings PLC'."""
    out = []
    key = None
    if company_name:
        toks = [w for w in re.split(r'\W+', company_name.lower()) if w and w not in ('plc', 'ltd', 'limited', 'group', 'holdings', 'the', 'of', 'and')]
        key = toks[0] if toks else None
    for pi, t in enumerate(pages, start=1):
        L = _lines(t)
        for i, line in enumerate(L):
            if not re.match(r'^(?:' + _HON + r')\.?\s+[A-Za-z][A-Za-z.\'\- ]+(?:,\s*[A-Za-z][A-Za-z,.\s]{1,15})?$', line, re.I) or line != line.upper():
                continue
            block = ' '.join(L[i + 1:i + 4])
            bm = re.match(r'^\s*(?:Boards?|Board\s+Positions?|Directorships?)\s*[:\-]+\s*(.+)$', block, re.I)
            if not bm:
                continue
            desig = bm.group(1)
            phrases = [p.strip() for p in re.split(r',\s+(?=[A-Z])', desig)]
            chosen = phrases[0] if phrases else desig
            if key:
                for p in range(len(phrases) - 1):
                    if key in phrases[p + 1].lower() and re.search(r'director|chair|executive|officer', phrases[p], re.I):
                        chosen = phrases[p]
                        break
            body = ' '.join(L[i + 1:i + 30])
            he = len(re.findall(r'\b(?:He|His|Him)\b', body[:900]))
            she = len(re.findall(r'\b(?:She|Her|Hers)\b', body[:900]))
            age = re.search(r'\b(?:aged?|age of)\s+(\d{2})\b', body[:1500], re.I)
            out.append({'name': line.split(',')[0].strip(), 'designation': _clean(chosen), 'page': pi,
                        'pronoun_gender': 'Male' if he > she else 'Female' if she > he else None,
                        'age': int(age.group(1)) if age else None})
    return out


def _role_of(designation, exec_marked=False, role_text=''):
    d = (designation or '') + ' ' + (role_text or '')
    dl = d.lower()
    if re.search(r'non-?\s?executive', dl):
        return 'non_executive'
    if re.search(r'chief executive|managing director|executive director|\bceo\b|\bgceo\b|group md', dl) or exec_marked:
        return 'executive'
    if re.search(r'chair(?:man|person|woman)?\b', dl):
        return 'non_executive'
    return None


def _gender_of(name, pronoun_gender):
    m = _NAME_RE.match(name.strip())
    hon = (m.group('hon') if m else '').lower().strip('.')
    if hon in ('mr', 'sir'):
        return 'Male'
    if hon in ('mrs', 'ms', 'miss', 'mdm', 'madam'):
        return 'Female'
    return pronoun_gender


def build_roster(pages, company_name=None):
    """Join every list of directors into one roster of the CURRENT board."""
    cards = _attendance_cards(pages)
    table = _directors_table(pages)
    profiles = _profiles(pages, company_name)

    # anchor list: the one that best represents the board itself
    people = []
    if len([c for c in cards if c['status'] != 'RETIRED']) >= 3:
        for c in cards:
            people.append({'name': c['name'], 'retired': c['status'] in ('RETIRED', 'RESIGNED', 'DECEASED'),
                           'chair': c['status'].startswith('CHAIR'), 'page': c['page'], 'source': 'attendance card'})
    elif len(table) >= 3:
        for d in table:
            people.append({'name': d['name'], 'retired': d['retired'],
                           'chair': bool(re.search(r'chair', d['role_text'], re.I)), 'page': d['page'], 'source': 'directors table'})
    else:
        return []

    for p in people:
        tb = next((d for d in table if _same_person(d['name'], p['name'])), None)
        pr = next((d for d in profiles if _same_person(d['name'], p['name'])), None)
        p['nationality'] = tb['nationality'] if tb else None
        if tb and tb['retired']:
            p['retired'] = True
        p['role_text'] = tb['role_text'] if tb else ''
        if tb and re.search(r'chair', tb['role_text'], re.I):
            p['chair'] = True
        p['designation'] = pr['designation'] if pr else None
        p['role'] = _role_of(p['designation'], tb['exec_marked'] if tb else False, p['role_text'])
        p['independent'] = (True if re.search(r'independent', p['designation'] or '', re.I) else
                            (False if p['designation'] else None))
        p['gender'] = _gender_of(p['name'], pr['pronoun_gender'] if pr else None)
        p['age'] = pr['age'] if pr else None
        p['profile_page'] = pr['page'] if pr else None
    return people


def _extract_from_roster(roster, pages, res):
    current = [p for p in roster if not p['retired']]
    if len(current) < 3:
        return current
    pg = current[0]['page']
    n = len(current)
    # board size: only trusted when no sentence states it (sentences are offered at 0.85)
    res.offer('board_size', n, pg, f"{n} current directors named in the attendance / directors lists (retired directors excluded)", 0.6, 'roster')

    roles = [p['role'] for p in current]
    if all(roles):
        ex = sum(1 for r in roles if r == 'executive')
        res.offer('executive_directors_count', ex, pg, f'{ex} executive directors among the {n} current directors', 0.75, 'roster')
        res.offer('non_executive_directors_count', n - ex, pg, f'{n - ex} non-executive directors among the {n} current directors', 0.75, 'roster')
    neds = [p for p in current if p['role'] == 'non_executive']
    if not all(roles):
        neds = []                       # a director with an unknown role makes every NED count unreliable
    if neds and all(p['independent'] is not None for p in neds):
        ind = sum(1 for p in neds if p['independent'])
        res.offer('independent_neds_count', ind, pg, f"{ind} of {len(neds)} non-executive directors are described as Independent Non-Executive Directors in their profiles", 0.8, 'roster')
        res.offer('non_independent_neds_count', len(neds) - ind, pg, f"{len(neds) - ind} of {len(neds)} non-executive directors are not described as independent", 0.8, 'roster')
    if neds and all(p['nationality'] for p in neds):
        ken = sum(1 for p in neds if _is_kenyan(p['nationality']))
        res.offer('neds_kenyan_count', ken, pg, f'{ken} of {len(neds)} non-executive directors are Kenyan (directors table, nationality column)', 0.8, 'roster')
        res.offer('neds_non_kenyan_count', len(neds) - ken, pg, f'{len(neds) - ken} of {len(neds)} non-executive directors hold another nationality', 0.8, 'roster')
    if all(p['gender'] for p in current):
        f = sum(1 for p in current if p['gender'] == 'Female')
        res.offer('directors_female', f, pg, f'{f} women among the {n} current directors (titles and profile pronouns)', 0.7, 'roster')
        res.offer('directors_male', n - f, pg, f'{n - f} men among the {n} current directors (titles and profile pronouns)', 0.7, 'roster')

    def avg_age(group):
        ages = [p['age'] for p in group if p['age']]
        return round(statistics.mean(ages), 1) if group and len(ages) == len(group) else None
    a = avg_age([p for p in current if p['role'] == 'executive'])
    if a:
        res.offer('avg_age_executive_directors', a, pg, 'mean of the executive directors\' stated ages', 0.7, 'roster')
    a = avg_age(neds)
    if a:
        res.offer('avg_age_non_executive_directors', a, pg, 'mean of the non-executive directors\' stated ages', 0.7, 'roster')
    a = avg_age([p for p in neds if p['independent']])
    if a:
        res.offer('avg_age_independent_neds', a, pg, 'mean of the independent NEDs\' stated ages', 0.7, 'roster')
    a = avg_age([p for p in neds if p['independent'] is False])
    if a:
        res.offer('avg_age_non_independent_neds', a, pg, 'mean of the non-independent NEDs\' stated ages', 0.7, 'roster')
    return current


def _cross_validate(res, current):
    """Sanity checks between fields; a failed check lowers confidence instead of silently trusting."""
    f = res.fields
    bs = f.get('board_size')
    m, w = f.get('directors_male'), f.get('directors_female')
    if bs and m and w and m['value'] + w['value'] != bs['value']:
        # the stat block and the board size disagree: trust the one the roster supports
        if current and len(current) == m['value'] + w['value']:
            f['board_size'] = {**bs, 'value': len(current), 'confidence': 0.75, 'method': 'roster (agrees with gender block)',
                               'text': f"{len(current)} current directors; male + female block also totals {len(current)}"}
        else:
            for k in ('directors_male', 'directors_female'):
                f[k]['confidence'] = min(f[k]['confidence'], 0.5)
                res.reject(k, f"confidence lowered to 0.5: male + female ({m['value'] + w['value']}) does not equal the "
                              f"board size ({bs['value']}) and the roster could not settle it", f[k]['value'], f[k]['page'], f[k]['text'])
    if bs and f.get('non_executive_directors_count') and f.get('executive_directors_count'):
        tot = f['non_executive_directors_count']['value'] + f['executive_directors_count']['value']
        if tot != bs['value']:
            for k in ('non_executive_directors_count', 'executive_directors_count'):
                f[k]['confidence'] = min(f[k]['confidence'], 0.5)
                res.reject(k, f"confidence lowered to 0.5: executive + non-executive ({tot}) does not equal the board size "
                              f"({bs['value']})", f[k]['value'], f[k]['page'], f[k]['text'])
    if f.get('independent_neds_count') and f.get('non_executive_directors_count') \
            and f['independent_neds_count']['value'] > f['non_executive_directors_count']['value']:
        _ind = f['independent_neds_count']
        res.reject('independent_neds_count', f"dropped: independent NEDs ({_ind['value']}) exceeds all non-executive "
                   f"directors ({f['non_executive_directors_count']['value']})", _ind['value'], _ind['page'], _ind['text'])
        del f['independent_neds_count']
        f.pop('non_independent_neds_count', None)
    # nationality block agrees with roster?
    for n in res.notes:
        if n[0] == 'nationality_block' and current and f.get('board_size'):
            if sum(c for c, _ in n[2]) != f['board_size']['value']:
                res.notes.append(('warning', n[1], 'nationality block total differs from board size'))


# ----------------------------------------------------------------------------- 4. committees

_COMMITTEE_TITLE_RE = re.compile(r'^(?:THE\s+)?(?P<n>[A-Za-z][A-Za-z,&\'\- ]{2,80}?\bCOMMITTEE)\s*(?:\((?P<a>[A-Za-z&]{2,8})\))?\s*$', re.I)


def extract_committees(pages):
    """[{'name','meetings','members','page'}] - one entry per committee that has an attendance block."""
    out = []
    seen = set()
    for pi, t in enumerate(pages, start=1):
        if not re.search(r'attendance|meeting eligibility|attended', t, re.I):
            continue
        L = _lines(t)
        titles = []
        for i, l in enumerate(L):
            m = _COMMITTEE_TITLE_RE.match(l)
            if m and len(l) < 90 and not re.match(r'^(?:board\s+)?committees?$', l, re.I) \
                    and not re.search(r'^% of committee', l, re.I):
                titles.append((i, _clean(re.sub(r'^THE\s+', '', l, flags=re.I))))
        if not titles:
            continue
        for k, (start, name) in enumerate(titles):
            end = titles[k + 1][0] if k + 1 < len(titles) else len(L)
            ratios = [(int(m.group(1)), int(m.group(2))) for l in L[start:end] for m in [_ATT_RATIO_RE.match(l)] if m]
            if not ratios:
                continue
            key = re.sub(r'\W+', '', name.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({'name': name, 'meetings': max(b for _, b in ratios), 'members': len(ratios), 'page': pi})
    return out


def _extract_committee_fields(pages, res):
    comms = extract_committees(pages)
    if comms:
        names = ', '.join(c['name'].title() for c in comms)
        res.offer('committees_per_board', len(comms), comms[0]['page'],
                  f'{len(comms)} board committees with attendance tables: {names}', 0.8, 'committee blocks')
        # same definition as app._sync_committee_survey_fields: the AVERAGE meetings held per committee
        avg = round(sum(c['meetings'] for c in comms) / len(comms))
        detail = '; '.join(f"{c['name'].title()} {c['meetings']}" for c in comms)
        res.offer('committee_meetings_per_year', avg, comms[0]['page'],
                  f'average meetings held per committee ({detail})', 0.7, 'committee blocks')
    return comms


# ----------------------------------------------------------------------------- 5. pay tables

_STARTERS = {'salary', 'salaries', 'basic', 'fee', 'fees', 'retainer', 'sitting', 'pension', 'bonus', 'gratuity', 'expense',
             'expenses', 'leave', 'estimated', 'other', 'total', 'medical', 'housing', 'incentive', 'benefits-in-kind',
             'emoluments', 'allowance', 'allowances', 'performance', 'wages', 'perquisites', 'commission'}
_LABEL_TEXT_RE = re.compile(r'^[A-Za-z][A-Za-z\s/&\-\'.()]*$')


def _classify_label(label):
    l = label.lower()
    if re.search(r'\btotal\b|emolument|aggregate', l):
        return 'total'
    if re.search(r'non-?\s?cash|benefits? in kind|estimated value|perquisite|in kind', l):
        return 'noncash'
    if re.search(r'\bleave\b', l):
        return 'leave'
    if re.search(r'defer', l):
        return 'deferred'
    if re.search(r'long-?\s?term|\bltip\b|share|option', l):
        return 'share'
    if re.search(r'bonus|incentive|performance|commission', l):
        return 'bonus'
    if re.search(r'gratuity|severance|service pay|terminal', l):
        return 'gratuity'
    if re.search(r'pension|provident|retirement|superannuation|nssf|contribution', l):
        return 'pension'
    if re.search(r'salary|salaries|basic|wages|remuneration', l):
        return 'salary'
    if re.search(r'\bfees?\b|retainer|honorari|sitting', l):
        return 'fees'
    if re.search(r'allowance|expense|housing|transport|per diem|medical|other', l):
        return 'allowances'
    return 'other'


_HEADER_STOP = re.compile(r'^(?:year ended|for the year|shs?|ksh|kes|sh\.|\d{4}$|note\b|audited|information)', re.I)


def _split_header_labels(lines):
    """Turn the header lines into one label per COLUMN. A word that starts a column ('Salary', 'Fees',
    'Leave', 'Estimated', 'Total') opens a new label; continuation words ('allowances', 'for non-cash
    benefits') attach to the label they follow. Works for one-line headers and one-word-per-line ones."""
    labels = []
    for l in lines:
        for w in l.split():
            wl = w.lower().strip('.,:;()')
            if not wl:
                continue
            starts = wl in _STARTERS
            # 'allowances' straight after 'expense'/'other'/'leave' continues that column
            if wl in ('allowance', 'allowances') and labels and re.search(r'expense|other|leave|housing|transport|medical', labels[-1].lower()) \
                    and not re.search(r'allowance', labels[-1].lower()):
                starts = False
            if wl in ('benefits',) and labels and re.search(r'non-?\s?cash|value|kind', labels[-1].lower()):
                starts = False
            if labels and labels[-1].split()[-1].lower().strip('.,:;()') in ('and', 'for', 'of', '&'):
                starts = False          # 'Leave pay and' + 'leave allowance' is ONE column
            if starts or not labels:
                labels.append(w)
            else:
                labels[-1] += ' ' + w
    return labels


def _detect_year(text):
    m = re.search(r'year\s+ended[^\d]{0,40}(20\d{2})', text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r'\b(20\d{2})\b', text)
    return int(m.group(1)) if m else None


def _detect_pay_unit(text):
    if re.search(r"(?:kes|ksh|kshs|shs?|sh)\s*\.?\s*['`]?\s*000\b|in\s+(?:kes|ksh|kshs|shs)\s+thousands?|thousands?\s+of\s+(?:kes|shillings)|['`]000", text, re.I):
        return 'thousands'
    if re.search(r'(?:kes|ksh|kshs|shs?)\.?\s*(?:m\b|millions?)|in\s+(?:kes|ksh|kshs|shs)\s+millions?|millions?\s+of', text, re.I):
        return 'millions'
    return 'units'


def _read_rows(lines):
    """Every '<honorific name> then the values that follow it'."""
    rows = []
    i = 0
    while i < len(lines):
        line = lines[i]
        nm = _NAME_RE.match(line)
        if not nm:
            i += 1
            continue
        rest = line[nm.end():].split()
        name = line[:nm.end()].strip()
        # name may continue with words that are not values (surname on the same line)
        vals = []
        for tok in rest:
            if _is_value_token(tok):
                vals.append(tok)
            elif not vals and re.match(r"^[A-Za-z'.\-]+$", tok):
                name += ' ' + tok
            else:
                vals = []
                break
        j = i + 1
        while j < len(lines):
            toks = lines[j].split()
            if not toks:
                j += 1
                continue
            if all(_is_value_token(t) for t in toks):
                vals.extend(toks)
                j += 1
                continue
            if re.match(r'^\(.*\)$', lines[j]):     # "(Retired June 2025)" between name and values
                j += 1
                continue
            break
        if len(vals) >= 3:
            rows.append({'name': name.strip(' *'), 'star': bool(nm.group('star')), 'tokens': vals})
        i = max(j, i + 1)
    return rows


def _align(tokens, ncols, total_idx, role_hint, labels_concepts):
    """Place tokens into ncols columns. A row that lost one dash is re-aligned so the components add up
    to the Total and the role fits (an executive's first non-zero value is Salary, a NED's is Fees)."""
    vals = [_value(t) for t in tokens]
    if len(vals) == ncols:
        return vals
    if len(vals) != ncols - 1 or ncols < 3:
        return None
    best, best_score = None, None
    total = vals[-1]
    body = vals[:-1]
    for ins in range(0, ncols - 1):
        cand = body[:ins] + [0.0] + body[ins:] + [total]
        if abs(sum(cand[:-1]) - total) > max(2.0, 0.005 * abs(total)):
            continue
        score = 0
        first_nz = next((k for k, v in enumerate(cand[:-1]) if v), None)
        concept = labels_concepts[first_nz] if first_nz is not None else None
        if role_hint == 'exec' and concept == 'salary':
            score += 2
        if role_hint == 'ned' and concept == 'fees':
            score += 2
        score += ins * 0.01                     # ties: the missing dash is the later column
        if best is None or score > best_score:
            best, best_score = cand, score
    return best


def extract_pay_tables(pages, fiscal_year=None):
    """[{'year','unit','page','columns':[concept...],'rows':[{'name','star','values':{concept:amount}}]}]
    Amounts are in the table's own unit ('thousands' etc.), NOT multiplied out."""
    tables = []
    for pi, t in enumerate(pages, start=1):
        if not re.search(r'remuneration|emoluments|fees', t, re.I):
            continue
        L = _lines(t)
        rows = _read_rows(L)
        if len(rows) < 2:
            continue
        first_name_idx = next((i for i, l in enumerate(L) if _NAME_RE.match(l)), None)
        # header = the label lines just above the first name row
        start = None
        for i in range(first_name_idx or 0):
            w0 = (L[i].split() or [''])[0].lower().strip('.,:;()')
            if w0 in _STARTERS and _LABEL_TEXT_RE.match(L[i] or 'x') and len(L[i]) < 60:
                start = i
                break
        if start is None:
            continue
        hdr = []
        for l in L[start:first_name_idx]:
            if _HEADER_STOP.match(l):
                break
            if _LABEL_TEXT_RE.match(l) and len(l) < 70:
                hdr.append(l)
        labels = _split_header_labels(hdr)
        concepts = [_classify_label(x) for x in labels]
        if len(labels) < 3 or 'total' not in concepts:
            continue
        ncols = len(labels)
        header_text = ' '.join(L[max(0, start - 2):(first_name_idx or 0) + 1])
        year = _detect_year(header_text)
        unit = _detect_pay_unit(' '.join(L[:(first_name_idx or 0) + 3]))
        parsed = []
        for r in rows:
            role_hint = 'exec' if r['star'] else None
            toks = r['tokens']
            if len(toks) > ncols >= 4:
                # the last director's row runs straight into the table's own Total line: keep the first
                # ncols values, and only if they add up to the row's Total (else the row is not trusted)
                first = [_value(x) for x in toks[:ncols]]
                if abs(sum(first[:-1]) - first[-1]) > max(2.0, 0.005 * abs(first[-1])):
                    continue
                toks = toks[:ncols]
            vals = _align(toks, ncols, concepts.index('total'), role_hint, concepts)
            if vals is None and role_hint is None:
                vals = _align(toks, ncols, concepts.index('total'), 'ned', concepts)
            if vals is None:
                continue
            comp = {}
            for c, v in zip(concepts, vals):
                comp[c] = comp.get(c, 0.0) + v
            parsed.append({'name': r['name'], 'star': r['star'], 'values': comp})
        if parsed:
            tables.append({'year': year, 'unit': unit, 'page': pi, 'columns': concepts, 'rows': parsed})
    if not tables:
        return []
    fy = None
    if fiscal_year:
        m = re.search(r'(20\d{2})', str(fiscal_year))
        fy = int(m.group(1)) if m else None
    years = [t['year'] for t in tables if t['year']]
    target = fy if fy in years else (max(years) if years else None)
    return [t for t in tables if t['year'] == target] or tables[:1]


_UNIT_MULT = {'units': 1.0, 'thousands': 1e3, 'millions': 1e6}


def _extract_pay_fields(pages, roster, res, fiscal_year, company_name):
    tables = extract_pay_tables(pages, fiscal_year)
    if not tables:
        return None
    tb = tables[0]
    mult = _UNIT_MULT[tb['unit']]
    rows = [r for r in tb['rows'] if r['values'].get('total', 0) > 0]
    pg = tb['page']

    def is_exec(r):
        v = r['values']
        return r['star'] or ((v.get('salary', 0) > 0 or v.get('bonus', 0) > 0 or v.get('pension', 0) > 0) and v.get('fees', 0) == 0)
    execs = [r for r in rows if is_exec(r)]
    neds = [r for r in rows if not is_exec(r)]

    # ---- CEO / MD: the executive the report calls the CEO / MD, else the best-paid executive
    ceo = None
    ceo_names = [p['name'] for p in roster if re.search(r'chief executive|managing director|\bceo\b', (p.get('designation') or '') + ' ' + (p.get('role_text') or ''), re.I)
                 and not re.search(r'non-?\s?executive', p.get('designation') or '', re.I)]
    for r in execs:
        if any(_same_person(r['name'], n) for n in ceo_names):
            ceo = r
            break
    if ceo is None and execs:
        ceo = max(execs, key=lambda r: r['values'].get('total', 0))
    if ceo:
        v = ceo['values']
        src = (f"{ceo['name']}'s row in the {tb['year'] or ''} directors' pay table (page {pg}, printed in {tb['unit']}): annual figure / 12")
        mp = lambda x: x * mult / 12.0
        def put(col, amount, label):
            res.offer(col, mp(amount), pg, f'{label} - {src}', 0.8, 'pay table')
        if 'salary' in v:
            put('ceo_monthly_salary', v['salary'], 'salary')
            res.offer('ceo_annual_salary_as_stated', v['salary'] * mult, pg, f"annual salary as printed - {src}", 0.8, 'pay table')
            res.offer('ceo_monthly_conversion_basis', 'annual / 12', pg, src, 0.8, 'pay table')
        allow = sum(v.get(k, 0.0) for k in ('allowances', 'leave', 'other'))
        if any(k in v for k in ('allowances', 'leave', 'other')):
            put('ceo_monthly_allowances', allow, 'expense, leave and other allowances combined')
        if 'bonus' in v:
            put('ceo_monthly_incentive_bonus', v['bonus'], 'bonus')
        if 'deferred' in v:
            put('ceo_monthly_deferred_incentive', v['deferred'], 'deferred incentive')
        if 'noncash' in v:
            put('ceo_monthly_non_cash_benefits', v['noncash'], 'estimated value of non-cash benefits')
        if 'pension' in v:
            put('ceo_monthly_pension', v['pension'], 'pension')
        if 'gratuity' in v:
            put('ceo_monthly_gratuity', v['gratuity'], 'gratuity')
        if 'share' in v:
            put('ceo_monthly_share_value', v['share'], 'share-based pay')
        if 'total' in v:
            put('ceo_monthly_cost_of_employment', v['total'], 'total remuneration')

    # ---- NED fees actually paid (a paid fee is not a stated rate, so confidence stays low)
    chair_names = [p['name'] for p in roster if p.get('chair')]
    chair_row = next((r for r in neds if any(_same_person(r['name'], n) for n in chair_names)), None)
    if chair_row and chair_row['values'].get('fees', 0) > 0:
        res.offer('chairperson_annual_retainer', chair_row['values']['fees'] * mult, pg,
                  f"fees paid to the Chairperson {chair_row['name']} for the year (page {pg}, in {tb['unit']}) - a paid amount, not a stated rate",
                  0.4, 'pay table')
    full_year = [p for p in roster if not p['retired'] and not p.get('chair')]
    other_fees = []
    for r in neds:
        if r is chair_row or r['values'].get('fees', 0) <= 0:
            continue
        if any(_same_person(r['name'], p['name']) for p in full_year):
            other_fees.append(r['values']['fees'])
    if len(other_fees) >= 2:
        res.offer('other_ned_annual_retainer', statistics.median(other_fees) * mult, pg,
                  f"median of fees paid to the {len(other_fees)} other current non-executive directors (page {pg}, in {tb['unit']}) - paid amounts, not a stated rate",
                  0.4, 'pay table')
    return {'unit': tb['unit'], 'page': pg, 'year': tb['year'], 'ceo': ceo['name'] if ceo else None}


# ----------------------------------------------------------------------------- 6. rate statements

_AMOUNT_RE = re.compile(r'(?:kshs?|kes|shs?|ksh)\.?\s*([\d][\d,]*(?:\.\d+)?)\s*(million|m\b|thousand|k\b)?', re.I)


def _amount(m):
    v = float(m.group(1).replace(',', ''))
    unit = (m.group(2) or '').lower()
    if unit in ('million', 'm'):
        v *= 1e6
    elif unit in ('thousand', 'k'):
        v *= 1e3
    return v


def _extract_rates(pages, res):
    for pi, t in enumerate(pages, start=1):
        if not re.search(r'retainer|allowance|sitting|per meeting|annual fee|fee of', t, re.I):
            continue
        for s in _sentences(t):
            sl = s.lower()
            if len(s) > 450 or not _AMOUNT_RE.search(s):
                continue
            # executive directors are not paid fees
            if re.search(r'executive directors?[^.]{0,120}(?:not (?:entitled|eligible)|do not (?:earn|receive)|are not paid)[^.]{0,80}(?:fees?|allowances?|sitting)', re.sub(r'non-?\s?executive', 'NX', s, flags=re.I), re.I):
                continue
            m = _AMOUNT_RE.search(s)
            amt = _amount(m)
            monthly = bool(re.search(r'per month|monthly|a month|p\.m', sl))
            per_meeting = bool(re.search(r'per (?:board |committee )?(?:meeting|sitting|day)|sitting|each meeting|attendance', sl))
            annual = bool(re.search(r'per annum|annual|yearly|retainer|p\.a', sl)) and not per_meeting
            if not (per_meeting or annual or monthly):
                continue
            if monthly and not per_meeting:
                amt *= 12
            committee = 'committee' in sl
            chair = bool(re.search(r'\bchair', sl))
            ned = bool(re.search(r'non-?\s?executive|directors?\b|members?\b', sl))
            if not (chair or ned):
                continue
            if committee:
                col = ('committee_chair_meeting_allowance' if chair else 'committee_member_meeting_allowance') if per_meeting else \
                      ('committee_chair_annual_retainer' if chair else 'committee_member_annual_retainer')
            else:
                col = ('chairperson_meeting_allowance' if chair else 'other_ned_meeting_allowance') if per_meeting else \
                      ('chairperson_annual_retainer' if chair else 'other_ned_annual_retainer')
            res.offer(col, amt, pi, s, 0.7, 'stated rate')


def _extract_exec_not_paid(pages, res):
    for pi, t in enumerate(pages, start=1):
        for s in _sentences(t):
            if len(s) > 500:
                continue
            flat = re.sub(r'non-?\s?executive', 'NX', s, flags=re.I)
            if re.search(r'\bexecutive directors?\b[^.]{0,140}?(?:not (?:entitled|eligible)|do not (?:earn|receive)|are not paid|not paid)[^.]{0,100}?(?:fees?|allowances?|sitting)', flat, re.I):
                res.offer('executive_director_annual_retainer', 0.0, pi, s, 0.75, 'stated sentence')
                res.offer('executive_director_meeting_allowance', 0.0, pi, s, 0.75, 'stated sentence')
                return


def _extract_no_share_scheme(pages, res):
    for pi, t in enumerate(pages, start=1):
        for s in _sentences(t):
            if len(s) > 400:
                continue
            if re.search(r'no\s+(?:share\s+options?|share\s+(?:award|incentive|scheme)s?|long-?\s?term incentive)[^.]{0,80}(?:issued|granted|awarded|outstanding)', s, re.I) \
                    and re.search(r'director', s, re.I):
                res.offer('ceo_monthly_share_value', 0.0, pi, s, 0.6, 'stated sentence')
                return


# ----------------------------------------------------------------------------- entry point

def extract_survey_dynamic(pdf_bytes=None, pages=None, fiscal_year=None, company_name=None):
    """Run every strategy and return
        {'fields': {column: {value, page, text, confidence, method}},
         'roster': [current directors], 'all_people': [...], 'committees': [...],
         'pay': {unit, page, year, ceo} | None, 'notes': [...]}
    Money fields are in ABSOLUTE currency units (the caller rescales to the survey row's pay unit)."""
    if pages is None:
        pages = load_pages(pdf_bytes)
    res = _Result()
    _extract_stated(pages, res)
    _extract_stat_blocks(pages, res)
    roster = build_roster(pages, company_name)
    current = _extract_from_roster(roster, pages, res)
    comms = _extract_committee_fields(pages, res)
    pay = _extract_pay_fields(pages, roster, res, fiscal_year, company_name)
    _extract_rates(pages, res)
    _extract_exec_not_paid(pages, res)
    _extract_no_share_scheme(pages, res)
    _cross_validate(res, current)
    return {'fields': res.fields, 'roster': current, 'all_people': roster, 'committees': comms, 'pay': pay, 'notes': res.notes,
            'rejections': res.rejections}


ALL_SURVEY_FIELDS = (
    'board_size', 'board_meetings_per_year', 'committees_per_board', 'committee_meetings_per_year', 'directors_female',
    'directors_male', 'executive_directors_count', 'non_executive_directors_count', 'independent_neds_count',
    'non_independent_neds_count', 'neds_kenyan_count', 'neds_non_kenyan_count', 'avg_age_executive_directors',
    'avg_age_non_executive_directors', 'avg_age_independent_neds', 'avg_age_non_independent_neds',
    'chairperson_annual_retainer', 'other_ned_annual_retainer', 'chairperson_meeting_allowance',
    'other_ned_meeting_allowance', 'executive_director_annual_retainer', 'executive_director_meeting_allowance',
    'committee_chair_annual_retainer', 'committee_member_annual_retainer', 'committee_chair_meeting_allowance',
    'committee_member_meeting_allowance', 'ceo_monthly_salary', 'ceo_monthly_allowances', 'ceo_monthly_incentive_bonus',
    'ceo_monthly_deferred_incentive', 'ceo_monthly_non_cash_benefits', 'ceo_monthly_pension', 'ceo_monthly_gratuity',
    'ceo_monthly_share_value', 'ceo_monthly_cost_of_employment', 'ceo_annual_salary_as_stated',
    'ceo_monthly_conversion_basis',
)
