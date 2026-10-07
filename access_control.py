"""
Anti-scraping access controls for Finance Intel.

Hiding a menu item is UI polish; this module is the server-side enforcement. It adds three
things, all wired in one call (access_control.register(...) in app.py):

  1. GUEST DEFAULT-DENY. A visitor who is not signed in can only read the handful of /api/ GET
     endpoints the Dashboard Overview itself needs (GUEST_GET_ALLOWED below). Every other GET
     under /api/ returns 403 for a guest - including routes added in future, because this is an
     allow-list, not a block-list.

  2. RATE LIMITS (Flask-Limiter). Per signed-in account (or per IP for guests):
       - every /api/ route:  API_RATE_LIMIT_PER_MIN (default 200) and API_RATE_LIMIT_PER_DAY (5000)
       - sign-in:            10 per minute and 40 per hour, per IP     (stops password guessing)
       - sign-up:            5 per hour, per IP                         (stops mass account creation)
     /api/events (the live-update stream) and /api/me are exempt. RATELIMIT_ENABLED=false turns
     the limiter off entirely (used by the test suite). If Flask-Limiter is not installed
     the app still starts (guest deny and the export cap keep working) and logs a warning.
     Counters live in memory per gunicorn worker by default, so with 2 workers the real ceiling is
     up to double. Set RATELIMIT_STORAGE_URI (e.g. a Redis URL) to share counters across workers.

  3. DAILY EXPORT CAP for 'user' (Member) accounts: EXPORT_DAILY_LIMIT (default 5) downloads per
     UTC day. Admins are exempt; set EXPORT_DAILY_LIMIT=0 to switch the cap off. The count comes
     from the account-tagged activity log (category "Exports & Downloads"), so it is shared by all
     workers and survives a restart.

Every limit is an environment variable so it can be tuned without a code change.
"""

import logging
import os
from datetime import datetime

from flask import g, jsonify, request, session

log = logging.getLogger(__name__)

# The only /api/ GET endpoints a guest may call: exactly what the Dashboard Overview fetches,
# plus the session probe and the live-update stream. Anything else needs an account.
GUEST_GET_ALLOWED = frozenset({
    '/api/me',
    '/api/events',
    '/api/companies',
    '/api/survey/companies',
    '/api/survey/data-quality',
    '/api/import-jobs',
})

# Not counted against the general per-minute / per-day API limits.
RATE_LIMIT_EXEMPT = frozenset({'/api/events', '/api/me'})


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _account_or_ip_key():
    uid = session.get('user_id')          # the session cookie is signed, so this cannot be forged
    return f'u:{uid}' if uid else f'ip:{request.remote_addr or "unknown"}'


def _ip_key():
    return f'ip:{request.remote_addr or "unknown"}'


def _utc_midnight():
    return datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


def register(app, db, ActivityLogEntry, current_user, activity_log):
    """Attach the guest allow-list, the export cap and (if available) the rate limiter."""

    # ---- 1. guests may only read what the Overview needs -------------------------------------
    @app.before_request
    def _guest_default_deny():
        if request.method not in ('GET', 'HEAD'):
            return None
        path = request.path
        if not path.startswith('/api/') or path in GUEST_GET_ALLOWED:
            return None
        if session.get('user_id') and current_user() is not None:
            return None
        return jsonify({'error': 'Please sign in to view this data.'}), 403

    # ---- 3. daily export cap for Member accounts ---------------------------------------------
    @app.before_request
    def _daily_export_cap():
        if request.method != 'GET' or not request.path.startswith('/api/') or not request.url_rule:
            return None
        limit = _int_env('EXPORT_DAILY_LIMIT', 5)
        if limit <= 0:
            return None
        _, category, _ = activity_log.classify_request('GET', request.url_rule.rule)
        if category != activity_log.CAT_EXPORT:
            return None
        user = current_user()
        if user is None or user.role == 'admin':
            return None                    # guests were already refused above; admins are exempt
        used = ActivityLogEntry.query.filter(
            ActivityLogEntry.user_id == user.id,
            ActivityLogEntry.category == activity_log.CAT_EXPORT,
            ActivityLogEntry.created_at >= _utc_midnight(),
        ).count()
        if used >= limit:
            return jsonify({'error': f'Daily download limit reached ({limit} per day). '
                                     'It resets at midnight UTC - ask an admin if you need more.'}), 429
        return None

    # ---- 2. rate limits ----------------------------------------------------------------------
    try:
        from flask_limiter import Limiter
    except ImportError:
        log.warning('Flask-Limiter is not installed: API rate limits are OFF. '
                    'Run "pip install Flask-Limiter" (it is listed in requirements.txt).')
        return None

    limiter = Limiter(
        key_func=_account_or_ip_key,
        app=app,
        default_limits=[
            f"{_int_env('API_RATE_LIMIT_PER_MIN', 200)} per minute",
            f"{_int_env('API_RATE_LIMIT_PER_DAY', 5000)} per day",
        ],
        default_limits_exempt_when=lambda: (not request.path.startswith('/api/')
                                            or request.path in RATE_LIMIT_EXEMPT),
        storage_uri=os.environ.get('RATELIMIT_STORAGE_URI', 'memory://'),
        headers_enabled=True,
    )

    limiter.enabled = os.environ.get('RATELIMIT_ENABLED', 'true').strip().lower() not in ('0', 'false', 'no', 'off')

    # Flask-Limiter only keeps a weak reference to itself on the views it wraps, so hold a strong one here
    # (the caller of register() does not keep the return value).
    app.extensions['access_control_limiter'] = limiter

    # Sign-in / sign-up are limited per IP and override the general limits.
    for endpoint, rule in (('login', '10 per minute;40 per hour'), ('register', '5 per hour')):
        view = app.view_functions.get(endpoint)
        if view is not None:
            app.view_functions[endpoint] = limiter.limit(rule, key_func=_ip_key)(view)
        else:
            log.warning('access_control: no "%s" endpoint found, so it has no special limit', endpoint)

    @app.errorhandler(429)
    def _too_many_requests(err):
        resp = jsonify({'error': 'Too many requests - please slow down and try again shortly.'})
        resp.status_code = 429
        retry = getattr(err, 'retry_after', None)
        if retry:
            resp.headers['Retry-After'] = str(retry)
        return resp

    return limiter
