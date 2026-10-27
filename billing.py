"""Billing for Finance Intel: plans, subscriptions, invoices, payments (Paystack + recorded bank/M-Pesa),
access checks, seat limits and one active sign-in per account.

Wired in from app.py with:
    import billing
    billing.register(app, current_user, require_super_admin, activity_log, ActivityLogEntry)
    ...and billing.start_login_session(user) right after a successful sign-in / sign-up.

Who pays: an ORGANISATION (models_billing.py). Member accounts belong to one; the owner subscribes, pays and
adds colleagues up to the plan's seats. Admin / super admin accounts are staff: billing never limits them.

Access (only while BILLING_ENFORCED=true - off by default, so installing this changes nothing until you
switch it on in Secrets):
    full       trial or paid period running, or renewal unpaid but still inside the grace period
    read_only  grace period over: pages load, but no exports and no changes
    none       no plan / cancelled / expired / no free seat: only the public Overview and the Billing page
Status is worked out from the dates every time, so access is right even if no renewal job has run.

Environment (all optional):
    BILLING_ENFORCED=true       turn the access checks on
    TRIAL_DAYS=14               free trial for a new organisation's first self-serve plan (0 = no trial)
    GRACE_DAYS=7                days of full access after a period ends unpaid, before read-only
    RENEWAL_NOTICE_DAYS=7       renewal invoice is raised this many days before the period ends
    VAT_RATE=0.16               VAT added to invoices (default 0 - set it once you are VAT-registered)
    SINGLE_SESSION=true         one active sign-in per member account (a new sign-in signs out the old one)
    SINGLE_SESSION_ADMINS=false apply the same to admin accounts
    PAYSTACK_SECRET_KEY         sk_live_... / sk_test_... - enables "Pay now" (card + M-Pesa) and card auto-renew
    BILLING_SELLER_NAME, BILLING_SELLER_PIN, BILLING_SELLER_ADDRESS, BILLING_SELLER_EMAIL,
    BILLING_BANK_DETAILS, BILLING_MPESA_DETAILS   printed on invoices
Paystack webhook URL to paste in the Paystack dashboard:  https://<your-site>/api/billing/paystack/webhook
"""
import calendar
import hashlib
import hmac
import html
import json
import logging
import math
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from flask import g, jsonify, redirect, request, session
from sqlalchemy.exc import IntegrityError

from models import db, User
from models_billing import Invoice, Organisation, Payment, PaymentEvent, Plan, Subscription

log = logging.getLogger(__name__)

LIVE = ('trialing', 'active', 'past_due', 'read_only')
LEVEL_RANK = {'full': 2, 'read_only': 1, 'none': 0}
PAYMENT_METHODS = ('bank_transfer', 'mpesa', 'cheque', 'paystack', 'other')

# Paths a signed-in member can always reach, whatever their plan: their account, sign-in, and billing itself.
ALWAYS_OPEN = ('/api/me', '/api/profile', '/api/logout', '/api/login', '/api/register', '/api/mfa/',
               '/api/events', '/api/billing/', '/api/home/')


class BillingError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- settings
def _flag(name, default):
    return os.environ.get(name, default).strip().lower() in ('1', 'true', 'yes', 'on')


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def enforced():
    return _flag('BILLING_ENFORCED', 'false')


def trial_days():
    return max(0, _int_env('TRIAL_DAYS', 14))


def grace_days():
    return max(0, _int_env('GRACE_DAYS', 7))


def vat_rate():
    try:
        r = float(os.environ.get('VAT_RATE', '0') or 0)
    except ValueError:
        return 0.0
    return r / 100 if r > 1 else r          # accepts 0.16 or 16


def now():
    return datetime.utcnow()


def add_months(dt, months):
    m = dt.month - 1 + months
    y, m = dt.year + m // 12, m % 12 + 1
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


# ---------------------------------------------------------------- packages: which pages each plan unlocks
# Features are what the app's pages need. Staff (admins) get everything; a member's features come from the tier
# of the plan they are on. While BILLING_ENFORCED is off, every member gets the Team set (the app as it was).
TIERS = ('professional', 'team', 'enterprise')
TIER_LABEL = {'free': 'Free account', 'professional': 'Professional', 'team': 'Team', 'enterprise': 'Enterprise'}
TIER_FEATURES = {
    'free': {'home', 'billing'},
    'professional': {'home', 'billing', 'reports', 'analytics', 'documents'},
    'team': {'home', 'billing', 'reports', 'analytics', 'documents', 'intelligence', 'data_quality'},
    'enterprise': {'home', 'billing', 'reports', 'analytics', 'documents', 'intelligence', 'data_quality'},
}
FEATURE_MIN_TIER = {'reports': 'professional', 'analytics': 'professional', 'documents': 'professional',
                    'intelligence': 'team', 'data_quality': 'team'}
# Data behind a feature. A member whose plan lacks the feature gets a 402 "upgrade" answer from these.
FEATURE_APIS = (
    ('intelligence', ('/api/intelligence-layer/companies',)),
    ('data_quality', ('/api/survey/data-quality-average', '/api/extraction-report/')),
)


def infer_tier(code):
    c = (code or '').lower()
    for t in TIERS:
        if c.startswith(t):
            return t
    return 'professional'


def plan_tier(plan):
    if plan is None:
        return 'free'
    t = (plan.tier or '').strip().lower()
    return t if t in TIERS else infer_tier(plan.code)


def member_tier(user, acc=None):
    """'staff' for admins; otherwise the tier of the plan giving this member access ('free' with none)."""
    if user is None:
        return 'free'
    if user.role == 'admin':
        return 'staff'
    if not enforced():
        return 'team'
    acc = acc or access_for(user)
    if acc['level'] == 'none' or acc.get('sub') is None:
        return 'free'
    return plan_tier(acc['sub'].plan)


def features_for(tier):
    if tier == 'staff':
        return set().union(*TIER_FEATURES.values())
    return set(TIER_FEATURES.get(tier, TIER_FEATURES['free']))


def feature_for_path(path):
    for feature, prefixes in FEATURE_APIS:
        if any(path == p.rstrip('/') or path.startswith(p) for p in prefixes):
            return feature
    return None


# ---------------------------------------------------------------- download kinds
DOWNLOAD_KINDS = {
    'report': ('report_downloads_per_month', 'report downloads (PDF and Word)'),
    'data': ('data_downloads_per_month', 'data downloads (CSV and Excel)'),
    'bulk': ('bulk_downloads_per_month', 'bulk downloads (ZIP and whole-portfolio files)'),
}
_activity = {'log': None, 'entry': None}       # set by register(): activity_log module and ActivityLogEntry model


def download_kind(path):
    """Which allowance a download uses, from its address. Bulk: anything zipped or covering the whole portfolio.
    Data: raw data files a customer could rebuild the dataset from. Everything else (PDF / Word) is a report."""
    p = (path or '').lower().split('?')[0]
    if '.zip' in p or '/bulk' in p or p.startswith(('/api/export/portfolio', '/api/export/comparison')) or 'export-db' in p:
        return 'bulk'
    if any(k in p for k in ('.csv', '.xlsx', '.json', '/snapshot', '/raw')):
        return 'data'
    return 'report'


def download_limit(plan, kind):
    return getattr(plan, DOWNLOAD_KINDS[kind][0]) if plan is not None else None


def downloads_used(org_id, at=None):
    """{kind: count} of downloads by the organisation's users this calendar month."""
    out = {k: 0 for k in DOWNLOAD_KINDS}
    log_mod, Entry = _activity['log'], _activity['entry']
    if log_mod is None or Entry is None or not org_id:
        return out
    start = (at or now()).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    ids = [m.id for m in org_members(org_id)]
    if not ids:
        return out
    for (path,) in db.session.query(Entry.path).filter(Entry.user_id.in_(ids), Entry.category == log_mod.CAT_EXPORT,
                                                       Entry.created_at >= start).all():
        out[download_kind(path)] += 1
    return out


def governs_downloads(user):
    """True when the customer's plan (not the old per-day cap) decides how much this member may download."""
    if not enforced() or user is None or user.role == 'admin' or not user.organization_id:
        return False
    sub, st = best_subscription(user.organization_id)
    return sub is not None and level_for_status(st) != 'none'


# ---------------------------------------------------------------- status and access
def effective_status(sub, at=None):
    """The subscription's status as of `at`, worked out from its dates (the stored status can lag)."""
    at = at or now()
    st = sub.status
    if st in ('pending', 'cancelled', 'expired'):
        return st
    if st == 'trialing':
        if sub.trial_end and at <= sub.trial_end:
            return 'trialing'
        return 'expired'
    end = sub.current_period_end
    if end is None:
        return 'expired'
    if at <= end:
        return 'active'
    if sub.cancel_at_period_end:
        return 'cancelled'
    if at <= end + timedelta(days=grace_days()):
        return 'past_due'
    return 'read_only'


def level_for_status(status):
    if status in ('trialing', 'active', 'past_due'):
        return 'full'
    if status == 'read_only':
        return 'read_only'
    return 'none'


def org_subscriptions(org_id):
    return Subscription.query.filter_by(organisation_id=org_id).order_by(Subscription.id.desc()).all()


def best_subscription(org_id, at=None):
    """(subscription giving the most access, its effective status). Falls back to the newest one."""
    subs = org_subscriptions(org_id)
    best, best_st = None, None
    for s in subs:
        st = effective_status(s, at)
        if best is None or LEVEL_RANK[level_for_status(st)] > LEVEL_RANK[level_for_status(best_st)]:
            best, best_st = s, st
    return best, best_st


def org_members(org_id):
    users = User.query.filter_by(organization_id=org_id).all()
    return sorted(users, key=lambda u: (0 if u.org_role == 'owner' else 1, u.id))


def open_invoice(org_id):
    return Invoice.query.filter_by(organisation_id=org_id, status='open').order_by(Invoice.due_at.asc(), Invoice.id.asc()).first()


def access_for(user, at=None):
    """-> {'level', 'reason', 'banner', 'status', 'org', 'sub'} for a member account."""
    at = at or now()
    out = {'level': 'full', 'reason': None, 'banner': None, 'status': None, 'org': None, 'sub': None}
    if user is None:
        return dict(out, level='none', reason='not_signed_in')
    if user.role == 'admin':
        return dict(out, reason='staff')
    org = db.session.get(Organisation, user.organization_id) if user.organization_id else None
    out['org'] = org
    if org is None:
        return dict(out, level='none', reason='no_plan',
                    banner='Choose a plan to unlock the survey data, reports and exports.')
    sub, st = best_subscription(org.id, at)
    out['sub'], out['status'] = sub, st
    inv = open_invoice(org.id)
    inv_txt = f' Invoice {inv.number} is open.' if inv else ''
    if sub is None or level_for_status(st) == 'none':
        msg = {'pending': 'Your plan starts as soon as the first invoice is paid.' + inv_txt,
               'expired': 'Your free trial or plan has ended. Pay the open invoice or choose a plan to continue.',
               'cancelled': 'Your plan was cancelled. Choose a plan to continue.'}.get(st, 'Your organisation has no active plan.')
        return dict(out, level='none', reason=st or 'no_plan', banner=msg)
    members = org_members(org.id)
    seat_index = next((i for i, m in enumerate(members) if m.id == user.id), 0)
    if seat_index >= sub.seats():
        return dict(out, level='none', reason='no_seat',
                    banner=f'Your organisation\'s plan has {sub.seats()} user(s) and they are all taken. '
                           'Ask your account owner to free a seat or upgrade.')
    level = level_for_status(st)
    banner = None
    if st == 'trialing':
        days = max(0, math.ceil((sub.trial_end - at).total_seconds() / 86400))
        banner = f'Free trial: {days} day{"" if days == 1 else "s"} left.{inv_txt} Pay it to keep access when the trial ends.'
    elif st == 'past_due':
        until = sub.current_period_end + timedelta(days=grace_days())
        banner = f'Your renewal is overdue. Full access continues until {until:%d %b %Y}.{inv_txt}'
    elif st == 'read_only':
        banner = f'Your plan has lapsed: you can view data but not export or change it.{inv_txt} Pay it to restore full access.'
    elif st == 'active' and sub.cancel_at_period_end:
        banner = f'Your plan is cancelled and ends on {sub.current_period_end:%d %b %Y}.'
    return dict(out, level=level, reason=st, banner=banner)


# ---------------------------------------------------------------- invoices and payments
def _next_number(at):
    prefix = f'FI-{at:%Y}-'
    last = (Invoice.query.filter(Invoice.number.like(prefix + '%')).order_by(Invoice.number.desc()).first())
    seq = int(last.number.rsplit('-', 1)[1]) + 1 if last else 1
    return f'{prefix}{seq:04d}'


def create_invoice(org, description, amount, currency='KES', kind='custom', sub=None, plan=None,
                   period_start=None, period_end=None, due_at=None, rate=None, user_id=None):
    """Create and commit an invoice. Returns None if this subscription period is already invoiced."""
    amount = int(round(amount))
    if amount < 0:
        raise BillingError('Amount cannot be negative.')
    rate = vat_rate() if rate is None else float(rate)
    rate = rate / 100 if rate > 1 else rate
    vat = int(round(amount * rate))
    at = now()
    for _attempt in range(5):
        inv = Invoice(organisation_id=org.id, subscription_id=sub.id if sub else None, plan_id=plan.id if plan else None,
                      kind=kind, description=description[:300], period_start=period_start, period_end=period_end,
                      amount=amount, vat_rate=rate, vat_amount=vat, total=amount + vat, currency=currency,
                      status='open', issued_at=at, due_at=due_at or at + timedelta(days=_int_env('INVOICE_DUE_DAYS', 14)),
                      number=_next_number(at), created_by_user_id=user_id)
        db.session.add(inv)
        try:
            db.session.commit()
            return inv
        except IntegrityError:
            db.session.rollback()
            if sub is not None and Invoice.query.filter_by(subscription_id=sub.id, period_start=period_start).first():
                return None                     # another worker raised this period's invoice first
    raise BillingError('Could not number the invoice - please try again.', 500)


def apply_payment(inv, amount, method, reference=None, user_id=None, note=None, paid_at=None):
    """Record money against an invoice; a fully paid subscription invoice starts/extends that period."""
    if inv.status == 'void':
        raise BillingError('That invoice was voided.')
    if method not in PAYMENT_METHODS:
        raise BillingError('Unknown payment method.')
    amount = int(round(amount))
    if amount <= 0:
        raise BillingError('Payment amount must be more than zero.')
    pay = Payment(invoice_id=inv.id, organisation_id=inv.organisation_id, amount=amount, currency=inv.currency,
                  method=method, reference=(reference or '')[:120] or None, paid_at=paid_at or now(),
                  recorded_by_user_id=user_id, note=(note or '')[:300] or None)
    db.session.add(pay)
    db.session.flush()
    if inv.status == 'open' and inv.amount_paid() >= inv.total:
        inv.status = 'paid'
        inv.paid_at = pay.paid_at
        if inv.subscription_id:
            _activate_period(db.session.get(Subscription, inv.subscription_id), inv)
    db.session.commit()
    return pay


def _activate_period(sub, inv):
    if sub is None:
        return
    if inv.plan_id:
        sub.plan_id = inv.plan_id
    start = inv.period_start or now()
    end = inv.period_end or add_months(start, sub.plan.interval_months if sub.plan else 12)
    # Paying a trial invoice early keeps the trial days: the paid period starts when the trial ends.
    if sub.current_period_end is None or end > sub.current_period_end:
        sub.current_period_start, sub.current_period_end = start, end
    sub.status = 'active'
    sub.cancel_at_period_end = False
    # A newly paid plan replaces whatever plan the organisation had before (an upgrade or downgrade).
    for other in org_subscriptions(sub.organisation_id):
        if other.id != sub.id and other.status in LIVE + ('pending',):
            other.status = 'cancelled'


def start_subscription(org, plan, user_id=None, mode='auto'):
    """Start `plan` for `org`. mode: 'auto' (trial if eligible, else pay first), 'trial', 'invoice' (pay first),
    'complimentary' (active now, no invoice - pilots and partners). Returns (subscription, invoice or None)."""
    at = now()
    live = [s for s in org_subscriptions(org.id) if level_for_status(effective_status(s, at)) != 'none']
    if mode == 'auto':
        mode = 'trial' if trial_days() and not org.trial_used and not live else 'invoice'
    # Unpaid sign-ups for another plan are dropped, so only one "waiting for payment" plan exists at a time.
    for s in org_subscriptions(org.id):
        if s.status == 'pending':
            s.status = 'cancelled'
            for inv in Invoice.query.filter_by(subscription_id=s.id, status='open').all():
                inv.status = 'void'
    sub = Subscription(organisation_id=org.id, plan_id=plan.id)
    if mode == 'trial':
        sub.status = 'trialing'
        sub.trial_end = at + timedelta(days=trial_days() or 14)
        org.trial_used = True
        start = sub.trial_end
    elif mode == 'complimentary':
        sub.status = 'active'
        start = at
        sub.current_period_start, sub.current_period_end = at, add_months(at, plan.interval_months)
    else:
        sub.status = 'pending'
        start = at
    db.session.add(sub)
    db.session.commit()
    if mode == 'complimentary':
        for other in org_subscriptions(org.id):
            if other.id != sub.id and other.status in LIVE:
                other.status = 'cancelled'
        db.session.commit()
        return sub, None
    end = add_months(start, plan.interval_months)
    inv = create_invoice(org, f'{plan.name} ({start:%d %b %Y} - {end:%d %b %Y})', plan.price, plan.currency,
                         kind='subscription', sub=sub, plan=plan, period_start=start, period_end=end,
                         due_at=start if mode == 'trial' else at, user_id=user_id)
    return sub, inv


# ---------------------------------------------------------------- renewals (the "sweep")
def run_sweep(at=None):
    """Raise renewal invoices before periods end, try saved cards, and store each subscription's status.
    Safe to run any number of times, from any number of app workers."""
    at = at or now()
    notice = timedelta(days=max(0, _int_env('RENEWAL_NOTICE_DAYS', 7)))
    raised = charged = 0
    for sub in Subscription.query.filter(Subscription.status.in_(LIVE)).all():
        st = effective_status(sub, at)
        if st != sub.status:
            sub.status = st
        if st in ('active', 'past_due', 'read_only') and not sub.cancel_at_period_end and sub.current_period_end \
                and sub.current_period_end - at <= notice and sub.plan and sub.plan.active:
            start = sub.current_period_end
            if Invoice.query.filter_by(subscription_id=sub.id, period_start=start).first() is None:
                end = add_months(start, sub.plan.interval_months)
                inv = create_invoice(sub.organisation, f'{sub.plan.name} renewal ({start:%d %b %Y} - {end:%d %b %Y})',
                                     sub.plan.price, sub.plan.currency, kind='renewal', sub=sub, plan=sub.plan,
                                     period_start=start, period_end=end, due_at=start)
                if inv is not None:
                    raised += 1
                    if sub.paystack_authorization_code and paystack_ready():
                        try:
                            if charge_saved_card(inv, sub):
                                charged += 1
                        except Exception as e:          # a declined card leaves the invoice open for "Pay now"
                            log.warning('billing: auto-renew charge failed for invoice %s: %s', inv.number, e)
    db.session.commit()
    return {'renewal_invoices': raised, 'cards_charged': charged}


_sweep_lock = threading.Lock()
_last_sweep = [0.0]


def _maybe_sweep(app):
    """Run the sweep in the background at most once an hour per worker (no cron needed)."""
    if app.testing or time.time() - _last_sweep[0] < _int_env('BILLING_SWEEP_SECONDS', 3600) \
            or not _sweep_lock.acquire(blocking=False):
        return
    _last_sweep[0] = time.time()

    def work():
        try:
            with app.app_context():
                run_sweep()
        except Exception:
            log.exception('billing: renewal sweep failed')
        finally:
            _sweep_lock.release()
    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- Paystack
PAYSTACK_API = 'https://api.paystack.co'


def paystack_ready():
    return bool(os.environ.get('PAYSTACK_SECRET_KEY'))


def _paystack_http(method, path, body=None):
    key = os.environ.get('PAYSTACK_SECRET_KEY')
    if not key:
        raise BillingError('Online payment is not set up yet (PAYSTACK_SECRET_KEY is missing). '
                           'Pay by bank transfer or M-Pesa using the details on the invoice.', 503)
    req = urllib.request.Request(PAYSTACK_API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get('message')
        except Exception:
            msg = None
        raise BillingError(f'Paystack refused the request: {msg or e.reason}', 502)
    except urllib.error.URLError as e:
        raise BillingError(f'Could not reach Paystack: {e.reason}', 502)


# Tests replace this with a fake; everything that talks to Paystack goes through it.
paystack_http = _paystack_http


def _billing_email(org):
    if org.billing_email:
        return org.billing_email
    owner = db.session.get(User, org.owner_user_id) if org.owner_user_id else None
    return owner.email if owner else None


def start_online_payment(inv, callback_url):
    """Ask Paystack for a checkout page for what is still owed on `inv`. -> authorization_url"""
    if inv.status != 'open':
        raise BillingError('That invoice is not open for payment.')
    owed = inv.total - inv.amount_paid()
    if owed <= 0:
        raise BillingError('Nothing is owed on that invoice.')
    email = _billing_email(inv.organisation)
    if not email:
        raise BillingError('Add a billing email for your organisation first.')
    ref = f'FI{inv.id}-{secrets.token_hex(5)}'
    res = paystack_http('POST', '/transaction/initialize', {
        'email': email, 'amount': owed * 100, 'currency': inv.currency, 'reference': ref,
        'callback_url': callback_url,
        'metadata': {'invoice_id': inv.id, 'organisation_id': inv.organisation_id, 'invoice_number': inv.number},
    })
    url = (res.get('data') or {}).get('authorization_url')
    if not res.get('status') or not url:
        raise BillingError(res.get('message') or 'Paystack did not return a payment page.', 502)
    return url


def _meta(data):
    m = data.get('metadata') or {}
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            m = {}
    return m if isinstance(m, dict) else {}


def process_paystack_charge(data, event_type='charge.success'):
    """Apply a successful Paystack charge (from the webhook, the return page or an auto-renew).
    Returns the Payment, or None if this reference was already applied or the charge did not succeed."""
    if (data or {}).get('status') != 'success':
        return None
    ref = data.get('reference')
    inv_id = str(_meta(data).get('invoice_id') or '')
    inv = db.session.get(Invoice, int(inv_id)) if inv_id.isdigit() else None
    if not ref or inv is None:
        log.warning('billing: Paystack charge %s has no matching invoice', ref)
        return None
    if (data.get('currency') or inv.currency) != inv.currency:
        log.warning('billing: Paystack charge %s currency %s does not match invoice %s', ref, data.get('currency'), inv.number)
        return None
    db.session.add(PaymentEvent(provider='paystack', reference=ref, event_type=event_type,
                                payload=json.dumps(data)[:20000]))
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return None                      # already applied (webhook and return page both arrive)
    auth = data.get('authorization') or {}
    sub = db.session.get(Subscription, inv.subscription_id) if inv.subscription_id else None
    if sub is not None and auth.get('reusable') and auth.get('authorization_code') and auth.get('channel') == 'card':
        sub.paystack_authorization_code = auth['authorization_code']
        sub.card_label = f"{(auth.get('card_type') or 'Card').strip().title()} ending {auth.get('last4') or '----'}"[:80]
        sub.paystack_customer_code = ((data.get('customer') or {}).get('customer_code') or None)
    channel = data.get('channel') or auth.get('channel') or 'online'
    amount = int(data.get('amount') or 0) // 100
    if inv.status != 'open':
        # Money arrived for an invoice that was already paid or voided: keep the record for a person to sort out.
        pay = Payment(invoice_id=inv.id, organisation_id=inv.organisation_id, amount=amount, currency=inv.currency,
                      method='paystack', reference=ref, paid_at=now(),
                      note=f'Paystack {channel} - received on a {inv.status} invoice, check and refund or reallocate')
        db.session.add(pay)
        db.session.commit()
        log.warning('billing: Paystack charge %s arrived for %s invoice %s', ref, inv.status, inv.number)
        return pay
    return apply_payment(inv, amount, 'paystack', ref, note=f'Paystack {channel}')


def verify_reference(ref):
    res = paystack_http('GET', f'/transaction/verify/{ref}')
    return process_paystack_charge(res.get('data') or {}, 'verify')


def charge_saved_card(inv, sub):
    owed = inv.total - inv.amount_paid()
    if owed <= 0 or not sub.paystack_authorization_code:
        return None
    res = paystack_http('POST', '/transaction/charge_authorization', {
        'authorization_code': sub.paystack_authorization_code, 'email': _billing_email(inv.organisation),
        'amount': owed * 100, 'currency': inv.currency, 'reference': f'FI{inv.id}-auto-{secrets.token_hex(4)}',
        'metadata': {'invoice_id': inv.id, 'organisation_id': inv.organisation_id, 'invoice_number': inv.number},
    })
    return process_paystack_charge(res.get('data') or {}, 'auto_renew')


def valid_webhook_signature(raw_body, signature):
    key = os.environ.get('PAYSTACK_SECRET_KEY') or ''
    if not key or not signature:
        return False
    expected = hmac.new(key.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------- one active sign-in per account
def single_session_applies(user):
    if user is None or not _flag('SINGLE_SESSION', 'true'):
        return False
    return user.role != 'admin' or _flag('SINGLE_SESSION_ADMINS', 'false')


def start_login_session(user):
    """Call right after session['user_id'] is set on sign-in / sign-up: this device becomes the account's
    only active sign-in, and any other device is signed out on its next request."""
    token = secrets.token_hex(16)
    user.session_token = token
    db.session.commit()
    session['sid'] = token
    import session_policy
    session_policy.start()            # sign-in clock: ends when the person leaves the app or after the maximum hours


# ---------------------------------------------------------------- invoice page
def _money(v, cur):
    return f'{cur} {v:,.0f}'


def invoice_html(inv):
    org = inv.organisation
    e = lambda s: html.escape(str(s or ''))
    seller = os.environ.get('BILLING_SELLER_NAME', 'Financial Intel')
    seller_lines = [os.environ.get('BILLING_SELLER_ADDRESS', ''), os.environ.get('BILLING_SELLER_EMAIL', ''),
                    ('KRA PIN: ' + os.environ['BILLING_SELLER_PIN']) if os.environ.get('BILLING_SELLER_PIN') else '']
    pays = Payment.query.filter_by(invoice_id=inv.id).order_by(Payment.paid_at).all()
    paid = sum(p.amount for p in pays)
    bank = os.environ.get('BILLING_BANK_DETAILS', '')
    mpesa = os.environ.get('BILLING_MPESA_DETAILS', '')
    how = ''.join(f'<p><b>{t}</b><br>{e(v).replace(chr(10), "<br>")}</p>' for t, v in
                  (('Bank transfer', bank), ('M-Pesa', mpesa)) if v)
    status = {'paid': 'PAID', 'void': 'VOID'}.get(inv.status, 'DUE')
    pay_rows = ''.join(f'<tr><td>{p.paid_at:%d %b %Y}</td><td>{e(p.method.replace("_", " ").title())}</td>'
                       f'<td>{e(p.reference)}</td><td class="r">{_money(p.amount, p.currency)}</td></tr>' for p in pays)
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Invoice {e(inv.number)}</title><style>
body{{font-family:Helvetica,Arial,sans-serif;color:#1b2333;margin:0;background:#f3f5f9}}
.page{{max-width:820px;margin:24px auto;background:#fff;padding:40px;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.top{{display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap}}
.logo{{display:flex;align-items:center;gap:10px;font-weight:700;font-size:18px;color:#13213d}}
.logo i{{display:inline-flex;align-items:flex-end;gap:3px;background:#13213d;border-radius:6px;padding:6px 7px;height:20px}}
.logo i b{{display:block;width:4px;background:#3b82f6;border-radius:1px}}
h1{{font-size:26px;margin:0 0 4px;color:#13213d}} .muted{{color:#667085;font-size:13px;line-height:1.5}}
.badge{{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:700;
background:{'#dcfce7' if status == 'PAID' else '#fee2e2' if status == 'VOID' else '#fef3c7'};
color:{'#166534' if status == 'PAID' else '#991b1b' if status == 'VOID' else '#92400e'}}}
table{{width:100%;border-collapse:collapse;margin-top:24px;font-size:14px}}
th{{text-align:left;background:#f1f4f9;padding:10px;font-size:12px;text-transform:uppercase;color:#475467}}
td{{padding:10px;border-bottom:1px solid #eaecf0}} .r{{text-align:right}} .tot td{{font-weight:700;border:none}}
.cols{{display:flex;gap:32px;margin-top:28px;flex-wrap:wrap}} .cols>div{{flex:1;min-width:220px}}
.bar{{max-width:820px;margin:16px auto 0;text-align:right}} .bar button{{padding:8px 16px;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer}}
@media print{{body{{background:#fff}}.page{{box-shadow:none;margin:0}}.bar{{display:none}}}}
</style></head><body>
<div class="bar"><button onclick="window.print()">Print / save as PDF</button></div>
<div class="page">
<div class="top"><div><div class="logo"><i><b style="height:8px"></b><b style="height:14px"></b><b style="height:20px"></b></i>{e(seller)}</div>
<div class="muted" style="margin-top:8px">{'<br>'.join(e(x) for x in seller_lines if x)}</div></div>
<div style="text-align:right"><h1>Invoice</h1><div class="muted">{e(inv.number)}</div><div style="margin-top:6px"><span class="badge">{status}</span></div></div></div>
<div class="cols"><div><div class="muted">Billed to</div><b>{e(org.name)}</b><div class="muted">
{'<br>'.join(e(x) for x in (org.address, org.billing_email, org.phone, ('KRA PIN: ' + org.kra_pin) if org.kra_pin else '') if x)}</div></div>
<div><div class="muted">Issued: {inv.issued_at:%d %b %Y}<br>Due: {inv.due_at:%d %b %Y}
{('<br>Paid: ' + f'{inv.paid_at:%d %b %Y}') if inv.paid_at else ''}{('<br>eTIMS no.: ' + e(inv.etims_number)) if inv.etims_number else ''}</div></div></div>
<table><tr><th>Description</th><th class="r">Amount</th></tr>
<tr><td>{e(inv.description)}</td><td class="r">{_money(inv.amount, inv.currency)}</td></tr>
<tr class="tot"><td class="r">Subtotal</td><td class="r">{_money(inv.amount, inv.currency)}</td></tr>
<tr class="tot"><td class="r">VAT ({(inv.vat_rate or 0) * 100:.0f}%)</td><td class="r">{_money(inv.vat_amount or 0, inv.currency)}</td></tr>
<tr class="tot"><td class="r">Total</td><td class="r">{_money(inv.total, inv.currency)}</td></tr>
<tr class="tot"><td class="r">Paid</td><td class="r">{_money(paid, inv.currency)}</td></tr>
<tr class="tot"><td class="r">Balance due</td><td class="r">{_money(max(0, inv.total - paid) if inv.status != 'void' else 0, inv.currency)}</td></tr></table>
{('<table><tr><th>Payment date</th><th>Method</th><th>Reference</th><th class="r">Amount</th></tr>' + pay_rows + '</table>') if pay_rows else ''}
{('<div class="cols"><div><div class="muted" style="margin-bottom:6px">How to pay - quote ' + e(inv.number) + ' as the reference</div>' + how + '</div></div>') if how and inv.status == 'open' else ''}
</div></body></html>"""


# ---------------------------------------------------------------- routes
def register(app, current_user, require_super_admin, activity_log=None, ActivityLogEntry=None):
    _activity['log'], _activity['entry'] = activity_log, ActivityLogEntry

    def _json():
        return request.get_json(silent=True) or {}

    def _err(e):
        return jsonify({'error': str(e)}), getattr(e, 'status', 400)

    def _me_or_401():
        u = current_user()
        if u is None:
            raise BillingError('Please sign in first.', 401)
        return u

    def _my_org(u, owner=False):
        org = db.session.get(Organisation, u.organization_id) if u.organization_id else None
        if org is None:
            raise BillingError('Your account is not part of a subscribed organisation yet.', 404)
        if owner and u.org_role != 'owner':
            raise BillingError('Only your organisation\'s account owner can do that.', 403)
        return org

    def _plan(code, staff=False):
        p = Plan.query.filter_by(code=(code or '').strip()).first()
        if p is None or (not p.active and not staff):
            raise BillingError('That plan is not available.', 404)
        if not staff and not p.self_serve:
            raise BillingError('That plan is arranged directly with us - please get in touch for a quote.', 400)
        return p

    def _org_summary(org, at=None):
        sub, st = best_subscription(org.id, at)
        members = org_members(org.id)
        invs = Invoice.query.filter_by(organisation_id=org.id).order_by(Invoice.id.desc()).all()
        owed = sum(max(0, i.total - i.amount_paid()) for i in invs if i.status == 'open')
        return {'organisation': org.to_dict(), 'subscription': sub.to_dict() if sub else None,
                'status': st, 'level': level_for_status(st) if sub else 'none',
                'seats_used': len(members), 'seats': sub.seats() if sub else 0,
                'members': [{'id': m.id, 'name': m.name, 'email': m.email, 'org_role': m.org_role} for m in members],
                'invoices': [i.to_dict() for i in invs], 'balance_due': owed}

    def _callback_url():
        site = (os.environ.get('SITE_URL') or '').strip().rstrip('/') or request.host_url.rstrip('/')
        return site + '/billing/paystack/callback'

    # ---- one active sign-in: runs before every other check -------------------------------------
    def _single_session_check():
        g.session_replaced = False
        uid = session.get('user_id')
        if not uid:
            return None
        u = db.session.get(User, uid)
        if u is None or not single_session_applies(u) or not u.session_token:
            return None
        if session.get('sid') != u.session_token:
            session.clear()
            g.session_replaced = True
        return None
    app.before_request_funcs.setdefault(None, []).insert(0, _single_session_check)

    @app.after_request
    def _flag_session_replaced(resp):
        if getattr(g, 'session_replaced', False):
            resp.headers['X-Session-Ended'] = 'replaced'
        return resp

    # ---- the plan gate ---------------------------------------------------------------------------
    @app.before_request
    def _billing_gate():
        if request.path.startswith('/api/') or request.path == '/app':
            _maybe_sweep(app)
        if not enforced() or not request.path.startswith('/api/'):
            return None
        path = request.path
        if any(path == p.rstrip('/') or path.startswith(p.rstrip('/') + '/') for p in ALWAYS_OPEN):
            return None
        u = current_user()
        if u is None or u.role == 'admin':
            return None                      # guests are handled by access_control; staff are never billed
        acc = access_for(u)
        g.billing_access = acc
        is_read = request.method in ('GET', 'HEAD')
        is_export = False
        if activity_log is not None and request.url_rule is not None:
            _, cat, _ = activity_log.classify_request(request.method, request.url_rule.rule)
            is_export = cat == activity_log.CAT_EXPORT
        feature = feature_for_path(path)
        if feature and acc['level'] != 'none' and feature not in features_for(member_tier(u, acc)):
            need = TIER_LABEL[FEATURE_MIN_TIER.get(feature, 'team')]
            return jsonify({'error': f'This is part of the {need} plan. Upgrade on the Billing & Plan page to open it.',
                            'billing': True, 'reason': 'upgrade', 'feature': feature, 'needs': need}), 402
        if acc['level'] == 'full':
            return _export_quota(u, acc) if is_export else None
        if acc['level'] == 'read_only' and is_read and not is_export:
            return None
        if acc['level'] == 'none' and is_read and not is_export:
            import access_control
            if path in access_control.GUEST_GET_ALLOWED:
                return None
        return jsonify({'error': acc['banner'] or 'Your plan does not include this.', 'billing': True,
                        'reason': acc['reason']}), 402

    def _export_quota(u, acc):
        sub = acc.get('sub')
        plan = sub.plan if sub is not None else None
        kind = download_kind(request.path)
        limit = download_limit(plan, kind)
        what = DOWNLOAD_KINDS[kind][1]
        if limit is None:
            return None
        if limit <= 0:
            return jsonify({'error': f'Your plan does not include {what}. Upgrade your plan to download this file.',
                            'billing': True, 'reason': 'not_in_plan', 'kind': kind}), 402
        used = downloads_used(u.organization_id)[kind]
        if used >= limit:
            return jsonify({'error': f'Your plan includes {limit} {what} a month and they have all been used. '
                                     'The allowance resets on the 1st, or upgrade for more.',
                            'billing': True, 'reason': 'export_limit', 'kind': kind}), 429
        return None

    # ---- customer side ---------------------------------------------------------------------------
    @app.route('/api/billing/plans')
    def billing_plans():
        plans = Plan.query.filter_by(active=True).order_by(Plan.sort_order, Plan.id).all()
        return jsonify({'plans': [p.to_dict() for p in plans], 'trial_days': trial_days(),
                        'vat_rate': vat_rate(), 'online_payment': paystack_ready(),
                        'contact_email': os.environ.get('BILLING_SELLER_EMAIL') or None})

    @app.route('/api/billing/me')
    def billing_me():
        u = current_user()
        if u is None:
            return jsonify({'signed_in': False, 'enforced': enforced()})
        acc = access_for(u)
        tier = member_tier(u, acc)
        out = {'signed_in': True, 'enforced': enforced(), 'staff': u.role == 'admin', 'org_role': u.org_role,
               'tier': tier, 'tier_label': TIER_LABEL.get(tier, 'Staff'), 'features': sorted(features_for(tier)),
               'feature_min_tier': {k: TIER_LABEL[v] for k, v in FEATURE_MIN_TIER.items()},
               'level': acc['level'] if enforced() or u.role == 'admin' else 'full',
               'reason': acc['reason'], 'banner': acc['banner'] if enforced() else None,
               'online_payment': paystack_ready(), 'trial_days': trial_days(), 'grace_days': grace_days()}
        if acc['org'] is not None:
            out.update(_org_summary(acc['org']))
            plan = acc['sub'].plan if acc['sub'] is not None else None
            used = downloads_used(acc['org'].id)
            out['downloads'] = {k: {'used': used[k], 'limit': download_limit(plan, k)} for k in DOWNLOAD_KINDS}
        return jsonify(out)

    @app.route('/api/billing/subscribe', methods=['POST'])
    def billing_subscribe():
        try:
            u = _me_or_401()
            if u.role == 'admin':
                raise BillingError('Staff accounts are not billed. Set customers up from the Billing page.')
            d = _json()
            plan = _plan(d.get('plan_code'))
            info = d.get('organisation') or {}
            if u.organization_id:
                org = _my_org(u, owner=True)
            else:
                name = (info.get('name') or u.organization or '').strip()
                if not name:
                    raise BillingError('Enter your organisation\'s name.')
                org = Organisation(name=name[:150], owner_user_id=u.id, country=(info.get('country') or 'Kenya')[:60])
                db.session.add(org)
                db.session.flush()
                u.organization_id, u.org_role = org.id, 'owner'
            for f, n in (('billing_email', 150), ('phone', 40), ('kra_pin', 30), ('address', 300)):
                if (info.get(f) or '').strip():
                    setattr(org, f, info[f].strip()[:n])
            if not org.billing_email:
                org.billing_email = u.email
            db.session.commit()
            sub, st = best_subscription(org.id)
            if sub is not None and sub.plan_id == plan.id and level_for_status(st) == 'full':
                raise BillingError('You are already on that plan.', 409)
            sub, inv = start_subscription(org, plan, user_id=u.id)
            return jsonify({'ok': True, 'subscription': sub.to_dict(), 'invoice': inv.to_dict() if inv else None,
                            'trial': sub.status == 'trialing'}), 201
        except BillingError as e:
            db.session.rollback()
            return _err(e)

    @app.route('/api/billing/invoices/<int:invoice_id>/pay', methods=['POST'])
    def billing_pay(invoice_id):
        try:
            u = _me_or_401()
            inv = db.session.get(Invoice, invoice_id)
            if inv is None or (u.role != 'admin' and inv.organisation_id != u.organization_id):
                raise BillingError('Invoice not found.', 404)
            return jsonify({'authorization_url': start_online_payment(inv, _callback_url())})
        except BillingError as e:
            return _err(e)

    @app.route('/billing/paystack/callback')
    def billing_paystack_return():
        ref = (request.args.get('reference') or request.args.get('trxref') or '').strip()
        result = 'failed'
        if ref:
            try:
                verify_reference(ref)
                # Either applied now or already applied by the webhook - check which way it went.
                inv_id = ref[2:].split('-', 1)[0] if ref.startswith('FI') else ''
                inv = db.session.get(Invoice, int(inv_id)) if inv_id.isdigit() else None
                result = 'paid' if inv is not None and inv.status == 'paid' else 'pending'
            except BillingError as e:
                db.session.rollback()
                log.warning('billing: verifying %s failed: %s', ref, e)
        return redirect(f'/app?billing={result}')

    @app.route('/api/billing/paystack/webhook', methods=['POST'])
    def billing_paystack_webhook():
        raw = request.get_data()
        if not valid_webhook_signature(raw, request.headers.get('x-paystack-signature')):
            return jsonify({'error': 'Bad signature.'}), 401
        try:
            evt = json.loads(raw.decode() or '{}')
        except ValueError:
            return jsonify({'error': 'Bad payload.'}), 400
        if evt.get('event') == 'charge.success':
            try:
                process_paystack_charge(evt.get('data') or {}, 'charge.success')
            except BillingError as e:
                db.session.rollback()
                log.warning('billing: webhook payment not applied: %s', e)
        return jsonify({'ok': True})

    @app.route('/api/billing/cancel', methods=['POST'])
    def billing_cancel():
        try:
            u = _me_or_401()
            org = _my_org(u, owner=True)
            sub, st = best_subscription(org.id)
            if sub is None or level_for_status(st) == 'none':
                raise BillingError('There is no running plan to cancel.')
            undo = bool(_json().get('undo'))
            if sub.status == 'trialing' and not undo:
                sub.status = 'cancelled'
                for inv in Invoice.query.filter_by(subscription_id=sub.id, status='open').all():
                    inv.status = 'void'
            else:
                sub.cancel_at_period_end = not undo
            db.session.commit()
            return jsonify({'ok': True, 'subscription': sub.to_dict()})
        except BillingError as e:
            return _err(e)

    @app.route('/api/billing/organisation', methods=['PATCH'])
    def billing_org_update():
        try:
            org = _my_org(_me_or_401(), owner=True)
            d = _json()
            for f, n in (('name', 150), ('billing_email', 150), ('phone', 40), ('kra_pin', 30), ('address', 300)):
                if f in d:
                    v = (d.get(f) or '').strip()[:n]
                    if f == 'name' and not v:
                        raise BillingError('The organisation needs a name.')
                    setattr(org, f, v or None)
            db.session.commit()
            return jsonify(org.to_dict())
        except BillingError as e:
            return _err(e)

    @app.route('/api/billing/organisation/members', methods=['POST'])
    def billing_add_member():
        """Owner adds a colleague: creates their account inside the organisation, if a seat is free."""
        try:
            u = _me_or_401()
            org = _my_org(u, owner=True)
            sub, st = best_subscription(org.id)
            if sub is None or level_for_status(st) == 'none':
                raise BillingError('Start a plan before adding colleagues.')
            if len(org_members(org.id)) >= sub.seats():
                raise BillingError(f'All {sub.seats()} seats on your plan are in use. Remove someone or upgrade.', 409)
            d = _json()
            name, email = (d.get('name') or '').strip(), (d.get('email') or '').strip().lower()
            password = d.get('password') or ''
            if not name or not email or len(password) < 6:
                raise BillingError('Name, email and a password of at least 6 characters are required.')
            if User.query.filter_by(email=email).first():
                raise BillingError('An account with that email already exists - ask us to move it to your organisation.', 409)
            m = User(name=name[:120], email=email[:150], role='user', organization=org.name[:150],
                     job_title=(d.get('job_title') or '').strip()[:120] or None, organization_id=org.id, org_role='member')
            m.set_password(password)
            db.session.add(m)
            db.session.commit()
            return jsonify({'id': m.id, 'name': m.name, 'email': m.email, 'org_role': m.org_role}), 201
        except BillingError as e:
            return _err(e)

    @app.route('/api/billing/organisation/members/<int:user_id>', methods=['DELETE'])
    def billing_remove_member(user_id):
        """Owner frees a seat. The account stays, just without access through this organisation."""
        try:
            u = _me_or_401()
            org = _my_org(u, owner=True)
            m = db.session.get(User, user_id)
            if m is None or m.organization_id != org.id:
                raise BillingError('That person is not in your organisation.', 404)
            if m.id == u.id:
                raise BillingError('You cannot remove yourself - ask us to transfer ownership first.')
            m.organization_id, m.org_role, m.session_token = None, None, None
            db.session.commit()
            return jsonify({'ok': True})
        except BillingError as e:
            return _err(e)

    @app.route('/billing/invoice/<int:invoice_id>')
    def billing_invoice_page(invoice_id):
        u = current_user()
        inv = db.session.get(Invoice, invoice_id)
        if u is None or inv is None or not (
                (u.role == 'admin' and u.is_super_admin) or inv.organisation_id == u.organization_id):
            return 'Invoice not found, or you need to sign in to view it.', 404
        return invoice_html(inv)

    # ---- staff side (super admin) ------------------------------------------------------------------
    def staff(fn):
        wrapped = require_super_admin(fn)
        wrapped.__name__ = fn.__name__
        return wrapped

    @app.route('/api/admin/billing/overview')
    @staff
    def billing_admin_overview():
        at = now()
        orgs = Organisation.query.order_by(Organisation.name).all()
        rows, mrr, by_status = [], {}, {}
        for o in orgs:
            sub, st = best_subscription(o.id, at)
            members = org_members(o.id)
            open_invs = Invoice.query.filter_by(organisation_id=o.id, status='open').all()
            owed = sum(max(0, i.total - i.amount_paid()) for i in open_invs)
            key = st or 'no_plan'
            by_status[key] = by_status.get(key, 0) + 1
            if sub is not None and st in ('active', 'past_due') and sub.plan:
                mrr[sub.plan.currency] = mrr.get(sub.plan.currency, 0) + sub.plan.price / max(1, sub.plan.interval_months)
            pe = (sub.trial_end if st == 'trialing' else sub.current_period_end) if sub else None
            rows.append({'id': o.id, 'name': o.name, 'billing_email': o.billing_email,
                         'plan': sub.plan.name if sub and sub.plan else None, 'status': key,
                         'period_end': pe.isoformat() if pe else None,
                         'seats_used': len(members), 'seats': sub.seats() if sub else 0,
                         'balance_due': owed, 'currency': sub.plan.currency if sub and sub.plan else 'KES',
                         'auto_renew_card': bool(sub and sub.paystack_authorization_code)})
        since = at - timedelta(days=30)
        collected = {}
        for p in Payment.query.filter(Payment.paid_at >= since).all():
            collected[p.currency] = collected.get(p.currency, 0) + p.amount
        outstanding = {}
        for i in Invoice.query.filter_by(status='open').all():
            outstanding[i.currency] = outstanding.get(i.currency, 0) + max(0, i.total - i.amount_paid())
        unassigned = User.query.filter(User.role == 'user', User.organization_id.is_(None)).count()

        # Money received per calendar month, last 6 months (oldest first), per currency.
        months = []
        first = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for back in range(5, -1, -1):
            m_start = add_months(first, -back)
            months.append({'month': m_start.strftime('%Y-%m'), 'label': m_start.strftime('%b'), 'amounts': {}})
        since6 = add_months(first, -5)
        for p in Payment.query.filter(Payment.paid_at >= since6).all():
            key = p.paid_at.strftime('%Y-%m')
            for m in months:
                if m['month'] == key:
                    m['amounts'][p.currency] = m['amounts'].get(p.currency, 0) + p.amount

        # Plans ending or renewing in the next 30 days.
        upcoming = []
        for o in orgs:
            sub, st = best_subscription(o.id, at)
            if sub is None or st not in ('trialing', 'active'):
                continue
            end = sub.trial_end if st == 'trialing' else sub.current_period_end
            if end and at <= end <= at + timedelta(days=30):
                upcoming.append({'organisation_id': o.id, 'organisation': o.name, 'plan': sub.plan.name if sub.plan else '',
                                 'status': st, 'date': end.isoformat(), 'price': sub.plan.price if sub.plan else 0,
                                 'currency': sub.plan.currency if sub.plan else 'KES',
                                 'auto_renew_card': bool(sub.paystack_authorization_code),
                                 'cancelling': bool(sub.cancel_at_period_end)})
        upcoming.sort(key=lambda r: r['date'])

        overdue = []
        for i in Invoice.query.filter(Invoice.status == 'open', Invoice.due_at < at).order_by(Invoice.due_at).all():
            owed_i = max(0, i.total - i.amount_paid())
            if owed_i:
                overdue.append({'id': i.id, 'number': i.number, 'organisation_id': i.organisation_id,
                                'organisation': i.organisation.name if i.organisation else '', 'balance': owed_i,
                                'currency': i.currency, 'due_at': i.due_at.isoformat(), 'days': (at - i.due_at).days})

        setup = [
            {'key': 'plans', 'label': 'Set your plans and prices', 'done': Plan.query.filter_by(active=True).count() > 0,
             'hint': 'Plans tab. The five starting plans are already in place - adjust the prices if needed.'},
            {'key': 'seller', 'label': 'Add your business details for invoices',
             'done': bool(os.environ.get('BILLING_SELLER_NAME') and os.environ.get('BILLING_SELLER_PIN')),
             'hint': 'Secrets: BILLING_SELLER_NAME, BILLING_SELLER_PIN, BILLING_SELLER_ADDRESS, BILLING_SELLER_EMAIL.'},
            {'key': 'pay_details', 'label': 'Add bank and M-Pesa payment details',
             'done': bool(os.environ.get('BILLING_BANK_DETAILS') or os.environ.get('BILLING_MPESA_DETAILS')),
             'hint': 'Secrets: BILLING_BANK_DETAILS and BILLING_MPESA_DETAILS - printed on every invoice.'},
            {'key': 'paystack', 'label': 'Connect Paystack for card and M-Pesa checkout', 'done': paystack_ready(),
             'hint': 'Secret: PAYSTACK_SECRET_KEY. Then set the webhook URL in Paystack to '
                     + (os.environ.get('SITE_URL') or request.host_url).rstrip('/') + '/api/billing/paystack/webhook', 'optional': True},
            {'key': 'customers', 'label': 'Move existing members into customer organisations',
             'done': len(orgs) > 0 and unassigned == 0,
             'hint': f'{unassigned} member account(s) are not in an organisation yet. Customers tab > New customer > Add user.'},
            {'key': 'enforce', 'label': 'Switch billing checks on', 'done': enforced(),
             'hint': 'Secret: BILLING_ENFORCED=true. Until then, members use everything free.'},
        ]

        return jsonify({'revenue_by_month': months, 'upcoming_renewals': upcoming, 'overdue_invoices': overdue,
                        'setup': setup, 'customers_total': len(orgs),'enforced': enforced(), 'online_payment': paystack_ready(), 'vat_rate': vat_rate(),
                        'trial_days': trial_days(), 'grace_days': grace_days(),
                        'organisations': rows, 'by_status': by_status,
                        'mrr': {k: round(v) for k, v in mrr.items()}, 'arr': {k: round(v * 12) for k, v in mrr.items()},
                        'collected_30d': collected, 'outstanding': outstanding, 'members_without_org': unassigned,
                        'recent_payments': [dict(p.to_dict(), organisation=(db.session.get(Organisation, p.organisation_id).name
                                                                           if p.organisation_id else None))
                                            for p in Payment.query.order_by(Payment.paid_at.desc()).limit(10).all()]})

    @app.route('/api/admin/billing/orgs', methods=['POST'])
    @staff
    def billing_admin_create_org():
        d = _json()
        name = (d.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'The organisation needs a name.'}), 400
        org = Organisation(name=name[:150], billing_email=(d.get('billing_email') or '').strip()[:150] or None,
                           phone=(d.get('phone') or '').strip()[:40] or None, kra_pin=(d.get('kra_pin') or '').strip()[:30] or None,
                           address=(d.get('address') or '').strip()[:300] or None, country=(d.get('country') or 'Kenya')[:60],
                           notes=(d.get('notes') or '').strip() or None)
        db.session.add(org)
        db.session.commit()
        return jsonify(org.to_dict()), 201

    def _org_or_404(org_id):
        org = db.session.get(Organisation, org_id)
        if org is None:
            raise BillingError('Organisation not found.', 404)
        return org

    @app.route('/api/admin/billing/orgs/<int:org_id>', methods=['GET', 'PATCH'])
    @staff
    def billing_admin_org(org_id):
        try:
            org = _org_or_404(org_id)
            if request.method == 'PATCH':
                d = _json()
                for f, n in (('name', 150), ('billing_email', 150), ('phone', 40), ('kra_pin', 30),
                             ('address', 300), ('country', 60), ('notes', 5000)):
                    if f in d:
                        setattr(org, f, (d.get(f) or '').strip()[:n] or (org.name if f == 'name' else None))
                db.session.commit()
            out = _org_summary(org)
            out['subscriptions'] = [dict(s.to_dict(), effective_status=effective_status(s)) for s in org_subscriptions(org.id)]
            for inv in out['invoices']:
                inv['payments'] = [p.to_dict() for p in Payment.query.filter_by(invoice_id=inv['id']).all()]
            return jsonify(out)
        except BillingError as e:
            return _err(e)

    @app.route('/api/admin/billing/orgs/<int:org_id>/members', methods=['POST'])
    @staff
    def billing_admin_add_member(org_id):
        """Put an existing member account into this organisation (or change its role there)."""
        try:
            org = _org_or_404(org_id)
            d = _json()
            m = User.query.filter_by(email=(d.get('email') or '').strip().lower()).first()
            if m is None:
                raise BillingError('No account with that email. Create it under Settings > User Management first.', 404)
            if m.role == 'admin':
                raise BillingError('Staff (admin) accounts do not belong to customer organisations.')
            role = 'owner' if d.get('org_role') == 'owner' else 'member'
            m.organization_id, m.org_role = org.id, role
            if role == 'owner':
                for other in org_members(org.id):
                    if other.id != m.id and other.org_role == 'owner':
                        other.org_role = 'member'
                org.owner_user_id = m.id
            db.session.commit()
            return jsonify(_org_summary(org))
        except BillingError as e:
            return _err(e)

    @app.route('/api/admin/billing/orgs/<int:org_id>/members/<int:user_id>', methods=['DELETE'])
    @staff
    def billing_admin_remove_member(org_id, user_id):
        try:
            org = _org_or_404(org_id)
            m = db.session.get(User, user_id)
            if m is None or m.organization_id != org.id:
                raise BillingError('That account is not in this organisation.', 404)
            m.organization_id, m.org_role, m.session_token = None, None, None
            if org.owner_user_id == m.id:
                org.owner_user_id = None
            db.session.commit()
            return jsonify(_org_summary(org))
        except BillingError as e:
            return _err(e)

    @app.route('/api/admin/billing/orgs/<int:org_id>/plan', methods=['POST'])
    @staff
    def billing_admin_set_plan(org_id):
        try:
            org = _org_or_404(org_id)
            d = _json()
            plan = _plan(d.get('plan_code'), staff=True)
            mode = d.get('mode') or 'invoice'
            if mode not in ('trial', 'invoice', 'complimentary'):
                raise BillingError('Mode must be trial, invoice or complimentary.')
            sub, inv = start_subscription(org, plan, user_id=current_user().id, mode=mode)
            if d.get('seats_override'):
                sub.seats_override = max(1, int(d['seats_override']))
                db.session.commit()
            return jsonify({'subscription': sub.to_dict(), 'invoice': inv.to_dict() if inv else None}), 201
        except (BillingError, ValueError) as e:
            db.session.rollback()
            return _err(e)

    @app.route('/api/admin/billing/subscriptions/<int:sub_id>', methods=['PATCH'])
    @staff
    def billing_admin_update_sub(sub_id):
        """extend_trial_days / extend_days (free extra time), seats_override, cancel (now), cancel_at_period_end."""
        try:
            sub = db.session.get(Subscription, sub_id)
            if sub is None:
                raise BillingError('Subscription not found.', 404)
            d = _json()
            if d.get('extend_trial_days'):
                base = max(sub.trial_end or now(), now())
                sub.trial_end = base + timedelta(days=int(d['extend_trial_days']))
                sub.status = 'trialing'
                for inv in Invoice.query.filter_by(subscription_id=sub.id, status='open').all():
                    span = (inv.period_end - inv.period_start) if inv.period_start and inv.period_end else None
                    inv.period_start = sub.trial_end
                    inv.due_at = sub.trial_end
                    if span:
                        inv.period_end = sub.trial_end + span
            if d.get('extend_days'):
                base = sub.current_period_end or now()
                sub.current_period_end = base + timedelta(days=int(d['extend_days']))
                if sub.status not in LIVE:
                    sub.status = 'active'
            if 'seats_override' in d:
                sub.seats_override = max(1, int(d['seats_override'])) if d['seats_override'] else None
            if 'cancel_at_period_end' in d:
                sub.cancel_at_period_end = bool(d['cancel_at_period_end'])
            if d.get('cancel'):
                sub.status = 'cancelled'
                for inv in Invoice.query.filter_by(subscription_id=sub.id, status='open').all():
                    inv.status = 'void'
            db.session.commit()
            return jsonify(dict(sub.to_dict(), effective_status=effective_status(sub)))
        except (BillingError, ValueError, TypeError) as e:
            db.session.rollback()
            return _err(e)

    @app.route('/api/admin/billing/orgs/<int:org_id>/invoices', methods=['POST'])
    @staff
    def billing_admin_custom_invoice(org_id):
        """A one-off invoice: an annual report, consulting, a quoted Enterprise price..."""
        try:
            org = _org_or_404(org_id)
            d = _json()
            desc = (d.get('description') or '').strip()
            if not desc:
                raise BillingError('Describe what the invoice is for.')
            amount = float(d.get('amount') or 0)
            if amount <= 0:
                raise BillingError('Enter an amount above zero.')
            due = now() + timedelta(days=int(d.get('due_days') or _int_env('INVOICE_DUE_DAYS', 14)))
            inv = create_invoice(org, desc, amount, (d.get('currency') or 'KES').upper()[:3], kind='custom',
                                 due_at=due, rate=d.get('vat_rate'), user_id=current_user().id)
            return jsonify(inv.to_dict()), 201
        except (BillingError, ValueError) as e:
            db.session.rollback()
            return _err(e)

    @app.route('/api/admin/billing/invoices')
    @staff
    def billing_admin_invoices():
        q = Invoice.query
        if request.args.get('status'):
            q = q.filter_by(status=request.args['status'])
        return jsonify([i.to_dict() for i in q.order_by(Invoice.id.desc()).limit(500).all()])

    @app.route('/api/admin/billing/invoices/<int:invoice_id>', methods=['PATCH'])
    @staff
    def billing_admin_update_invoice(invoice_id):
        try:
            inv = db.session.get(Invoice, invoice_id)
            if inv is None:
                raise BillingError('Invoice not found.', 404)
            d = _json()
            if 'etims_number' in d:
                inv.etims_number = (d.get('etims_number') or '').strip()[:60] or None
            if d.get('status') == 'void':
                if inv.status == 'paid':
                    raise BillingError('A paid invoice cannot be voided.')
                inv.status = 'void'
            db.session.commit()
            return jsonify(inv.to_dict())
        except BillingError as e:
            return _err(e)

    @app.route('/api/admin/billing/invoices/<int:invoice_id>/payments', methods=['POST'])
    @staff
    def billing_admin_record_payment(invoice_id):
        """Record a bank transfer, M-Pesa paybill payment or cheque against an invoice."""
        try:
            inv = db.session.get(Invoice, invoice_id)
            if inv is None:
                raise BillingError('Invoice not found.', 404)
            if inv.status != 'open':
                raise BillingError(f'That invoice is already {inv.status}.')
            d = _json()
            paid_at = None
            if d.get('paid_at'):
                paid_at = datetime.fromisoformat(str(d['paid_at'])[:10])
            pay = apply_payment(inv, float(d.get('amount') or 0), d.get('method') or 'bank_transfer',
                                d.get('reference'), user_id=current_user().id, note=d.get('note'), paid_at=paid_at)
            return jsonify({'payment': pay.to_dict(), 'invoice': inv.to_dict()}), 201
        except (BillingError, ValueError) as e:
            db.session.rollback()
            return _err(e)

    @app.route('/api/admin/billing/plans', methods=['GET', 'POST'])
    @staff
    def billing_admin_plans():
        if request.method == 'GET':
            return jsonify([p.to_dict() for p in Plan.query.order_by(Plan.sort_order, Plan.id).all()])
        try:
            d = _json()
            code = ''.join(ch for ch in (d.get('code') or '').strip().lower().replace(' ', '_') if ch.isalnum() or ch == '_')
            if not code or not (d.get('name') or '').strip():
                raise BillingError('A plan needs a code and a name.')
            if Plan.query.filter_by(code=code).first():
                raise BillingError('A plan with that code already exists.', 409)
            p = Plan(code=code[:40], bulk_downloads_per_month=0, tier=infer_tier(code))   # bulk files only when a plan says so
            _apply_plan_fields(p, d)
            db.session.add(p)
            db.session.commit()
            return jsonify(p.to_dict()), 201
        except (BillingError, ValueError, TypeError) as e:
            db.session.rollback()
            return _err(e)

    @app.route('/api/admin/billing/plans/<int:plan_id>', methods=['PATCH'])
    @staff
    def billing_admin_update_plan(plan_id):
        try:
            p = db.session.get(Plan, plan_id)
            if p is None:
                raise BillingError('Plan not found.', 404)
            _apply_plan_fields(p, _json())
            db.session.commit()
            return jsonify(p.to_dict())
        except (BillingError, ValueError, TypeError) as e:
            db.session.rollback()
            return _err(e)

    def _apply_plan_fields(p, d):
        if 'name' in d:
            p.name = (d['name'] or '').strip()[:80] or p.name
        if 'description' in d:
            p.description = (d['description'] or '').strip()[:400] or None
        if 'price' in d:
            p.price = int(round(float(d['price'])))
            if p.price < 0:
                raise BillingError('Price cannot be negative.')
        if 'currency' in d:
            p.currency = (d['currency'] or 'KES').strip().upper()[:3]
        if 'interval_months' in d:
            p.interval_months = max(1, int(d['interval_months']))
        if 'seats' in d:
            p.seats = max(1, int(d['seats']))
        for kind in DOWNLOAD_KINDS:              # blank / None = unlimited, 0 = not included
            f = DOWNLOAD_KINDS[kind][0]
            if f in d:
                v = d[f]
                setattr(p, f, None if v in (None, '') else max(0, int(v)))
        if 'countries' in d:
            p.countries = (d['countries'] or 'all').strip()[:200]
        if 'tier' in d:
            t = (d['tier'] or '').strip().lower()
            if t not in TIERS:
                raise BillingError('Tier must be professional, team or enterprise.')
            p.tier = t
        for f in ('self_serve', 'active'):
            if f in d:
                setattr(p, f, bool(d[f]))
        if 'sort_order' in d:
            p.sort_order = int(d['sort_order'] or 0)
        if p.price is None:
            p.price = 0

    @app.route('/api/admin/billing/sweep', methods=['POST'])
    @staff
    def billing_admin_sweep():
        return jsonify(run_sweep())
