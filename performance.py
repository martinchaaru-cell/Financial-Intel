"""Bounded, process-local cache for explicitly selected admin read endpoints.
Authorization and rate limiting run before views; never cache session/billing responses.
"""
import os
import time
import threading
from collections import OrderedDict
from functools import wraps

class ReadCache:
    def __init__(self, limit=128, max_bytes=16*1024*1024, clock=time.monotonic):
        self.limit, self.max_bytes, self.clock = limit, max_bytes, clock
        self.entries = OrderedDict()
        self.lock = threading.RLock()
        self.generation = self.size = 0

    def clear(self):
        with self.lock:
            self.generation += 1
            self.entries.clear()
            self.size = 0

    def lookup(self, key):
        with self.lock:
            item = self.entries.pop(key, None)
            if item:
                expires, data = item
                if expires > self.clock():
                    self.entries[key] = item
                    return self.generation, data
                self.size -= len(data)
            return self.generation, None

    def store(self, key, data, ttl, generation):
        with self.lock:
            if generation != self.generation or ttl <= 0 or len(data) > self.max_bytes:
                return
            prior = self.entries.pop(key, None)
            if prior: self.size -= len(prior[1])
            self.entries[key] = (self.clock()+ttl, data)
            self.size += len(data)
            while len(self.entries) > self.limit or self.size > self.max_bytes:
                _, (_, removed) = self.entries.popitem(last=False)
                self.size -= len(removed)

cache = ReadCache()


def admin_read(current_user, ttl=20):
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            from flask import current_app, request, make_response, g
            u = current_user()
            if current_app.testing or not u or u.role != 'admin' or os.getenv('PERF_CACHE_ENABLED','1') != '1':
                return fn(*args, **kwargs)
            key = (u.id, bool(getattr(u,'is_super_admin',False)), request.path, request.query_string)
            generation, data = cache.lookup(key)
            if data is not None:
                g.perf_cache = 'HIT'
                return current_app.response_class(data, mimetype='application/json', headers={'Cache-Control':'private, no-store'})
            response = make_response(fn(*args, **kwargs))
            if response.status_code == 200 and response.is_json and not response.is_streamed:
                cache.store(key, response.get_data(), ttl, generation)
                response.headers['Cache-Control'] = 'private, no-store'
                g.perf_cache = 'MISS'
            return response
        return wrapped
    return deco


def register(app):
    from flask import g, request
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    # Only survey/financial mutations invalidate; account presence and audit writes do not.
    def changed(obj):
        name = getattr(obj, '__tablename__', '')
        return name.startswith(('survey_', 'financial', 'calculated_metrics')) or name in ('company','source_documents','director_remuneration_rows','market_data_snapshots')
    @event.listens_for(Session, 'before_flush')
    def mark(session, *_):
        if any(changed(o) for o in list(session.new)+list(session.dirty)+list(session.deleted)):
            session.info['_perf_dirty'] = True
    @event.listens_for(Session, 'do_orm_execute')
    def mark_bulk(state):
        if state.is_update or state.is_delete or getattr(state, "is_insert", False):
            state.session.info['_perf_dirty'] = True
    @event.listens_for(Session, 'after_commit')
    def invalidate(session):
        if session.info.pop('_perf_dirty', False): cache.clear()
    @event.listens_for(Session, 'after_rollback')
    def rollback(session):
        session.info.pop('_perf_dirty', None)
    def start():
        g.perf_started = time.perf_counter()
    app.before_request_funcs.setdefault(None, []).insert(0, start)
    from flask import has_request_context
    from sqlalchemy.engine import Engine
    @event.listens_for(Engine, 'before_cursor_execute')
    def sql_start(conn, cursor, statement, parameters, context, executemany):
        context._rp_sql_started = time.perf_counter()
    @event.listens_for(Engine, 'after_cursor_execute')
    def sql_finish(conn, cursor, statement, parameters, context, executemany):
        if has_request_context():
            g.rp_sql_ms = getattr(g, 'rp_sql_ms', 0) + (time.perf_counter()-context._rp_sql_started)*1000
            g.rp_sql_count = getattr(g, 'rp_sql_count', 0)+1
    @app.after_request
    def finish(response):
        if request.method not in ('GET','HEAD','OPTIONS') and response.status_code < 400:
            cache.clear()
        elapsed = (time.perf_counter()-getattr(g,'perf_started',time.perf_counter()))*1000
        response.headers.add('Server-Timing', f'app;dur={max(0, elapsed):.1f}')
        response.headers.add('Server-Timing', f'db;dur={getattr(g, "rp_sql_ms", 0):.1f};desc="{getattr(g, "rp_sql_count", 0)} queries"')
        if hasattr(g,'perf_cache'): response.headers['X-Performance-Cache'] = g.perf_cache
        if elapsed >= 750 and request.path.startswith('/api/'):
            app.logger.warning('slow request %s %s %.0fms status=%s', request.method, request.url_rule.rule if request.url_rule else '/api/unknown', elapsed, response.status_code)
        return response

    # Flask executes after_request hooks in reverse order; record full processing.
    hooks = app.after_request_funcs[None]
    hooks.remove(finish)
    hooks.insert(0, finish)

    @app.cli.command('performance-indexes')
    def performance_indexes():
        """Explicit maintenance-window migration, safe to repeat."""
        from models import db
        from sqlalchemy import Index
        import click
        targets = (('survey_company_data', ('fiscal_year','company_id'), 'ix_rp_survey_year_company'),
                   ('financials', ('company_id','period'), 'ix_rp_financial_company_period'))
        for table_name, columns, name in targets:
            table = db.metadata.tables[table_name]
            idx = next((i for i in table.indexes if i.name == name), None)
            if idx is None: idx = Index(name, *(table.c[c] for c in columns))
            idx.create(db.engine, checkfirst=True)
            click.echo(name + ': ready')
