"""Fallback reader for per-director pay tables, run only when the word-position extractor in pdf_parse.py finds
no non-executive rows (2026-09-28: BOC, Liberty, Sameer, KPLC and Bamburi all print a per-director table the
main extractor stored nothing for, so retainers, chair identification and the register all came up empty).

It works from the plain page text, one line per table row, and understands these printed layouts:

  KPLC     Salary/honoraria | Fees | Expense allowances | Total      names wrap onto a 2nd line ("... -" /
           "Chairman"), 'Executive Director' / 'Non-Executive Directors' section lines, one page per year
  BOC      Name | Category | Fees | Sitting allowance | Total        2025 block then 2024 block on each row
  Liberty  Retainer fees | Attendance fees | Total  (KShs m)         2025 block then 2024 block
  Sameer   Fees | Sitting allowances | Total (KShs '000)             2025 block then 2024 block
  Bamburi  numbered rows: 2025 total fees, then the 2024 split       only the 2025 total is a 2025 figure

Rules that keep it honest:
  * figures are stored exactly as printed (the filing's unit is detected elsewhere in app.py);
  * only the CURRENT-year block is read - when a header states a year other than the target, the block is skipped;
  * a dash is "nothing paid", stored as no figure, never as an invented zero;
  * a chairman is marked in the name as "(Chairman)" only when the table itself says so - never guessed;
  * a block with fewer than 3 person-like rows is discarded, so a financial-statement table with a "Fees"
    column cannot pass as a director table.
"""
import re

_NUM = re.compile(r"^(?:\d[\d,]*(?:\.\d+)?|[-\u2013\u2014])$")
_UNIT_LINE = re.compile(r"\b(?:k?shs?|kes|frw|usd)\b|\bunit", re.I)
_COLS = [                                    # (label stored, header regex) - order in the table decides column order
    ('Salary / Honoraria', r"salary|honorari"),
    ('Fees', r"retainer|\bfees?\b"),
    ('Sitting Allowance', r"sitting|attendance"),
    ('Expense Allowances', r"expense"),
    ('Total', r"\btotal\b"),
]
_CHAIR = re.compile(r"\bchair(?:man|person|woman)?\b", re.I)
_CATEGORY = re.compile(r"\b(?:non[\s\-]*executive(?:\s+directors?)?|independent)\b", re.I)


def _tokens(line):
    return line.split()


def _trailing_numbers(line):
    """(text_before, [tokens]) where tokens are the maximal run of numeric/dash cells at the end of the line."""
    toks = _tokens(line)
    i = len(toks)
    while i > 0 and _NUM.match(toks[i - 1]):
        i -= 1
    return ' '.join(toks[:i]), toks[i:]


def _value(tok):
    if tok in ('-', '\u2013', '\u2014'):
        return None
    try:
        return float(tok.replace(',', ''))
    except ValueError:
        return None


def _columns(header_text):
    """Ordered column labels of ONE year's block: everything up to and including the first 'Total'."""
    m = re.search(r"\btotal\b", header_text, re.I)
    if not m:
        return []
    head = header_text[:m.end()]
    found = []
    for label, pat in _COLS:
        mm = re.search(pat, head, re.I)
        if mm:
            found.append((mm.start(), label))
    return [label for _pos, label in sorted(found)]


def _is_unit_only(line):
    """A line made only of unit cells: "Shs'000 Shs'000", "KShs 000  KShs 000", "KShs m KShs m"."""
    rest = re.sub(r"k?shs?|kes|frw|usd|million|thousand|[\u2018\u2019'`]|\b000\b|\bm\b|\bmn\b|\d+|[\s,.()]", '', line, flags=re.I)
    return bool(line.strip()) and not rest and bool(re.search(r"shs?|kes|frw|usd|\b000\b|million|thousand|\bm\b", line, re.I))


def _clean_name(raw):
    """(name, is_chair) from a printed name cell: category words removed, chairman marker normalised."""
    text = re.sub(r"^\s*(?:by invitation\s*:\s*)", '', raw, flags=re.I)
    text = re.sub(r"^\s*\d{1,2}\s*[.)]\s+", '', text)                 # Bamburi's "1. "
    text = re.sub(r"\s+", ' ', text).strip()
    chair = bool(_CHAIR.search(text))
    text = _CHAIR.sub(' ', text)
    text = _CATEGORY.sub(' ', text)
    text = re.sub(r"\s*[-\u2013\u2014]\s*$", '', re.sub(r"\s+", ' ', text).strip())      # "Joy Brenda Masinde -"
    text = re.sub(r"\s*[-\u2013\u2014]\s*(?=\()", ' ', text).strip(' ,-\u2013')
    return text, chair


def _block_year(header_text):
    m = re.search(r"\b(20\d{2})\b", header_text)
    return int(m.group(1)) if m else None


def _parse_block(lines, start, target_year):
    """One header at lines[start]; returns (rows, next_index) or ([], start+1)."""
    # first data row d: the first line (within 12) ending in 2+ figures that is neither a year line nor a unit line
    d = None
    for k_ in range(start, min(start + 12, len(lines))):
        _t, nn = _trailing_numbers(lines[k_])
        if len(nn) >= 2 and not all(re.fullmatch(r"20\d{2}", t) for t in nn) and not _UNIT_LINE.search(lines[k_]) \
                and not _is_unit_only(lines[k_]):
            d = k_
            break
    if d is None:
        return [], start + 1
    # the header ends at the last unit line printed above the first data row ("Shs'000 Shs'000 ..."), which keeps
    # section lines and wrapped names ("Executive Director", "Joy Brenda Masinde -", "Chairman") out of it
    unit_idx = [k_ for k_ in range(start, d) if _is_unit_only(lines[k_])]
    e = unit_idx[-1] if unit_idx else d - 1
    hdr_lines = lines[start:e + 1]
    j = e + 1
    header_text = ' '.join(hdr_lines)
    ctx = ' '.join(lines[max(0, start - 3):start] + hdr_lines)
    col_text = re.split(r"year ended.*?20\d{2}|below\s*:", header_text, flags=re.I)[-1]      # column labels only, not the prose above
    cols = _columns(col_text)
    k = len(cols)
    if k < 2 or cols[-1] != 'Total':
        return [], start + 1
    year = _block_year(ctx)
    if target_year and year and year != target_year:
        return [], j
    section = 'non_executive' if re.search(r"non[\s\-]*executive", ctx, re.I) or ('Sitting Allowance' in cols and 'Salary / Honoraria' not in cols) else None

    rows, pending = [], []
    order = 0
    for i in range(j, min(j + 45, len(lines))):
        ln = lines[i].strip()
        if not ln:
            continue
        if re.match(r"(?:information subject|by order of)", ln, re.I):
            break
        sec = re.match(r"^(non[\s\-]*executive directors?|executive directors?)\s*$", ln, re.I)
        if sec:
            section = 'non_executive' if sec.group(1).lower().startswith('non') else 'executive'
            pending = []
            continue
        text, nums = _trailing_numbers(ln)
        if len(nums) not in (k, 2 * k) and nums and nums[0] in ('-', '\u2013', '\u2014') and len(nums) - 1 in (k, 2 * k):
            text, nums = (text + ' -').strip(), nums[1:]                # the name's own trailing dash, not a cell
        if len(nums) not in (k, 2 * k):
            if (not nums or nums == ['-']) and len(ln) < 70:
                pending.append(ln)                                    # a wrapped name line, incl. one ending "-"
                pending = pending[-3:]
            elif rows:
                break
            continue
        label_text = ' '.join(pending + ([text] if text else [])).strip()
        pending = []
        current = nums[:k]
        vals = [_value(t) for t in current]
        if not label_text or re.fullmatch(r"totals?|grand total", label_text, re.I):
            rows.append({'director_name': 'Total', 'role': 'unknown', 'table_kind': 'unknown', 'is_grand_total': True,
                         'is_total_row': True, 'total': vals[-1],
                         'components': {c: v for c, v in zip(cols, vals) if v is not None}, 'order_index': order})
            order += 1
            break
        name, chair = _clean_name(label_text)
        star = '*' if '*' in label_text else ''
        name = name.replace('*', '').strip()
        if not name:
            continue
        cat_ne = bool(re.search(r"non[\s\-]*executive", label_text, re.I))
        role = 'non_executive' if cat_ne else (section or 'unknown')
        rows.append({'director_name': f"{name}{star}" + (' (Chairman)' if chair else ''), 'role': role,
                     'table_kind': 'non_executive' if role == 'non_executive' else 'executive' if role == 'executive' else 'unknown',
                     'is_grand_total': False, 'is_total_row': False, 'total': vals[-1],
                     'components': {c: v for c, v in zip(cols, vals) if v is not None}, 'order_index': order})
        order += 1
    return rows, j + len(rows) + 1


def _parse_bamburi(lines, target_year):
    """'1. Dr John P .N. Simba 4,989 1,980 2,726 4,706' - the first figure is the 2025 total; the rest is 2024."""
    rows = []
    for ln in lines:
        m = re.match(r"^\s*(\d{1,2})\.\s+(.+?)\s+(\d[\d,]*)(?:\s+(?:\d[\d,]*|-)){0,3}\s*$", ln)
        if not m:
            continue
        name, chair = _clean_name(m.group(2))
        tot = _value(m.group(3))
        if name and tot is not None:
            rows.append({'director_name': name + (' (Chairman)' if chair else ''), 'role': 'non_executive',
                         'table_kind': 'non_executive', 'is_grand_total': False, 'is_total_row': False,
                         'total': tot, 'components': {'Total 2025': tot}, 'order_index': len(rows)})
    return rows


def extract_ned_pay_rows_text(pdf_bytes, target_period_label=None, page_texts=None):
    """Named per-director pay rows read from plain page text (see module docstring). [] when none is found."""
    from board_extract import _looks_like_person, _split_honorific_and_name
    from pdf_parse import _pypdf_page_texts_cached, _is_pay_row_name
    target_year = int(target_period_label[2:]) if target_period_label and re.fullmatch(r"FY\d{4}", target_period_label) else None
    out = []
    for pn, text in (page_texts if page_texts is not None else (_pypdf_page_texts_cached(pdf_bytes) or [])):
        low = (text or '').lower()
        if 'non-executive' not in low and 'non executive' not in low:
            continue
        if not re.search(r"sitting|attendance|honorari|expense allowance|annual fees", low):
            continue
        lines = text.split('\n')
        rows = []
        if re.search(r"annual fees\s+2025", low) and re.search(r"\n\s*1\.\s+\S", text):
            rows = _parse_bamburi(lines, target_year)
        else:
            i = 0
            while i < len(lines):
                if re.search(r"\bfees?\b|retainer|honorari", lines[i], re.I):
                    window = ' '.join(lines[i:i + 8])
                    if re.search(r"\btotal\b", window, re.I) and not re.search(r"\d{2,}[,.]\d", lines[i]):
                        got, nxt = _parse_block(lines, i, target_year)
                        if got:
                            rows = got
                            break
                        i = max(i + 1, nxt)
                        continue
                i += 1
        # A named director OR a valid ex-officio/institutional seat ("PS, National Treasury" - a government
        # nominee director, common on state-owned filers like KPLC) counts; single stray words ("Against",
        # "397", a column heading) do not - see _is_pay_row_name's docstring in pdf_parse.py.
        named = [r for r in rows if not r['is_total_row'] and _is_pay_row_name(r)]
        if len(named) >= 3:
            for r in rows:
                r['page'] = pn
                r['fiscal_year'] = target_year
            out.extend(r for r in rows if r['is_total_row'] or r in named)
            break
    for i, r in enumerate(out):
        r['order_index'] = i
    return out
