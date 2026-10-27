"""Tables for email verification, password reset, account invitations, the sent-email log and contact enquiries.

  auth_tokens        one-time links (verify email / reset password / invite). Only a SHA-256 hash of the token is
                     stored, so a database leak does not hand out working links.
  email_log          every email the app tried to send, with its outcome (sent / failed / not_configured). When SMTP
                     is not set up yet, super admins can still read the message here and pass the link on by hand.
  contact_enquiries  messages from the website's Contact page.
"""
from datetime import datetime

from models import db


class AuthToken(db.Model):
    __tablename__ = 'auth_tokens'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    purpose = db.Column(db.String(20), nullable=False)           # verify | reset | invite
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    email = db.Column(db.String(150))                            # the address the link was sent to
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)
    ip = db.Column(db.String(64))


class EmailLog(db.Model):
    __tablename__ = 'email_log'
    id = db.Column(db.Integer, primary_key=True)
    to_address = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(300), nullable=False)
    body_text = db.Column(db.Text)
    kind = db.Column(db.String(30))                              # verify | reset | invite | password_changed | enquiry | ...
    status = db.Column(db.String(20), nullable=False)            # sent | failed | not_configured
    error = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    def to_dict(self, full=False):
        d = {'id': self.id, 'to': self.to_address, 'subject': self.subject, 'kind': self.kind, 'status': self.status,
             'error': self.error, 'created_at': self.created_at.isoformat() + 'Z' if self.created_at else None}
        if full:
            d['body_text'] = self.body_text
        return d


class ContactEnquiry(db.Model):
    __tablename__ = 'contact_enquiries'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(40))
    organisation = db.Column(db.String(150))
    topic = db.Column(db.String(80))
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='new')     # new | replied | closed
    note = db.Column(db.String(500))                                       # internal note by staff
    ip = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'email': self.email, 'phone': self.phone,
                'organisation': self.organisation, 'topic': self.topic, 'message': self.message,
                'status': self.status, 'note': self.note,
                'created_at': self.created_at.isoformat() + 'Z' if self.created_at else None,
                'updated_at': self.updated_at.isoformat() + 'Z' if self.updated_at else None}
