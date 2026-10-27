"""Sending email over SMTP (any provider: Zoho, Google Workspace, Microsoft 365, Brevo, your domain host...).

Secrets (Replit > Tools > Secrets):
  MAIL_HOST       e.g. smtp.zoho.com / smtp.gmail.com / smtp-relay.brevo.com
  MAIL_PORT       465 (SSL) or 587 (STARTTLS). Default 587.
  MAIL_USERNAME   the SMTP login (usually the mailbox address)
  MAIL_PASSWORD   the SMTP password or app password
  MAIL_FROM       the sender shown to people, e.g.  Financial Intel <info@yourdomain.co.ke>
                  (defaults to MAIL_USERNAME)

Every send is written to email_log with its outcome. Without MAIL_HOST nothing leaves the server: the message is
logged as 'not_configured' so a super admin can read it (and the link inside) under Settings > Emails.
"""
import html
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

log = logging.getLogger(__name__)


def configured():
    return bool((os.environ.get('MAIL_HOST') or '').strip())


def sender():
    raw = (os.environ.get('MAIL_FROM') or os.environ.get('MAIL_USERNAME') or '').strip()
    name, addr = parseaddr(raw)
    brand = (os.environ.get('BILLING_SELLER_NAME') or 'Financial Intel').strip()
    return formataddr((name or brand, addr)) if addr else ''


def _wrap_html(body_html, footer):
    brand = html.escape((os.environ.get('BILLING_SELLER_NAME') or 'Financial Intel').strip())
    return f"""<!doctype html><html><body style="margin:0;background:#f3f5fa;font-family:Segoe UI,Arial,sans-serif;color:#0f1b33;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f5fa;padding:28px 12px;"><tr><td align="center">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:14px;border:1px solid #e3e8f2;">
<tr><td style="background:#1e3a8a;border-radius:14px 14px 0 0;padding:18px 26px;color:#ffffff;font-size:17px;font-weight:700;">{brand}</td></tr>
<tr><td style="padding:26px;font-size:15px;line-height:1.6;">{body_html}</td></tr>
<tr><td style="padding:0 26px 22px;font-size:12px;color:#768299;line-height:1.5;">{footer}</td></tr>
</table></td></tr></table></body></html>"""


def button(url, label):
    u = html.escape(url, quote=True)
    return (f'<p style="margin:22px 0;"><a href="{u}" style="background:#1e3a8a;color:#ffffff;text-decoration:none;'
            f'padding:12px 22px;border-radius:9px;font-weight:600;display:inline-block;">{html.escape(label)}</a></p>'
            f'<p style="font-size:12px;color:#768299;">If the button does not work, copy this link into your browser:<br>'
            f'<span style="word-break:break-all;">{u}</span></p>')


def send(to, subject, text, body_html=None, kind=None, reply_to=None, footer=None):
    """Send one email. Never raises; returns the email_log row's status ('sent' | 'failed' | 'not_configured')."""
    from models import db
    from models_auth import EmailLog
    footer = footer or 'You received this because of an action on your Financial Intel account.'
    status, error = 'not_configured', None
    if configured():
        try:
            msg = EmailMessage()
            msg['Subject'] = subject
            msg['From'] = sender()
            msg['To'] = to
            if reply_to:
                msg['Reply-To'] = reply_to
            msg['Message-ID'] = make_msgid()
            msg.set_content(text)
            msg.add_alternative(_wrap_html(body_html or '<p>' + html.escape(text).replace('\n', '<br>') + '</p>', footer),
                                subtype='html')
            host = os.environ['MAIL_HOST'].strip()
            port = int(os.environ.get('MAIL_PORT') or 587)
            user = (os.environ.get('MAIL_USERNAME') or '').strip()
            pwd = os.environ.get('MAIL_PASSWORD') or ''
            ctx = ssl.create_default_context()
            if port == 465:
                with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
                    if user:
                        s.login(user, pwd)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=20) as s:
                    s.ehlo()
                    if s.has_extn('starttls'):
                        s.starttls(context=ctx)
                        s.ehlo()
                    if user:
                        s.login(user, pwd)
                    s.send_message(msg)
            status = 'sent'
        except Exception as e:          # wrong password, blocked port, rejected sender...
            status, error = 'failed', str(e)[:500]
            log.warning('Email to %s failed: %s', to, e)
    try:
        db.session.add(EmailLog(to_address=to[:200], subject=subject[:300], body_text=text, kind=kind,
                                status=status, error=error))
        db.session.commit()
    except Exception:
        db.session.rollback()
    return status
