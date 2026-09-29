"""Export the live database (Postgres on Replit, SQLite locally) to ONE portable SQLite file.

Why: when a tab says "Not available" you need to see what is actually stored and where each number came
from, without a Postgres client. Open the file with DB Browser for SQLite, `sqlite3`, or pandas.

What is in the file
  * every application table, copied as-is (same names, same columns) - EXCEPT `app_users`, whose password
    hashes never leave the server;
  * `field_trace` - one flat row per survey metric per company/year: value, unit, the source note the
    extractor wrote (page + the sentence/table it read), confidence, review status and any correction;
  * `metric_gaps` - the same thing seen from the other side: every metric a company has NO value for,
    so "why is this tab empty" is a one-line query;
  * views `v_pay_rows` and `v_tab_status` for the two questions asked most.

Use:   python export_db.py [out.sqlite]         (from the Replit shell, app env vars set)
       GET /api/admin/export-db                 (admin only; downloads the same file)
"""
import datetime as _dt
import json
import os
import sqlite3
import tempfile

from sqlalchemy import inspect as sa_inspect

EXCLUDED_TABLES = {'app_users'}          # credentials: never exported


def _sqlite_type(col):
    t = str(col.type).upper()
    if any(k in t for k in ('INT', 'BOOL')):
        return 'INTEGER'
    if any(k in t for k in ('FLOAT', 'NUMERIC', 'REAL', 'DOUBLE')):
        return 'REAL'
    return 'TEXT'


def _jsonable(v):
    if v is None or isinstance(v, (int, float, str)):
        return v
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


def _copy_tables(out, db):
    engine = db.engine
    insp = sa_inspect(engine)
    copied = {}
    for table in db.metadata.sorted_tables:
        if table.name in EXCLUDED_TABLES or not insp.has_table(table.name):
            continue
        cols = [c.name for c in table.columns]
        ddl = ', '.join(f'"{c.name}" {_sqlite_type(c)}' for c in table.columns)
        out.execute(f'CREATE TABLE "{table.name}" ({ddl})')
        with engine.connect() as conn:
            rows = conn.execute(table.select()).fetchall()
        out.executemany(
            f'INSERT INTO "{table.name}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) '
            f'VALUES ({", ".join("?" for _ in cols)})',
            [[_jsonable(v) for v in r] for r in rows])
        copied[table.name] = len(rows)
    return copied


def _build_trace_tables(out, db):
    from models import Company
    from models_survey import SurveyCompanyData
    from tab_modules import TAB_MODULES

    metric_cols = {}
    for mod in TAB_MODULES:
        for key, label in mod['metrics']:
            metric_cols[key] = (mod['label'], label)
    # policy / board metrics the tabs read that are not listed as manual-entry columns
    for extra in ('committee_chair_meeting_allowance', 'committee_member_meeting_allowance',
                  'ceo_monthly_gratuity', 'ceo_monthly_share_value'):
        metric_cols.setdefault(extra, ('Remuneration', extra.replace('_', ' ')))

    out.execute('''CREATE TABLE field_trace (
        company TEXT, sector TEXT, fiscal_year TEXT, tab TEXT, metric_key TEXT, metric_label TEXT,
        value REAL, unit TEXT, source_note TEXT, confidence REAL, review_status TEXT, review_correction TEXT)''')
    out.execute('''CREATE TABLE metric_gaps (
        company TEXT, sector TEXT, fiscal_year TEXT, tab TEXT, metric_key TEXT, metric_label TEXT)''')

    companies = {c.id: c for c in Company.query.all()}
    n_trace = n_gap = 0
    for row in SurveyCompanyData.query.all():
        comp = companies.get(row.company_id)
        name, sector = (comp.name if comp else f'company {row.company_id}'), (row.sector or (comp.sector if comp else None))
        sources = row.field_sources or {}
        conf = row.field_confidence or {}
        review = row.field_review_status or {}
        corr = row.field_review_corrections or {}
        for key, (tab, label) in metric_cols.items():
            if not hasattr(row, key):
                continue
            val = getattr(row, key)
            if val is None:
                out.execute('INSERT INTO metric_gaps VALUES (?,?,?,?,?,?)',
                            (name, sector, row.fiscal_year, tab, key, label))
                n_gap += 1
                continue
            unit = row.director_figures_unit if key.split('_')[0] in ('chairperson', 'other', 'executive', 'committee', 'ceo') \
                else row.unit
            out.execute('INSERT INTO field_trace VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (name, sector, row.fiscal_year, tab, key, label, float(val), unit,
                         sources.get(key), conf.get(key), review.get(key), _jsonable(corr.get(key))))
            n_trace += 1
    return {'field_trace': n_trace, 'metric_gaps': n_gap}


def _build_views(out):
    have = {r[0] for r in out.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {'director_remuneration_rows', 'financial_periods', 'company'} <= have:
        out.execute('''CREATE VIEW v_pay_rows AS
            SELECT c.name AS company, p.period_label, r.director_name, r.role, r.table_kind, r.total,
                   r.is_grand_total, r.is_total_row, r.page, r.confidence, r.components
            FROM director_remuneration_rows r
            JOIN financial_periods p ON p.id = r.period_id
            JOIN company c ON c.id = p.company_id''')
    if {'field_trace', 'metric_gaps'} <= have:
        out.execute('''CREATE VIEW v_tab_status AS
            SELECT company, fiscal_year, tab,
                   SUM(filled) AS filled, SUM(1 - filled) AS missing
            FROM (SELECT company, fiscal_year, tab, 1 AS filled FROM field_trace
                  UNION ALL SELECT company, fiscal_year, tab, 0 FROM metric_gaps)
            GROUP BY company, fiscal_year, tab''')


def export_sqlite_snapshot(path=None):
    """Write the snapshot; returns (path, {table: row_count}). Call inside a Flask app context."""
    from models import db
    path = path or os.path.join(tempfile.gettempdir(), f"finintel_snapshot_{_dt.datetime.utcnow():%Y%m%d_%H%M%S}.sqlite")
    if os.path.exists(path):
        os.remove(path)
    out = sqlite3.connect(path)
    try:
        counts = _copy_tables(out, db)
        counts.update(_build_trace_tables(out, db))
        _build_views(out)
        out.execute('CREATE TABLE _export_info (key TEXT, value TEXT)')
        out.executemany('INSERT INTO _export_info VALUES (?,?)', [
            ('exported_at_utc', _dt.datetime.utcnow().isoformat()),
            ('source_dialect', db.engine.dialect.name),
            ('excluded_tables', ','.join(sorted(EXCLUDED_TABLES)))])
        out.commit()
    finally:
        out.close()
    return path, counts


if __name__ == '__main__':
    import sys
    from app import app
    with app.app_context():
        dest, counts = export_sqlite_snapshot(sys.argv[1] if len(sys.argv) > 1 else 'finintel_snapshot.sqlite')
    print('wrote', dest)
    for k, v in sorted(counts.items()):
        print(f'  {k}: {v}')
