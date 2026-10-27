"""Billing data model: organisations (the paying customer), plans, subscriptions, invoices and payments.

How the pieces fit
  * An Organisation is the customer. Member accounts (app_users, role 'user') belong to one through
    app_users.organization_id; the first member is the owner (org_role 'owner') and can add colleagues up
    to the plan's seat count.
  * A Plan is a price list entry: price, currency, billing period in months, seats, exports per month.
    Super admins edit plans from the Billing page; the defaults are seeded by migrate_v11_billing.py.
  * A Subscription links an organisation to a plan for a period. Its status drives access:
        trialing  -> full access until trial_end
        active    -> full access until current_period_end
        past_due  -> renewal unpaid, still full access during the grace period (banner shown)
        read_only -> grace period over: can view, cannot export or change anything
        cancelled / expired -> no access beyond the public overview
  * An Invoice is what the customer pays (a plan period, or a custom line such as a one-off report).
    Amounts are whole currency units (KES has no cents in practice); VAT is added on top at vat_rate.
  * A Payment records money received - by Paystack (card / M-Pesa) automatically, or by bank transfer /
    M-Pesa paybill / cheque recorded by a super admin. A paid invoice extends the subscription.
  * PaymentEvent stores every notification Paystack sends, so the same payment is never applied twice.
"""
from datetime import datetime

from models import db


class Organisation(db.Model):
    __tablename__ = 'billing_organisations'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    billing_email = db.Column(db.String(150))
    phone = db.Column(db.String(40))
    kra_pin = db.Column(db.String(30))            # the customer's tax PIN, printed on invoices
    address = db.Column(db.String(300))
    country = db.Column(db.String(60))
    owner_user_id = db.Column(db.Integer, index=True)
    trial_used = db.Column(db.Boolean, default=False)
    company_id = db.Column(db.Integer)            # the customer's own listed company (their dashboard compares it)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'billing_email': self.billing_email, 'phone': self.phone,
                'kra_pin': self.kra_pin, 'address': self.address, 'country': self.country,
                'owner_user_id': self.owner_user_id, 'trial_used': bool(self.trial_used), 'notes': self.notes,
                'created_at': _iso(self.created_at)}


class Plan(db.Model):
    __tablename__ = 'billing_plans'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), nullable=False, unique=True)
    name = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(400))
    price = db.Column(db.Integer, nullable=False, default=0)          # whole currency units, before VAT
    currency = db.Column(db.String(3), nullable=False, default='KES')
    interval_months = db.Column(db.Integer, nullable=False, default=12)
    seats = db.Column(db.Integer, nullable=False, default=1)
    exports_per_month = db.Column(db.Integer)                          # superseded by the three below; kept for old rows
    # Downloads per month by kind (billing.download_kind): None = unlimited, 0 = not included in the plan.
    report_downloads_per_month = db.Column(db.Integer)                 # PDF / Word reports
    data_downloads_per_month = db.Column(db.Integer)                   # CSV / Excel data files
    bulk_downloads_per_month = db.Column(db.Integer)        # ZIP bundles and whole-portfolio files
    countries = db.Column(db.String(200), default='all')
    tier = db.Column(db.String(20))                                    # professional | team | enterprise: decides which pages unlock               # 'all' or comma list of country codes
    self_serve = db.Column(db.Boolean, default=True)                   # False = "contact us" (quoted) plans
    active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)

    def to_dict(self):
        return {'id': self.id, 'code': self.code, 'name': self.name, 'description': self.description,
                'price': self.price, 'currency': self.currency, 'interval_months': self.interval_months,
                'seats': self.seats, 'countries': self.countries or 'all', 'tier': self.tier,
                'report_downloads_per_month': self.report_downloads_per_month,
                'data_downloads_per_month': self.data_downloads_per_month,
                'bulk_downloads_per_month': self.bulk_downloads_per_month,
                'self_serve': bool(self.self_serve), 'active': bool(self.active), 'sort_order': self.sort_order}


class Subscription(db.Model):
    __tablename__ = 'billing_subscriptions'
    id = db.Column(db.Integer, primary_key=True)
    organisation_id = db.Column(db.Integer, db.ForeignKey('billing_organisations.id'), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('billing_plans.id'), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='trialing')
    trial_end = db.Column(db.DateTime)
    current_period_start = db.Column(db.DateTime)
    current_period_end = db.Column(db.DateTime)
    seats_override = db.Column(db.Integer)                 # extra seats agreed for this customer (None = plan's)
    cancel_at_period_end = db.Column(db.Boolean, default=False)
    # Saved by Paystack after a successful card payment, so renewals can be charged without the customer.
    paystack_authorization_code = db.Column(db.String(80))
    paystack_customer_code = db.Column(db.String(80))
    card_label = db.Column(db.String(80))                  # e.g. "Visa ending 4081" (display only)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    plan = db.relationship('Plan')
    organisation = db.relationship('Organisation', backref=db.backref('subscriptions', lazy='dynamic'))

    def seats(self):
        return self.seats_override if self.seats_override else (self.plan.seats if self.plan else 1)

    def to_dict(self):
        return {'id': self.id, 'organisation_id': self.organisation_id, 'plan': self.plan.to_dict() if self.plan else None,
                'status': self.status, 'trial_end': _iso(self.trial_end),
                'current_period_start': _iso(self.current_period_start), 'current_period_end': _iso(self.current_period_end),
                'seats': self.seats(), 'seats_override': self.seats_override,
                'cancel_at_period_end': bool(self.cancel_at_period_end),
                'auto_renew_card': bool(self.paystack_authorization_code), 'card_label': self.card_label}


class Invoice(db.Model):
    __tablename__ = 'billing_invoices'
    # One invoice per subscription period: stops two app workers raising the same renewal twice.
    __table_args__ = (db.UniqueConstraint('subscription_id', 'period_start', name='uq_invoice_sub_period'),)
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(30), unique=True)
    organisation_id = db.Column(db.Integer, db.ForeignKey('billing_organisations.id'), nullable=False, index=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey('billing_subscriptions.id'), index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('billing_plans.id'))
    kind = db.Column(db.String(20), default='subscription')        # subscription | renewal | custom
    description = db.Column(db.String(300), nullable=False)
    period_start = db.Column(db.DateTime)
    period_end = db.Column(db.DateTime)
    amount = db.Column(db.Integer, nullable=False, default=0)       # before VAT
    vat_rate = db.Column(db.Float, default=0.0)                     # e.g. 0.16
    vat_amount = db.Column(db.Integer, default=0)
    total = db.Column(db.Integer, nullable=False, default=0)
    currency = db.Column(db.String(3), nullable=False, default='KES')
    status = db.Column(db.String(12), nullable=False, default='open')   # open | paid | void
    issued_at = db.Column(db.DateTime, default=datetime.utcnow)
    due_at = db.Column(db.DateTime)
    paid_at = db.Column(db.DateTime)
    etims_number = db.Column(db.String(60))                         # KRA eTIMS invoice number, entered once issued
    created_by_user_id = db.Column(db.Integer)

    organisation = db.relationship('Organisation')
    plan = db.relationship('Plan')

    def amount_paid(self):
        return sum(p.amount for p in Payment.query.filter_by(invoice_id=self.id).all())

    def to_dict(self):
        return {'id': self.id, 'number': self.number, 'organisation_id': self.organisation_id,
                'organisation': self.organisation.name if self.organisation else None,
                'subscription_id': self.subscription_id, 'plan_code': self.plan.code if self.plan else None,
                'kind': self.kind, 'description': self.description,
                'period_start': _iso(self.period_start), 'period_end': _iso(self.period_end),
                'amount': self.amount, 'vat_rate': self.vat_rate or 0, 'vat_amount': self.vat_amount or 0,
                'total': self.total, 'currency': self.currency, 'status': self.status,
                'issued_at': _iso(self.issued_at), 'due_at': _iso(self.due_at), 'paid_at': _iso(self.paid_at),
                'etims_number': self.etims_number, 'amount_paid': self.amount_paid()}


class Payment(db.Model):
    __tablename__ = 'billing_payments'
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('billing_invoices.id'), nullable=False, index=True)
    organisation_id = db.Column(db.Integer, index=True)
    amount = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default='KES')
    method = db.Column(db.String(20), nullable=False)       # paystack | bank_transfer | mpesa | cheque | other
    reference = db.Column(db.String(120), index=True)
    paid_at = db.Column(db.DateTime, default=datetime.utcnow)
    recorded_by_user_id = db.Column(db.Integer)              # None when it came from Paystack
    note = db.Column(db.String(300))

    def to_dict(self):
        return {'id': self.id, 'invoice_id': self.invoice_id, 'amount': self.amount, 'currency': self.currency,
                'method': self.method, 'reference': self.reference, 'paid_at': _iso(self.paid_at), 'note': self.note}


class PaymentEvent(db.Model):
    __tablename__ = 'billing_payment_events'
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(20), nullable=False, default='paystack')
    reference = db.Column(db.String(120), nullable=False, unique=True)   # a payment is applied once, ever
    event_type = db.Column(db.String(60))
    payload = db.Column(db.Text)
    received_at = db.Column(db.DateTime, default=datetime.utcnow)


def _iso(dt):
    return dt.isoformat() if dt else None
