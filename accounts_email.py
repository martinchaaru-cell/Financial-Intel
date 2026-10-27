"""Email-based account flows and website enquiries.

  Sign-up verification   /api/register creates the account unconfirmed and emails a link (24 hours). The link
                         confirms the address and signs the person in. Until then sign-in answers "check your email"
                         with a resend option. Accounts that existed before, and accounts staff create, count as
                         confirmed. EMAIL_VERIFICATION=off turns the check off.
  Forgotten password     /api/password/forgot emails a one-time link (1 hour). The answer is the same whether or
                         not the email has an account, so the form cannot be used to find out who is registered.
                         Setting the new password clears any sign-in lock, signs out other devices and sends a
                         "your password was changed" email.
  Invitations            a super admin can create an account without a password: the person gets a link (72 hours)
                         to choose their own.
  Contact enquiries      saved, emailed to the contact address (Reply-To: the sender) and answered with an
                         automatic acknowledgement. Super admins work through them under Settings > Enquiries.

Links contain a random token; only its SHA-256 hash is stored (models_auth.AuthToken).
Wired from app.py with one call:  accounts_email.register(app, current_user, require_super_admin)
"""
import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta

from flask import jsonify, redirect, render_template, request, session

import mailer
from models import db, User
from models_auth import AuthToken, ContactEnquiry, EmailLog

TTL = {'verify': timedelta(hours=24), 'reset': timedelta(hours=1), 'invite': timedelta(hours=72)}
PATHS = {'verify': '/verify-email', 'reset': '/reset-password', 'invite': '/set-password'}
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
MIN_PASSWORD = 8


def verification_required():
    return os.environ.get('EMAIL_VERIFICATION', 'on').strip().lower() not in ('0', 'off', 'false', 'no')


def valid_email(e):
    return bool(EMAIL_RE.match((e or '').strip()))


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _ip():
    return (request.headers.get('X-Forwarded-For') or request.remote_addr or '')[:64].split(',')[0].strip()


def _site():
    from web_hardening import site_url
    return site_url()


def brand():
    return (os.environ.get('BILLING_SELLER_NAME') or 'Financial Intel').strip()


def contact_details():
    """One place for the business's contact details (website, emails, invoices)."""
    return {'name': brand(),
            'email': (os.environ.get('CONTACT_EMAIL') or os.environ.get('BILLING_SELLER_EMAIL') or '').strip(),
            'phone': (os.environ.get('CONTACT_PHONE') or '').strip(),
            'whatsapp': re.sub(r'\D', '', os.environ.get('CONTACT_WHATSAPP') or os.environ.get('CONTACT_PHONE') or ''),
            'address': (os.environ.get('CONTACT_ADDRESS') or os.environ.get('BILLING_SELLER_ADDRESS') or 'Nairobi, Kenya').strip(),
            'hours': (os.environ.get('CONTACT_HOURS') or 'Monday to Friday, 8am – 5pm EAT').strip(),
            # What the contact page's map shows: a place name or full street address (defaults to the address).
            'map_query': (os.environ.get('CONTACT_MAP_QUERY') or '').strip()}


# ---------------------------------------------------------------- tokens
def issue(user, purpose):
    """New one-time link for this user and purpose; any earlier unused link for the same purpose stops working."""
    now = datetime.utcnow()
    for t in AuthToken.query.filter_by(user_id=user.id, purpose=purpose, used_at=None).all():
        t.used_at = now
    raw = secrets.token_urlsafe(32)
    db.session.add(AuthToken(user_id=user.id, purpose=purpose, token_hash=_hash(raw), email=user.email,
                             expires_at=now + TTL[purpose], ip=_ip()))
    db.session.commit()
    return raw


def lookup(raw, purposes):
    """-> (token, user, None) when the link is good, else (None, None, reason)."""
    if not raw:
        return None, None, 'missing'
    t = AuthToken.query.filter_by(token_hash=_hash(raw)).first()
    if t is None or t.purpose not in purposes:
        return None, None, 'invalid'
    if t.used_at is not None:
        return None, None, 'used'
    if t.expires_at < datetime.utcnow():
        return None, None, 'expired'
    u = db.session.get(User, t.user_id)
    if u is None or (t.email and t.email != u.email):
        return None, None, 'invalid'          # the account was deleted, or its email changed since
    return t, u, None


def link_for(purpose, raw):
    return f'{_site()}{PATHS[purpose]}?token={raw}'


def recent(purpose, since, user_id=None, ip=None):
    q = AuthToken.query.filter(AuthToken.purpose == purpose, AuthToken.created_at >= since)
    if user_id is not None:
        q = q.filter(AuthToken.user_id == user_id)
    if ip is not None:
        q = q.filter(AuthToken.ip == ip)
    return q.count()


# ---------------------------------------------------------------- emails
def _first(u):
    return (u.name or '').strip().split(' ')[0] or 'there'


def send_verify(user):
    raw = issue(user, 'verify')
    url = link_for('verify', raw)
    b = brand()
    text = (f'Hello {_first(user)},\n\nPlease confirm your email address to finish creating your {b} account:\n{url}\n\n'
            f'The link works for 24 hours. If you did not sign up, ignore this email and no account will be activated.')
    body = (f'<p>Hello {mailer.html.escape(_first(user))},</p><p>Please confirm your email address to finish creating your '
            f'{mailer.html.escape(b)} account.</p>{mailer.button(url, "Confirm my email")}'
            f'<p>The link works for 24 hours. If you did not sign up, ignore this email and no account will be activated.</p>')
    return mailer.send(user.email, f'Confirm your email for {b}', text, body, kind='verify'), url


def send_reset(user):
    raw = issue(user, 'reset')
    url = link_for('reset', raw)
    b = brand()
    text = (f'Hello {_first(user)},\n\nWe received a request to reset the password for your {b} account. '
            f'Choose a new password here:\n{url}\n\nThe link works once and for 1 hour. If you did not ask for this, '
            f'ignore this email: your password stays the same.')
    body = (f'<p>Hello {mailer.html.escape(_first(user))},</p><p>We received a request to reset the password for your '
            f'{mailer.html.escape(b)} account.</p>{mailer.button(url, "Choose a new password")}'
            f'<p>The link works once and for 1 hour. If you did not ask for this, ignore this email: your password stays the same.</p>')
    return mailer.send(user.email, f'Reset your {b} password', text, body, kind='reset'), url


def send_invite(user, inviter=None):
    raw = issue(user, 'invite')
    url = link_for('invite', raw)
    b = brand()
    who = f'{inviter.name} has' if inviter is not None and inviter.name else 'We have'
    text = (f'Hello {_first(user)},\n\n{who} created a {b} account for you ({user.email}). Choose your password to '
            f'start:\n{url}\n\nThe link works for 72 hours.')
    body = (f'<p>Hello {mailer.html.escape(_first(user))},</p><p>{mailer.html.escape(who)} created a {mailer.html.escape(b)} '
            f'account for you (<b>{mailer.html.escape(user.email)}</b>). Choose your password to start.</p>'
            f'{mailer.button(url, "Choose my password")}<p>The link works for 72 hours.</p>')
    return mailer.send(user.email, f'Your {b} account is ready', text, body, kind='invite'), url


def send_password_changed(user):
    b = brand()
    c = contact_details()
    when = datetime.utcnow().strftime('%d %b %Y, %H:%M UTC')
    help_line = f' or write to {c["email"]}' if c['email'] else ''
    text = (f'Hello {_first(user)},\n\nThe password for your {b} account was changed on {when}. Other devices were '
            f'signed out.\n\nIf this was not you, reset your password straight away at {_site()}/forgot-password{help_line}.')
    return mailer.send(user.email, f'Your {b} password was changed', text, kind='password_changed')


# ---------------------------------------------------------------- routes
def register(app, current_user, require_super_admin):
    import billing

    def page(mode, **ctx):
        from web_hardening import site_url
        ctx.setdefault('next_url', '/app')
        ctx.setdefault('plan', '')
        return render_template('site/auth.html', mode=mode, site_url=site_url(), active='', **ctx)

    def sign_in(user):
        session.clear()
        session['user_id'] = user.id
        billing.start_login_session(user)        # a new session token: any other device is signed out

    def _activity(user, action, detail):
        try:
            import activity_log
            from models_survey import ActivityLogEntry
            activity_log.write_entry(db, ActivityLogEntry, lambda: user, None, action, detail=detail)
        except Exception:
            db.session.rollback()

    # ---- email verification ---------------------------------------------------------------------
    @app.route('/verify-email')
    def verify_email_page():
        t, u, why = lookup(request.args.get('token'), ('verify',))
        if why:
            return page('verify_failed', reason=why), 400
        u.email_verified = True
        t.used_at = datetime.utcnow()
        db.session.commit()
        sign_in(u)
        _activity(u, 'email_verified', 'Confirmed their email address')
        return redirect('/app?welcome=1')

    @app.route('/api/verify/resend', methods=['POST'])
    def verify_resend():
        email = ((request.get_json(silent=True) or {}).get('email') or '').strip().lower()
        u = User.query.filter_by(email=email).first() if email else None
        hour = datetime.utcnow() - timedelta(hours=1)
        if (u is not None and not u.email_verified and recent('verify', hour, user_id=u.id) < 3
                and recent('verify', hour, ip=_ip()) < 10):
            send_verify(u)
        return jsonify({'ok': True, 'message': 'If that account is waiting to be confirmed, a new link is on its way. '
                                               'Check your inbox and spam folder.'})

    # ---- forgotten password ----------------------------------------------------------------------
    @app.route('/forgot-password')
    def forgot_page():
        return page('forgot')

    @app.route('/api/password/forgot', methods=['POST'])
    def password_forgot():
        email = ((request.get_json(silent=True) or {}).get('email') or '').strip().lower()
        if not valid_email(email):
            return jsonify({'error': 'Enter the email address you sign in with.'}), 400
        u = User.query.filter_by(email=email).first()
        hour = datetime.utcnow() - timedelta(hours=1)
        if u is not None and recent('reset', hour, user_id=u.id) < 3 and recent('reset', hour, ip=_ip()) < 10:
            send_reset(u)
        return jsonify({'ok': True, 'message': 'If an account uses that email, we have sent a link to reset the password. '
                                               'It works for 1 hour. Check your inbox and spam folder.'})

    @app.route('/reset-password')
    def reset_page():
        raw = request.args.get('token')
        _t, u, why = lookup(raw, ('reset',))
        if why:
            return page('reset_failed', reason=why), 400
        return page('reset', token=raw, account_email=u.email)

    @app.route('/set-password')
    def invite_page():
        raw = request.args.get('token')
        _t, u, why = lookup(raw, ('invite',))
        if why:
            return page('reset_failed', reason=why, invite=True), 400
        return page('invite', token=raw, account_email=u.email, account_name=u.name)

    @app.route('/api/password/reset', methods=['POST'])
    def password_reset():
        d = request.get_json(silent=True) or {}
        t, u, why = lookup(d.get('token'), ('reset', 'invite'))
        if why:
            msg = {'expired': 'This link has expired. Ask for a new one.', 'used': 'This link has already been used. Ask for a new one.'}
            return jsonify({'error': msg.get(why, 'This link is not valid. Ask for a new one.'), 'reason': why}), 400
        pwd = d.get('password') or ''
        if len(pwd) < MIN_PASSWORD:
            return jsonify({'error': f'Use a password of at least {MIN_PASSWORD} characters.'}), 400
        if pwd.lower() in (u.email.lower(), (u.name or '').lower()):
            return jsonify({'error': 'Choose a password that is not your name or email.'}), 400
        now = datetime.utcnow()
        u.set_password(pwd)
        u.email_verified = True                 # they proved they read that inbox
        u.failed_login_count = 0
        u.locked_until = None
        for other in AuthToken.query.filter(AuthToken.user_id == u.id, AuthToken.used_at.is_(None),
                                            AuthToken.purpose.in_(('reset', 'invite', 'verify'))).all():
            other.used_at = now
        t.used_at = now
        db.session.commit()
        sign_in(u)
        if t.purpose == 'reset':
            send_password_changed(u)
        _activity(u, 'password_reset' if t.purpose == 'reset' else 'invite_accepted',
                  'Reset their password by email link' if t.purpose == 'reset' else 'Set their password from an invitation')
        return jsonify({'ok': True, 'next': '/app'})

    # ---- super admin helpers -----------------------------------------------------------------
    def _result(status, url):
        out = {'status': status}
        if status != 'sent':
            out['link'] = url          # email is not set up (or failed): the super admin passes the link on
        return out

    @app.route('/api/users/<int:user_id>/send-reset', methods=['POST'])
    @require_super_admin
    def admin_send_reset(user_id):
        u = User.query.get_or_404(user_id)
        status, url = send_reset(u)
        return jsonify(_result(status, url))

    @app.route('/api/users/<int:user_id>/send-invite', methods=['POST'])
    @require_super_admin
    def admin_send_invite(user_id):
        u = User.query.get_or_404(user_id)
        status, url = send_invite(u, current_user())
        return jsonify(_result(status, url))

    @app.route('/api/users/<int:user_id>/verify-email', methods=['POST'])
    @require_super_admin
    def admin_mark_verified(user_id):
        u = User.query.get_or_404(user_id)
        u.email_verified = True
        db.session.commit()
        return jsonify(u.to_dict())

    @app.route('/api/admin/mail')
    @require_super_admin
    def admin_mail_status():
        c = contact_details()
        q = EmailLog.query.order_by(EmailLog.created_at.desc())
        kind = request.args.get('kind')
        if kind:
            q = q.filter(EmailLog.kind == kind)
        return jsonify({'configured': mailer.configured(), 'sender': mailer.sender(),
                        'host': (os.environ.get('MAIL_HOST') or '').strip(), 'port': os.environ.get('MAIL_PORT') or '587',
                        'verification_required': verification_required(), 'contact': c,
                        'emails': [e.to_dict() for e in q.limit(200).all()],
                        'counts': {s: EmailLog.query.filter_by(status=s).count() for s in ('sent', 'failed', 'not_configured')}})

    @app.route('/api/admin/mail/<int:email_id>')
    @require_super_admin
    def admin_mail_one(email_id):
        return jsonify(EmailLog.query.get_or_404(email_id).to_dict(full=True))

    @app.route('/api/admin/mail/test', methods=['POST'])
    @require_super_admin
    def admin_mail_test():
        to = ((request.get_json(silent=True) or {}).get('to') or '').strip()
        if not valid_email(to):
            return jsonify({'error': 'Enter a valid email address.'}), 400
        b = brand()
        status = mailer.send(to, f'{b}: test email', f'This is a test email from {b}. If you can read it, email is set up correctly.',
                             kind='test')
        last = EmailLog.query.order_by(EmailLog.id.desc()).first()
        return jsonify({'status': status, 'error': last.error if last else None})

    # ---- contact enquiries -------------------------------------------------------------------
    @app.route('/api/contact', methods=['POST'])
    def contact_submit():
        d = request.get_json(silent=True) or {}
        if (d.get('website') or '').strip():            # hidden field that only robots fill in
            return jsonify({'ok': True})
        name = (d.get('name') or '').strip()[:150]
        email = (d.get('email') or '').strip().lower()[:150]
        message = (d.get('message') or '').strip()[:5000]
        if not name or not message:
            return jsonify({'error': 'Please give your name and a message.'}), 400
        if not valid_email(email):
            return jsonify({'error': 'Enter a valid email address so we can reply.'}), 400
        day = datetime.utcnow() - timedelta(hours=1)
        if ContactEnquiry.query.filter(ContactEnquiry.ip == _ip(), ContactEnquiry.created_at >= day).count() >= 5:
            return jsonify({'error': 'Too many messages from this connection. Please try again in an hour, or email us.'}), 429
        e = ContactEnquiry(name=name, email=email, phone=(d.get('phone') or '').strip()[:40] or None,
                           organisation=(d.get('organisation') or '').strip()[:150] or None,
                           topic=(d.get('topic') or '').strip()[:80] or None, message=message, ip=_ip())
        db.session.add(e)
        db.session.commit()
        c = contact_details()
        b = brand()
        if c['email']:
            text = (f'New enquiry from the website\n\nName: {e.name}\nEmail: {e.email}\nPhone: {e.phone or "-"}\n'
                    f'Organisation: {e.organisation or "-"}\nTopic: {e.topic or "-"}\n\n{e.message}\n\n'
                    f'Reply to this email to answer {e.name} directly. All enquiries: {_site()}/app (Settings > Enquiries).')
            mailer.send(c['email'], f'Enquiry: {e.topic or "website"} - {e.name}', text, kind='enquiry', reply_to=e.email,
                        footer='Sent by the Contact page of your website.')
        ack = (f'Hello {e.name.split(" ")[0]},\n\nThank you for contacting {b}. We have received your message about '
               f'"{e.topic or "your enquiry"}" and will reply within one working day.\n\nYour message:\n{e.message}\n\n'
               f'{b}' + (f'\n{c["phone"]}' if c['phone'] else '') + (f'\n{c["email"]}' if c['email'] else ''))
        mailer.send(e.email, f'We have received your message - {b}', ack, kind='enquiry_ack', reply_to=c['email'] or None,
                    footer='You received this because you sent a message through our website.')
        return jsonify({'ok': True, 'message': 'Thank you. Your message has been sent, and a copy is on its way to your inbox. '
                                               'We reply within one working day.'})

    @app.route('/api/admin/enquiries')
    @require_super_admin
    def admin_enquiries():
        q = ContactEnquiry.query.order_by(ContactEnquiry.created_at.desc())
        st = request.args.get('status')
        if st:
            q = q.filter(ContactEnquiry.status == st)
        counts = {s: ContactEnquiry.query.filter_by(status=s).count() for s in ('new', 'replied', 'closed')}
        return jsonify({'enquiries': [e.to_dict() for e in q.limit(300).all()], 'counts': counts})

    @app.route('/api/admin/enquiries/<int:eid>', methods=['PATCH'])
    @require_super_admin
    def admin_enquiry_update(eid):
        e = ContactEnquiry.query.get_or_404(eid)
        d = request.get_json(silent=True) or {}
        if 'status' in d:
            if d['status'] not in ('new', 'replied', 'closed'):
                return jsonify({'error': 'Status must be new, replied or closed.'}), 400
            e.status = d['status']
        if 'note' in d:
            e.note = (d.get('note') or '').strip()[:500] or None
        db.session.commit()
        return jsonify(e.to_dict())

    @app.route('/api/admin/enquiries/count')
    @require_super_admin
    def admin_enquiries_count():
        return jsonify({'new': ContactEnquiry.query.filter_by(status='new').count()})
