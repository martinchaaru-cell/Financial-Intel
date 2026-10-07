"""Super-admin approval queue.

Rule: only a SUPER ADMIN can add reports (annual-report uploads, survey file imports,
manual entry) straight into the app. Every other admin can still do the work, but the
request is parked here and nothing is saved until a super admin approves it.

How it works
  * @queue_unless_super wraps a write route. A super admin's call runs normally. Any other
    admin's call is stored as a PendingChange (the route name, URL arguments, query string,
    form fields / JSON body and the uploaded files) and the caller gets HTTP 202 with
    {"pending_approval": true, ...} instead of the route's normal result.
  * Approving replays the stored request through the very same route function (so the
    parsing/saving code is exactly the code a direct upload would have run). Rejecting just
    discards it.
  * Nothing is guessed or merged: what runs on approval is byte-for-byte what was submitted.
"""
import io
import json
from datetime import datetime
from functools import wraps

from flask import jsonify, request, session

from models import db, User


class PendingChange(db.Model):
    __tablename__ = 'pending_changes'
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(20), nullable=False)          # 'upload' | 'manual'
    endpoint = db.Column(db.String(120), nullable=False)     # Flask view function name
    method = db.Column(db.String(10), nullable=False)
    path = db.Column(db.String(300), nullable=False)
    view_args_json = db.Column(db.Text)
    query_string = db.Column(db.Text)
    form_json = db.Column(db.Text)
    json_body = db.Column(db.Text)
    summary = db.Column(db.String(500))
    status = db.Column(db.String(20), default='pending')     # pending | approved | rejected | failed
    submitted_by_id = db.Column(db.Integer)
    submitted_by_name = db.Column(db.String(150))
    submitted_by_email = db.Column(db.String(150))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    decided_by_name = db.Column(db.String(150))
    decided_at = db.Column(db.DateTime)
    result_text = db.Column(db.Text)

    def to_dict(self):
        return {'id': self.id, 'kind': self.kind, 'summary': self.summary, 'status': self.status,
                'submitted_by': self.submitted_by_name, 'submitted_by_email': self.submitted_by_email,
                'created_at': self.created_at.isoformat() if self.created_at else None,
                'decided_by': self.decided_by_name,
                'decided_at': self.decided_at.isoformat() if self.decided_at else None,
                'result': self.result_text,
                'file_count': PendingChangeFile.query.filter_by(change_id=self.id).count()}


class PendingChangeFile(db.Model):
    __tablename__ = 'pending_change_files'
    id = db.Column(db.Integer, primary_key=True)
    change_id = db.Column(db.Integer, db.ForeignKey('pending_changes.id'), nullable=False, index=True)
    field = db.Column(db.String(60), nullable=False)
    filename = db.Column(db.String(300))
    content_type = db.Column(db.String(120))
    data = db.Column(db.LargeBinary)


def _user():
    uid = session.get('user_id')
    return db.session.get(User, uid) if uid else None


def is_super_admin(user=None):
    u = user or _user()
    return bool(u and u.role == 'admin' and getattr(u, 'is_super_admin', False))


def queue_unless_super(kind, describe=None):
    """Decorator for a write route (apply UNDER @require_role('admin')).
    describe(view_kwargs) -> short human summary shown in the approvals list."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if is_super_admin():
                return fn(*args, **kwargs)
            u = _user()
            names = [f.filename for fl in request.files.listvalues() for f in fl if f and f.filename]
            what = (describe(kwargs) if describe else fn.__name__.replace('_', ' '))
            if names:
                what += ': ' + ', '.join(names[:4]) + (f' (+{len(names) - 4} more)' if len(names) > 4 else '')
            ch = PendingChange(
                kind=kind, endpoint=fn.__name__, method=request.method, path=request.path,
                view_args_json=json.dumps(kwargs), query_string=request.query_string.decode('utf-8', 'replace'),
                form_json=json.dumps(request.form.to_dict(flat=False)) if request.form else None,
                json_body=json.dumps(request.get_json(silent=True)) if request.is_json else None,
                summary=what[:500], status='pending',
                submitted_by_id=u.id if u else None, submitted_by_name=u.name if u else None,
                submitted_by_email=u.email if u else None)
            db.session.add(ch)
            db.session.flush()
            for field, fl in request.files.lists():
                for f in fl:
                    if f and f.filename:
                        db.session.add(PendingChangeFile(change_id=ch.id, field=field, filename=f.filename,
                                                         content_type=f.mimetype, data=f.read()))
            db.session.commit()
            return jsonify({'pending_approval': True, 'approval_id': ch.id,
                            'message': 'Sent to a super admin for approval. Nothing is saved until it is approved.'}), 202
        wrapper._direct = fn
        return wrapper
    return decorator


def _replay(app, ch):
    view = app.view_functions.get(ch.endpoint)
    direct = getattr(view, '_direct', None)
    if direct is None:
        raise RuntimeError(f'Route {ch.endpoint} can no longer be replayed.')
    kwargs = json.loads(ch.view_args_json or '{}')
    kw = {'method': ch.method, 'query_string': ch.query_string or ''}
    files = PendingChangeFile.query.filter_by(change_id=ch.id).all()
    if files or ch.form_json:
        data = {}
        for k, vals in json.loads(ch.form_json or '{}').items():
            data[k] = vals if len(vals) != 1 else vals[0]
        for f in files:
            lst = data.setdefault(f.field, [])
            if not isinstance(lst, list):
                lst = data[f.field] = [lst]
            lst.append((io.BytesIO(f.data or b''), f.filename, f.content_type or 'application/octet-stream'))
        kw['data'] = data
        kw['content_type'] = 'multipart/form-data'
    elif ch.json_body is not None:
        kw['json'] = json.loads(ch.json_body)
    with app.test_request_context(ch.path, **kw):
        rv = app.make_response(direct(**kwargs))
    return rv.status_code, rv.get_data(as_text=True)[:2000]


def register(app, require_role):
    sup = require_role('admin')

    def super_only(fn):
        @wraps(fn)
        def w(*a, **k):
            if not is_super_admin():
                return jsonify({'error': 'Only a super admin can do that.'}), 403
            return fn(*a, **k)
        return w

    @app.route('/api/approvals')
    @sup
    def approvals_list():
        """Super admin: every request. Other admins: only their own (so they can see what is waiting)."""
        q = PendingChange.query
        if not is_super_admin():
            q = q.filter_by(submitted_by_id=session.get('user_id'))
        if request.args.get('status'):
            q = q.filter_by(status=request.args['status'])
        return jsonify({'is_super_admin': is_super_admin(),
                        'items': [c.to_dict() for c in q.order_by(PendingChange.id.desc()).limit(200).all()]})

    @app.route('/api/approvals/count')
    @sup
    def approvals_count():
        q = PendingChange.query.filter_by(status='pending')
        if not is_super_admin():
            q = q.filter_by(submitted_by_id=session.get('user_id'))
        return jsonify({'pending': q.count(), 'is_super_admin': is_super_admin()})

    @app.route('/api/approvals/<int:cid>/approve', methods=['POST'])
    @sup
    @super_only
    def approvals_approve(cid):
        ch = PendingChange.query.get_or_404(cid)
        if ch.status != 'pending':
            return jsonify({'error': f'This request is already {ch.status}.'}), 400
        u = _user()
        try:
            code, body = _replay(app, ch)
            ch.status = 'approved' if code < 400 else 'failed'
            ch.result_text = f'HTTP {code}: {body}'[:2000]
        except Exception as e:  # keep the request so it can be retried or rejected
            db.session.rollback()
            ch = db.session.get(PendingChange, cid)
            ch.status, ch.result_text = 'failed', str(e)[:2000]
            code = 500
        ch.decided_by_name, ch.decided_at = (u.name if u else None), datetime.utcnow()
        db.session.commit()
        return jsonify({'item': ch.to_dict()}), (200 if ch.status == 'approved' else 422)

    @app.route('/api/approvals/<int:cid>/reject', methods=['POST'])
    @sup
    @super_only
    def approvals_reject(cid):
        ch = PendingChange.query.get_or_404(cid)
        if ch.status not in ('pending', 'failed'):
            return jsonify({'error': f'This request is already {ch.status}.'}), 400
        u = _user()
        PendingChangeFile.query.filter_by(change_id=ch.id).delete(synchronize_session=False)
        ch.status, ch.decided_by_name, ch.decided_at = 'rejected', (u.name if u else None), datetime.utcnow()
        db.session.commit()
        return jsonify({'item': ch.to_dict()})

    @app.route('/api/users/<int:user_id>/super-admin', methods=['PATCH'])
    @sup
    @super_only
    def set_super_admin(user_id):
        user = User.query.get_or_404(user_id)
        on = bool((request.get_json(force=True) or {}).get('is_super_admin'))
        if user.role != 'admin' and on:
            return jsonify({'error': 'Only an admin account can be a super admin.'}), 400
        if not on and user.is_super_admin and User.query.filter_by(is_super_admin=True).count() <= 1:
            return jsonify({'error': 'At least one super admin must remain.'}), 400
        user.is_super_admin = on
        db.session.commit()
        return jsonify(user.to_dict())
