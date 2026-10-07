"""Account security for Finance Intel: lockout, optional two-step verification (TOTP), CSRF origin check.

Wired in with two lines in app.py:
    import account_security
    account_security.init(app)            # once, after web_hardening.init(app)

What it adds
  1. LOCKOUT. 5 wrong passwords/codes in a row lock THAT ACCOUNT for 15 minutes (per account, so a
     distributed guesser spread over many IPs is still stopped; the existing per-IP limiter stays).
     Env: LOGIN_MAX_FAILURES (default 5), LOGIN_LOCK_MINUTES (default 15).
  2. TWO-STEP VERIFICATION (TOTP, RFC 6238 - works with Google Authenticator, Authy, Microsoft
     Authenticator, 1Password...). Opt-in per account from My Profile. No extra pip package needed.
       POST /api/mfa/setup            -> {secret, otpauth_uri}  (not active until confirmed)
       POST /api/mfa/enable  {code}   -> turns it on
       POST /api/mfa/disable {password, code}
       POST /api/users/<id>/reset-mfa -> super admin only; clears MFA + lockout for a user who lost their phone
  3. ORIGIN CHECK (CSRF). A state-changing /api/ request whose Origin header points at a different site
     is rejected. Same-origin browser requests, tests and curl (no Origin header) are unaffected.
     Set CSRF_ORIGIN_CHECK=0 to turn it off.
  4. Session lifetime: SESSION_HOURS (default 12) - logins expire instead of lasting until the browser closes.
  5. is_production(): True on a Replit Deployment (or APP_ENV=production). app.py uses it to refuse to start
     on the throw-away SQLite file or the built-in dev session key.
"""
import base64
import hashlib
import hmac
import math
import os
import secrets
import struct
import time
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse

from flask import jsonify, request, session

ISSUER = 'Finance Intel'


# ---------------------------------------------------------------- environment
def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def is_production():
    return (os.environ.get('REPLIT_DEPLOYMENT', '').strip().lower() in ('1', 'true')
            or os.environ.get('APP_ENV', '').strip().lower() == 'production')


# ---------------------------------------------------------------- TOTP (RFC 6238, stdlib only)
def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')


def _hotp(secret, counter):
    key = base64.b32decode(secret + '=' * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f'{value:06d}'


def match_totp(secret, code, window=1, now=None):
    """Return the 30-second step the code belongs to, or None. Accepts +/- `window` steps of clock drift."""
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if len(code) != 6:
        return None
    step = int((time.time() if now is None else now) // 30)
    for drift in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret, step + drift), code):
            return step + drift
    return None


def otpauth_uri(secret, email):
    label = quote(f'{ISSUER}:{email}')
    return f'otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&algorithm=SHA1&digits=6&period=30'


def verify_user_code(user, code):
    """Check a code for an MFA-enabled user. A code (step) can be used once - replays are refused."""
    if not user.totp_secret:
        return False
    step = match_totp(user.totp_secret, code)
    if step is None or step <= (user.mfa_last_step or 0):
        return False
    user.mfa_last_step = step
    return True


# ---------------------------------------------------------------- lockout
def lock_message(user):
    until = getattr(user, 'locked_until', None)
    if until and until > datetime.utcnow():
        minutes = max(1, math.ceil((until - datetime.utcnow()).total_seconds() / 60))
        return f'Too many failed attempts. Try again in {minutes} minute{"s" if minutes != 1 else ""}.'
    return None


def register_failure(user, db):
    limit = _int_env('LOGIN_MAX_FAILURES', 5)
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= limit:
        user.locked_until = datetime.utcnow() + timedelta(minutes=_int_env('LOGIN_LOCK_MINUTES', 15))
        user.failed_login_count = 0
    db.session.commit()


def unlock(user, db):
    user.failed_login_count = 0
    user.locked_until = None
    db.session.commit()


def attempt_login(user, password, code, db):
    """The whole sign-in decision. Returns (http_status, json_payload); status 200 means authenticated."""
    if user is None:
        return 401, {'error': 'Incorrect email or password.'}
    blocked = lock_message(user)
    if blocked:
        return 429, {'error': blocked}
    if not user.check_password(password):
        register_failure(user, db)
        return 401, {'error': 'Incorrect email or password.'}
    if user.mfa_enabled:
        if not code:
            return 401, {'error': 'Enter the 6-digit code from your authenticator app.', 'mfa_required': True}
        if not verify_user_code(user, code):
            register_failure(user, db)
            return 401, {'error': 'Incorrect or already-used code.', 'mfa_required': True}
    user.failed_login_count = 0
    user.locked_until = None
    db.session.commit()
    return 200, {}


# ---------------------------------------------------------------- wiring
def init(app):
    from models import db, User

    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=_int_env('SESSION_HOURS', 12))

    def _me():
        uid = session.get('user_id')
        return User.query.get(uid) if uid else None

    # ---- CSRF: refuse cross-site state-changing API calls -------------------------------------
    @app.before_request
    def _origin_check():
        if request.method in ('GET', 'HEAD', 'OPTIONS') or not request.path.startswith('/api/'):
            return None
        if os.environ.get('CSRF_ORIGIN_CHECK', '1').strip().lower() in ('0', 'false', 'no', 'off'):
            return None
        origin = request.headers.get('Origin')
        if not origin:
            return None
        allowed = {request.host}
        site = (os.environ.get('SITE_URL') or '').strip()
        if site:
            allowed.add(urlparse(site).netloc)
        if urlparse(origin).netloc not in allowed:
            return jsonify({'error': 'Cross-site request blocked.'}), 403
        return None

    # ---- MFA enrolment ---------------------------------------------------------------------------
    @app.route('/api/mfa/setup', methods=['POST'])
    def mfa_setup():
        u = _me()
        if not u:
            return jsonify({'error': 'You must be logged in.'}), 401
        if u.mfa_enabled:
            return jsonify({'error': 'Two-step verification is already on.'}), 409
        secret = new_secret()
        session['mfa_pending_secret'] = secret
        return jsonify({'secret': secret, 'otpauth_uri': otpauth_uri(secret, u.email)})

    @app.route('/api/mfa/enable', methods=['POST'])
    def mfa_enable():
        u = _me()
        if not u:
            return jsonify({'error': 'You must be logged in.'}), 401
        secret = session.get('mfa_pending_secret')
        if not secret:
            return jsonify({'error': 'Start the setup first.'}), 400
        code = (request.get_json(force=True) or {}).get('code')
        step = match_totp(secret, code)
        if step is None:
            return jsonify({'error': 'That code is not correct. Check the time on your phone and try again.'}), 400
        u.totp_secret = secret
        u.mfa_enabled = True
        u.mfa_last_step = step
        db.session.commit()
        session.pop('mfa_pending_secret', None)
        return jsonify({'ok': True, 'mfa_enabled': True})

    @app.route('/api/mfa/disable', methods=['POST'])
    def mfa_disable():
        u = _me()
        if not u:
            return jsonify({'error': 'You must be logged in.'}), 401
        if not u.mfa_enabled:
            return jsonify({'ok': True, 'mfa_enabled': False})
        data = request.get_json(force=True) or {}
        if not u.check_password(data.get('password') or '') or not verify_user_code(u, data.get('code')):
            register_failure(u, db)
            return jsonify({'error': 'Password or code is incorrect.'}), 401
        u.mfa_enabled = False
        u.totp_secret = None
        u.mfa_last_step = None
        db.session.commit()
        return jsonify({'ok': True, 'mfa_enabled': False})

    @app.route('/api/users/<int:user_id>/reset-mfa', methods=['POST'])
    def mfa_admin_reset(user_id):
        me = _me()
        if not (me and me.role == 'admin' and me.is_super_admin):
            return jsonify({'error': 'Only a super admin can reset two-step verification.'}), 403
        target = User.query.get_or_404(user_id)
        target.mfa_enabled = False
        target.totp_secret = None
        target.mfa_last_step = None
        unlock(target, db)
        return jsonify({'ok': True})
