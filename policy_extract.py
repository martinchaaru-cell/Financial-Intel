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
import re

from board_extract import _split_honorific_and_name, _looks_like_person, _to_int
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


# ------------------------------------------------------------ NED pay policy
_CUR = r"(?:KES|KSh|Ksh|Kshs|KShs|Shs?\.?|Sh\.?|Kenya\s+Shillings?)"
_AMT = r"([\d][\d ,]*(?:\.\d+)?)\s*(million|m\b|thousand|k\b)?"
_POLICY_SENTENCES = [
    # (field, regex over ONE flattened page, period)
    ('chairperson_annual_retainer', re.compile(
        r"chair(?:man|person)?[^.]{0,120}?(?:entitled\s+to|receives?|is\s+paid|paid)\s+an?\s+(?:annual\s+)?(?:retainer|fee)\s+of\s+" + _CUR + r"\s*" + _AMT, re.I)),
    ('other_ned_annual_retainer', re.compile(
        r"(?:other\s+)?non[\s\-]*executive\s+directors?[^.]{0,80}?(?:entitled\s+to|receive|are\s+paid)\s+an?\s+(?:annual\s+)?(?:retainer|fee)\s+of\s+" + _CUR + r"\s*" + _AMT, re.I)),
    ('other_ned_meeting_allowance', re.compile(
        r"(?:sitting\s+(?:allowance|fee)|meeting\s+allowance)[^.]{0,60}?(?:of|is|at)\s+" + _CUR + r"\s*" + _AMT + r"[^.]{0,30}?per\s+(?:meeting|sitting)", re.I)),
]
_TABLE_ROW_RE = re.compile(r"^\s*(?P<label>[A-Za-z][A-Za-z &/\-']{3,60}?)\s{2,}(?P<chair>-|[\d ]{5,})\s{2,}(?P<member>-|[\d ]{5,})\b")


def _amount(num: str, scale: str | None):
    try:
        v = float(re.sub(r"[ ,]", "", num))
    except ValueError:
        return None
    return v * {'million': 1e6, 'm': 1e6, 'thousand': 1e3, 'k': 1e3}.get((scale or '').lower(), 1.0)


def extract_ned_policy(pdf_bytes: bytes) -> dict:
    """{field: {'value': float (Shs, whole units), 'page', 'text'}} for the NED pay policy the report
    STATES: a sentence ("The Board Chairman is entitled to an annual retainer of Shs 10,650,400"), or a
    capacity table row ("Board of Directors  -  3 428 100": chairman '-', member 3 428 100). A committee
    table whose rows differ (chair 1,761,800 in one committee, 1,122,200 in another) is NOT reduced to one
    number - the survey field has one slot, so it stays empty rather than picking a committee."""
    out = {}
    for pn, text in _pypdf_page_texts_cached(pdf_bytes) or []:
        if not re.search(r"non[\s\-]*executive", text or '', re.I):
            continue
        flat = re.sub(r"\s+", " ", text or '')
        for field, rx in _POLICY_SENTENCES:
            if field in out:
                continue
            m = rx.search(flat)
            if m:
                v = _amount(m.group(1), m.group(2))
                if v and v >= 100:
                    out[field] = {'value': v, 'page': pn, 'text': m.group(0)[:160]}
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


def extract_ceo_pay(pdf_bytes: bytes) -> dict | None:
    """The CEO's / MD's pay for the CURRENT year from a labelled block:
        Abdi Mohamed, Managing Director and Chief Executive Officer      2025    2024
        Base salary        48 000 000     45 000 000
        Retirement benefits ...
        Total remuneration (cost to company)   ...
    Returns {'name', 'title', 'unit', 'components': {salary, pension, ...}, 'page', 'text'} (annual figures,
    first numeric column = current year) or None. "Total fixed" / "Total variable" sub-totals are never
    taken as the total."""
    from pdf_parse import _split_label_and_numbers
    for pn, text in _pypdf_page_texts_cached(pdf_bytes) or []:
        raw_lines = [l.rstrip() for l in (text or '').splitlines()]
        # a label can wrap ("Cash bonus (non-def" / "erred) 22 855 445 19 801 301"): re-join a
        # figure-less line with the next line when that one starts with a lower-case fragment
        lines = []
        for ln in raw_lines:
            if lines and re.match(r"^[a-z]+\)?\s+[\d(]", ln) and not re.search(r"\d", lines[-1]):
                lines[-1] = lines[-1] + ln
            else:
                lines.append(ln)
        for i, line in enumerate(lines):
            m = _CEO_HEADER_RE.match(line)
            if not m or not _CEO_TITLE_RE.search(m.group('title')):
                continue
            comps, seen_any = {}, False
            for nxt in lines[i + 1: i + 16]:
                if _CEO_HEADER_RE.match(nxt) and not re.search(r"\d", nxt):
                    break
                label, nums = _split_label_and_numbers(nxt, has_note_column=False)
                if not label or not nums or nums[0] is None:
                    continue
                low = label.lower()
                if re.search(r"total\s+(?:fixed|variable)", low):
                    continue
                for key, rx in _CEO_LABELS:
                    if rx.search(label) and key not in comps:
                        comps[key] = float(nums[0]); seen_any = True
                        break
            head = " ".join(lines[max(0, i - 3): i + 6])
            unit = ('thousands' if re.search(r"(?:shs?|kshs?|kes)\s*['\u2019]?\s*000", head, re.I) else
                    'millions' if re.search(r"(?:shs?|kshs?|kes)\s*['\u2019]?\s*million", head, re.I) else 'units')
            if seen_any and ('cost_of_employment' in comps or 'salary' in comps):
                return {'name': m.group('name').strip(), 'title': m.group('title').strip(), 'unit': unit,
                        'components': comps, 'page': pn, 'text': line.strip()[:120]}
    return None
