"""Rule-based readers for two things that were empty for EVERY company: the NED benefits
checklist and committees whose membership is stated in a sentence rather than an
attendance table. They run at upload with no API key; the AI module (ai_extract.py)
covers what these rules cannot read.

Both readers work on sentences and only report what a sentence says:
  * benefits: a sentence about non-executive directors that names a benefit is a
    yes; the same sentence with a negation ("does not operate a share option scheme
    for Directors") is a no. Sentences about employees are ignored.
  * committees: "The members of the X Committee during the year were A, B and C" gives
    the committee, its members, and - when a nearby sentence says "met four times" - its meetings.
"""
import io
import re

import pdfplumber

from board_extract import _split_honorific_and_name, _looks_like_person, _to_int, _clean_person_name, \
    _group_rows, _regions
from pdf_parse import _pypdf_page_texts_cached

_BENEFIT_PATTERNS = {
    'MedicalCover': r"medical\s+(?:cover|insurance|expenses|scheme|benefits?)",
    'IndemnityInsurance': r"indemnity|directors\W{0,3}\s*(?:and|&)\s*officers|\bd\s?&\s?o\b",
    'TravelAccommodation': r"travell?ing|accommodation|subsistence|travel\s+(?:costs|expenses|and)",
    'TelephoneAllowance': r"telephone|airtime|mobile\s+phone",
    'TransportAllowance': r"transport\s+allowance|mileage",
    'MealAllowance': r"meal\s+allowance|\bmeals\b|lunch",
    'ClubMembership': r"club\s+membership|membership\s+of\s+(?:a\s+)?club",
    'DutyDayAllowance': r"duty\s+day|per\s+diem",
    'GroupPersonalAccident': r"personal\s+accident",
    'ShareSchemeParticipation': r"share\s+option|share\s+scheme|share\s+award|share\s+incentive|long[\s\-]term\s+incentive",
}
_NED_RE = re.compile(r"non[\s\-]*executive|\bneds?\b|\ball\s+(?:the\s+)?directors\b|\bthe\s+directors\b|\beach\s+director\b|directors\W{0,3}\s*(?:and|&)\s*officers", re.I)
_EXEC_ONLY_RE = re.compile(r"(?<!non-)(?<!non\s)\bexecutive\s+directors?\b", re.I)
_STAFF_RE = re.compile(r"\bemployees?\b|\bstaff\b|\bmanagement\b|\bexecutives?\b(?!\s+directors)", re.I)
_NEG_RE = re.compile(r"\b(?:no|not|neither|nor|never|without|none|nil)\b", re.I)


def _sentences(text: str):
    flat = re.sub(r"\s+", " ", text or "")
    return [s.strip() for s in re.split(r"(?<=[.;])\s+(?=[A-Z\u2022\u00bb•])|\s[\u2022\u00bb•]\s", flat) if s.strip()]


def extract_ned_benefits(pdf_bytes: bytes) -> dict:
    """{key: {'provided': bool, 'detail': sentence, 'page': n}} for benefits the remuneration
    policy states for non-executive directors (yes or no). Keys the report does not
    address are absent - never defaulted to No."""
    found = {}
    for pn, text in _pypdf_page_texts_cached(pdf_bytes) or []:
        low = (text or '').lower()
        if 'remuneration' not in low or not re.search(r"non[\s\-]*executive", low):
            continue
        sents = _sentences(text)
        for i, sent in enumerate(sents):
            for key, pat in _BENEFIT_PATTERNS.items():
                matches = list(re.finditer(pat, sent, re.I))
                if not matches:
                    continue
                m = matches[-1]            # the LAST mention: "Share Options There are no share options ..." -> the second
                # a page's navigation header is glued to its first sentence, so judge only the text
                # around the mention, not the whole "sentence"
                win = sent[max(0, m.start() - 230): m.end() + 120]
                if not _NED_RE.search(win):
                    continue
                if _STAFF_RE.search(win) and not re.search(r"non[\s\-]*executive|\bneds?\b", win, re.I):
                    continue               # "core benefits are provided to all employees ..."
                if _EXEC_ONLY_RE.search(win) and not re.search(r"non[\s\-]*executive|\bneds?\b|\ball\s+directors|\bthe\s+directors", win, re.I):
                    continue               # an executive directors' incentive plan is not a NED benefit
                before = sent[max(0, m.start() - 110): m.start()]
                negated = bool(_NEG_RE.search(before)) or bool(re.search(r"\b(?:does|do|did|is|are)\s+not\b", win, re.I))
                entry = {'provided': not negated, 'detail': win.strip()[:220], 'page': pn}
                prev = found.get(key)
                if prev is None or (entry['provided'] and not prev['provided']):
                    found[key] = entry
    return found


_HON = r"(?:Mr|Mrs|Ms|Miss|Dr|Prof|Amb|Hon|Eng|CS|CPA)\."
_COMMITTEE_NAME = r"((?:[\w&'\-]+\s+){0,7}?Committee)"
_MEMBERS_RE = re.compile(
    r"members?\s+of\s+the\s+" + _COMMITTEE_NAME + r"(?:[^.]|" + _HON + r"){0,80}?\b(?:were|are|comprised|comprise|include[d]?)\s*:?\s*"
    r"((?:[^.]|" + _HON + r"){20,500}?)"
    r"(?<!\bMr)(?<!\bMrs)(?<!\bMs)(?<!\bMiss)(?<!\bDr)(?<!\bProf)(?<!\bAmb)(?<!\bHon)(?<!\bEng)(?<!\bCS)(?<!\bCPA)\.(?=\s+[A-Z]|\s*$)", re.I)
_MET_RE = re.compile(r"\b(?:met|held|convened)\s+(\w+)\s*(?:\(\s*(\d+)\s*\)\s*)?(?:times|meetings)\b", re.I)


def _split_names(chunk: str):
    chunk = re.sub(r"\(.*?\)", " ", chunk)
    parts = [p.strip() for p in re.split(r",|\band\b|&|;", chunk) if p.strip()]
    names = []
    for p in parts:
        hon, name = _split_honorific_and_name(p)
        if name and _looks_like_person(name):
            names.append((name, hon))
    return names


def extract_committee_prose(pdf_bytes: bytes) -> list:
    """[{'name', 'members': [name], 'meetings': int|None, 'page'}] from sentences like
    'The members of the Audit Committee during the year were A, B and C.'"""
    out, seen = [], set()
    for pn, text in _pypdf_page_texts_cached(pdf_bytes) or []:
        flat = re.sub(r"\s+", " ", text or "")
        for m in _MEMBERS_RE.finditer(flat):
            name = re.sub(r"^(?:the)\s+", "", m.group(1).strip(), flags=re.I)
            people = _split_names(m.group(2))
            if len(people) < 2 or name.lower() in seen:
                continue
            seen.add(name.lower())
            tail = flat[m.end(): m.end() + 900]
            mm = _MET_RE.search(tail)
            meetings = (_to_int(mm.group(2)) or _to_int(mm.group(1))) if mm else None
            out.append({'name': name, 'members': [p[0] for p in people],
                        'meetings': meetings if meetings and 1 <= meetings <= 40 else None, 'page': pn})
    return out


_COMMITTEE_HEADING_RE = re.compile(r"^(?!\u2022)([A-Z][A-Za-z ,&'\u2019\-]{2,68}\bCommittee)\s*$")
_ROSTER_ANCHOR_RE = re.compile(r"^Membership and attendance of committee meetings:?\s*$", re.I)
_ROSTER_ROLE_RE = re.compile(r"^(Chairperson|Chairman|Chairlady|Chair|Member)\*?\s*$", re.I)
_ROSTER_ATTEND_RE = re.compile(r"^\(\s*(\d+)\s*/\s*(\d+)\s*\)\*?\s*$")
_ROSTER_STOP_RE = re.compile(r"^(Key\s+focus\s+areas|Management\s+committees?)\b", re.I)


def extract_committee_roster(pdf_bytes: bytes) -> list:
    """[{'name', 'members': [{'name','role','attended','eligible'}], 'page'}] from
    reports whose committee membership is a line-broken roster rather than a sentence:
    a standalone '<...> Committee' heading, then later (same page or a following one)
    'Membership and attendance of committee meetings:' followed by repeating
    Name / Chairperson-or-Member / (attended/eligible) triples, one per line
    (confirmed on Equity Group Holdings' 2022 report, p.103-107). The committee name
    is carried forward from the nearest preceding heading line since the roster for a
    committee whose heading sits at the bottom of one page is often printed at the very
    top of the next. Stops at 'Key focus areas' or the next committee heading. A page
    with none of this - a table, a two-column layout, or no attendance figures at all -
    yields nothing here rather than a guess; that's what the AI tier is for."""
    out = []
    current_name = None
    for pn, text in _pypdf_page_texts_cached(pdf_bytes) or []:
        lines = [l.strip() for l in (text or '').split('\n')]
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line:
                i += 1
                continue
            hm = _COMMITTEE_HEADING_RE.match(line)
            if hm:
                current_name = re.sub(r"\s+", " ", hm.group(1)).strip()
                i += 1
                continue
            if _ROSTER_ANCHOR_RE.match(line) and current_name:
                members = []
                j = i + 1
                while j < len(lines):
                    nline = lines[j]
                    if not nline:
                        j += 1
                        continue
                    if _ROSTER_STOP_RE.match(nline) or _COMMITTEE_HEADING_RE.match(nline):
                        break
                    if j + 2 >= len(lines):
                        break
                    rname = _clean_person_name(nline)
                    rm = _ROSTER_ROLE_RE.match(lines[j + 1].strip())
                    am = _ROSTER_ATTEND_RE.match(lines[j + 2].strip())
                    if not (rm and am and _looks_like_person(rname)):
                        break
                    members.append({'name': rname, 'role': rm.group(1).title(),
                                     'attended': int(am.group(1)), 'eligible': int(am.group(2))})
                    j += 3
                if len(members) >= 2:
                    out.append({'name': current_name, 'members': members, 'page': pn})
                i = j
                continue
            i += 1
    return out


# 2026-09-26 addition: a THIRD real committee-roster shape, distinct from the
# triple-line roster above - confirmed on the NSE 2022/2023 Integrated Reports.
# A numbered heading ("1.  Derivatives Market Oversight Committee"), then a
# "Name  Nature of [P]articipation  Attendance" header, then one member per
# LINE ("J Swai Chairperson 3/4") rather than one member per three lines.
_NUMBERED_COMMITTEE_HEADING_RE = re.compile(r"^\d+\.\s+([A-Z][A-Za-z ,&\u2019'\-]{2,68}\bCommittee)\s*$")
_LINE_ROSTER_HEADER_RE = re.compile(r"^Name\s+Nature\s+of\s+[Pp]articipation\s+Attendance\s*$")
_LINE_ROSTER_ROW_RE = re.compile(
    r"^(?P<name>[A-Z][A-Za-z.\u2019'\- ]+?)\s{1,4}(?P<role>Chairperson|Chairman|Chairlady|Chair|Member)\s+"
    r"(?P<att>\d+)\s*/\s*(?P<elig>\d+)\s*$", re.I)


def extract_committee_roster_lines(pdf_bytes: bytes) -> list:
    """[{'name', 'members': [{'name','role','attended','eligible'}], 'page'}] from reports whose
    committee roster is one member per LINE ("J Swai Chairperson 3/4") under a "Name Nature of
    Participation Attendance" header, rather than the three-lines-per-member roster
    extract_committee_roster reads (both are real - this one confirmed on the NSE 2022/2023
    Integrated Reports, p.55: "1.  Derivatives Market Oversight Committee" then that header then
    three one-line rows). Committee headings here may be numbered ("1.  X Committee"); the bare,
    unnumbered heading extract_committee_roster also recognises still works too. Same discipline as
    that function: a page with neither line shape yields nothing here."""
    out = []
    current_name = None
    for pn, text in _pypdf_page_texts_cached(pdf_bytes) or []:
        lines = [l.strip() for l in (text or '').split('\n')]
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line:
                i += 1
                continue
            hm = _COMMITTEE_HEADING_RE.match(line) or _NUMBERED_COMMITTEE_HEADING_RE.match(line)
            if hm:
                current_name = re.sub(r"\s+", " ", hm.group(1)).strip()
                i += 1
                continue
            if _LINE_ROSTER_HEADER_RE.match(line) and current_name:
                members = []
                j = i + 1
                while j < len(lines):
                    nline = lines[j].strip()
                    if not nline:
                        j += 1
                        continue
                    rm = _LINE_ROSTER_ROW_RE.match(nline)
                    if not rm:
                        break
                    rname = _clean_person_name(rm.group('name'))
                    if not _looks_like_person(rname):
                        break
                    members.append({'name': rname, 'role': rm.group('role').title(),
                                     'attended': int(rm.group('att')), 'eligible': int(rm.group('elig'))})
                    j += 1
                if len(members) >= 2:
                    out.append({'name': current_name, 'members': members, 'page': pn})
                    current_name = None
                i = j
                continue
            i += 1
    return out


# A two-column "Roles & Responsibilities | Membership" layout, one committee per page
# (or per block on a page), heading = the nearest preceding "<...> Committee" line, name
# entries bulleted in the right column only ("• Mr. X - Chairman", "• Ms. Y (Co-opted
# member)"). Position-based (pdfplumber), since pdftotext's reading order interleaves
# the two columns' bullets unpredictably. Confirmed on a real filing: Britam Holdings
# Plc's 2025 Integrated Report, e.g. p.74 ("Customer Experience, Brand and Marketing
# Committee"), p.76 ("Nominations, Governance and Remuneration Committee").
_ROLES_MEMBERSHIP_HEADER_RE = re.compile(r"Roles\s*&\s*Responsibilities", re.I)
_COMMITTEE_NAME_LINE_RE = re.compile(r"^[A-Z][A-Za-z ,&\u2019'\-]{2,68}\bCommittee$")
_MEMBER_CHAIR_RE = re.compile(r"[-\u2010\u2011\u2012\u2013\u2014]\s*(Chairman|Chairperson|Chair|Chairlady)\b", re.I)


_NUM_COMMITTEE_HEAD_RE = re.compile(r"^\s*\d{1,2}\.\s+([A-Z][A-Za-z ,&'\u2019\-]{3,90}?\bCommittee)\s*(?:\(\s*Continued\s*\))?\s*$")
_NUM_MEMBERS_HEAD_RE = re.compile(r"members\s+of\s+the\s+(?:[A-Za-z ,&\-]{0,80})?committee\b[^:]{0,60}\bwere\s*:?\s*[-\u2013]?\s*$", re.I)
_NUM_MEMBER_LINE_RE = re.compile(r"^\s*\d{1,2}\.\s+(.{3,60}?)\s*(?:[-\u2013]\s*(chair\w*|member)\s*)?$", re.I)
_MEETING_DATE_HEAD_RE = re.compile(r"^\s*(?:[A-Z][a-z]{2}\.?[-/ ]?\d{2,4}\s*){2,12}$")
_TICK_TOKENS = ('\u221a', '\u2713', '\u2714', 'x', 'X', '\u00d7')


def extract_committee_numbered_members(pdf_bytes: bytes) -> list:
    """[{'name', 'members': [name], 'chair', 'meetings': int|None, 'attendance_rate': float|None, 'page'}]
    for committees written as a numbered heading ("1. Audit, Risk and Compliance Committee"), then a line
    "The members of the Committee during the period were: -" followed by a numbered list of people
    ("1. George Karanja -Chairman"), then an attendance grid of ticks under meeting dates
    ("Jun-24 Nov-24 Mar-25 May-25" / "George Karanja  \u221a \u221a \u221a \u221a"). Confirmed on Uchumi's 2025 report,
    where none of the sentence, roster or column readers can match because the sentence says "the Committee",
    not "the Audit Committee". Meetings = the number of dates in the grid header (never guessed when there is
    no grid); attendance rate = ticks / (members x meetings)."""
    out, by_name = [], {}
    current = None
    for pn, text in _pypdf_page_texts_cached(pdf_bytes) or []:
        lines = [ln.strip() for ln in (text or '').splitlines()]
        i = 0
        while i < len(lines):
            ln = lines[i]
            mh = _NUM_COMMITTEE_HEAD_RE.match(ln)
            if mh:
                nm = re.sub(r"\s+", " ", mh.group(1)).strip(" ,")
                # a heading whose first word is a number-list marker of a sentence is not a heading
                current = by_name.get(nm.lower())
                if current is None:
                    current = {'name': nm, 'members': [], 'chair': None, 'meetings': None,
                               'attendance_rate': None, 'page': pn, '_ticks': 0, '_cells': 0}
                    by_name[nm.lower()] = current
                    out.append(current)
                i += 1
                continue
            if current is not None and _NUM_MEMBERS_HEAD_RE.search(ln) and not current['members']:
                j = i + 1
                while j < len(lines):
                    mm = _NUM_MEMBER_LINE_RE.match(lines[j])
                    if not mm:
                        break
                    hon, name = _split_honorific_and_name(mm.group(1).strip(' -\u2013'))
                    if not name or not _looks_like_person(name):
                        break
                    current['members'].append(name)
                    if mm.group(2) and mm.group(2).lower().startswith('chair'):
                        current['chair'] = name
                    j += 1
                i = j
                continue
            if current is not None and current['members'] and _MEETING_DATE_HEAD_RE.match(ln) and current['meetings'] is None:
                n_dates = len(ln.split())
                j = i + 1
                ticks = cells = 0
                while j < len(lines):
                    toks = lines[j].split()
                    marks = [t for t in toks if t in _TICK_TOKENS]
                    if not marks or len(marks) != n_dates:
                        break
                    ticks += sum(1 for t in marks if t in ('\u221a', '\u2713', '\u2714'))
                    cells += len(marks)
                    j += 1
                if cells:
                    current['meetings'] = n_dates
                    current['_ticks'], current['_cells'] = ticks, cells
                    current['attendance_rate'] = round(100.0 * ticks / cells, 1)
                i = j
                continue
            i += 1
    result = []
    for c in out:
        if len(c['members']) >= 2:
            c.pop('_ticks', None); c.pop('_cells', None)
            result.append(c)
    return result


def extract_committee_membership_columns(pdf_bytes: bytes) -> list:
    """[{'name', 'members': [name], 'chair': name|None, 'meetings': None, 'page'}] from
    the two-column "Roles & Responsibilities | Membership" layout - see the module
    comment above. Position-based, so a page with no such header yields nothing here
    (extract_committee_prose / extract_committee_roster cover the other two layouts)."""
    out = []
    texts = _pypdf_page_texts_cached(pdf_bytes) or []
    pages = [pn for pn, t in texts if t and _ROLES_MEMBERSHIP_HEADER_RE.search(t)
            and re.search(r"\bmembership\b", t, re.I)]
    if not pages:
        return out
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pn in pages[:15]:
            for region in _regions(pdf.pages[pn - 1]):
                words = region.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
                rows = _group_rows(words, tol=3.0)
                head_idx = next((i for i, r in enumerate(rows) if _ROLES_MEMBERSHIP_HEADER_RE.search(r['text'])
                                 and re.search(r"\bmembership\b", r['text'], re.I)), None)
                if head_idx is None:
                    continue
                name = next((r['text'].strip() for r in reversed(rows[:head_idx])
                            if _COMMITTEE_NAME_LINE_RE.match(r['text'].strip())), None)
                if not name:
                    continue
                # the "Membership" HEADER word's own x0 is not a reliable column boundary (it can
                # sit well to the right of where the column's bullets actually start) - the bullet
                # characters themselves are: the right column's are the right-most bullet x0 on the
                # page, distinct from the left ("Roles & Responsibilities") column's own bullets.
                bullet_xs = sorted({w['x0'] for r in rows[head_idx + 1:] for w in r['words'] if w['text'] == '\u2022'})
                if not bullet_xs:
                    continue
                mem_x0 = bullet_xs[-1]
                stop_idx = len(rows)
                for i in range(head_idx + 1, len(rows)):
                    t = rows[i]['text'].strip()
                    if re.match(r"^(As\s+at|Focus\s+Areas|Key\s+focus)", t, re.I) or _COMMITTEE_NAME_LINE_RE.match(t):
                        stop_idx = i
                        break
                right_words = sorted(
                    ((r['top'], w['x0'], w['text']) for r in rows[head_idx + 1: stop_idx] for w in r['words']
                     if w['x0'] >= mem_x0 - 5),
                    key=lambda w: (w[0], w[1]))
                flat = " ".join(w[2] for w in right_words)
                members, chair = [], None
                for chunk in (c.strip() for c in flat.split('\u2022') if c.strip()):
                    cm = _MEMBER_CHAIR_RE.search(chunk)
                    is_chair = bool(cm)
                    nm_part = chunk[:cm.start()] if cm else chunk
                    nm_part = re.sub(r"\(.*?\)", "", nm_part).strip(" -\u2010\u2011\u2012\u2013\u2014")
                    hon, nm = _split_honorific_and_name(nm_part)
                    if not _looks_like_person(nm):
                        continue
                    members.append(nm)
                    if is_chair:
                        chair = nm
                if len(members) >= 2:
                    out.append({'name': name, 'members': members, 'chair': chair, 'meetings': None, 'page': pn})
    return out


# 2026-09-26 addition: a FOURTH real committee layout, distinct from the bulleted
# two-column one above - confirmed on Equity Group Holdings' 2024 Integrated Report,
# p.104-107. Three columns ("Roles and/& Responsibilities | Membership | Attendance"),
# with NO bullet between members - each is just its own line in the Membership column,
# a chair's role ("Chairperson") sometimes wrapping onto its own following line, and the
# Attendance fraction ("4/4") aligned with only the FIRST line of each member (a role-
# continuation line carries no fraction). One page can carry TWO committees' tables back
# to back (heading, heading, table, table - not heading, table, heading, table), so
# headings are queued in the order they're seen and each table claims the next one in
# that queue, rather than "nearest preceding heading" (which would misattribute the
# first table to the second heading in that case).
#
# The Membership and Attendance columns are read out INDEPENDENTLY of each other and
# of the (much bulletier, differently-spaced) Roles column, rather than by matching them
# up through board_extract._group_rows' single shared row grouping. That was tried first
# and undercounted a real committee (7 of 8 Sustainability Committee members) - _group_rows
# chains words together by comparing each new word only to the last one *already in that
# bucket*, so once the Roles column's tighter-spaced bullet-wrap lines nudge a row's
# apparent position, a later Attendance fraction can drift into the row above the name it
# actually belongs to instead of its own. Clustering each column's words by their own top
# position, using only words in that column's x-range, avoids that cross-column drift
# entirely, and a bare role word ("Chairperson" alone, no name) is what marks a Membership
# line as a continuation of the previous member rather than needing it to still be aligned
# with an Attendance figure.
_TABLE_STOP_RE = re.compile(r"^(Key\s+focus\s+areas)\b", re.I)
_BARE_ROLE_LINE_RE = re.compile(r"^(Chairperson|Chairman|Chairlady|Chair|Member)\*?$", re.I)
_CLEAN_FRACTION_RE = re.compile(r"^(\d+)\s*/\s*(\d+)$")


def _cluster_by_top(words, tol=3.0):
    """Group words into visual lines by top position, comparing each new word only
    to the LAST word already placed (not the bucket's first word), so a run of
    lines spaced further apart than tol never daisy-chains into one bucket."""
    lines = []
    for w in sorted(words, key=lambda w: (w['top'], w['x0'])):
        if lines and abs(lines[-1]['words'][-1]['top'] - w['top']) <= tol:
            lines[-1]['words'].append(w)
        else:
            lines.append({'words': [w]})
    for l in lines:
        l['words'].sort(key=lambda w: w['x0'])
        l['text'] = " ".join(w['text'] for w in l['words']).strip()
    return lines


def extract_committee_membership_table(pdf_bytes: bytes) -> list:
    """[{'name', 'members': [{'name','role','attended','eligible'}], 'page'}] from the three-column
    "Roles and Responsibilities | Membership | Attendance" layout - see the module comment above.
    Position-based (pdfplumber), since the Membership and Attendance columns are read out as two
    separate blocks (all the names, then all the fractions) by plain text extraction rather than
    interleaved. A page with no "Membership" + "Attendance" header row yields nothing here."""
    out = []
    texts = _pypdf_page_texts_cached(pdf_bytes) or []
    pages = [pn for pn, t in texts if t and re.search(r"\bmembership\b", t, re.I) and re.search(r"\battendance\b", t, re.I)]
    if not pages:
        return out
    heading_queue = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pn in pages[:30]:
            for region in _regions(pdf.pages[pn - 1]):
                words = region.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
                rows = _group_rows(words, tol=3.0)   # whole-row text is still fine for HEADING/STOP text matches
                i = 0
                while i < len(rows):
                    row = rows[i]
                    hm = _COMMITTEE_HEADING_RE.match(row['text'].strip())
                    if hm:
                        heading_queue.append(re.sub(r"\s+", " ", hm.group(1)).strip())
                        i += 1
                        continue
                    mem_w = next((w for w in row['words'] if w['text'].strip().lower() == 'membership'), None)
                    att_w = next((w for w in row['words'] if w['text'].strip().lower() == 'attendance'), None)
                    if not (mem_w and att_w):
                        i += 1
                        continue
                    if not heading_queue:
                        i += 1
                        continue
                    name = heading_queue.pop(0)
                    mem_x0, att_x0, top0 = mem_w['x0'], att_w['x0'], mem_w['top']
                    # Find where this table ends (next stop/heading/header row's top), scanning the
                    # already-computed whole-row text - just for the Y boundary, not for column data.
                    stop_top = None
                    for j in range(i + 1, min(len(rows), i + 200)):
                        rtext = rows[j]['text'].strip()
                        if _TABLE_STOP_RE.match(rtext) or _COMMITTEE_HEADING_RE.match(rtext) or \
                                any(w['text'].strip().lower() == 'membership' for w in rows[j]['words']):
                            stop_top = rows[j]['words'][0]['top']
                            break
                    lo, hi = top0 + 1, stop_top if stop_top is not None else 1e9
                    mem_words = [w for w in words if lo < w['top'] < hi and mem_x0 - 8 <= w['x0'] < att_x0 - 15]
                    att_words = [w for w in words if lo < w['top'] < hi and w['x0'] >= att_x0 - 15]
                    mem_lines = _cluster_by_top(mem_words)
                    att_fracs = [m for l in _cluster_by_top(att_words) for m in [_CLEAN_FRACTION_RE.match(l['text'])] if m]
                    members, k = [], 0
                    for l in mem_lines:
                        if _BARE_ROLE_LINE_RE.match(l['text']):
                            if members:
                                members[-1]['name_parts'].append(l['text'])
                            continue
                        if k >= len(att_fracs):
                            break   # more name lines than attendance figures - stop rather than guess
                        members.append({'name_parts': [l['text']], 'attended': int(att_fracs[k].group(1)),
                                         'eligible': int(att_fracs[k].group(2))})
                        k += 1
                    parsed = []
                    for m in members:
                        chunk = " ".join(m['name_parts'])
                        cm = _MEMBER_CHAIR_RE.search(chunk)
                        role = cm.group(1).title() if cm else 'Member'
                        nm_part = chunk[:cm.start()] if cm else chunk
                        nm_part = re.sub(r"\(.*?\)", "", nm_part).strip(" -\u2010\u2011\u2012\u2013\u2014*")
                        hon, nm = _split_honorific_and_name(nm_part)
                        if not _looks_like_person(nm):
                            continue
                        parsed.append({'name': nm, 'role': role, 'attended': m['attended'], 'eligible': m['eligible']})
                    if len(parsed) >= 2 and len(parsed) == len(att_fracs):
                        out.append({'name': name, 'members': parsed, 'page': pn})
                    i = i + 1
    return out


# ------------------------------------------------------------ NED pay policy
#
# 2026-09-25 fix: this used to cover only 3 of the 10 Company-Level Remuneration
# fields the Survey Report's Remuneration tab actually shows (chairperson_annual_
# retainer, other_ned_annual_retainer, other_ned_meeting_allowance) - the other 7
# (chairperson_meeting_allowance, both executive_director_* fields, and all 4
# committee_chair_*/committee_member_* fields) had NO regex at all, so they were
# guaranteed to read "Not available" no matter what the filing said. Fixed by
# giving every field its own anchored sentence pattern below.
#
# A capacity-table reading pass (header row names columns left-to-right, a
# labelled row below supplies the numbers) was also attempted here and then
# DELIBERATELY DROPPED, not left half-done: tested against a realistic
# per-committee breakdown ("Audit Committee / Chairman 1,761,800 / Member
# 880,900" for one committee, a different pair for another), a bare
# "Chairman | Member" table header is indistinguishable from a genuine
# board-wide "chair vs other NED" policy table using only text positions -
# it mis-read the Audit Committee's own member rate as the board-wide
# other_ned_annual_retainer. That is exactly the mistake
# test_ned_policy_sentence_gives_the_chairman_retainer_and_never_guesses_
# committee_rates (test_policy_extract.py) exists to catch, just landing on a
# different field. Getting this right needs real column x-positions (the way
# extract_committee_membership_columns already does it with pdfplumber word
# boxes), which is a bigger, separate piece of work - left for that, or for
# the AI/manual-entry paths, rather than shipped half-safe.
_CUR = r"(?:KES|KSh|Ksh|Kshs|KShs|Shs?\.?|Sh\.?|Kenya\s+Shillings?)"
_AMT = r"([\d][\d ,]*(?:\.\d+)?)\s*(million|m\b|thousand|k\b)?"

# Anchor phrases shared by several fields below, so each field's pattern only
# needs to say who it's about, not repeat all the wording variants.
_A_CHAIR = r"chair(?:man|person|lady)?"
_A_OTHER_NED = r"(?:(?:other\s+)?non[\s\-]*executive\s+directors?|\bneds?\b)"
_A_EXEC_DIR = r"(?<!non-)(?<!non\s)executive\s+directors?"
_A_COMM_CHAIR = r"(?:committee\s+chair(?:man|person|lady)?s?|chair(?:man|person|lady)?s?\s+of\s+(?:a|the|each|every)?\s*committees?)"
_A_COMM_MEMBER = r"(?:committee\s+members?|members?\s+of\s+(?:a|the|each|every)?\s*committees?)"

# Real filings phrase this bullet-by-bullet ("• Sitting allowance payable to
# the Chairman of the Board retained at KShs 230,000 per meeting.") at least
# as often as a single flowing sentence ("The Chairman is entitled to an
# annual retainer of Shs 10,650,400"), and the two put the role name and the
# amount in the OPPOSITE order ("fees paid to the Chairman ... at X" vs "the
# Chairman is paid a fee of X"). Rather than chase every order, each bullet/
# sentence is checked with two independent, order-agnostic conditions: does
# it name this role, and does it separately carry a qualifying amount. Both
# conditions have to hold in the SAME sentence (from _sentences(), which
# already splits on bullet markers as well as periods) - that's still a real
# constraint, just not an ordering one.

def _distinct_amounts(sentence):
    """Distinct currency amounts printed in one sentence. More than one means the "sentence" is really a
    flattened table row (\"Honoraria Shs 80,000 N/A / Sitting allowance Shs 20,000 ...\") and the first
    amount cannot be trusted to belong to the role/field being asked about (2026-09-28: KPLC's chairman
    honoraria of Shs 80,000 a MONTH was being stored as both the chair's annual retainer and the chair's
    meeting allowance)."""
    vals = set()
    for m in re.finditer(_CUR + r"\s*" + _AMT, sentence, re.I):
        v = _amount(m.group(1), m.group(2))
        if v and v >= 100:
            vals.add(v)
    return vals

def _has_retainer_amount(sentence):
    """A currency amount in the sentence, present alongside a retainer/fee
    word (anywhere in the sentence, either order) and NOT immediately
    followed by a per-meeting/sitting tail (that's the meeting-allowance
    case, checked separately) - so "fees ... to the Chairman ... at Shs X"
    and "the Chairman ... paid a fee of Shs X" both count, but a bare
    "Shs X" with no fee-ish word anywhere nearby (e.g. a share count or an
    unrelated cost) does not. Also excludes a Directors' Emoluments-style
    AGGREGATE disclosure ("the total Non-Executive Directors' remuneration
    for the year was KShs 82.6 million") - that's a fiscal-year total across
    every non-executive director combined, not the per-role annual policy
    figure this field means, even though it names the role and a currency
    amount too. "remuneration" is deliberately not one of the trigger
    words below for the same reason - in practice it shows up on this kind
    of total far more often than on a per-role retainer statement."""
    if re.search(r"\btotal\b|\baggregate\b|\bcombined\b|\bcumulative\b", sentence, re.I):
        return None
    if len(_distinct_amounts(sentence)) > 1:
        return None
    if not re.search(r"(?:retainers?|fees?)\b", sentence, re.I):
        return None
    m = re.search(_CUR + r"\s*" + _AMT + r"(?![^.]{0,20}?per\s+(?:board\s+)?(?:meeting|sitting))", sentence, re.I)
    return _amount(m.group(1), m.group(2)) if m else None

def _has_meeting_amount(sentence):
    """A currency amount followed (within the same sentence) by "per
    meeting"/"per sitting" - the amount itself is the unambiguous signal
    here, so no separate keyword is required (mirrors the loose but safe
    design that already worked for this field before this fix)."""
    if len(_distinct_amounts(sentence)) > 1:
        return None
    m = re.search(_CUR + r"\s*" + _AMT + r"[^.]{0,30}?per\s+(?:board\s+)?(?:meeting|sitting)", sentence, re.I)
    return _amount(m.group(1), m.group(2)) if m else None

_POLICY_FIELD_ANCHORS = [
    # (field, anchor regex fragment, 'retainer' | 'meeting')
    ('chairperson_annual_retainer', _A_CHAIR, 'retainer'),
    ('other_ned_annual_retainer', _A_OTHER_NED, 'retainer'),
    ('executive_director_annual_retainer', _A_EXEC_DIR, 'retainer'),
    ('committee_chair_annual_retainer', _A_COMM_CHAIR, 'retainer'),
    ('committee_member_annual_retainer', _A_COMM_MEMBER, 'retainer'),
    ('chairperson_meeting_allowance', _A_CHAIR, 'meeting'),
    ('other_ned_meeting_allowance', _A_OTHER_NED, 'meeting'),
    ('executive_director_meeting_allowance', _A_EXEC_DIR, 'meeting'),
    ('committee_chair_meeting_allowance', _A_COMM_CHAIR, 'meeting'),
    ('committee_member_meeting_allowance', _A_COMM_MEMBER, 'meeting'),
]


def _amount(num: str, scale: str | None):
    try:
        v = float(re.sub(r"[ ,]", "", num))
    except ValueError:
        return None
    return v * {'million': 1e6, 'm': 1e6, 'thousand': 1e3, 'k': 1e3}.get((scale or '').lower(), 1.0)


def _extract_ned_policy_sentences(pdf_bytes: bytes) -> dict:
    """{field: {'value': float (Shs, whole units), 'page', 'text'}} for the NED/executive-director/
    committee pay policy the report STATES, one sentence or bullet at a time (via _sentences(),
    same splitter extract_ned_benefits already uses) - e.g. "The Board Chairman is entitled to an
    annual retainer of Shs 10,650,400" or "Sitting allowance payable to the Chairman of the Board
    retained at KShs 230,000 per meeting." A committee table whose rows differ (chair 1,761,800 in
    one committee, 1,122,200 in another) is NOT reduced to one number - the survey field has one
    slot, so it stays empty rather than picking a committee (see the long comment above
    _CUR for why a table-reading pass isn't attempted here)."""
    out = {}
    for pn, text in _pypdf_page_texts_cached(pdf_bytes) or []:
        if not re.search(r"non[\s\-]*executive", text or '', re.I):
            continue
        for sentence in _sentences(text):
            for field, anchor, kind in _POLICY_FIELD_ANCHORS:
                if field in out:
                    continue
                if not re.search(anchor, sentence, re.I):
                    continue
                v = _has_meeting_amount(sentence) if kind == 'meeting' else _has_retainer_amount(sentence)
                if v and v >= 100:
                    out[field] = {'value': v, 'page': pn, 'text': sentence[:160]}
    return out



# ---------------------------------------------------- NED pay policy: table readers (2026-09-28)
# Most filers print director pay as a TABLE, not a sentence, so the sentence reader above finds nothing.
# Two layouts are read here, in this priority order (a stated schedule beats figures derived from what
# individual directors happened to be paid):
#   1. extract_fee_schedule()  - "entitlement per Board Member" table with a Chairman column and a Member
#      column (KPLC: Honoraria per month / Sitting allowance per sitting / Directors' fees per annum).
#   2. extract_ned_fee_table() - one row per NED with a fees/retainer column and a sitting/attendance
#      column (Liberty, Bamburi, BOC). The chair and other-NED retainer are DERIVED from those rows, so
#      they carry a lower confidence and the source text says so.
_AMT_TOKEN = re.compile(r"(N/?A\*?|(?:" + _CUR + r")\s*[\d][\d,]*(?:\.\d+)?)", re.I)
_SKIP_LABEL = re.compile(r"telephone|airtime|lunch|transport|mileage|accommodation|travel|bonus|medical|insurance|subsistence|per\s+diem", re.I)


def _amt_tokens(line):
    """[value | None] for each 'Shs 80,000' / 'N/A' cell on the line, left to right."""
    out = []
    for m in _AMT_TOKEN.finditer(line):
        tok = m.group(1)
        if tok.upper().startswith('N'):
            out.append(None)
        else:
            num = re.search(r"[\d][\d,]*(?:\.\d+)?", tok).group(0)
            out.append(_amount(num, None))
    return out


def extract_fee_schedule(pdf_bytes: bytes) -> dict:
    """{field: {'value', 'page', 'text', 'confidence'}} from a 'Type of payment | Chairman | Member' table."""
    out = {}
    for pn, text in _pypdf_page_texts_cached(pdf_bytes) or []:
        lines = (text or '').split('\n')
        for i, ln in enumerate(lines):
            if not (re.search(r"chair(?:man|person)?", ln, re.I) and re.search(r"\bmembers?\b|\bdirectors?\b", ln, re.I)
                    and re.search(r"type|payment|entitlement|item|allowance", ln + ' ' + (lines[i - 1] if i else ''), re.I)
                    and not _amt_tokens(ln)):
                continue
            chair_ret = other_ret = None
            parts = []
            buf = []                                   # label lines seen since the last amount row (labels wrap)
            for j in range(i + 1, min(i + 30, len(lines))):
                row = lines[j]
                if re.match(r"\s*(?:\*|INFORMATION|Directors?\W{0,2}\s+(?:emoluments|remuneration)\b)", row):
                    break
                toks = _amt_tokens(row)
                if len(toks) < 2:
                    buf = (buf + [row])[-4:]
                    continue
                row_text = ' '.join(buf + [row])
                buf = []
                label = re.sub(_AMT_TOKEN, '', row_text)
                if _SKIP_LABEL.search(label):
                    continue
                chair_v, other_v = toks[0], toks[1]
                if re.search(r"sitting|attendance|per\s+(?:sitting|meeting)", label, re.I):
                    for f, v in (('chairperson_meeting_allowance', chair_v), ('other_ned_meeting_allowance', other_v)):
                        if v and v >= 100 and f not in out:
                            out[f] = {'value': v, 'page': pn, 'confidence': 0.85,
                                      'text': ('fee schedule: ' + re.sub(r"\s+", ' ', row_text).strip())[:160]}
                elif re.search(r"honorari|retainer|\bfees?\b|per\s+annum|per\s+year|per\s+month|monthly", label, re.I):
                    per = 12.0 if re.search(r"per\s+month|monthly", label, re.I) else 1.0
                    if chair_v:
                        chair_ret = (chair_ret or 0) + chair_v * per
                    if other_v:
                        other_ret = (other_ret or 0) + other_v * per
                    parts.append(re.sub(r"\s+", ' ', row_text).strip())
            if parts:
                note = 'fee schedule (annualised; monthly honoraria x12 plus annual fees): ' + ' | '.join(parts)
                for f, v in (('chairperson_annual_retainer', chair_ret), ('other_ned_annual_retainer', other_ret)):
                    if v and v >= 100 and f not in out:
                        out[f] = {'value': v, 'page': pn, 'confidence': 0.8, 'text': note[:160]}
            if out:
                return out
    return out


_TBL_NUM = r"(?:\d[\d,]*(?:\.\d+)?|[-\u2013\u2014])"
_TBL_ROW = re.compile(r"^\s*(?:\d{1,2}\.\s+)?(?P<name>[A-Za-z][^\d\n]{2,70}?)\s+(?P<nums>(?:" + _TBL_NUM + r"\s+){1,6}" + _TBL_NUM + r")\s*$")


def _tbl_num(tok):
    tok = tok.strip()
    if tok in ('-', '\u2013', '\u2014'):
        return 0.0
    try:
        return float(tok.replace(',', ''))
    except ValueError:
        return None


def _mode_or_median(vals):
    from collections import Counter
    from statistics import median
    top = Counter(vals).most_common()
    if top and top[0][1] >= 2:
        return top[0][0]
    return median(vals)


def extract_ned_fee_table(pdf_bytes: bytes) -> dict:
    """Chair and other-NED annual retainer DERIVED from a per-director fee table (fee/retainer column plus a
    sitting/attendance column). Only the current-year columns (the first block) are read, footnoted rows
    (a name ending in *) are skipped because they include back-pay, and the other-NED figure is the most
    common fee (median when nothing repeats) so directors who served part of the year don't drag it down."""
    for pn, text in _pypdf_page_texts_cached(pdf_bytes) or []:
        low = (text or '').lower()
        if not re.search(r"non[\s\-]*executive", low) or not re.search(r"sitting|attendance", low) \
                or not re.search(r"retainer|\bfees\b", low):
            continue
        lines = text.split('\n')
        head = None
        for i, ln in enumerate(lines):
            if re.search(r"retainer|\bfees\b", ln, re.I) and re.search(r"sitting|attendance|allowance", ln + ' ' + ' '.join(lines[i:i + 3]), re.I):
                head = i
                break
        if head is None:
            continue
        header_txt = ' '.join(lines[max(0, head - 3): head + 6])
        mult = 1e6 if re.search(r"(?:k?shs?|kes)\.?\s*(?:m|mn|million)\b", header_txt, re.I) else \
            1e3 if re.search(r"[\u2018\u2019']?000|thousand", header_txt, re.I) else 1.0
        four_col = bool(re.search(r"annual\s+fees\s+2025", header_txt, re.I) and re.search(r"2024\s+retainer", header_txt, re.I))       # Bamburi: 2025 total, then 2024 columns
        rows = []
        for ln in lines[head + 1: head + 40]:
            m = _TBL_ROW.match(ln)
            if not m or re.match(r"\s*(?:total|totals|grand)", m.group('name'), re.I):
                if m and re.match(r"\s*(?:total|totals|grand)", m.group('name'), re.I) and rows:
                    break
                continue
            name = m.group('name').strip()
            toks = [_tbl_num(t) for t in m.group('nums').split()]
            if None in toks or len(toks) < 3 and not four_col:
                continue
            if name.endswith('*') or '*' in name:
                continue
            fee = toks[1] if four_col and len(toks) >= 3 else toks[0]
            rows.append({'name': name, 'fee': fee * mult, 'chair': bool(re.search(r"chair", name, re.I))})
        if len(rows) < 3:
            continue
        found = {}
        basis = 'the 2024 retainer column (report states NED pay was not increased in 2025)' if four_col else 'the current-year fee column'
        others = [r['fee'] for r in rows if not r['chair'] and r['fee'] > 0]
        chairs = [r['fee'] for r in rows if r['chair'] and r['fee'] > 0]
        if len(others) >= 2:
            from collections import Counter
            repeated = Counter(others).most_common(1)[0][1] >= 2
            found['other_ned_annual_retainer'] = {
                'value': float(_mode_or_median(others)), 'page': pn,
                'confidence': 0.5 if four_col else (0.6 if repeated else 0.45),
                'text': (f"derived from {len(others)} NED rows of the fee table, {basis}: "
                         + ('most common fee' if repeated else 'median fee paid (no common rate; part-year directors included)'))[:160]}
        if chairs:
            found['chairperson_annual_retainer'] = {
                'value': float(max(chairs)), 'page': pn, 'confidence': 0.5,
                'text': f"derived from the fee-table row labelled chairman, {basis}"[:160]}
        if found:
            return found
    return {}


def extract_ned_policy(pdf_bytes: bytes) -> dict:
    """Merged NED/committee pay policy: stated schedule > policy sentence > figures derived from a per-director
    fee table. Same {field: {'value','page','text'}} shape as before, plus an optional 'confidence'."""
    out = {}
    for reader in (extract_fee_schedule, _extract_ned_policy_sentences, extract_ned_fee_table):
        try:
            found = reader(pdf_bytes)
        except Exception:
            continue
        for field, info in (found or {}).items():
            out.setdefault(field, info)
    return out

# ---------------------------------------------------------------- CEO pay
_CEO_TITLE_RE = re.compile(r"chief\s+executive|managing\s+director|\bceo\b|group\s+md", re.I)
_CEO_HEADER_RE = re.compile(r"^\s*(?P<name>[A-Z][A-Za-z'.\-]+(?:\s+[A-Z][A-Za-z'.\-]+){1,4})\s*,\s*(?P<title>[^,]{3,90}?)\s*(?:\s{2,}.*)?$")
_CEO_LABELS = [
    ('deferred_incentive', re.compile(r"deferred\s+(?:bonus|incentive|award)|\bcvp\b", re.I)),
    ('non_cash_benefits', re.compile(r"other\s+(?:employee\s+)?benefits|non[\s\-]*cash\s+benefits|benefits\s+in\s+kind|estimated\s+value", re.I)),
    ('pension', re.compile(r"retirement\s+benefits?|pension|provident", re.I)),
    ('incentive_bonus', re.compile(r"cash\s+bonus|(?<!deferred\s)bonus|short[\s\-]term\s+incentive", re.I)),
    ('allowances', re.compile(r"\ballowances?\b", re.I)),
    ('cost_of_employment', re.compile(r"total\s+remuneration|cost\s+to\s+company|total\s+cost\s+of\s+employment|total\s+emoluments", re.I)),
    ('salary', re.compile(r"(?:base|basic|annual)?\s*salary|gross\s+salary", re.I)),
]


_EXEC_TITLE_RE = re.compile(r"chief\s+(?:executive|financial|operating|risk|commercial)|managing\s+director|\bceo\b|\bcfo\b|group\s+md|executive\s+director|finance\s+director", re.I)

# how a block's components are stored on the Remuneration / Benefits tabs (names chosen so the Benefits
# tab's benefit-column pattern - benefit|pension|allowance|... - recognises the benefit-like ones)
COMPONENT_DISPLAY = {
    'salary': 'Base salary', 'allowances': 'Allowances', 'pension': 'Retirement benefits',
    'non_cash_benefits': 'Other employee benefits', 'incentive_bonus': 'Cash bonus',
    'deferred_incentive': 'Deferred bonus', 'cost_of_employment': 'Total remuneration',
}


def extract_executive_pay_blocks(pdf_bytes: bytes) -> list:
    """Every executive director's labelled pay block:
        Abdi Mohamed, Managing Director                 Shs      Shs
        Base salary        53 399 838   50 445 591
        ...
        Total remuneration (cost to company)   120 098 469  109 781 904
    -> [{'name','title','unit','components': {salary, pension, ...} (ANNUAL, first numeric column =
    current year), 'page','text'}]. "Total fixed" / "Total variable" sub-totals are never the total."""
    from pdf_parse import _split_label_and_numbers
    blocks, seen = [], set()
    for pn, text in _pypdf_page_texts_cached(pdf_bytes) or []:
        raw_lines = [l.rstrip() for l in (text or '').splitlines()]
        lines = []
        for ln in raw_lines:       # a wrapped label ("Cash bonus (non-def" / "erred) 22 855 445 ...") is re-joined
            if lines and re.match(r"^[a-z]+\)?\s+[\d(]", ln) and not re.search(r"\d", lines[-1]):
                lines[-1] = lines[-1] + ln
            else:
                lines.append(ln)
        for i, line in enumerate(lines):
            m = _CEO_HEADER_RE.match(line)
            if not m or not _EXEC_TITLE_RE.search(m.group('title')):
                continue
            comps, seen_any = {}, False
            for nxt in lines[i + 1: i + 16]:
                if _CEO_HEADER_RE.match(nxt) and not re.search(r"\d", nxt):
                    break
                label, nums = _split_label_and_numbers(nxt, has_note_column=False)
                if not label or not nums or nums[0] is None:
                    continue
                if re.search(r"total\s+(?:fixed|variable)", label.lower()):
                    continue
                for key, rx in _CEO_LABELS:
                    if rx.search(label) and key not in comps:
                        comps[key] = float(nums[0]); seen_any = True
                        break
            head = " ".join(lines[max(0, i - 3): i + 6])
            unit = ('thousands' if re.search(r"(?:shs?|kshs?|kes)\.?\s*['\u2018\u2019]?\s*000", head, re.I) else
                    'millions' if re.search(r"(?:shs?|kshs?|kes)\.?\s*['\u2018\u2019]?\s*million", head, re.I) else 'units')
            name = m.group('name').strip()
            if seen_any and ('cost_of_employment' in comps or 'salary' in comps) and name.lower() not in seen:
                seen.add(name.lower())
                blocks.append({'name': name, 'title': re.sub(r"(?:\s+(?:Shs?|KShs?|Kshs?|KES))+$", "", m.group('title').strip()), 'unit': unit, 'components': comps,
                               'page': pn, 'text': line.strip()[:120]})
    return blocks


def extract_ceo_pay(pdf_bytes: bytes) -> dict | None:
    """The CEO's / Managing Director's block (first executive block whose title says so)."""
    for b in extract_executive_pay_blocks(pdf_bytes):
        if _CEO_TITLE_RE.search(b['title']):
            return b
    return None