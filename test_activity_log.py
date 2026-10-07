"""Pure-logic tests for activity_log.py (no database needed)."""
from datetime import datetime, timedelta
from types import SimpleNamespace as N

import activity_log as al


def _e(i, uid, name, action, cat, dt, detail=None, cid=None):
    return N(id=i, user_id=uid, actor=name, actor_email=f'{name.lower()}@x.com' if uid else None,
             actor_role='admin' if uid else 'guest', action=action, category=cat, created_at=dt,
             detail=detail, company_id=cid, fiscal_year='FY2025')


def test_classify_known_and_generic_routes():
    assert al.classify_request('POST', '/api/login')[:2] == ('login', al.CAT_AUTH)
    assert al.classify_request('DELETE', '/api/users/<int:user_id>')[1] == al.CAT_USERS
    assert al.classify_request('GET', '/api/export/combined-report.pdf')[1] == al.CAT_EXPORT
    assert al.classify_request('POST', '/api/manual/<int:company_id>/<fiscal_year>/directors')[1] == al.CAT_DATA
    assert al.classify_request('POST', '/api/review/drafts/<int:draft_id>/approve')[1] == al.CAT_REVIEW


def test_report_groups_by_account_category_and_local_day():
    base = datetime(2026, 10, 3, 10, 0)
    rows = [
        _e(1, 1, 'Martin', 'login', al.CAT_AUTH, base),
        _e(2, 1, 'Martin', 'survey_pdf_extracted', al.CAT_UPLOAD, base + timedelta(minutes=5), '3 fields', 7),
        _e(3, 2, 'Amina', 'field_approved', al.CAT_REVIEW, base + timedelta(hours=14), 'x', 7),   # 00:00 UTC = 03:00 in UTC+3
    ]
    rep = al.build_report(rows, {7: 'Britam'}, tz_offset=-180)
    assert rep['overall']['total'] == 3 and rep['overall']['accounts_active'] == 2
    assert {a['account']: a['count'] for a in rep['overall']['by_account']} == {'Martin': 2, 'Amina': 1}
    # the viewer's own calendar day, not UTC's
    assert [(d['date'], d['total']) for d in rep['days']] == [('2026-10-04', 1), ('2026-10-03', 2)]
    first = rep['days'][0]['entries'][0]
    assert first['time_local'] == '03:00:00' and first['company'] == 'Britam' and first['at'].endswith('Z')
