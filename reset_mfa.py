"""Emergency: turn off two-step verification and clear the lockout for one account.

Use when the only super admin loses their phone. In the Replit Shell:
    python reset_mfa.py you@example.com
"""
import sys

from app import app
from models import db, User
import account_security

if len(sys.argv) != 2:
    sys.exit('Usage: python reset_mfa.py <email>')

with app.app_context():
    user = User.query.filter_by(email=sys.argv[1].strip().lower()).first()
    if not user:
        sys.exit('No account with that email.')
    user.mfa_enabled = False
    user.totp_secret = None
    user.mfa_last_step = None
    account_security.unlock(user, db)
    print(f'Two-step verification off and lockout cleared for {user.email}.')
