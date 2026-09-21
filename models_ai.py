"""Tables for the AI extraction module (ai_extract.py). New tables only - db.create_all()
creates them on startup, so no migration is needed for an existing database."""
from datetime import datetime
import json

from models import db


class AIExtractionJob(db.Model):
    __tablename__ = 'ai_extraction_jobs'
    id = db.Column(db.Integer, primary_key=True)
    mode = db.Column(db.String(10), nullable=False)          # 'batch' | 'direct'
    model = db.Column(db.String(60))
    batch_id = db.Column(db.String(80))
    status = db.Column(db.String(20), default='submitted')   # submitted | ended | failed | canceled
    n_items = db.Column(db.Integer, default=0)
    est_cost_usd = db.Column(db.Float)
    actual_cost_usd = db.Column(db.Float)
    input_tokens = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime)

    def to_dict(self):
        return {'id': self.id, 'mode': self.mode, 'model': self.model, 'batch_id': self.batch_id, 'status': self.status,
                'n_items': self.n_items, 'est_cost_usd': self.est_cost_usd, 'actual_cost_usd': self.actual_cost_usd,
                'input_tokens': self.input_tokens, 'output_tokens': self.output_tokens, 'error': self.error,
                'created_at': self.created_at.isoformat() if self.created_at else None,
                'finished_at': self.finished_at.isoformat() if self.finished_at else None}


class AIExtractionItem(db.Model):
    """One (report, task) unit of work: the relevant pages' text is stored at upload time,
    because the uploaded PDF itself is not kept."""
    __tablename__ = 'ai_extraction_items'
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    fiscal_year = db.Column(db.String(20), nullable=False)
    source_document_id = db.Column(db.Integer)
    filename = db.Column(db.String(255))
    task = db.Column(db.String(20), nullable=False)          # board | committees | pay
    pages_json = db.Column(db.Text)                          # [page numbers sent]
    page_text = db.Column(db.Text)                           # {"113": "...layout text..."}
    status = db.Column(db.String(20), default='pending')     # pending | submitted | done | failed | applied | discarded
    job_id = db.Column(db.Integer, db.ForeignKey('ai_extraction_jobs.id'))
    custom_id = db.Column(db.String(80))
    result_json = db.Column(db.Text)
    verification_json = db.Column(db.Text)
    usage_json = db.Column(db.Text)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    applied_at = db.Column(db.DateTime)

    def pages(self):
        return json.loads(self.page_text) if self.page_text else {}

    def _meta(self):
        raw = json.loads(self.pages_json) if self.pages_json else []
        return raw if isinstance(raw, dict) else {'pages': raw, 'gaps': []}

    def gaps(self):
        """What the rule-based readers could NOT fill for this task - the only things the AI is asked for."""
        return self._meta().get('gaps', [])

    def to_dict(self, full=False):
        v = json.loads(self.verification_json) if self.verification_json else None
        d = {'id': self.id, 'company_id': self.company_id, 'fiscal_year': self.fiscal_year, 'filename': self.filename,
             'task': self.task, 'status': self.status, 'job_id': self.job_id,
             'pages': self._meta().get('pages', []), 'gaps': self._meta().get('gaps', []),
             'summary': v['summary'] if v else None, 'error': self.error,
             'applied_at': self.applied_at.isoformat() if self.applied_at else None,
             'chars': len(self.page_text or '')}
        if full:
            d['result'] = json.loads(self.result_json) if self.result_json else None
            d['checks'] = v['checks'] if v else []
        return d
