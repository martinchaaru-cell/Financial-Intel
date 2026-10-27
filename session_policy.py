"""When a signed-in user is signed out.

  Left the app     While the app is open it checks in with the server every minute (GET /api/me), including from a
                   background tab. When the person closes the tab or browser, locks the phone or switches to another
                   app on the phone (the browser then pauses the page), the check-ins stop. Once nothing has been
                   heard for SESSION_AWAY_MINUTES (default 3) the sign-in ends, and the next visit asks for the
                   password again.
  Too long         A sign-in lasts at most SESSION_MAX_HOURS (default 12), however active the person is.
  Elsewhere        Signing in on another device ends this one (billing.py, one active sign-in per account).

The sign-in cookie itself is also a browser-session cookie, so closing the browser removes it.
Each ended sign-in is reported to the page in the X-Session-Ended header (away / expired / replaced), and the page
sends the person to the sign-in screen with a message saying why.

Wired from app.py with one call:  session_policy.register(app)
"""
import os
import time

from flask import g, request, session


def _num(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def away_seconds():
    return _num('SESSION_AWAY_MINUTES', 3) * 60


def max_seconds():
    return _num('SESSION_MAX_HOURS', 12) * 3600


def start(sess=None):
    """Call when a sign-in begins: stamps when it started and when the person was last seen."""
    s = session if sess is None else sess
    now = time.time()
    s.permanent = False             # a browser-session cookie: gone when the browser closes
    s['login_at'] = now
    s['seen'] = now


def register(app):
    def _presence():
        g.session_ended = None
        if not session.get('user_id') or request.path.startswith('/static/'):
            return None
        now = time.time()
        login_at, seen = session.get('login_at'), session.get('seen')
        if login_at is None:            # signed in before this rule existed: start the clock now
            start()
            return None
        reason = None
        if now - login_at > max_seconds():
            reason = 'expired'
        elif seen is not None and now - seen > away_seconds():
            reason = 'away'
        if reason:
            session.clear()
            g.session_ended = reason
            return None
        if now - (seen or 0) > 15:      # avoid rewriting the cookie on every request
            session['seen'] = now
        return None

    # Runs before every other check, so a stale sign-in is treated as signed out everywhere.
    app.before_request_funcs.setdefault(None, []).insert(0, _presence)

    @app.after_request
    def _report_ended(resp):
        reason = getattr(g, 'session_ended', None)
        if reason and not resp.headers.get('X-Session-Ended'):
            resp.headers['X-Session-Ended'] = reason
        return resp
