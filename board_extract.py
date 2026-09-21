"""Board-side extraction for the Survey Report tabs.

Before this module, an uploaded annual report filled the financial
statements and director pay tables but nothing on the board itself, so
Board Composition, the Directors' Register (gender, nationality,
appointment), Committees and "Board meetings held" all read "Not
available". Two layouts are read here, both by WORD POSITION (column
bands taken from the table's own header row), because both are real
tables whose cells wrap over several lines and defeat line-by-line regex:

1. extract_board_attendance()    "Directors' attendance at scheduled Board
   and Board Committee meetings": a header row of committee names, a row of
   "total number of scheduled meetings", then one row per director with
   "attended(eligible)" cells such as 4(4) or 2(2). Yields the number of
   Board meetings, every committee with its meetings and members, and each
   committee's attendance rate computed from the filed table.
   (Confirmed on Absa Bank Kenya's 2025 report, p.113.)

2. extract_board_register_table() "Director / Skills / Category / Age /
   Date of appointment / Tenure / Nationality / Gender" board table.
   Yields role, independence, age, appointment date, nationality and
   gender per director - all as printed in the table's own columns.
   (Confirmed on Absa Bank Kenya's 2022 report, p.45.)

derive_board_composition() turns a register into the SurveyCompanyData
counts (executive / non-executive / independent / female / Kenyan / average
ages). It only counts what a table row states: a director whose row has no
gender is not counted as male or female, and a count is only produced when
EVERY row states the fact, so a partial table never yields a wrong total.

Nothing here guesses. A page that does not have the expected header
returns None / [] and the caller leaves the fields empty.
"""
import difflib
import io
import re
from collections import defaultdict

import pdfplumber

from pdf_parse import _pypdf_page_texts_cached

_CELL_RE = re.compile(r"^\d{1,3}\s*\(\s*\d{1,3}\s*\)$")
_INT_RE = re.compile(r"^\d{1,3}$")
_TITLE_TOKENS = {'mr', 'mrs', 'ms', 'miss', 'dr', 'prof', 'hon', 'eng', 'amb', 'rev', 'sir', 'cpa', 'fcpa'}
_NAME_MARKER_TAIL_RE = re.compile(r"[\s*\u2020\u2021\u00a7#^]+$")


# ---------------------------------------------------------------- helpers
def _clean_person_name(text: str) -> str:
    text = _NAME_MARKER_TAIL_RE.sub("", (text or "").strip())
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,.")


def _looks_like_person(name: str) -> bool:
    parts = [p for p in re.split(r"\s+", name) if p]
    if not (2 <= len(parts) <= 6):
        return False
    if any(re.search(r"\d", p) for p in parts):
        return False
    low = name.lower()
    return not any(w in low for w in ('total', 'attended', 'members', 'committee', 'board', 'meeting', 'director'))


def _group_rows(words, tol=3.0):
    """Group words into visual rows by their `top`."""
    rows = []
    for w in sorted(words, key=lambda w: (w['top'], w['x0'])):
        if rows and abs(rows[-1]['top'] - w['top']) <= tol:
            rows[-1]['words'].append(w)
        else:
            rows.append({'top': w['top'], 'words': [w]})
    for r in rows:
        r['words'].sort(key=lambda w: w['x0'])
        r['text'] = " ".join(w['text'] for w in r['words'])
    return rows


def _regions(page):
    """A landscape page that is really two facing pages is read as two
    regions, so a table on one half is not confused by text on the other."""
    w, h = page.width, page.height
    if w > 1.5 * h:
        return [page.crop((0, 0, w / 2, h)), page.crop((w / 2, 0, w, h))]
    return [page]


def _candidate_pages(pdf_bytes: bytes, required_any: list, required_all: list):
    """Page numbers (1-based) whose pypdf text has every phrase in
    `required_all` and at least one in `required_any` - a cheap prefilter so
    the position-based pass only opens a handful of pages."""
    texts = _pypdf_page_texts_cached(pdf_bytes)
    out = []
    if texts is None:
        return out
    for pn, text in texts:
        low = (text or '').lower()
        if all(k in low for k in required_all) and (not required_any or any(k in low for k in required_any)):
            out.append(pn)
    return out


def _merge_cell_words(words):
    """'4' + '(4)' -> '4(4)' when the PDF put them in separate words."""
    merged, used = [], set()
    by_row = _group_rows(words)
    for row in by_row:
        ws = row['words']
        i = 0
        while i < len(ws):
            w = ws[i]
            if (i + 1 < len(ws) and re.fullmatch(r"\d{1,3}", w['text'])
                    and re.fullmatch(r"\(\s*\d{1,3}\s*\)", ws[i + 1]['text'])
                    and ws[i + 1]['x0'] - w['x1'] < 8):
                nxt = ws[i + 1]
                merged.append({'text': w['text'] + nxt['text'], 'x0': w['x0'], 'x1': nxt['x1'],
                               'top': w['top'], 'bottom': max(w['bottom'], nxt['bottom'])})
                i += 2
            else:
                merged.append(w)
                i += 1
    return merged


# --------------------------------------------------- 1. attendance table
_PLAIN_CELL_RE = re.compile(r"^\d{1,3}$")


def _crop_to_table_left(region):
    """On a page laid out in several text columns the attendance table sits in
    ONE column, and the neighbouring columns' paragraphs share its rows. Crop
    the region at the left edge of the table's own title so only the table's
    column (and anything to its right) is parsed."""
    words = region.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
    for row in _group_rows(words):
        low = row['text'].lower()
        if 'attendance' not in low or 'meeting' not in low:
            continue
        ws = row['words']
        idx = next((i for i, w in enumerate(ws) if w['text'].lower().startswith('attendance')), None)
        if idx is None:
            continue
        left = ws[idx - 1]['x0'] if idx > 0 and ws[idx - 1]['text'].lower().startswith('directors') else ws[idx]['x0'] - 55
        left = max(left - 3, region.bbox[0])
        if left > region.bbox[0] + 5:
            return region.crop((left, region.bbox[1], region.bbox[2], region.bbox[3]))
        return region
    return region


def _parse_attendance_region(region):
    region = _crop_to_table_left(region)
    words = _merge_cell_words(region.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False))
    all_rows = _group_rows(words)

    # title row ("Directors' attendance at scheduled Board and Board Committee meetings ...")
    title_idx = next((i for i, r in enumerate(all_rows)
                      if re.search(r"attendance", r['text'], re.I) and re.search(r"meeting", r['text'], re.I)), None)
    if title_idx is None:
        return None
    title_row = all_rows[title_idx]
    # the row of "total number of scheduled meetings": integers only, first one below the title
    number_idx = None
    for i in range(title_idx + 1, min(title_idx + 14, len(all_rows))):
        r = all_rows[i]
        ints = [w for w in r['words'] if _INT_RE.match(w['text'])]
        others = [w for w in r['words'] if not _INT_RE.match(w['text'])]
        # integers only - tolerating a stray qualifier such as "(scheduled)" / "(ad hoc)"
        if ints and len(ints) >= 0.6 * len(r['words']) and all(re.fullmatch(r"\(?[a-z]+\)?", w['text'], re.I) for w in others):
            number_idx = i
            break
    if number_idx is None:
        return None
    number_row = all_rows[number_idx]
    anchors = sorted(((w['x0'] + w['x1']) / 2, int(w['text']), w['x0'])
                     for w in number_row['words'] if _INT_RE.match(w['text']))
    spacing = min([b[0] - a[0] for a, b in zip(anchors, anchors[1:])] or [120.0])
    max_dist = max(spacing * 0.6, 25.0)
    first_anchor_x = anchors[0][0]
    first_anchor_x0 = anchors[0][2]

    def nearest(w):
        xc = (w['x0'] + w['x1']) / 2
        idx, dist = min(((i, min(abs(xc - a[0]), abs(w['x0'] - a[2]))) for i, a in enumerate(anchors)),
                        key=lambda t: t[1])
        return idx, dist

    # column names: header PHRASES between the title row and the numbers row.
    # A header cell ("Nominations and Remuneration") is wider than one column's
    # spacing, so words are first joined into phrases (small gaps) and each
    # phrase, not each word, goes to the nearest column anchor.
    header_floor = max(w['bottom'] for w in title_row['words'])
    headers = defaultdict(list)
    header_words = [w for w in words if header_floor - 1 <= w['top'] < number_row['top'] - 1
                    and w['x1'] > first_anchor_x0 - 30]      # drop the row-label text ("Total number of scheduled meetings")
    for row in _group_rows(header_words):
        phrase = []

        def flush_phrase():
            if not phrase:
                return
            probe = {'x0': phrase[0]['x0'], 'x1': phrase[-1]['x1']}
            idx, dist = nearest(probe)
            if dist <= max_dist:
                headers[idx].append({'top': row['top'], 'x0': probe['x0'],
                                     'text': " ".join(w['text'] for w in phrase)})
            phrase.clear()
        for w in row['words']:
            # a normal word space is ~0.25 x the font height; a column gap is wider
            if phrase and w['x0'] - phrase[-1]['x1'] > max(3.5, 0.42 * (w['bottom'] - w['top'])):
                flush_phrase()
            phrase.append(w)
        flush_phrase()
    columns = []
    for i, (ax, meetings, _ax0) in enumerate(anchors):
        hw = sorted(headers.get(i, []), key=lambda h: (round(h['top']), h['x0']))
        columns.append({'name': re.sub(r"\s+", " ", " ".join(h['text'] for h in hw)).strip(),
                        'meetings': meetings, 'members': []})

    directors = []
    for r in all_rows[number_idx + 1:]:
        low = r['text'].lower().lstrip()
        if low.startswith(('numbers in', 'all the directors', 'note')):
            break
        name_words = [w for w in r['words'] if w['x1'] <= first_anchor_x0 - 5]
        cell_words = [w for w in r['words'] if w['x0'] >= first_anchor_x0 - 25
                      and (_CELL_RE.match(w['text']) or _PLAIN_CELL_RE.match(w['text']))]
        if not cell_words:
            continue
        name = _clean_person_name(" ".join(w['text'] for w in name_words))
        name = re.sub(r"(?<=[A-Za-z])\d{1,2}$", "", name)          # footnote number fused to the name ("Fulvio Tonelli4")
        name = _clean_person_name(name)
        if not _looks_like_person(name):
            continue
        entry = {'name': name, 'cells': {}}
        for w in cell_words:
            idx, dist = nearest(w)
            if dist > max_dist:
                continue
            nums = [int(x) for x in re.findall(r"\d+", w['text'])]
            if len(nums) == 2:
                att, elig = nums
            else:
                # a bare figure = meetings attended; brackets appear only when the
                # member was eligible for fewer than all scheduled meetings
                att, elig = nums[0], columns[idx]['meetings']
            entry['cells'][idx] = (att, elig)
            columns[idx]['members'].append({'name': name, 'attended': att, 'eligible': elig})
        if entry['cells']:
            directors.append(entry)
    if len(directors) < 3:
        return None
    return {'columns': columns, 'directors': directors}


def extract_board_attendance(pdf_bytes: bytes):
    """Returns None when no attendance table is found, else
    {'page': int, 'board_meetings': int|None, 'committees': [ {name,
    meetings, members:[{name, attended, eligible}], attendance_rate,
    consistent} ], 'directors': [{name, committees:[names]}]}."""
    pages = _candidate_pages(pdf_bytes, required_any=['scheduled meetings', 'meetings held', 'number of meetings'],
                             required_all=['attend'])
    if not pages:
        return None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pn in pages:
            page = pdf.pages[pn - 1]
            for region in _regions(page):
                parsed = _parse_attendance_region(region)
                if not parsed:
                    continue
                cols = parsed['columns']
                board_col = next((c for c in cols if re.search(r"\bboard\b", c['name'], re.I)
                                  and not re.search(r"committee", c['name'], re.I)), None)
                if board_col is None and cols and not cols[0]['name']:
                    board_col = cols[0]
                committees = []
                for c in cols:
                    if c is board_col or not c['members']:
                        continue
                    if re.search(r"\bboard\b", c['name'], re.I) and not re.search(r"committee", c['name'], re.I):
                        continue          # e.g. an extra "Main Board (ad hoc)" column is board meetings, not a committee
                    att = sum(m['attended'] for m in c['members'])
                    elig = sum(m['eligible'] for m in c['members'])
                    committees.append({
                        'name': c['name'] or 'Committee',
                        'meetings': c['meetings'],
                        'members': c['members'],
                        'attendance_rate': round(100.0 * att / elig, 1) if elig else None,
                        # a member cannot be eligible for more meetings than were scheduled
                        'consistent': all(m['eligible'] <= c['meetings'] and m['attended'] <= m['eligible']
                                          for m in c['members']),
                    })
                directors = []
                for d in parsed['directors']:
                    sits = []
                    for i, c in enumerate(cols):
                        if (i in d['cells'] and c is not board_col and c['name']
                                and re.search(r"committee", c['name'], re.I)):
                            sits.append(c['name'])
                    directors.append({'name': d['name'],
                                      'on_board': board_col is not None and cols.index(board_col) in d['cells'],
                                      'committees': sits})
                if not committees and board_col is None:
                    continue
                return {'page': pn,
                        'board_meetings': board_col['meetings'] if board_col else None,
                        'committees': committees, 'directors': directors}
    return None


# ----------------------------------------------------- 2. board register
_HEADER_KEYS = [
    ('director', re.compile(r"^director(s)?$", re.I)),
    ('skills', re.compile(r"^skills$", re.I)),
    ('category', re.compile(r"^category$", re.I)),
    ('age', re.compile(r"^age$", re.I)),
    ('date', re.compile(r"^(date|appointment|appointed)$", re.I)),
    ('tenure', re.compile(r"^(tenure|\(years|months\))$", re.I)),
    ('nationality', re.compile(r"^nationality$", re.I)),
    ('gender', re.compile(r"^gender$", re.I)),
]
_NAME_LINE_RE = re.compile(r"^[A-Z][A-Za-z'.\-]+(?:\s+[A-Z][A-Za-z'.\-]+){1,4}[\s*\u2020#^]*$")


def _parse_register_region(region):
    words = region.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
    rows = _group_rows(words)
    header_idx = None
    for i, r in enumerate(rows):
        toks = {w['text'].lower() for w in r['words']}
        if 'director' in toks and 'nationality' in toks and ('gender' in toks or 'age' in toks or 'category' in toks):
            header_idx = i
            break
    if header_idx is None:
        return []
    header_top = rows[header_idx]['top']
    anchors = {}
    for w in words:
        if not (header_top - 45 <= w['top'] <= header_top + 3):
            continue
        for key, rx in _HEADER_KEYS:
            if rx.match(w['text']):
                # header words on the anchor row win; wrapped header lines above may only LOWER x for date/tenure
                if key in ('date', 'tenure'):
                    anchors[key] = min(anchors.get(key, w['x0']), w['x0'])
                elif key not in anchors or abs(w['top'] - header_top) <= 3:
                    anchors[key] = w['x0']
                break
    for need in ('director', 'category'):
        if need not in anchors:
            return []
    if not any(k in anchors for k in ('nationality', 'gender', 'age', 'date')):
        return []
    ordered = sorted(anchors.items(), key=lambda kv: kv[1])
    bands = {}
    for i, (key, x) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else x + 70
        bands[key] = (x - 4, end - 4)
    right_limit = bands[ordered[-1][0]][1]

    body = [w for w in words if w['top'] > header_top + 3 and w['x0'] < right_limit]
    dir_lo, dir_hi = bands['director']
    name_lines = []
    for r in _group_rows([w for w in body if dir_lo <= w['x0'] < dir_hi]):
        text = r['text'].strip()
        if _NAME_LINE_RE.match(text) and _looks_like_person(_clean_person_name(text)):
            name_lines.append((r['top'], _clean_person_name(text)))
    if len(name_lines) < 3:
        return []

    people = []
    for i, (top, name) in enumerate(name_lines):
        end = name_lines[i + 1][0] - 2 if i + 1 < len(name_lines) else top + 70
        seg = [w for w in body if top - 2 <= w['top'] < end]
        cols = {}
        for key, (lo, hi) in bands.items():
            cw = sorted((w for w in seg if lo <= w['x0'] < hi), key=lambda w: (round(w['top'] / 3), w['x0']))
            cols[key] = re.sub(r"\s+", " ", " ".join(w['text'] for w in cw)).strip()
        people.append((name, top, cols))
    return people


def _normalise_register_row(order, name, cols, page):
    cat = (cols.get('category') or '').lower()
    role = None
    if 'executive' in cat:
        role = 'non_executive' if re.search(r"non[\s\-]*executive", cat) else 'executive'
    independent = None
    if 'independent' in cat:
        independent = True
    elif role == 'non_executive':
        independent = False           # the table's own category says "Non-Executive" (not "Independent Non-Executive")
    age = None
    m = re.search(r"\b(\d{2})\b", cols.get('age') or '')
    if m and 18 <= int(m.group(1)) <= 100:
        age = int(m.group(1))
    gender = None
    g = (cols.get('gender') or '').strip().lower()
    if g in ('male', 'm'):
        gender = 'Male'
    elif g in ('female', 'f'):
        gender = 'Female'
    date = re.sub(r"\s+", " ", cols.get('date') or '').strip() or None
    nationality = (cols.get('nationality') or '').strip() or None
    return {'director_name': name, 'position': (cols.get('category') or None), 'role': role or 'unknown',
            'independent': independent, 'gender': gender, 'nationality': nationality, 'appointed_date': date,
            'age': age, 'order_index': order, 'page': page}




def _parse_register_by_gender(region, page_no):
    """Board table whose rows are anchored by the Gender column (Equity Group:
    "Name | Executive/Non-Executive Director | Skills | Qualifications | Gender |
    Age | Appointed | Nationality"). Names and category text wrap over several
    lines, so a row is "everything between this Male/Female word and the next".
    Returns rows as dicts, or [] when the header/gender column is not there."""
    words = region.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
    rows = _group_rows(words)
    header = next((r for r in rows if {'nationality', 'gender'} <= {w['text'].lower() for w in r['words']}), None)
    if header is None:
        return []
    htop = header['top']
    xs = {}
    for w in header['words']:
        t = w['text'].lower()
        if t in ('name', 'names') and 'name' not in xs:
            xs['name'] = w['x0']
        elif t in ('director', 'directors', 'category', 'designation', 'position', 'role') and 'cat' not in xs:
            xs['cat'] = w['x0']
        elif t == 'gender':
            xs['gender'] = w['x0']
        elif t == 'age':
            xs['age'] = w['x0']
        elif t in ('appointed', 'appointment', 'date'):
            xs.setdefault('date', w['x0'])
        elif t == 'nationality':
            xs['nationality'] = w['x0']
        elif t in ('skills', 'qualifications', 'qualification', 'experience', 'expertise', 'committees', 'tenure',
                   'independence', 'skill') and ('x_' + t) not in xs:
            xs['x_' + t] = w['x0']          # columns we do not read still bound their neighbours' bands
    if 'name' not in xs and 'cat' in xs:
        xs['name'] = xs.pop('cat')          # the only "Director" column IS the name column
    if not {'name', 'gender', 'nationality'} <= set(xs):
        return []
    order = sorted(xs.items(), key=lambda kv: kv[1])
    bands = {}
    for i, (k, x) in enumerate(order):
        bands[k] = (x - 3, (order[i + 1][1] - 3) if i + 1 < len(order) else x + 90)
    glo, ghi = bands['gender']
    anchors = sorted([w for w in words if w['top'] > htop + 3 and glo <= w['x0'] < ghi
                      and w['text'].lower() in ('male', 'female')], key=lambda w: w['top'])
    if len(anchors) < 2:
        return []
    out = []
    for i, g in enumerate(anchors):
        top = g['top'] - 2
        end = anchors[i + 1]['top'] - 2 if i + 1 < len(anchors) else top + 110
        seg = [w for w in words if top <= w['top'] < end]
        cells = {}
        for k, (lo, hi) in bands.items():
            cw = sorted((w for w in seg if lo <= w['x0'] < hi), key=lambda w: (round(w['top'] / 3), w['x0']))
            cells[k] = re.sub(r"\s+", " ", " ".join(w['text'] for w in cw)).replace('\u25a0', ' ').strip()
        _hon, name = _split_honorific_and_name(cells.get('name', ''))
        if not _looks_like_person(name):
            continue
        if re.search(r"secretary", cells.get('cat', ''), re.I) and not re.search(r"director", cells.get('cat', ''), re.I):
            continue                        # the company secretary is listed in the table but is not a director
        age = None
        m = re.search(r"\b(\d{2})\b", cells.get('age', ''))
        if m and 18 <= int(m.group(1)) <= 100:
            age = int(m.group(1))
        cat = re.sub(r"\s+", " ", cells.get('cat', '').replace('- ', '-')).strip()
        role, independent = _role_from_title(cat)
        out.append({'director_name': name, 'position': cat[:80] or None, 'role': role, 'independent': independent,
                    'gender': 'Male' if g['text'].lower() == 'male' else 'Female',
                    'nationality': cells.get('nationality') or None,
                    'appointed_date': _date_text(cells.get('date')), 'age': age, 'page': page_no})
    return out


def extract_board_register_table(pdf_bytes: bytes) -> list:
    """[] when no board register table is found, else one dict per director
    (director_name, position, role, independent, gender, nationality,
    appointed_date, age, order_index, page). Two table shapes are read: rows
    anchored by a name line (Absa) and rows anchored by the Gender column
    (Equity); a table that continues on the next page is followed."""
    pages = _candidate_pages(pdf_bytes, required_any=[], required_all=['nationality', 'gender'])
    if not pages:
        return []
    out, last_ok = [], None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pn in pages:
            if last_ok is not None and pn != last_ok + 1:
                break                                   # only consecutive pages continue one table
            got = []
            for region in _regions(pdf.pages[pn - 1]):
                people = _parse_register_region(region)
                if people:
                    got = [_normalise_register_row(0, n, c, pn) for (n, _t, c) in people]
                    break
                got = _parse_register_by_gender(region, pn)
                if got:
                    break
            if got:
                out.extend(got)
                last_ok = pn
            elif last_ok is not None:
                break
    seen, uniq = set(), []
    for d in out:
        key = " ".join(sorted(person_tokens(d['director_name'])))
        if key in seen:
            continue
        seen.add(key)
        d['order_index'] = len(uniq)
        uniq.append(d)
    # a plain "Non-executive director" only means "not independent" where the table itself
    # labels some directors "Independent ..."
    if not any(d.get('independent') for d in uniq):
        for d in uniq:
            if d.get('independent') is False and d.get('role') == 'non_executive':
                d['independent'] = None
    return uniq if len(uniq) >= 3 else []


# --------------------------------------------- 2b. director profile cards
# "Our Board of Directors": one card per director - name, title
# ("Chairman and Independent Non-executive director", "Executive Director and
# Chief Financial Officer") and an "Appointed to Board: June 2009" line -
# laid out in 2-3 columns. Used when a report has no Category/Age/Gender table
# (Absa 2023-2025). Gives role, independence and appointment date; it never
# states gender or nationality, so those stay empty.
_TITLE_WORDS_RE = re.compile(
    r"\b(chair(?:man|person|woman)?|director|independent|executive|officer|managing|secretary|ceo|cfo)\b", re.I)
_CREDENTIALS_TAIL_RE = re.compile(r"(?:,\s*[A-Z][A-Za-z.]{1,6})+,?\s*$")


def _card_role(title: str):
    t = (title or '').lower()
    non_exec = re.search(r"non[\s\-]*executive", t)
    if non_exec:
        return 'non_executive', ('independent' in t)      # plain non-executive: resolved in extract_director_profile_cards
    if re.search(r"\bexecutive\b|managing director|chief (executive|financial)", t):
        return 'executive', None
    return 'unknown', None


def _is_bold(w) -> bool:
    return 'bold' in str(w.get('fontname', '')).lower() or 'black' in str(w.get('fontname', '')).lower()


def _parse_profile_region(region):
    words = region.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False,
                                 extra_attrs=['fontname'])
    rows = _group_rows(words)
    anchors = []   # (x0, top, date_text)
    for r in rows:
        ws = r['words']
        for i, w in enumerate(ws):
            if (w['text'].lower() == 'appointed' and i + 2 < len(ws) + 0 and ws[i + 1]['text'].lower() == 'to'
                    and ws[i + 2]['text'].lower().startswith('board')):
                # the date is the words after "Board:" up to the next card's text (a gap of 25+pt)
                date_words, last = [], ws[i + 2]
                for nxt in ws[i + 3:]:
                    if nxt['x0'] - last['x1'] > 25:
                        break
                    date_words.append(nxt)
                    last = nxt
                if ws[i + 2]['text'].lower() not in ('board', 'board:'):
                    date_words.insert(0, ws[i + 2])
                anchors.append({'x0': w['x0'], 'top': r['top'],
                                'date': re.sub(r"\s+", " ", " ".join(x['text'] for x in date_words)).strip()})
    if len(anchors) < 2:
        return []
    starts = sorted({round(a['x0'] / 12) * 12 for a in anchors})
    def band_for(x0):
        cx = min(starts, key=lambda c: abs(c - x0))
        i = starts.index(cx)
        return (cx - 14, (starts[i + 1] - 14) if i + 1 < len(starts) else region.bbox[2] + 1)
    people = []
    for a in anchors:
        lo, hi = band_for(a['x0'])
        # A card's header (name + title) is set in a BOLD face directly above its
        # "Appointed to Board:" line; the card body above that is a lighter face.
        # Walking up through contiguous bold rows therefore isolates exactly this
        # card's header and cannot bleed into the previous card's body text.
        above = [w for w in words if lo <= w['x0'] < hi and a['top'] - 70 < w['top'] < a['top'] - 1]
        lines = []
        prev_top = a['top']
        for r in reversed(_group_rows(above)):
            # a neighbouring card can share this band: keep only the run of words
            # that starts at THIS card's left edge and has no wide gap in it
            mine, last = [], None
            for w in r['words']:
                if last is None:
                    if abs(w['x0'] - a['x0']) > 12:
                        continue
                elif w['x0'] - last['x1'] > 20:
                    break
                mine.append(w)
                last = w
            if not mine or not all(_is_bold(w) for w in mine) or prev_top - r['top'] > 14:
                break
            lines.append(" ".join(w['text'] for w in mine).strip())
            prev_top = r['top']
        lines.reverse()
        if len(lines) < 2:
            continue
        # title lines = from the first line carrying title vocabulary; name = the lines before it
        first_title = next((i for i, l in enumerate(lines) if _TITLE_WORDS_RE.search(l)), None)
        if first_title is None or first_title == 0:
            continue
        name = " ".join(lines[:first_title])
        name = _CREDENTIALS_TAIL_RE.sub("", name.strip())
        name = _clean_person_name(re.sub(r"^(?:mr|mrs|ms|dr|prof|hon|amb)\.?\s+", "", name, flags=re.I))
        title = " ".join(lines[first_title:])
        if not _looks_like_person(name) or not re.search(r"director|chair", title, re.I):
            continue
        people.append((a['top'], a['x0'], name, title, a['date']))
    return people


def extract_director_profile_cards(pdf_bytes: bytes) -> list:
    """[] when the report has no "Appointed to Board:" profile cards, else one
    dict per director (director_name, position, role, independent,
    appointed_date, page, order_index)."""
    texts = _pypdf_page_texts_cached(pdf_bytes) or []
    pages = [pn for pn, t in texts if len(re.findall(r"appointed to board", t or '', re.I)) >= 2]
    if not pages:
        return []
    out, seen = [], set()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pn in pages:
            for region in _regions(pdf.pages[pn - 1]):
                for top, x0, name, title, date in sorted(_parse_profile_region(region), key=lambda t: (round(t[1] / 12), t[0])):
                    if name.lower() in seen:
                        continue
                    seen.add(name.lower())
                    role, independent = _card_role(title)
                    out.append({'director_name': name, 'position': title[:80], 'role': role,
                                'independent': independent, 'gender': None, 'nationality': None,
                                'appointed_date': date or None, 'age': None, 'page': pn,
                                'order_index': len(out), 'source': 'profile'})
    if len(out) < 3:
        return []
    # A plain "Non-executive director" card means "not independent" ONLY when the
    # same report labels the others "Independent Non-executive director" - i.e. the
    # cards visibly distinguish the two. If no card mentions independence at all,
    # independence is simply not stated.
    labels_independence = any(d['independent'] for d in out)
    for d in out:
        if d['role'] == 'non_executive' and not d['independent']:
            d['independent'] = False if labels_independence else None
    return out


def person_tokens(name: str) -> frozenset:
    """Lower-case name tokens, hyphens split, titles/initials dropped."""
    toks = re.split(r"[\s\-]+", (name or '').lower())
    return frozenset(t.strip('.,*') for t in toks if len(t.strip('.,*')) > 1 and t.strip('.,*') not in _TITLE_TOKENS)


def _token_match(a: str, b: str) -> bool:
    """Same name token, tolerating a typo in the printed spelling ("Imahtu" /
    "Imathiu", "Japhet" / "Japheth" - both appear in one Absa report)."""
    if a == b:
        return True
    if min(len(a), len(b)) >= 5:
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85
    return False


def same_person(a: str, b: str) -> bool:
    """One name's tokens contained in the other's ("Louis Otieno" ~
    "Louis Onyango Otieno"): the same person printed with and without a
    middle name or with a typo. Needs at least two shared tokens or a
    single-token name."""
    ta, tb = person_tokens(a), person_tokens(b)
    if not ta or not tb:
        return False
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if all(any(_token_match(x, y) for y in big) for x in small) and (len(small) >= 2 or len(big) == 1):
        return True
    # One badly misspelled token ("Imahtu" for "Imathiu", ratio 0.77) is still the same person when
    # every OTHER token matches exactly.
    if len(small) >= 2:
        loose = [x for x in small if x not in big]
        if len(loose) == 1 and len(loose[0]) >= 5:
            cand = [y for y in big if y not in small]
            return any(difflib.SequenceMatcher(None, loose[0], y).ratio() >= 0.75 for y in cand)
    return False


def profile_cards_look_complete(cards: list, roster_names: list) -> bool:
    """Profile cards are only trusted for counts when they look complete:
    every card must be a person who appears in the attendance roster, and
    together they must cover at least 75% of that roster (a roster also lists
    directors who left during the year, so 100% is not expected). With no
    roster to check against, at least five clean cards are required."""
    if not cards:
        return False
    if not roster_names:
        return len(cards) >= 5
    matched = [c for c in cards if any(same_person(c['director_name'], r) for r in roster_names)]
    return len(matched) == len(cards) and len(cards) >= 0.75 * len(roster_names)


# ------------------------------------------ 2c. labelled director cards
# Cards whose facts are printed as "Label: value" lines under the director's
# name and title - the layout of Eaagads ("Age: 43 / Nationality: Kenyan /
# Appointed: 05/09/2023"), Safaricom ("Nationality: Kenyan / Appointed: 22
# December 2022 / Committees: ...") and KCB ("Date of Appointment to Board:
# March 2023"). Columns are found from the label words' x positions, so it
# does not matter whether a page has one, two or three cards across.
_LABEL_FIND_RE = re.compile(
    r"(Nationality|Appointed(?:\s+to\s+(?:the\s+)?Board)?|Date\s+of\s+Appointment(?:\s+to\s+(?:the\s+)?Board)?|Age|Committees?)\s*:", re.I)
_HEADERISH_STOP_RE = re.compile(r"[.;]$")
_CREDENTIAL_SUFFIXES = {'mbs', 'cbs', 'ebs', 'egh', 'ogw', 'hsc', 'sc', 'mgh', 'cpa', 'fcpa', 'fcs', 'mp', 'cs', 'phd', 'ca', 'cfa', 'ogw'}
_HONORIFICS = {'mr': 'Male', 'mrs': 'Female', 'ms': 'Female', 'miss': 'Female', 'madam': 'Female'}
_PREFIX_TOKENS = {'mr', 'mrs', 'ms', 'miss', 'madam', 'dr', 'prof', 'hon', 'eng', 'amb', 'rev', 'sir', 'cpa', 'fcpa',
                  'fcs', 'cs', 'ca', 'cfa', 'adv', 'sen', 'gen', 'lt', 'col', 'maj', 'ambassador', 'justice', 'judge'}
_TITLE_VOCAB_RE = re.compile(
    r"\b(chair(?:man|person|woman)?|director|executive|officer|independent|alternate|ceo|managing|nominee|secretary|chief|acting)\b", re.I)


def _split_honorific_and_name(raw: str):
    """('Mrs.', 'Jane Doe') style split: returns (gender_or_None, cleaned_name)."""
    text = re.sub(r"\([^)]*\)", " ", raw or "")                 # "(MGH)"
    text = re.sub(r",.*$", "", text)                            # ", CBS, SC" credentials
    parts = [p for p in re.split(r"\s+", text.replace('.', '. ').strip()) if p]
    gender = None
    kept = []
    for i, tok in enumerate(parts):
        key = tok.strip('.').lower()
        if key in _PREFIX_TOKENS and len(kept) == 0:
            if key in _HONORIFICS:
                gender = _HONORIFICS[key]
            continue
        kept.append(tok.strip('.') if len(tok) > 2 or not tok.endswith('.') else tok)
    while len(kept) > 2 and kept[-1].strip('.').lower() in _CREDENTIAL_SUFFIXES:
        kept.pop()                                            # "... Gichohi MBS" / "... Kinyua EGH"
    name = " ".join(kept).strip(" ,.*")
    if name.isupper():
        name = name.title()
    return gender, _clean_person_name(name)


def _role_from_title(title: str):
    """(role, independent) from a director's stated title; None where the
    title does not say."""
    t = re.sub(r"[\u2013\u2014]", "-", (title or "")).lower()
    if 'alternate' in t:
        return 'unknown', None              # "Alternate Director to CEO" is not the CEO
    independent = None
    if re.search(r"non[\s\-]*independent", t):
        independent = False
    elif 'independent' in t:
        independent = True
    if re.search(r"non[\s\-]*executive", t):
        return 'non_executive', independent
    if re.search(r"chief executive|managing director|\bceo\b|\bexecutive director\b|group md", t) or \
            (re.search(r"\bexecutive\b", t) and not re.search(r"non", t)):
        return 'executive', (False if independent is None and False else independent)
    return 'unknown', independent


def _date_text(value: str):
    value = re.sub(r"\s+", " ", (value or "")).strip(" .,;")
    return value or None


def _parse_label_cards_region(region, page_no):
    words = region.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
    rows = _group_rows(words)
    anchors = []
    for ri, row in enumerate(rows):
        text, spans = "", []
        for w in row['words']:
            spans.append((len(text), len(text) + len(w['text']), w))
            text += w['text'] + " "
        for m in _LABEL_FIND_RE.finditer(text):
            w = next((sp[2] for sp in spans if sp[0] <= m.start() < sp[1] + 1), None)
            if w is None:
                continue
            raw = m.group(1).lower()
            kind = ('nationality' if raw.startswith('nationality') else
                    'age' if raw == 'age' else
                    'committees' if raw.startswith('committee') else 'appointed')
            anchors.append({'kind': kind, 'x0': w['x0'], 'top': row['top'], 'ri': ri, 'label_end': w['x0'] + len(m.group(0)) * 4.2,
                            'label_word': w})
    if len(anchors) < 2:
        return []

    # columns = clusters of anchor x0
    xs = sorted({round(a['x0'] / 18) * 18 for a in anchors})
    def col_of(x0):
        return min(xs, key=lambda c: abs(c - x0))
    def col_band(cx):
        i = xs.index(cx)
        return (cx - 22, (xs[i + 1] - 22) if i + 1 < len(xs) else region.bbox[2] + 1)

    cards = []
    for cx in xs:
        col = sorted([a for a in anchors if col_of(a['x0']) == cx], key=lambda a: a['top'])
        cur = None
        for a in col:
            if cur is None or a['kind'] in cur['kinds'] and a['kind'] != 'committees' or a['top'] - cur['last_top'] > 48:
                cur = {'cx': cx, 'anchors': [], 'kinds': set(), 'last_top': a['top']}
                cards.append(cur)
            cur['anchors'].append(a); cur['kinds'].add(a['kind']); cur['last_top'] = a['top']

    people = []
    for card in cards:
        if not ({'nationality', 'appointed', 'age'} & card['kinds']):
            continue
        lo, hi = col_band(card['cx'])
        first = card['anchors'][0]
        card_left = min(a['x0'] for a in card['anchors'])
        # ---- header lines above the first label
        above = [w for w in words if lo - 60 <= w['x0'] < hi and first['top'] - 80 < w['top'] < first['top'] - 1]
        lines, prev_top = [], first['top']
        for r in reversed(_group_rows(above)):
            # split the row into runs of words (a wide gap = another card's text) and keep the
            # run that overlaps this card's own x-range: names are set flush-left OR centred
            # over the indented "Label:" lines
            runs, cur_run = [], []
            for w in r['words']:
                if cur_run and w['x0'] - cur_run[-1]['x1'] > 22:
                    runs.append(cur_run); cur_run = []
                cur_run.append(w)
            if cur_run:
                runs.append(cur_run)
            card_right = card_left + 210
            best_run, best_ov = [], 0.0
            for run in runs:
                ov = min(run[-1]['x1'], card_right) - max(run[0]['x0'], card_left - 75)
                if ov > best_ov:
                    best_run, best_ov = run, ov
            mine = best_run
            txt = " ".join(w['text'] for w in mine).strip()
            if (not mine or prev_top - r['top'] > 20 or len(txt.split()) > 9 or _HEADERISH_STOP_RE.search(txt)
                    or ':' in txt or len(lines) >= 4):
                break
            lines.append(txt); prev_top = r['top']
        lines.reverse()
        first_title = next((i for i, l in enumerate(lines) if _TITLE_VOCAB_RE.search(l)), None)
        if first_title is None or first_title == 0:
            # No name/title header above the labels (KCB prints them in a photo box): take the
            # name from the biography sentence "<Name> is/was ..." under "Education and
            # Professional Background:", and the title from the first designation phrase in it.
            bio_rows = [r2 for r2 in rows if first['top'] < r2['top'] < first['top'] + 120]
            bio = []
            for r2 in bio_rows:
                run = [w for w in r2['words'] if card_left - 6 <= w['x0'] < min(hi, card_left + 320)]
                if run:
                    bio.append(" ".join(w['text'] for w in run))
            bio_text = " ".join(bio)
            bio_text = re.sub(r"^.*?Background:\s*", "", bio_text)
            mname = re.match(r"^((?:[A-Z]{2,5}\s+)?(?:(?:Dr|Prof|Mr|Mrs|Ms|Hon|Eng|Amb|CPA|CS)\.?\s+)*[A-Z][\w'\-]+(?:\s+[A-Z][\w'\-]+){1,3})(?:,[^,]{0,25}?)*\s+(?:is|was|has|holds|joined|brings|serves)\b", bio_text)
            if not mname:
                continue
            gender, name = _split_honorific_and_name(mname.group(1))
            mt = re.search(r"((?:Independent\s+)?Non[\s\-]*Executive\s+Director|(?:Group\s+)?(?:Chief Executive Officer|Managing Director)|Executive Director|Chairman)", bio_text)
            title = mt.group(1) if mt else ""
        else:
            gender, name = _split_honorific_and_name(" ".join(lines[:first_title]))
            title = " ".join(lines[first_title:])
        if not _looks_like_person(name):
            continue
        if re.search(r"secretary", title, re.I) and not re.search(r"director", title, re.I):
            continue                      # the company secretary is an officer, not a board member
        # ---- label values (same row to the right of the label, else the next row)
        vals = {}
        for a in card['anchors']:
            row = rows[a['ri']]
            nxt_x = min([b['x0'] for b in anchors if b['ri'] == a['ri'] and b['x0'] > a['x0'] + 5] or [hi])
            lw = a['label_word']
            same = [w for w in row['words'] if w['x0'] > lw['x1'] - 1 and w['x0'] < nxt_x - 3
                    and not (w['x0'] < lw['x1'] and w is lw)]
            # the label may span several words ("Appointed to Board:") - drop words up to the colon
            same_txt = " ".join(w['text'] for w in same)
            same_txt = re.sub(r"^.*?:\s*", "", same_txt) if ':' in same_txt else same_txt
            if not same_txt.strip() and a['ri'] + 1 < len(rows):
                nxt_row = rows[a['ri'] + 1]
                cand = [w for w in nxt_row['words'] if abs(w['x0'] - a['x0']) < 60 and w['x0'] < nxt_x - 3]
                same_txt = " ".join(w['text'] for w in cand)
            vals[a['kind']] = same_txt.strip()
        age = None
        m = re.search(r"\b(\d{2})\b", vals.get('age', ''))
        if m and 18 <= int(m.group(1)) <= 100:
            age = int(m.group(1))
        committees = None
        if 'committees' in vals:
            ca = next(a for a in card['anchors'] if a['kind'] == 'committees')
            crow_idx = ca['ri']
            parts = [vals['committees']] if vals['committees'] else []
            for r2 in rows[crow_idx + 1: crow_idx + 6]:
                cand = [w for w in r2['words'] if lo <= w['x0'] < hi and abs(w['x0'] - card_left) < 40]
                t2 = " ".join(w['text'] for w in cand).strip()
                if not t2 or _LABEL_FIND_RE.search(t2) or len(t2.split()) > 8:
                    break
                parts.append(t2)
            joined = re.sub(r"\s+", " ", " ".join(parts)).strip(" ,;")
            committees = [c.strip() for c in re.split(r"[;,]|\s{2,}", joined)
                          if len(re.findall(r"[A-Za-z]", c)) >= 4 and not re.search(r"annual report|integrated report|\bplc\b", c, re.I)] or None
        role, independent = _role_from_title(title)
        people.append({'top': first['top'], 'x': card['cx'], 'director_name': name, 'position': title[:80],
                       'role': role, 'independent': independent, 'gender': gender,
                       'nationality': _date_text(vals.get('nationality')),
                       'appointed_date': _date_text(vals.get('appointed')), 'age': age,
                       'committees': committees, 'page': page_no})
    return people


def extract_director_label_cards(pdf_bytes: bytes) -> list:
    """[] when no "Nationality: / Appointed: / Age:" cards are found, else one
    dict per director (director_name, position, role, independent, gender
    [only from Mr./Mrs./Ms.], nationality, appointed_date, age, committees, page)."""
    texts = _pypdf_page_texts_cached(pdf_bytes) or []
    pages = []
    for pn, t in texts:
        n = len(re.findall(r"(?:nationality|appointed(?:\s+to\s+(?:the\s+)?board)?|date\s+of\s+appointment(?:\s+to\s+(?:the\s+)?board)?|age)\s*:", t or '', re.I))
        if n >= 4:
            pages.append(pn)
    if not pages:
        return []
    out, seen = [], set()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pn in pages:
            for region in _regions(pdf.pages[pn - 1]):
                for d in sorted(_parse_label_cards_region(region, pn), key=lambda d: (d['x'], d['top'])):
                    key = " ".join(sorted(person_tokens(d['director_name'])))
                    if key in seen:
                        continue
                    seen.add(key)
                    d.pop('top', None); d.pop('x', None)
                    d['order_index'] = len(out)
                    d['source'] = 'profile'
                    out.append(d)
    return out if len(out) >= 3 else []


# ------------------------------------------- 2d. honorific-led prose cards
# Director biographies that start with "Mr. Kiprono Kittony, EBS" and a title line
# ("Independent Non-Executive Director"), followed by prose that says when the
# person joined ("... was appointed a Board Director of the NSE on May 30, 2018").
# Layout of the NSE annual report; common in short reports with 2-3 text columns.
_HON_LINE_RE = re.compile(r"^(?:(?:Mr|Mrs|Ms|Miss|Dr|Prof|Hon|Amb|Eng|CPA|CS|FCPA)\.?\s+){1,3}[A-Z][\w'\-]+(?:\s+[A-Z][\w'\-.]+){1,4}(?:,\s*[A-Z][A-Za-z.]{1,8})*$")
_APPT_PROSE_RE = re.compile(
    r"appointed[^.]{0,90}?\b(?:Board|Director|Chair\w*)\b[^.]{0,70}?\b(?:on|in|effective)\s+"
    r"((?:[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})|(?:\d{1,2}(?:st|nd|rd|th)?\s+[A-Z][a-z]+\s+\d{4})|(?:[A-Z][a-z]+\s+\d{4})|(?:19|20)\d{2})")


def _parse_prose_cards_region(region, page_no):
    words = region.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
    rows = _group_rows(words)

    def runs_of(row):
        runs, cur = [], []
        for w in row['words']:
            if cur and w['x0'] - cur[-1]['x1'] > 22:
                runs.append(cur); cur = []
            cur.append(w)
        if cur:
            runs.append(cur)
        return runs

    headers = []          # (row_index, x0, name_text, top)
    for i, row in enumerate(rows):
        for run in runs_of(row):
            txt = " ".join(w['text'] for w in run).strip()
            if _HON_LINE_RE.match(txt) and i + 1 < len(rows):
                # the title sits on the next row, at the same left edge
                nxt = [r for r in runs_of(rows[i + 1]) if abs(r[0]['x0'] - run[0]['x0']) < 12]
                if nxt:
                    title = " ".join(w['text'] for w in nxt[0]).strip()
                    if _TITLE_VOCAB_RE.search(title) and len(title.split()) <= 9:
                        headers.append({'ri': i, 'x0': run[0]['x0'], 'name': txt, 'title': title,
                                        'top': row['top']})
    if len(headers) < 2:
        return []
    people = []
    for h in headers:
        # body = later rows' runs in this column until the next header in the same column
        col = [g for g in headers if abs(g['x0'] - h['x0']) < 12 and g['ri'] > h['ri']]
        stop_ri = min([g['ri'] for g in col] or [h['ri'] + 40])
        body = []
        for r in rows[h['ri'] + 2: min(stop_ri, h['ri'] + 40)]:
            for run in runs_of(r):
                if abs(run[0]['x0'] - h['x0']) < 12:
                    body.append(" ".join(w['text'] for w in run))
        body_text = re.sub(r"\s+", " ", " ".join(body))
        m = _APPT_PROSE_RE.search(body_text)
        gender, name = _split_honorific_and_name(h['name'])
        if not _looks_like_person(name):
            continue
        role, independent = _role_from_title(h['title'])
        if re.search(r"secretary", h['title'], re.I) and not re.search(r"director", h['title'], re.I):
            continue
        people.append({'top': h['top'], 'x': h['x0'], 'director_name': name, 'position': h['title'][:80],
                       'role': role, 'independent': independent, 'gender': gender, 'nationality': None,
                       'appointed_date': _date_text(m.group(1)) if m else None, 'age': None,
                       'committees': None, 'page': page_no})
    return people


def extract_director_prose_cards(pdf_bytes: bytes) -> list:
    """[] unless the report has honorific-led biographies with a title line."""
    texts = _pypdf_page_texts_cached(pdf_bytes) or []
    pages = [pn for pn, t in texts if len(re.findall(r"^\s*(?:Mr|Mrs|Ms|Dr|Prof|Hon|Amb|CPA)\.?\s+[A-Z][a-z]+", t or '', re.M)) >= 2
             and re.search(r"non[\s\-]*executive|chief executive|independent", t or '', re.I)]
    out, seen = [], set()
    if not pages:
        return []
    last_hit = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pn in pages[:25]:
            if last_hit is not None and pn > last_hit + 2:
                break                                  # the biographies are a run of pages; stop after it
            got_here = False
            for region in _regions(pdf.pages[pn - 1]):
                for d in sorted(_parse_prose_cards_region(region, pn), key=lambda d: (d['x'], d['top'])):
                    key = " ".join(sorted(person_tokens(d['director_name'])))
                    if key in seen:
                        continue
                    seen.add(key)
                    d.pop('top', None); d.pop('x', None)
                    d['order_index'] = len(out)
                    d['source'] = 'profile'
                    out.append(d)
                    got_here = True
            if got_here:
                last_hit = pn
    return out if len(out) >= 3 else []


# ------------------------------------------------------ 4. prose facts
_NUM_WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
              'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
              'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20}


def _to_int(tok):
    tok = (tok or '').strip().lower()
    if tok.isdigit():
        return int(tok)
    return _NUM_WORDS.get(tok)


def extract_prose_facts(pdf_bytes: bytes) -> dict:
    """Facts the report STATES IN A SENTENCE, read from the text of every page:
      board_meetings   "the Board held five Board meetings" / "met 4 times"
      composition      "10 Non-Executive Directors and 1 Executive Director"
      market_cap       "market capitalisation of KES 1.2 trillion" (millions)
    Each value comes with the page and the matched sentence fragment. Nothing
    is inferred: a sentence that does not say it is not a fact."""
    texts = _pypdf_page_texts_cached(pdf_bytes) or []
    facts = {}
    meet_re = re.compile(
        r"\b(?:board(?:\s+of\s+directors)?|directors)\s+(?:held|met|convened|had|conducted)\s+(?:a\s+total\s+of\s+)?"
        r"(\w+)\s*(?:\(\s*(\d+)\s*\)\s*)?(?:[a-z]+\s+){0,2}?(?:meetings|times)\b", re.I)
    comp_re = re.compile(
        r"\b(\w+)\s*(?:\(\s*\d+\s*\)\s*)?non[\s\-]*executive\s+directors?\s*(?:,\s*)?(?:and|&|,)\s*(\w+)\s*(?:\(\s*\d+\s*\)\s*)?executive\s+directors?\b", re.I)
    cap_re = re.compile(
        r"market\s+capitali[sz]ation[^.]{0,60}?(?:KES|KSh|Ksh|Shs?\.?|Sh)\s?([\d][\d,.]*)\s*(trillion|billion|million|bn|tn|m)\b", re.I)
    for pn, t in texts:
        flat = re.sub(r"\s+", " ", t or "")
        if 'board_meetings' not in facts:
            for m in meet_re.finditer(flat):
                n = _to_int(m.group(2)) or _to_int(m.group(1))
                if n and 1 <= n <= 30:
                    facts['board_meetings'] = {'value': n, 'page': pn, 'text': m.group(0)[:90]}
                    break
        if 'composition' not in facts:
            m = comp_re.search(flat)
            if m:
                ned, ex = _to_int(m.group(1)), _to_int(m.group(2))
                # "two non-executive directors and three executive directors" is usually a SUBSIDIARY
                # or committee description; a listed company's own board is stated as 5+ people
                if ned and ex is not None and ned <= 25 and ex <= 10 and (ned + ex) >= 5 and \
                        re.search(r"board", flat[max(0, m.start() - 160):m.start()], re.I):
                    facts['composition'] = {'non_executive': ned, 'executive': ex, 'page': pn, 'text': m.group(0)[:100]}
        if 'market_cap' not in facts:
            m = None
            for cand in cap_re.finditer(flat):
                span = cand.group(0)
                before = flat[max(0, cand.start() - 90):cand.start()].lower()
                # a prior-year figure "(FY2024: KShs 0.71 trillion" or a statement about the equity
                # MARKET ("market capitalization of equities" - NSE's report) is not the company's
                if re.search(r"fy\s?\d{2,4}|\b20\d\d\b|\(\s*\d{4}", span, re.I):
                    continue
                if re.search(r"equit|securities|the market|market segment|nairobi securities", span + before, re.I) and \
                        not re.search(r"our market capitali|group.s market capitali|company.s market capitali", before + span, re.I):
                    continue
                m = cand
                break
            if m:
                try:
                    v = float(m.group(1).replace(',', ''))
                except ValueError:
                    v = None
                if v:
                    mult = {'trillion': 1e6, 'tn': 1e6, 'billion': 1e3, 'bn': 1e3, 'million': 1.0, 'm': 1.0}[m.group(2).lower()]
                    facts['market_cap'] = {'value_millions': v * mult, 'page': pn, 'text': m.group(0)[:100]}
    return facts


def extract_board_bundle(pdf_bytes: bytes) -> dict:
    """Everything the board tabs can take from one report:
    {'register': [...rows...], 'register_kind': 'table'|'cards'|'prose'|None,
     'attendance': {...}|None, 'facts': {...}}. The register is the FIRST reader
    that finds at least three directors, richest source first."""
    register, kind = extract_board_register_table(pdf_bytes), 'table'
    if not register:
        # The card readers are cheap, and each layout is read by only one of them, but a partial
        # match by the wrong reader must not pre-empt the right one: keep the reader that found
        # the MOST directors (ties go to the earlier, more explicit reader).
        best = []
        for name, fn in (('cards', extract_director_label_cards), ('cards', extract_director_profile_cards),
                         ('prose', extract_director_prose_cards)):
            rows = fn(pdf_bytes)
            if len(rows) > len(best):
                best, kind = rows, name
        register = best
    return {'register': register, 'register_kind': kind if register else None,
            'attendance': extract_board_attendance(pdf_bytes), 'facts': extract_prose_facts(pdf_bytes)}


# ------------------------------------------------- 3. board composition
def derive_board_composition(directors: list, where: str | None = None) -> dict:
    """{'fields': {...}, 'notes': {field: text}} from a register. A count is
    produced only when EVERY row states the fact it counts, so a partial
    table never yields a wrong total."""
    n = len(directors)
    if n == 0:
        return {'fields': {}, 'notes': {}}
    page = directors[0].get('page')
    if where:
        pass
    elif directors[0].get('source') == 'profile':
        where = f"director profiles from page {page} ({n} directors profiled)"
    else:
        where = f"board table on page {page} ({n} directors listed as in office during the year)"
    fields, notes = {}, {}
    cat_src = ("titles in the " if directors[0].get('source') == 'profile' else "category column of the ") + where
    age_src = "age column of the " + where
    nat_src = "nationality column of the " + where
    gen_src = "gender column of the " + where

    fields['board_size'] = n
    notes['board_size'] = f"count of rows in the {where}"
    roles = [d.get('role') for d in directors]
    if all(r in ('executive', 'non_executive') for r in roles):
        execs = [d for d in directors if d['role'] == 'executive']
        neds = [d for d in directors if d['role'] == 'non_executive']
        fields['executive_directors_count'] = len(execs)
        fields['non_executive_directors_count'] = len(neds)
        notes['executive_directors_count'] = notes['non_executive_directors_count'] = cat_src
        if all(d.get('independent') is not None for d in neds):
            ind = [d for d in neds if d['independent']]
            fields['independent_neds_count'] = len(ind)
            fields['non_independent_neds_count'] = len(neds) - len(ind)
            notes['independent_neds_count'] = notes['non_independent_neds_count'] = cat_src
            for key, group in (('avg_age_independent_neds', ind),
                               ('avg_age_non_independent_neds', [d for d in neds if not d['independent']])):
                ages = [d['age'] for d in group if d.get('age')]
                if group and len(ages) == len(group):
                    fields[key] = round(sum(ages) / len(ages), 1)
                    notes[key] = age_src
        for key, group in (('avg_age_executive_directors', execs), ('avg_age_non_executive_directors', neds)):
            ages = [d['age'] for d in group if d.get('age')]
            if group and len(ages) == len(group):
                fields[key] = round(sum(ages) / len(ages), 1)
                notes[key] = age_src
        if neds and all(d.get('nationality') for d in neds):
            kenyan = [d for d in neds if d['nationality'].strip().lower() in ('kenyan', 'kenya')]
            fields['neds_kenyan_count'] = len(kenyan)
            fields['neds_non_kenyan_count'] = len(neds) - len(kenyan)
            notes['neds_kenyan_count'] = notes['neds_non_kenyan_count'] = nat_src
    if all(d.get('gender') for d in directors):
        female = sum(1 for d in directors if d['gender'] == 'Female')
        fields['directors_female'] = female
        fields['directors_male'] = n - female
        notes['directors_female'] = notes['directors_male'] = gen_src
    return {'fields': fields, 'notes': notes}
