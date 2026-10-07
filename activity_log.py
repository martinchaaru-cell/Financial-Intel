"""
Account-tagged activity log.

Every action that changes something (uploads, edits, reviews, deletes, user management),
every sign-in / sign-out, every failed sign-in and every export/download is written to
activity_log_entries, tagged to the ACCOUNT that did it and stamped with a UTC timestamp.

Two layers feed the log:

  1. Explicit entries - app.py's log_activity(company_id, action, ...) at the domain write
     paths (survey upload, field review, committees...). These carry company + fiscal year
     and a human description.
  2. A catch-all after_request hook - any other successful POST/PUT/PATCH/DELETE under /api/,
     plus export/download GETs, is logged automatically, so nothing slips through just because
     nobody remembered to add a log_activity() call. If a request already logged an explicit
     entry, the catch-all stays quiet for it (no duplicates).

Reading it back (admin only):

  GET /api/activity             overall + per-day report (JSON), filterable
  GET /api/activity/export.csv  the same filtered entries as a CSV

Logging never breaks the request it describes: every write is wrapped and rolled back on error.
"""

import csv
import io
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from flask import g, request, jsonify, Response
from sqlalchemy import or_

# ----------------------------------------------------------------------------- categories
CAT_AUTH = 'Sign-in & Accounts'
CAT_USERS = 'User Management'
CAT_UPLOAD = 'Uploads & Extraction'
CAT_REVIEW = 'Review & Corrections'
CAT_DATA = 'Data Entry & Edits'
CAT_COMPANY = 'Companies'
CAT_AI = 'AI'
CAT_EXPORT = 'Exports & Downloads'
CAT_OTHER = 'Other'

CATEGORIES = [CAT_AUTH, CAT_USERS, CAT_UPLOAD, CAT_REVIEW, CAT_DATA, CAT_COMPANY, CAT_AI, CAT_EXPORT, CAT_OTHER]

# Explicit log_activity() actions -> (category, human label)
ACTION_INFO = {
    'login': (CAT_AUTH, 'Signed in'),
    'logout': (CAT_AUTH, 'Signed out'),
    'login_failed': (CAT_AUTH, 'Failed sign-in attempt'),
    'register': (CAT_AUTH, 'Created an account'),
    'profile_updated': (CAT_AUTH, 'Updated their profile'),
    'survey_uploaded': (CAT_UPLOAD, 'Uploaded survey data'),
    'survey_updated': (CAT_UPLOAD, 'Updated survey data'),
    'survey_pdf_extracted': (CAT_UPLOAD, 'Imported a report PDF'),
    'field_approved': (CAT_REVIEW, 'Approved a field'),
    'field_rejected': (CAT_REVIEW, 'Rejected a field'),
    'field_corrected': (CAT_REVIEW, 'Corrected a field'),
    'unit_corrected': (CAT_REVIEW, 'Corrected a headline unit'),
    'ai_apply': (CAT_AI, 'Applied an AI result'),
    'committees_updated': (CAT_DATA, 'Saved committees'),
    'company_year_deleted': (CAT_COMPANY, "Deleted a company's year of data"),
    'company_deleted': (CAT_COMPANY, 'Deleted a company'),
}

# Request-derived entries: (method, url rule) -> (action, category, label). Anything not listed
# here is still logged, categorised and labelled by the generic rules in classify_request().
ROUTE_INFO = {
    ('POST', '/api/login'): ('login', CAT_AUTH, 'Signed in'),
    ('POST', '/api/logout'): ('logout', CAT_AUTH, 'Signed out'),
    ('POST', '/api/register'): ('register', CAT_AUTH, 'Created an account'),
    ('PUT', '/api/profile'): ('profile_updated', CAT_AUTH, 'Updated their profile'),
    ('POST', '/api/users'): ('user_created', CAT_USERS, 'Created a user account'),
    ('PATCH', '/api/users/<int:user_id>/role'): ('user_role_changed', CAT_USERS, "Changed a user's role"),
    ('POST', '/api/users/<int:user_id>/reset-password'): ('user_password_reset', CAT_USERS, "Reset a user's password"),
    ('DELETE', '/api/users/<int:user_id>'): ('user_deleted', CAT_USERS, 'Deleted a user account'),
    ('POST', '/api/companies'): ('company_added', CAT_COMPANY, 'Added a company'),
    ('DELETE', '/api/companies/<int:company_id>'): ('company_deleted', CAT_COMPANY, 'Deleted a company'),
    ('POST', '/api/companies/bulk-delete'): ('companies_deleted', CAT_COMPANY, 'Deleted several companies'),
    ('POST', '/api/companies/delete-year'): ('company_years_deleted', CAT_COMPANY, 'Deleted a year of data for several companies'),
    ('POST', '/api/companies/<int:company_id>/financials'): ('financials_saved', CAT_DATA, 'Saved company financials'),
    ('POST', '/api/import/upload-batch'): ('upload_batch', CAT_UPLOAD, 'Uploaded a batch of reports'),
    ('POST', '/api/survey-data/import'): ('survey_import', CAT_UPLOAD, 'Imported survey data'),
    ('POST', '/api/survey-data/import-pdf'): ('survey_import_pdf', CAT_UPLOAD, 'Imported a report PDF'),
    ('POST', '/api/survey-data/import-policy-pdf'): ('policy_import_pdf', CAT_UPLOAD, 'Imported a policy PDF'),
    ('DELETE', '/api/survey/companies/<int:company_id>/<fiscal_year>'): ('survey_deleted', CAT_DATA, 'Deleted a company-year of survey data'),
    ('POST', '/api/ai/run'): ('ai_run', CAT_AI, 'Started an AI extraction run'),
    ('POST', '/api/ai/jobs/<int:job_id>/collect'): ('ai_collect', CAT_AI, 'Collected AI results'),
    ('POST', '/api/ai/items/<int:item_id>/apply'): ('ai_apply', CAT_AI, 'Applied an AI result'),
    ('POST', '/api/ai/items/<int:item_id>/discard'): ('ai_discard', CAT_AI, 'Discarded an AI result'),
}

# Mutating routes that are not changes and would only be noise.
_SKIP_RULES = {('POST', '/api/ai/estimate')}


def classify_request(method, rule):
    """-> (action, category, label) for a request. Known routes use ROUTE_INFO; the rest are
    categorised from the shape of the URL so new routes are covered without editing this file."""
    hit = ROUTE_INFO.get((method, rule))
    if hit:
        return hit
    r = (rule or '').lower()
    verb = {'POST': 'Saved', 'PUT': 'Updated', 'PATCH': 'Updated', 'DELETE': 'Deleted', 'GET': 'Downloaded'}.get(method, method.title())
    segs = [s for s in r.split('/') if s and not s.startswith('<') and s != 'api']
    noun = ' / '.join(s.replace('-', ' ') for s in segs[-2:]) or 'item'
    if any(k in r for k in ('export', 'download', '.pdf', '.docx', '.csv', '.zip', 'form.pdf')):
        cat = CAT_EXPORT
        verb = 'Downloaded'
    elif any(k in r for k in ('upload', 'import', 'extract', 'scan')):
        cat = CAT_UPLOAD
    elif any(k in r for k in ('/review', 'approve', 'reject', 'resolve', 'conflict', 'reconcile', 'discard')):
        cat = CAT_REVIEW
    elif '/api/ai' in r:
        cat = CAT_AI
    elif '/api/users' in r:
        cat = CAT_USERS
    elif r.startswith('/api/companies') and method in ('POST', 'DELETE') and r.count('/') <= 3:
        cat = CAT_COMPANY
    elif method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        cat = CAT_DATA
    else:
        cat = CAT_OTHER
    action = f"{method.lower()}:{'/'.join(segs) or 'root'}"[:80]
    return action, cat, f'{verb} {noun}'


def category_for(action):
    info = ACTION_INFO.get(action)
    return info[0] if info else CAT_OTHER


def label_for(action, detail=None):
    info = ACTION_INFO.get(action)
    if info:
        return info[1]
    for act, _cat, label in ROUTE_INFO.values():
        if act == action:
            return label
    if detail:                                   # generic request entries start with their label
        return detail.split(' · ')[0]
    return (action or '').replace('_', ' ').replace(':', ' ').strip().capitalize()


# ----------------------------------------------------------------------------- writing
def _actor_snapshot(current_user):
    """Who is acting right now, as plain values (so the entry survives account changes)."""
    try:
        u = getattr(g, '_activity_actor', None) or current_user()
    except Exception:
        u = None
    if u is None:
        return {'user_id': None, 'actor': 'Guest', 'actor_email': None, 'actor_role': 'guest'}
    if isinstance(u, dict):
        return u
    return {'user_id': u.id, 'actor': u.name, 'actor_email': u.email, 'actor_role': u.role}


def write_entry(db, Model, current_user, company_id, action, detail=None, fiscal_year=None,
                actor=None, category=None, commit=True):
    """Insert one entry tagged to the signed-in account. Never raises."""
    try:
        who = _actor_snapshot(current_user)
        note = detail
        # A free-text "reviewed by" name typed into a form is kept as information, but the
        # entry itself is always tagged to the real signed-in account.
        if actor and actor != who['actor'] and who['user_id'] is not None:
            note = f"{detail} · reviewer: {actor}" if detail else f'reviewer: {actor}'
        elif actor and who['user_id'] is None:
            who = dict(who, actor=actor)
        try:
            method, path = request.method, request.path[:200]
        except RuntimeError:        # no request context (background job)
            method = path = None
        row = Model(
            company_id=company_id, fiscal_year=fiscal_year, action=action[:80],
            detail=(note or '')[:300] or None,
            actor=(who['actor'] or '')[:150] or None, user_id=who['user_id'],
            actor_email=who['actor_email'], actor_role=who['actor_role'],
            category=category or category_for(action),
            http_method=method, path=path,
        )
        db.session.add(row)
        if commit:
            db.session.commit()
        try:
            g._activity_logged = True
        except RuntimeError:
            pass
        return row
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


# ----------------------------------------------------------------------------- reporting
def _local(dt, tz_offset):
    """Naive-UTC datetime -> the viewer's local naive datetime. tz_offset is JavaScript's
    Date.getTimezoneOffset() (minutes, UTC minus local), so Nairobi sends -180."""
    return dt - timedelta(minutes=tz_offset)


def _iso(dt):
    return dt.isoformat() + 'Z' if dt else None


def _entry_dict(e, company_names, tz_offset):
    return {
        'id': e.id,
        'at': _iso(e.created_at),
        'time_local': _local(e.created_at, tz_offset).strftime('%H:%M:%S'),
        'account': e.actor or 'Guest',
        'user_id': e.user_id,
        'email': e.actor_email,
        'role': e.actor_role,
        'category': e.category or category_for(e.action),
        'action': e.action,
        'label': label_for(e.action, e.detail),
        'company_id': e.company_id,
        'company': company_names.get(e.company_id),
        'fiscal_year': e.fiscal_year,
        'detail': e.detail,
    }


def _account_key(e):
    return e.user_id if e.user_id is not None else f"guest:{e.actor or 'Guest'}"


def build_report(entries, company_names=None, tz_offset=0, max_entries_in_days=1500):
    """entries: ActivityLogEntry-like rows, any order. Returns the overall summary and the
    per-day breakdown. Pure function - no database access - so it is easy to test."""
    company_names = company_names or {}
    entries = sorted(entries, key=lambda e: e.created_at, reverse=True)

    by_cat = Counter()
    accounts = {}
    for e in entries:
        cat = e.category or category_for(e.action)
        by_cat[cat] += 1
        k = _account_key(e)
        a = accounts.setdefault(k, {
            'user_id': e.user_id, 'account': e.actor or 'Guest', 'email': e.actor_email,
            'role': e.actor_role, 'count': 0, 'first_at': e.created_at, 'last_at': e.created_at,
            'categories': Counter(), 'days': set(),
        })
        a['count'] += 1
        a['categories'][cat] += 1
        a['first_at'] = min(a['first_at'], e.created_at)
        a['last_at'] = max(a['last_at'], e.created_at)
        a['days'].add(_local(e.created_at, tz_offset).date())
        if e.actor and a['last_at'] == e.created_at:      # keep the most recent name/email/role
            a['account'], a['email'], a['role'] = e.actor, e.actor_email or a['email'], e.actor_role or a['role']

    overall = {
        'total': len(entries),
        'accounts_active': len(accounts),
        'first_at': _iso(entries[-1].created_at) if entries else None,
        'last_at': _iso(entries[0].created_at) if entries else None,
        'by_category': [{'category': c, 'count': n} for c, n in
                        sorted(by_cat.items(), key=lambda kv: (-kv[1], CATEGORIES.index(kv[0]) if kv[0] in CATEGORIES else 99))],
        'by_account': [{
            'user_id': a['user_id'], 'account': a['account'], 'email': a['email'], 'role': a['role'],
            'count': a['count'], 'active_days': len(a['days']),
            'first_at': _iso(a['first_at']), 'last_at': _iso(a['last_at']),
            'categories': [{'category': c, 'count': n} for c, n in a['categories'].most_common()],
        } for a in sorted(accounts.values(), key=lambda a: -a['count'])],
    }

    days = defaultdict(list)
    for e in entries:
        days[_local(e.created_at, tz_offset).date()].append(e)
    day_list, budget = [], max_entries_in_days
    for d in sorted(days, reverse=True):
        es = days[d]
        cats, accts = Counter(), {}
        for e in es:
            cats[e.category or category_for(e.action)] += 1
            k = _account_key(e)
            accts.setdefault(k, {'user_id': e.user_id, 'account': e.actor or 'Guest', 'count': 0})['count'] += 1
        shown = es[:budget] if budget > 0 else []
        budget -= len(shown)
        day_list.append({
            'date': d.isoformat(),
            'weekday': d.strftime('%A'),
            'total': len(es),
            'by_category': [{'category': c, 'count': n} for c, n in cats.most_common()],
            'by_account': sorted(accts.values(), key=lambda a: -a['count']),
            'entries': [_entry_dict(e, company_names, tz_offset) for e in shown],   # newest first
            'entries_truncated': len(shown) < len(es),
        })
    return {'overall': overall, 'days': day_list}


# ----------------------------------------------------------------------------- wiring
def _parse_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def register(app, db, Model, Company, current_user, require_role):
    """Attach the capture hooks and the admin-only read routes to the Flask app."""

    @app.before_request
    def _remember_actor():
        # Captured before the view runs so a sign-out (which clears the session) is still
        # attributed to the account that signed out.
        g._activity_logged = False
        g._activity_actor = None
        try:
            u = current_user()
            if u is not None:
                g._activity_actor = {'user_id': u.id, 'actor': u.name, 'actor_email': u.email, 'actor_role': u.role}
        except Exception:
            pass

    @app.after_request
    def _log_request(resp):
        try:
            if not request.path.startswith('/api/') or request.path.startswith('/api/activity'):
                return resp
            rule = request.url_rule.rule if request.url_rule else None
            if not rule:
                return resp
            method = request.method
            if (method, rule) in _SKIP_RULES:
                return resp
            action, cat, label = classify_request(method, rule)

            if (method, rule) == ('POST', '/api/login'):
                if resp.status_code >= 400:
                    body = request.get_json(silent=True) or {}
                    attempted = (body.get('email') or '').strip().lower()[:150] or None
                    g._activity_actor = {'user_id': None, 'actor': attempted or 'Unknown', 'actor_email': attempted, 'actor_role': None}
                    write_entry(db, Model, current_user, None, 'login_failed',
                                detail='Incorrect email or password', category=CAT_AUTH)
                    return resp
            elif method == 'GET':
                # Only exports/downloads count as activity; ordinary page reads do not.
                if cat != CAT_EXPORT or resp.status_code >= 400:
                    return resp
            else:
                if resp.status_code >= 400:
                    return resp

            if getattr(g, '_activity_logged', False):
                return resp          # the view already wrote a richer entry for this request

            args = request.view_args or {}
            company_id = args.get('company_id')
            fy = args.get('fiscal_year') or args.get('period_label')
            bits = [label]
            for k, v in args.items():
                if k not in ('company_id', 'fiscal_year', 'period_label'):
                    bits.append(f'{k.replace("_", " ")} {v}')
            if request.files:
                names = [f.filename for f in request.files.getlist(next(iter(request.files)))][:3]
                if names:
                    bits.append('file: ' + ', '.join(n for n in names if n))
            # a successful sign-in / registration has only just created the session
            if (method, rule) in (('POST', '/api/login'), ('POST', '/api/register')):
                g._activity_actor = None          # fall back to the freshly-set session user
            write_entry(db, Model, current_user, company_id, action,
                        detail=' · '.join(bits), fiscal_year=fy, category=cat)
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
        return resp

    # ---- read side -------------------------------------------------------------
    def _filtered(args):
        q = Model.query
        days = args.get('days')
        if days and days != 'all':
            n = _parse_int(days, 30)
            q = q.filter(Model.created_at >= datetime.utcnow() - timedelta(days=n))
        uid = args.get('user_id')
        if uid:
            if str(uid).startswith('guest'):
                q = q.filter(Model.user_id.is_(None))
            else:
                q = q.filter(Model.user_id == _parse_int(uid, -1))
        cat = args.get('category')
        if cat:
            q = q.filter(Model.category == cat)
        term = (args.get('q') or '').strip()
        if term:
            like = f'%{term}%'
            q = q.filter(or_(Model.detail.ilike(like), Model.action.ilike(like),
                                Model.actor.ilike(like), Model.actor_email.ilike(like)))
        return q.order_by(Model.created_at.desc())

    def _company_names(rows):
        ids = {r.company_id for r in rows if r.company_id}
        return {c.id: c.name for c in Company.query.filter(Company.id.in_(ids)).all()} if ids else {}

    @app.route('/api/activity', methods=['GET'])
    @require_role('admin')
    def activity_report():
        """Overall + per-day activity. ?days=7|30|90|all  ?user_id=  ?category=  ?q=
        ?tz_offset=<JS getTimezoneOffset()> so 'per day' follows the viewer's own calendar day."""
        tz = _parse_int(request.args.get('tz_offset'), 0)
        rows = _filtered(request.args).limit(20000).all()
        rep = build_report(rows, _company_names(rows), tz)
        # filter choices come from the whole log, not just the current filter
        all_accounts = db.session.query(Model.user_id, Model.actor, Model.actor_email).distinct().all()
        seen, choices = set(), []
        for uid, name, email in all_accounts:
            k = uid if uid is not None else 'guest'
            if k in seen:
                continue
            seen.add(k)
            choices.append({'value': str(uid) if uid is not None else 'guest', 'label': name or 'Guest', 'email': email})
        rep['filters'] = {
            'accounts': sorted(choices, key=lambda c: c['label'].lower()),
            'categories': [c for c in CATEGORIES],
        }
        return jsonify(rep)

    @app.route('/api/activity/export.csv', methods=['GET'])
    @require_role('admin')
    def activity_export_csv():
        tz = _parse_int(request.args.get('tz_offset'), 0)
        rows = _filtered(request.args).limit(50000).all()
        names = _company_names(rows)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['Date', 'Time', 'Timestamp (UTC)', 'Account', 'Email', 'Role', 'Category',
                    'Activity', 'Company', 'Fiscal year', 'Detail'])
        for e in rows:
            loc = _local(e.created_at, tz)
            w.writerow([loc.strftime('%Y-%m-%d'), loc.strftime('%H:%M:%S'), _iso(e.created_at),
                        e.actor or 'Guest', e.actor_email or '', e.actor_role or '',
                        e.category or category_for(e.action), label_for(e.action, e.detail),
                        names.get(e.company_id, ''), e.fiscal_year or '', e.detail or ''])
        return Response('\ufeff' + buf.getvalue(), mimetype='text/csv', headers={
            'Content-Disposition': 'attachment; filename=activity-log.csv'})
