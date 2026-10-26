"""Upload of filled Manual Entry forms (see manual_entry_form.py) -> preview -> approve -> stored.

Flow (nothing is written until step 3):
  1. preview_upload(files)   files = [(filename, bytes)], each a .pdf or a .zip of .pdf forms. Every form is read
                             (its fillable fields + hidden `meta` field) and turned into a list of proposed changes,
                             compared with what is stored now.
  2. the admin ticks / unticks changes on the Manual Entry page.
  3. apply_changes(changes)  writes only the approved ones with the same rules as the Manual Entry page's own saves
                             (typed values are marked "entered manually" and are never overwritten by a re-upload or
                             the AI), then rebuilds the tabs (tab_modules.reconcile_tabs) so the Survey Report shows them.

The pure part (read_form / intents_from_values / parse_number) needs no database and is unit-testable.
Must run inside a Flask app context for preview/apply.
"""
import io
import json
import math
import re
import zipfile
from datetime import datetime

from pypdf import PdfReader
from pypdf.generic import NameObject

import tab_modules
from models import db, Company, FinancialPeriod, DirectorRemunerationRow, Committee, CommitteeMember
from models_survey import SurveyCompanyData, SurveyDirector, EvidenceConflict

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 150 * 1024 * 1024
MAX_ZIP_ENTRIES = 400
BLANKS = {'', '-', '\u2014', '\u2013'}
INT_COLS = {'board_size', 'board_meetings_per_year', 'committees_per_board'}
SOURCE_TAG = 'entered manually: form upload'
SUMMARY_PREFIX = '00_Data_Quality_Summary'   # written by manual_entry_form.build_all_forms_zip; not a fillable form
TEXT_MAX = 120        # width of the free-text survey columns (ceo_monthly_conversion_basis is String(120))


# =============================================================== pure helpers (no database)
def parse_number(raw):
    """'1,200.5' / 'KES 1 200' / '12%' -> float. None when it is not a plain number."""
    t = re.sub(r'\s+', '', str(raw or '')).replace(',', '')
    t = re.sub(r'^(?:KES|KSH|KSHS|USD|UGX|TZS|RWF|\$)', '', t, flags=re.I).rstrip('%')
    try:
        v = float(t)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


_PLAIN = {'\u2018': "'", '\u2019': "'", '\u201a': "'", '\u201b': "'", '\u201c': '"', '\u201d': '"', '\u201e': '"',
          '\u2013': '-', '\u2014': '-', '\u2212': '-', '\u2026': '...', '\u00a0': ' ', '\u2022': '-', '\u00b7': '-'}


def box_safe(t):
    """PDF form boxes only hold Latin-1 text (reportlab raises KeyError on e.g. a curly apostrophe, 8217): curly quotes /
    dashes become plain ones and anything else outside Latin-1 becomes '?'."""
    t = ''.join(_PLAIN.get(c, c) for c in str(t))
    return t.encode('latin-1', 'replace').decode('latin-1')


def prefill_text(v):
    """How a stored value is printed in a pre-filled form box (manual_entry_form uses this too). An unchanged box reads
    back as exactly this text, so 'did the person change it?' is a plain string comparison - no float rounding traps."""
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'Yes' if v else 'No'
    if isinstance(v, (int, float)):
        if float(v) == int(v):
            return f'{int(v):,}'
        return f'{v:,.6f}'.rstrip('0').rstrip('.')
    return box_safe(str(v).strip())


def _same_box(a, b):
    """Equal, or equal once printed in a form box (so 'O'Brien' read back from a box matches 'O\u2019Brien' on file)."""
    if _same(a, b):
        return True
    return isinstance(a, str) and isinstance(b, str) and _norm_box(prefill_text(a)).lower() == _norm_box(prefill_text(b)).lower()


def _norm_box(t):
    return re.sub(r'\s+', ' ', str(t or '')).strip()


def _is_int_col(col):
    return col.endswith('_count') or col in INT_COLS


def _blank(v):
    return str(v or '').strip() in BLANKS


def _norm_role(v):
    v = str(v or '').strip().lower().replace('_', '-')
    return {'executive': 'executive', 'non-executive': 'non_executive', 'nonexecutive': 'non_executive'}.get(v)


def _norm_gender(v):
    return {'male': 'Male', 'female': 'Female'}.get(str(v or '').strip().lower())


def _norm_yesno(v):
    return {'yes': True, 'no': False}.get(str(v or '').strip().lower())


def _checked(v):
    return str(v or '').strip().lower() not in ('', 'off', '0', 'false', 'no')


def parse_components(raw):
    """'Fees=5000, Allowances=1,200' -> ({'Fees': 5000.0, 'Allowances': 1200.0}, [problems])"""
    out, bad = {}, []
    for part in re.split(r'[;\n,](?![0-9]{3}\b)', str(raw or '')):
        part = part.strip()
        if not part:
            continue
        if '=' not in part:
            bad.append(part)
            continue
        k, v = part.split('=', 1)
        n = parse_number(v)
        if k.strip() and n is not None:
            out[k.strip()[:60]] = n
        else:
            bad.append(part)
    return out, bad


def read_form(pdf_bytes):
    """-> (meta | None, {field_name: text}). meta = {'cid': int, 'fy': str, 'name': str} from the hidden field."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    fields = reader.get_fields() or {}
    values = {}
    for name, f in fields.items():
        v = f.get('/V')
        if v is None:
            v = ''
        if isinstance(v, NameObject) or (f.get('/FT') == '/Btn' and str(v).startswith('/')):
            v = str(v).lstrip('/')
        values[str(name)] = str(v).strip()
    raw = values.pop('meta', None)
    meta = None
    if raw:
        try:
            m = json.loads(raw)
            if isinstance(m, dict) and str(m.get('cid', '')).isdigit() and str(m.get('fy') or '').strip():
                meta = {'cid': int(m['cid']), 'fy': str(m['fy']).strip(), 'name': str(m.get('name') or ''),
                        'v': int(m.get('v') or 1) if str(m.get('v') or 1).isdigit() else 1}
        except ValueError:
            meta = None
    return meta, values


def intents_from_values(values, label_of=None, metric_cols=None):
    """Field values of ONE form -> ([intent], [problem text]). Empty boxes and the "-" dropdown entry are ignored.
    intent = {'op': ..., 'key' | 'target_id' | 'n': ..., 'data': {...}}"""
    label_of = label_of or {}
    metric_cols = metric_cols if metric_cols is not None else set(label_of)
    intents, problems = [], []
    rows = {}      # ('dir', '5') -> {attr: value}

    def lbl(k):
        return label_of.get(k, k)

    for name, val in values.items():
        parts = name.split('|')
        kind = parts[0]
        if kind == 'metric' and len(parts) == 2 and not _blank(val) and parts[1] in tab_modules.survey_text_columns():
            k = parts[1]                       # free-text field (e.g. CEO conversion basis "annual / 12") - kept as text
            note = (values.get(f'metricnote|{k}') or '').strip()[:80]
            intents.append({'op': 'metric', 'key': k, 'data': {'value': str(val).strip()[:TEXT_MAX], 'note': note}})
        elif kind == 'metric' and len(parts) == 2 and not _blank(val):
            k = parts[1]
            n = parse_number(val)
            if k not in metric_cols:
                problems.append(f'{lbl(k)}: not a field that can be entered here - skipped')
            elif n is None:
                problems.append(f'{lbl(k)}: "{val}" is not a number - skipped')
            else:
                note = (values.get(f'metricnote|{k}') or '').strip()[:80]
                intents.append({'op': 'metric', 'key': k, 'data': {'value': int(n) if _is_int_col(k) else n, 'note': note}})
        elif kind == 'conflict' and len(parts) == 2 and not _blank(val):
            k = parts[1]
            n = parse_number(val)
            if k not in metric_cols:
                problems.append(f'Conflict on {k}: not a field that can be entered here - skipped')
            elif n is None:
                problems.append(f'Conflict on {lbl(k)}: "{val}" is not a number - skipped')
            else:
                intents.append({'op': 'conflict', 'key': k, 'data': {'value': int(n) if _is_int_col(k) else n}})
        elif kind in ('dir', 'newdir', 'com', 'newcom', 'pay', 'newpay') and len(parts) == 3:
            rows.setdefault((kind, parts[1]), {})[parts[2]] = val
        elif kind == 'val' and len(parts) == 2:              # pre-filled value box (v2 forms): changed text = correction
            k = parts[1]
            if k in label_of or k in metric_cols or k in tab_modules.survey_text_columns():
                intents.append({'op': 'val', 'key': k, 'data': {'raw': _norm_box(val),
                                                                'ok': _checked(values.get(f'confirmok|{k}'))}})
        elif kind == 'dirname' and len(parts) == 2:
            rows.setdefault(('dir', parts[1]), {})['name'] = val
        elif kind == 'dirfix' and len(parts) == 3:          # a pre-filled register cell: unchanged values drop out later
            r = rows.setdefault(('dir', parts[1]), {})
            r[parts[2]] = val
            r.setdefault('_fix', set()).add(parts[2])
        elif kind == 'ben' and len(parts) == 2:
            rows.setdefault(('ben', parts[1]), {})['status'] = val
        elif kind == 'bendetail' and len(parts) == 2:
            rows.setdefault(('ben', parts[1]), {})['detail'] = val

    # confirm rows need both the value box and the OK tick
    for name, val in values.items():
        if not name.startswith('confirm|'):
            continue
        k = name.split('|', 1)[1]
        ok = _checked(values.get(f'confirmok|{k}'))
        if k in tab_modules.survey_text_columns():
            # free-text field listed under "values on file": a typed correction replaces it, a tick approves it
            if not _blank(val):
                intents.append({'op': 'confirm', 'key': k, 'data': {'value': str(val).strip()[:TEXT_MAX]}})
            elif ok:
                intents.append({'op': 'confirm', 'key': k, 'data': {'value': None}})
            continue
        if not _blank(val):
            n = parse_number(val)
            if k not in metric_cols or n is None:
                problems.append(f'{lbl(k)}: "{val}" is not a number - skipped')
            else:
                intents.append({'op': 'confirm', 'key': k, 'data': {'value': int(n) if _is_int_col(k) else n}})
        elif ok and k in metric_cols:
            intents.append({'op': 'confirm', 'key': k, 'data': {'value': None}})

    for (kind, ident), f in sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if kind in ('dir', 'newdir'):
            data = {}
            if not _blank(f.get('role')):
                r = _norm_role(f['role'])
                data['role'] = r if r else problems.append(f'Register row {ident}: role "{f["role"]}" not recognised - ignored')
            if not _blank(f.get('independent')):
                b = _norm_yesno(f['independent'])
                if b is not None:
                    data['independent'] = b
            if not _blank(f.get('gender')):
                g = _norm_gender(f['gender'])
                if g:
                    data['gender'] = g
            for a in ('nationality', 'appointed'):
                if not _blank(f.get(a)):
                    data['appointed_date' if a == 'appointed' else a] = f[a].strip()[:60]
            data = {k: v for k, v in data.items() if v is not None}
            if kind == 'dir' and not _blank(f.get('name')):
                data['director_name'] = f['name'].strip()[:150]
            if kind == 'dir' and data and ident.isdigit():
                fixed = {'appointed_date' if a == 'appointed' else a for a in f.get('_fix', ())}
                if 'director_name' in data:
                    fixed.add('director_name')        # the name box is pre-filled, so an edit is a deliberate correction
                intents.append({'op': 'dir_update', 'target_id': int(ident), 'data': data,
                                'fix': sorted(fixed & set(data))})
            elif kind == 'newdir':
                name = (f.get('name') or '').strip()
                if name:
                    data['director_name'] = name[:150]
                    intents.append({'op': 'dir_new', 'n': ident, 'data': data})
                elif data:
                    problems.append(f'Register row {int(ident) + 1}: details given but no name - skipped')
        elif kind in ('com', 'newcom'):
            data = {}
            if not _blank(f.get('chair')):
                data['chairperson_name'] = f['chair'].strip()[:150]
            if not _blank(f.get('meetings')):
                n = parse_number(f['meetings'])
                if n is None or n < 0:
                    problems.append(f'Committee meetings "{f["meetings"]}" is not a number - ignored')
                else:
                    data['meetings_held'] = int(n)
            if not _blank(f.get('members')):
                data['members'] = [m.strip() for m in re.split(r'[,\n;]', f['members']) if m.strip()]
            if kind == 'com' and data and ident.isdigit():
                intents.append({'op': 'com_update', 'target_id': int(ident), 'data': data})
            elif kind == 'newcom':
                name = (f.get('name') or '').strip()
                if name:
                    data['name'] = name[:100]
                    intents.append({'op': 'com_new', 'n': ident, 'data': data})
                elif data:
                    problems.append(f'Committee row {int(ident) + 1}: details given but no name - skipped')
        elif kind in ('pay', 'newpay'):
            data = {}
            if not _blank(f.get('total')):
                n = parse_number(f['total'])
                if n is None:
                    problems.append(f'Pay total "{f["total"]}" is not a number - ignored')
                else:
                    data['total'] = n
            if not _blank(f.get('components')):
                comps, bad = parse_components(f['components'])
                if comps:
                    data['components'] = comps
                for b in bad:
                    problems.append(f'Pay component "{b}" not understood (use Name=amount) - ignored')
            if kind == 'pay' and data and ident.isdigit():
                intents.append({'op': 'pay_update', 'target_id': int(ident), 'data': data})
            elif kind == 'newpay':
                name = (f.get('name') or '').strip()
                if name:
                    data['director_name'] = name[:150]
                    data['role'] = _norm_role(f.get('role')) or 'unknown'
                    intents.append({'op': 'pay_new', 'n': ident, 'data': data})
                elif data:
                    problems.append(f'Pay row {int(ident) + 1}: details given but no name - skipped')
        elif kind == 'ben':
            st = str(f.get('status') or '').strip().lower()
            detail = (f.get('detail') or '').strip() or None
            if st in ('provided', 'not provided'):
                intents.append({'op': 'ben', 'key': ident, 'data': {'provided': st == 'provided', 'detail': detail}})
            elif detail:
                problems.append(f'Benefit {ident}: detail given but no status chosen - skipped')
    return intents, problems


# =============================================================== reading the uploaded files
def collect_pdfs(files):
    """[(filename, bytes)] -> ([(display_name, pdf_bytes)], [notes]). Accepts .pdf and .zip (zips are read in memory,
    never extracted to disk; nested zips are not opened)."""
    pdfs, notes, total = [], [], 0
    for fname, data in files:
        low = (fname or '').lower()
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            notes.append('Upload is larger than 150 MB in total - the rest was ignored.')
            break
        if len(data) > MAX_FILE_BYTES and not low.endswith('.zip'):
            notes.append(f'{fname}: larger than 25 MB - skipped.')
        elif data[:4] == b'%PDF':
            pdfs.append((fname, data))
        elif data[:2] == b'PK' and zipfile.is_zipfile(io.BytesIO(data)):
            try:
                z = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                notes.append(f'{fname}: not a valid zip - skipped.')
                continue
            infos = [i for i in z.infolist() if not i.is_dir() and '__MACOSX' not in i.filename
                     and not i.filename.split('/')[-1].startswith('._')]
            if len(infos) > MAX_ZIP_ENTRIES:
                notes.append(f'{fname}: more than {MAX_ZIP_ENTRIES} files inside - only the first {MAX_ZIP_ENTRIES} were read.')
                infos = infos[:MAX_ZIP_ENTRIES]
            inner_total = 0
            for i in infos:
                if i.filename.split('/')[-1].startswith(SUMMARY_PREFIX):      # the read-only summary in a downloaded zip
                    continue
                if not i.filename.lower().endswith('.pdf'):
                    if i.filename.lower().endswith('.zip'):
                        notes.append(f'{fname}: "{i.filename}" is a zip inside a zip - not opened.')
                    continue
                inner_total += i.file_size
                if i.file_size > MAX_FILE_BYTES or inner_total > MAX_TOTAL_BYTES:
                    notes.append(f'{fname}: "{i.filename}" is too large - skipped.')
                    continue
                blob = z.read(i)
                if blob[:4] == b'%PDF':
                    pdfs.append((f'{fname} > {i.filename}', blob))
        else:
            notes.append(f'{fname}: only .pdf and .zip files are accepted - skipped.')
    return pdfs, notes


# =============================================================== database side
def _labels():
    return tab_modules.all_metric_labels()


def _fmt(v):
    if v is None or v == '':
        return '\u2014'
    if isinstance(v, bool):
        return 'Yes' if v else 'No'
    if isinstance(v, float):
        return f'{v:,.2f}'.rstrip('0').rstrip('.')
    if isinstance(v, (list, tuple)):
        return ', '.join(str(x) for x in v) or '\u2014'
    if isinstance(v, dict):
        return ', '.join(f'{k}={_fmt(x)}' for k, x in v.items()) or '\u2014'
    return str(v)


def _same(a, b):
    if isinstance(a, bool) or isinstance(b, bool):          # Independent: No (False) is an answer, not a blank
        return a is b or (isinstance(a, bool) and isinstance(b, bool) and a == b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(float(a) - float(b)) < 1e-9
    return str(a or '').strip().lower() == str(b or '').strip().lower()


def _period(company_id, fy):
    return FinancialPeriod.query.filter_by(company_id=company_id, period_label=fy).first()


def _owned_period(period_id, cid, fy):
    p = db.session.get(FinancialPeriod, period_id) if period_id else None
    return p if p is not None and p.company_id == cid and p.period_label == fy else None


DIR_ATTR_LABEL = {'director_name': 'Name', 'role': 'Role', 'independent': 'Independent', 'gender': 'Gender', 'nationality': 'Nationality',
                  'appointed_date': 'Appointed'}


def _annotate(intent, cid, fy):
    """Fill in label / current / new / warning for one intent from the database. Returns a change dict, or None when
    the form value equals what is already stored (nothing to do)."""
    labels = _labels()
    op, data = intent['op'], intent['data']
    ch = {'op': op, 'company_id': cid, 'fiscal_year': fy, 'data': data, 'warning': None, 'checked': True,
          'target_id': intent.get('target_id'), 'key': intent.get('key'), 'fix': intent.get('fix') or []}
    row = tab_modules.survey_row(cid, fy)

    if op == 'val':
        k = intent['key']
        cur = getattr(row, k, None) if row is not None else None
        raw, ok = data['raw'], data.get('ok')
        ch['key'] = k
        if raw == _norm_box(prefill_text(cur)) or not raw:       # unchanged (or emptied: use Replace mode to clear)
            if ok and cur is not None:
                ch.update(op='confirm', data={'value': None}, label=f'{labels.get(k, k)} (confirm)',
                          current=_fmt(cur), new=f'{_fmt(cur)} (confirmed correct)')
                return ch
            return None
        if k in tab_modules.survey_text_columns():
            v = raw[:TEXT_MAX]
        else:
            n = parse_number(raw)
            if n is None:
                ch.update(op='confirm', data={'value': None}, label=labels.get(k, k), current=_fmt(cur), new=raw,
                          warning=f'"{raw}" is not a number - not saved.', checked=False, broken=True)
                return ch
            v = int(n) if _is_int_col(k) else n
        ch.update(op='confirm', data={'value': v}, label=f'{labels.get(k, k)} (corrected)', current=_fmt(cur), new=_fmt(v))
        return ch

    if op in ('metric', 'confirm', 'conflict'):
        k = intent['key']
        cur = getattr(row, k, None) if row is not None else None
        ch['label'] = {'metric': labels.get(k, k), 'confirm': f'{labels.get(k, k)} (confirm)',
                       'conflict': f'{labels.get(k, k)} (conflict)'}[op]
        ch['current'] = _fmt(cur)
        if op == 'confirm' and data['value'] is None:
            if cur is None:
                return None
            ch['new'] = f'{_fmt(cur)} (confirmed correct)'
            return ch
        ch['new'] = _fmt(data['value'])
        if cur is not None and _same(cur, data['value']) and op != 'conflict':
            if op == 'confirm':
                ch['data'] = {'value': None}
                ch['new'] = f'{_fmt(cur)} (confirmed correct)'
                return ch
            return None
        if op == 'metric' and cur is not None:
            ch['warning'], ch['checked'] = 'A value has been stored since this form was made - it would be replaced.', False
        if op == 'conflict':
            n_open = EvidenceConflict.query.filter_by(company_id=cid, fiscal_year=fy, field_name=k, resolution=None).count()
            if not n_open:
                ch['warning'], ch['checked'] = 'No open conflict on this field any more.', False
        return ch

    if op in ('dir_update', 'dir_new'):
        d = db.session.get(SurveyDirector, ch['target_id']) if op == 'dir_update' else None
        if op == 'dir_update' and (d is None or d.company_id != cid or d.fiscal_year != fy):
            ch['label'], ch['current'], ch['new'] = 'Register row', '\u2014', _fmt({DIR_ATTR_LABEL[k]: v for k, v in data.items()})
            ch['warning'], ch['checked'], ch['broken'] = 'That register row no longer exists.', False, True
            return ch
        if op == 'dir_new':
            name = data['director_name']
            d = next((x for x in SurveyDirector.query.filter_by(company_id=cid, fiscal_year=fy).all()
                      if _same(x.director_name, name)), None)
            if d is not None:
                ch['op'], ch['target_id'] = 'dir_update', d.id
                ch['data'] = {k: v for k, v in data.items() if k != 'director_name'}
                data = ch['data']
                ch['warning'] = 'Already in the register - the row is updated, not duplicated.'
                if not data:
                    return None
        attrs = {k: v for k, v in data.items() if k in DIR_ATTR_LABEL}
        if d is not None:
            changed = {k: v for k, v in attrs.items() if not _same_box(getattr(d, k, None), v)}
            if not changed:
                return None
            ch['data'] = dict(changed)
            ch['label'] = f'Register: {d.director_name}'
            ch['current'] = ', '.join(f'{DIR_ATTR_LABEL[k]}: {_fmt(getattr(d, k, None))}' for k in changed)
            fix = set(ch.get('fix') or [])
            if any(getattr(d, k, None) not in (None, '', 'unknown') and k not in fix for k in changed):
                ch['warning'], ch['checked'] = 'Some of these cells were filled since the form was made - they would be replaced.', False
            elif changed.keys() & fix:
                ch['warning'] = 'Correction - replaces the value on file.'
        else:
            ch['label'] = f'Register: new director {data["director_name"]}'
            ch['current'] = '(not in register)'
        ch['new'] = ', '.join(f'{DIR_ATTR_LABEL[k]}: {_fmt(v)}' for k, v in ch['data'].items() if k in DIR_ATTR_LABEL) or 'add director'
        return ch

    if op in ('com_update', 'com_new'):
        period = _period(cid, fy)
        c = db.session.get(Committee, ch['target_id']) if op == 'com_update' else None
        if op == 'com_update' and (c is None or _owned_period(c.period_id, cid, fy) is None):
            ch['label'], ch['current'], ch['new'] = 'Committee', '\u2014', _fmt(data)
            ch['warning'], ch['checked'], ch['broken'] = 'That committee no longer exists.', False, True
            return ch
        if op == 'com_new' and period is not None:
            c = next((x for x in Committee.query.filter_by(period_id=period.id).all() if _same(x.name, data['name'])), None)
            if c is not None:
                ch['op'], ch['target_id'] = 'com_update', c.id
                ch['data'] = data = {k: v for k, v in data.items() if k != 'name'}
                ch['warning'] = 'Already on file - the committee is updated, not duplicated.'
                if not data:
                    return None
        if c is not None:
            members = [m.director_name for m in CommitteeMember.query.filter_by(committee_id=c.id).all()]
            cur = {'chairperson_name': c.chairperson_name, 'meetings_held': c.meetings_held, 'members': members}
            changed = {k: v for k, v in data.items() if k in cur and not _same(cur[k], v)}
            if not changed:
                return None
            ch['data'] = changed
            ch['label'] = f'Committee: {c.name}'
            ch['current'] = _fmt({k: cur[k] for k in changed})
            if any(cur[k] not in (None, '', []) for k in changed):
                ch['warning'], ch['checked'] = 'Some of these were filled since the form was made - they would be replaced.', False
        else:
            ch['label'], ch['current'] = f'Committee: new {data["name"]}', '(not on file)'
        rest = {k: v for k, v in ch['data'].items() if k != 'name'}
        ch['new'] = _fmt(rest) if rest else 'add committee'
        return ch

    if op in ('pay_update', 'pay_new'):
        r = db.session.get(DirectorRemunerationRow, ch['target_id']) if op == 'pay_update' else None
        if op == 'pay_update':
            if r is None or _owned_period(r.period_id, cid, fy) is None:
                ch['label'], ch['current'], ch['new'] = 'Director pay row', '\u2014', _fmt(data)
                ch['warning'], ch['checked'], ch['broken'] = 'That pay row no longer exists.', False, True
                return ch
            ch['label'] = f'Pay: {r.director_name}'
            ch['current'] = _fmt(r.total)
            if r.total is not None:
                ch['warning'], ch['checked'] = 'A total was stored since the form was made - it would be replaced.', False
        else:
            ch['label'], ch['current'] = f'Pay: new row {data["director_name"]}', '(not on file)'
            period = _period(cid, fy)
            if period is not None and any(_same(x.director_name, data['director_name']) and not x.is_grand_total
                                          for x in DirectorRemunerationRow.query.filter_by(period_id=period.id).all()):
                ch['warning'], ch['checked'] = 'A pay row for this name already exists - this would add a second one.', False
        parts = []
        if 'total' in data:
            parts.append(f'Total: {_fmt(data["total"])}')
        if data.get('components'):
            parts.append(_fmt(data['components']))
        ch['new'] = '; '.join(parts) or 'add row'
        return ch

    if op == 'ben':
        k = intent['key']
        cur = ((row.ned_benefits or {}) if row is not None else {}).get(k)
        ch['label'] = f'NED benefit: {k}'
        ch['current'] = ('Provided' if cur.get('provided') else 'Not provided') if cur else 'not stated'
        ch['new'] = ('Provided' if data['provided'] else 'Not provided') + (f' ({data["detail"]})' if data.get('detail') else '')
        if cur and cur.get('provided') == data['provided'] and (cur.get('detail') or None) == (data.get('detail') or None):
            return None
        if cur:
            ch['warning'], ch['checked'] = 'A status was stored since the form was made - it would be replaced.', False
        return ch
    return None


def preview_upload(files, replace=False):
    """[(filename, bytes)] -> {'files': [...], 'changes': [...], 'notes': [...], 'problems': [...]}. Writes nothing.
    replace=True: each form REPLACES its company + year's survey fields and directors' register (one 'replace' change
    per form); committees / pay rows / benefits / conflicts on the form are still applied as normal changes."""
    pdfs, notes = collect_pdfs(files)
    labels = _labels()
    metric_cols = tab_modules.survey_metric_columns()
    out_files, changes, problems_all = [], [], []
    seen = set()
    for fname, blob in pdfs:
        try:
            meta, values = read_form(blob)
        except Exception as e:                      # damaged / encrypted PDF
            notes.append(f'{fname}: could not be read ({type(e).__name__}) - skipped.')
            continue
        if meta is None:
            notes.append(f'{fname}: not a FinSight Manual Entry form, or its fillable fields were lost (printed / scanned / '
                         '"flattened" copies cannot be read). Fill the downloaded PDF on screen and save it.')
            continue
        company = db.session.get(Company, meta['cid'])
        if company is None:
            notes.append(f'{fname}: the company "{meta["name"]}" no longer exists - skipped.')
            continue
        key = (meta['cid'], meta['fy'])
        if key in seen:
            notes.append(f'{fname}: a second form for {company.name} {meta["fy"]} - the earlier one in this upload is used, this one skipped.')
            continue
        seen.add(key)
        if replace and meta.get('v', 1) < REPLACE_MIN_VERSION:
            notes.append(f'{fname}: made before forms carried every value, so it cannot replace a year - download a new '
                         'form for this company and year, check it, and upload that one. Skipped.')
            continue
        intents, problems = intents_from_values(values, labels, metric_cols)
        n_before = len(changes)
        if replace:
            rc, rprobs = replace_change(meta['cid'], meta['fy'], values, fname, company.name)
            problems += rprobs
            if rc is not None:
                rc.update({'id': f'c{len(changes)}', 'file': fname, 'company': company.name})
                changes.append(rc)
            intents = [i for i in intents if i['op'] not in ('val', 'metric', 'confirm', 'dir_update', 'dir_new')]
        for it in intents:
            ch = _annotate(it, meta['cid'], meta['fy'])
            if ch is not None:
                ch.update({'id': f'c{len(changes)}', 'file': fname, 'company': company.name})
                changes.append(ch)
        out_files.append({'file': fname, 'company_id': company.id, 'company': company.name, 'fiscal_year': meta['fy'],
                          'proposed': len(changes) - n_before, 'filled_boxes': len(intents)})
        problems_all += [f'{company.name} {meta["fy"]}: {p}' for p in problems]
    return {'files': out_files, 'changes': changes, 'notes': notes, 'problems': problems_all}


# ------------------------------------------------------------------ replace mode
REPLACE_MIN_VERSION = 2       # forms from before pre-filled boxes cannot be used to replace a year (ticks carry no value)
_KEEP = object()
_DIR_ATTRS = ('role', 'independent', 'gender', 'nationality', 'appointed')


def _parse_box(k, raw, label):
    raw = _norm_box(raw)
    if raw in BLANKS:
        return None, None
    if k in tab_modules.survey_text_columns():
        return raw[:TEXT_MAX], None
    n = parse_number(raw)
    if n is None:
        return _KEEP, f'{label}: "{raw}" is not a number - the stored value is kept'
    return (int(n) if _is_int_col(k) else n), None


def _dir_value(attr, raw):
    raw = _norm_box(raw)
    if raw in BLANKS:
        return None
    if attr == 'role':
        return _norm_role(raw)
    if attr == 'independent':
        return _norm_yesno(raw)
    if attr == 'gender':
        return _norm_gender(raw)
    return raw[:60]


def replace_change(cid, fy, values, fname, company_name):
    """One form -> ONE 'replace' change for that company + year: the 40 survey fields and the directors' register become
    exactly what the form says (blank box = not disclosed = cleared). Financial statements, committees, pay rows, the
    stored report text and import traces are not touched. -> (change | None, [problem])"""
    labels = _labels()
    row = tab_modules.survey_row(cid, fy)
    sets, clears, verified, details, probs = {}, [], [], [], []
    for k, lbl in labels.items():
        if f'val|{k}' in values:
            raw = values[f'val|{k}']
        elif f'metric|{k}' in values:
            raw = values[f'metric|{k}']
        else:
            continue                                   # not on this form: left exactly as it is
        cur = getattr(row, k, None) if row is not None else None
        if cur is not None and _norm_box(raw) == _norm_box(prefill_text(cur)):
            verified.append(k)
            continue
        v, prob = _parse_box(k, raw, lbl)
        if prob:
            probs.append(prob)
            continue
        if v is None:
            if cur is not None:
                clears.append(k)
                details.append({'label': lbl, 'current': _fmt(cur), 'new': '(cleared - left blank on the form)'})
            continue
        if cur is not None and _same(cur, v):
            verified.append(k)
            continue
        sets[k] = v
        details.append({'label': lbl, 'current': _fmt(cur), 'new': _fmt(v)})

    # directors' register: every row printed on the form, plus any new rows typed in
    existing = {d.id: d for d in SurveyDirector.query.filter_by(company_id=cid, fiscal_year=fy).all()}
    on_form = {}
    for name, val in values.items():
        parts = name.split('|')
        if parts[0] == 'dirname' and len(parts) == 2 and parts[1].isdigit():
            on_form.setdefault(int(parts[1]), {})['name'] = val
        elif parts[0] in ('dir', 'dirfix') and len(parts) == 3 and parts[1].isdigit():
            on_form.setdefault(int(parts[1]), {})[parts[2]] = val
    d_upd, d_del, d_add = [], [], []
    for did, f in sorted(on_form.items()):
        d = existing.get(did)
        if d is None or 'name' not in f:
            continue                                   # old-style row without a name box, or a row deleted since
        name = _norm_box(f['name'])
        if name in BLANKS:
            d_del.append(did)
            details.append({'label': f'Register: {d.director_name}', 'current': 'in the register', 'new': '(removed)'})
            continue
        new = {'director_name': name[:150]}
        for a in _DIR_ATTRS:
            if a in f:
                new['appointed_date' if a == 'appointed' else a] = _dir_value(a, f[a])
        cur = {'director_name': d.director_name, 'role': d.role if d.role in ('executive', 'non_executive') else None,
               'independent': d.independent, 'gender': d.gender, 'nationality': d.nationality, 'appointed_date': d.appointed_date}
        for k, v in list(new.items()):
            if _same_box(cur.get(k), v):
                new[k] = cur.get(k) if k != 'role' else d.role      # unchanged: keep the stored spelling exactly
        diff = {k: v for k, v in new.items() if not _same_box(cur.get(k), v)}
        if diff:
            d_upd.append({'id': did, **new})
            details.append({'label': f'Register: {d.director_name}',
                            'current': ', '.join(f'{DIR_ATTR_LABEL[k]}: {_fmt(cur.get(k))}' for k in diff),
                            'new': ', '.join(f'{DIR_ATTR_LABEL[k]}: {_fmt(v)}' for k, v in diff.items())})
    newrows = {}
    for name, val in values.items():
        parts = name.split('|')
        if parts[0] == 'newdir' and len(parts) == 3:
            newrows.setdefault(parts[1], {})[parts[2]] = val
    def _key(x):
        return re.sub(r'[^a-z]', '', str(x or '').lower())
    upd_ids = {x['id'] for x in d_upd}
    # Every register row that survives this upload counts - including rows added after the form was downloaded
    # (they are not on the form, so Replace leaves them alone). Checking only rows printed on the form let an older
    # form re-add directors a newer upload had already added, creating duplicates.
    kept = {_key(x.get('director_name')) for x in d_upd} | {
        _key(d.director_name) for did, d in existing.items() if did not in d_del and did not in upd_ids}
    for n, f in sorted(newrows.items()):
        name = _norm_box(f.get('name'))
        if name in BLANKS:
            continue
        if _key(name) in kept:
            continue                                   # already in the register (e.g. the same form uploaded twice)
        kept.add(_key(name))
        add = {'director_name': name[:150]}
        for a in _DIR_ATTRS:
            add['appointed_date' if a == 'appointed' else a] = _dir_value(a, f.get(a))
        d_add.append(add)
        details.append({'label': f'Register: {name}', 'current': '(not in register)', 'new': 'added'})

    if not (sets or clears or verified or d_upd or d_del or d_add):
        return None, probs
    summary = (f"{len(sets)} changed, {len(clears)} cleared, {len(verified)} confirmed"
               + (f"; register: {len(d_upd)} updated, {len(d_del)} removed, {len(d_add)} added" if (d_upd or d_del or d_add) else ''))
    ch = {'op': 'replace', 'company_id': cid, 'fiscal_year': fy, 'checked': True, 'key': None, 'target_id': None,
          'label': f'Replace {fy} with the form', 'current': 'what is stored now', 'new': summary, 'details': details,
          'warning': (f'{len(clears)} value(s) left blank on the form will be cleared.' if clears else None),
          'data': {'set': sets, 'clear': clears, 'verified': verified, 'dir_update': d_upd, 'dir_delete': d_del, 'dir_add': d_add}}
    return ch, probs


def _apply_replace(cid, fy, data, file=''):
    allowed = tab_modules.survey_metric_columns() | tab_modules.survey_text_columns()
    row = tab_modules.survey_row(cid, fy, create=True)
    srcs, confs = dict(row.field_sources or {}), dict(row.field_confidence or {})
    status, corr = dict(row.field_review_status or {}), dict(row.field_review_corrections or {})
    note = f'{SOURCE_TAG} (replace{": " + file[:60] if file else ""})'
    now = datetime.utcnow().isoformat()
    for k, v in (data.get('set') or {}).items():
        if k not in allowed:
            continue
        before = getattr(row, k, None)
        if k in tab_modules.survey_text_columns():
            setattr(row, k, str(v)[:TEXT_MAX])
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            setattr(row, k, int(v) if _is_int_col(k) else float(v))
        else:
            continue
        srcs[k], confs[k], status[k] = note, 1.0, 'corrected'
        if before is not None:
            corr[k] = {'extracted_value': before, 'approved_value': v, 'reviewed_by': 'form upload (replace)', 'reviewed_at': now}
    for k in data.get('clear') or []:
        if k in allowed:
            before = getattr(row, k, None)
            setattr(row, k, None)
            # a deliberate blank: marked 'rejected' (as in the review workflow) so no reader, AI pass or
            # tab reconciliation fills the field back in later
            srcs[k], confs[k], status[k] = f'{note} - left blank on the form (not disclosed)', 1.0, 'rejected'
            corr[k] = {'extracted_value': before, 'approved_value': None, 'reviewed_by': 'form upload (replace)', 'reviewed_at': now}
    for k in data.get('verified') or []:
        if k in allowed and getattr(row, k, None) is not None:
            confs[k], status[k] = 1.0, 'approved'
    row.field_sources, row.field_confidence, row.field_review_status, row.field_review_corrections = srcs, confs, status, corr

    def fill(d, x):
        d.director_name = str(x['director_name'])[:150]
        d.role = x.get('role') if x.get('role') in ('executive', 'non_executive') else 'unknown'
        d.independent = x.get('independent') if isinstance(x.get('independent'), bool) else None
        d.gender = x.get('gender') if x.get('gender') in ('Male', 'Female') else None
        d.nationality = (str(x['nationality'])[:60] if x.get('nationality') else None)
        d.appointed_date = (str(x['appointed_date'])[:40] if x.get('appointed_date') else None)
        d.confidence = 1.0

    for x in data.get('dir_update') or []:
        d = db.session.get(SurveyDirector, x.get('id'))
        if d is not None and d.company_id == cid and d.fiscal_year == fy:
            fill(d, x)
    for did in data.get('dir_delete') or []:
        d = db.session.get(SurveyDirector, did)
        if d is not None and d.company_id == cid and d.fiscal_year == fy:
            db.session.delete(d)
    n = SurveyDirector.query.filter_by(company_id=cid, fiscal_year=fy).count()
    for i, x in enumerate(data.get('dir_add') or []):
        d = SurveyDirector(company_id=cid, fiscal_year=fy, director_name=str(x['director_name'])[:150], order_index=n + i)
        db.session.add(d)
        fill(d, x)
    return True, 'replaced'


# ------------------------------------------------------------------ apply
def _set_metric(row, col, value, note, confirm_only=False, fix=False):
    srcs, confs, status = dict(row.field_sources or {}), dict(row.field_confidence or {}), dict(row.field_review_status or {})
    corr = dict(row.field_review_corrections or {})
    if not confirm_only:
        before = getattr(row, col, None)
        if col in tab_modules.survey_text_columns():
            setattr(row, col, str(value)[:TEXT_MAX])
        else:
            setattr(row, col, int(value) if _is_int_col(col) else float(value))
        srcs[col] = note
        status[col] = 'corrected'
        if fix:
            corr[col] = {'extracted_value': before, 'approved_value': value, 'reviewed_by': 'form upload',
                         'reviewed_at': datetime.utcnow().isoformat()}
    else:
        status[col] = 'approved'
    confs[col] = 1.0
    row.field_sources, row.field_confidence, row.field_review_status = srcs, confs, status
    if fix:
        row.field_review_corrections = corr


def _apply_one(ch):
    cid, fy, op, data = int(ch['company_id']), str(ch['fiscal_year']), ch['op'], ch.get('data') or {}
    if db.session.get(Company, cid) is None:
        return False, 'company no longer exists'
    allowed = tab_modules.survey_metric_columns() | tab_modules.survey_text_columns()
    note = f'{SOURCE_TAG} ({ch.get("file", "")[:60]})' if ch.get('file') else SOURCE_TAG

    if op == 'replace':
        return _apply_replace(cid, fy, data, ch.get('file', ''))

    if op in ('metric', 'confirm', 'conflict'):
        col = ch.get('key')
        if col not in allowed:
            return False, f'{col} is not an enterable field'
        row = tab_modules.survey_row(cid, fy, create=True)
        if op == 'confirm' and data.get('value') is None:     # "stored value is right" - numeric or text field
            _set_metric(row, col, None, note, confirm_only=True)
            return True, 'confirmed'
        if col in tab_modules.survey_text_columns():          # free-text field: only a plain 'metric' entry, stored as text
            v = data.get('value')
            if op not in ('metric', 'confirm') or not isinstance(v, str) or not v.strip():
                return False, 'not text'
            if data.get('note'):
                note = f'{note} - {str(data["note"])[:80]}'
            _set_metric(row, col, v.strip(), note, fix=(op == 'confirm'))
            return True, 'saved'
        if op == 'confirm' and data.get('value') is None:
            _set_metric(row, col, None, note, confirm_only=True)
            return True, 'confirmed'
        v = data.get('value')
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            return False, 'not a number'
        if op == 'metric' and data.get('note'):
            note = f'{note} - {str(data["note"])[:80]}'
        _set_metric(row, col, v, note, fix=(op == 'confirm'))
        if op == 'conflict':
            for ec in EvidenceConflict.query.filter_by(company_id=cid, fiscal_year=fy, field_name=col, resolution=None).all():
                ec.resolution, ec.resolution_value = 'other', float(v)
                ec.resolved_by, ec.resolved_at = 'form upload', datetime.utcnow()
        return True, 'saved'

    if op == 'dir_update' or op == 'dir_new':
        if op == 'dir_update':
            d = db.session.get(SurveyDirector, ch.get('target_id'))
            if d is None or d.company_id != cid or d.fiscal_year != fy:
                return False, 'row no longer exists'
        else:
            name = str(data.get('director_name') or '').strip()
            if not name:
                return False, 'name missing'
            d = SurveyDirector(company_id=cid, fiscal_year=fy, director_name=name[:150], role='unknown',
                               order_index=SurveyDirector.query.filter_by(company_id=cid, fiscal_year=fy).count())
            db.session.add(d)
        if op == 'dir_update' and data.get('director_name'):
            d.director_name = str(data['director_name'])[:150]
        if data.get('role') in ('executive', 'non_executive'):
            d.role = data['role']
        if data.get('gender') in ('Male', 'Female'):
            d.gender = data['gender']
        if isinstance(data.get('independent'), bool):
            d.independent = data['independent']
        if data.get('nationality'):
            d.nationality = str(data['nationality'])[:60]
        if data.get('appointed_date'):
            d.appointed_date = str(data['appointed_date'])[:40]
        d.confidence = 1.0
        return True, 'saved'

    if op in ('com_update', 'com_new'):
        if op == 'com_update':
            c = db.session.get(Committee, ch.get('target_id'))
            if c is None or _owned_period(c.period_id, cid, fy) is None:
                return False, 'committee no longer exists'
        else:
            name = str(data.get('name') or '').strip()
            if not name:
                return False, 'name missing'
            period = tab_modules.ensure_period(cid, fy)
            c = Committee(period_id=period.id, source_document_id=None, name=name[:100],
                          order_index=Committee.query.filter_by(period_id=period.id).count())
            db.session.add(c)
            db.session.flush()
        if data.get('chairperson_name'):
            c.chairperson_name = str(data['chairperson_name'])[:150]
        if isinstance(data.get('meetings_held'), int) and data['meetings_held'] >= 0:
            c.meetings_held = data['meetings_held']
        if isinstance(data.get('members'), list) and data['members']:
            CommitteeMember.query.filter_by(committee_id=c.id).delete()
            for j, m in enumerate(str(x)[:150] for x in data['members']):
                db.session.add(CommitteeMember(committee_id=c.id, director_name=m, order_index=j,
                                               role_on_committee='chair' if c.chairperson_name and m.lower() == c.chairperson_name.lower() else 'member'))
            c.member_count = len(data['members'])
        c.confidence = 1.0
        return True, 'saved'

    if op in ('pay_update', 'pay_new'):
        if op == 'pay_update':
            r = db.session.get(DirectorRemunerationRow, ch.get('target_id'))
            if r is None or _owned_period(r.period_id, cid, fy) is None:
                return False, 'pay row no longer exists'
        else:
            name = str(data.get('director_name') or '').strip()
            if not name:
                return False, 'name missing'
            period = tab_modules.ensure_period(cid, fy)
            r = DirectorRemunerationRow(period_id=period.id, source_document_id=None, table_kind='manual', is_total_row=False,
                                        is_grand_total=False, director_name=name[:150],
                                        role=data.get('role') if data.get('role') in ('executive', 'non_executive') else 'unknown',
                                        order_index=DirectorRemunerationRow.query.filter_by(period_id=period.id).count())
            db.session.add(r)
        if isinstance(data.get('total'), (int, float)) and math.isfinite(data['total']):
            r.total = float(data['total'])
        if isinstance(data.get('components'), dict) and data['components']:
            try:
                have = json.loads(r.components) if r.components else {}
            except ValueError:
                have = {}
            have.update({str(k)[:60]: float(v) for k, v in data['components'].items() if isinstance(v, (int, float))})
            r.components = json.dumps(have)
        r.confidence = 1.0
        return True, 'saved'

    if op == 'ben':
        k = ch.get('key')
        if k not in tab_modules.BENEFIT_KEYS or not isinstance(data.get('provided'), bool):
            return False, 'not a valid benefit'
        row = tab_modules.survey_row(cid, fy, create=True)
        ned = dict(row.ned_benefits or {})
        ned[k] = {'provided': data['provided'], 'detail': (str(data.get('detail'))[:200] if data.get('detail') else None)}
        row.ned_benefits = ned
        srcs, confs = dict(row.field_sources or {}), dict(row.field_confidence or {})
        srcs['ned_benefits'], confs['ned_benefits'] = SOURCE_TAG, 1.0
        row.field_sources, row.field_confidence = srcs, confs
        return True, 'saved'
    return False, f'unknown change type {op}'


def apply_changes(changes):
    """Write the approved changes. Each change is its own transaction, so one bad row never blocks the others; then
    each affected company/year has its tabs rebuilt. -> {'results': [{'id', 'ok', 'message'}], 'reconciled': [...]}"""
    results, touched = [], set()
    for ch in changes or []:
        cid_, fy_ = ch.get('company_id'), ch.get('fiscal_year')
        try:
            ok, msg = _apply_one(ch)
            if ok:
                db.session.commit()
                touched.add((int(cid_), str(fy_)))
            else:
                db.session.rollback()
        except Exception as e:
            db.session.rollback()
            ok, msg = False, f'{type(e).__name__}: {e}'[:200]
        results.append({'id': ch.get('id'), 'ok': ok, 'message': msg})
    reconciled = []
    for cid, fy in sorted(touched):
        try:
            tab_modules.reconcile_tabs(cid, fy)
            db.session.commit()
            reconciled.append({'company_id': cid, 'fiscal_year': fy})
        except Exception:
            db.session.rollback()
    return {'results': results, 'reconciled': reconciled}