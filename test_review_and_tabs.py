"""Tab modules (cross-tab reconciliation), manual entry, the review gate, schema check."""
import json

import pytest

import policy_extract as pe
import tab_modules
import review_api


def _admin(client, db):
    from models import User
    u = User(name='a', email='adm@a.com', role='admin'); u.set_password('x'); db.session.add(u); db.session.commit()
    with client.session_transaction() as s:
        s['user_id'] = u.id
    return u


def _company_with_pay_rows_only(db):
    """The reported failure: the pay tables name every director, but the survey-side reader found nothing."""
    from models import Company, FinancialPeriod, DirectorRemunerationRow
    co = Company(name='Pay Only Co', ticker='POC', sector='Banking'); db.session.add(co); db.session.flush()
    per = FinancialPeriod(company_id=co.id, period_label='FY2025', period_type='FY', fiscal_year=2025)
    db.session.add(per); db.session.flush()
    rows = [('Dr. James Mwangi', 'executive'), ('Mrs. Mary Wamae', 'executive'), ('Prof. Isaac Macharia', 'non_executive'),
            ('Mr. Vijay Gidoomal', 'non_executive'), ('Dr. Helen Gichohi', 'non_executive')]
    for i, (n, role) in enumerate(rows):
        db.session.add(DirectorRemunerationRow(period_id=per.id, director_name=n, role=role, total=100.0 + i, order_index=i))
    db.session.add(DirectorRemunerationRow(period_id=per.id, director_name='Total', total=510.0, is_grand_total=True, order_index=9))
    db.session.commit()
    return co


def test_register_and_composition_are_rebuilt_from_the_pay_tables(client, db):
    from models_survey import SurveyDirector, SurveyCompanyData
    co = _company_with_pay_rows_only(db)
    assert SurveyDirector.query.filter_by(company_id=co.id).count() == 0          # the symptom
    out = tab_modules.reconcile_tabs(co.id, 'FY2025')
    assert out['register_added'] == 5
    reg = {d.director_name: d for d in SurveyDirector.query.filter_by(company_id=co.id)}
    assert reg['Mary Wamae'].role == 'executive' and reg['Mary Wamae'].gender == 'Female'   # Mrs. printed in the pay table
    assert reg['Helen Gichohi'].gender is None                                             # "Dr." says nothing - never guessed
    row = SurveyCompanyData.query.filter_by(company_id=co.id, fiscal_year='FY2025').first()
    assert (row.board_size, row.executive_directors_count, row.non_executive_directors_count) == (5, 2, 3)
    assert row.directors_female is None                                                    # gender not stated for everyone
    again = tab_modules.reconcile_tabs(co.id, 'FY2025')                                    # idempotent
    assert again['register_added'] == 0 and SurveyDirector.query.filter_by(company_id=co.id).count() == 5


def test_a_good_register_is_only_enriched_never_replaced(client, db):
    from models_survey import SurveyDirector
    co = _company_with_pay_rows_only(db)
    for i, n in enumerate(['James Mwangi', 'Mary Wamae', 'Isaac Macharia']):
        db.session.add(SurveyDirector(company_id=co.id, fiscal_year='FY2025', director_name=n, role='unknown', order_index=i))
    db.session.commit()
    out = tab_modules.reconcile_tabs(co.id, 'FY2025')
    assert out['register_added'] == 0 and out['register_enriched'] == 3
    assert {d.director_name: d.role for d in SurveyDirector.query.filter_by(company_id=co.id)}['Mary Wamae'] == 'executive'


def test_tab_status_lists_what_each_tab_is_missing(client, db):
    co = _company_with_pay_rows_only(db)
    st = tab_modules.tab_status(co.id, 'FY2025')
    assert st['directors-register']['missing'] == ['register'] and st['remuneration']['rows'] == 5
    tab_modules.reconcile_tabs(co.id, 'FY2025')
    assert tab_modules.tab_status(co.id, 'FY2025')['directors-register']['rows'] == 5


def test_manual_entry_saves_protects_and_survives_reimport(client, db):
    from models import DirectorRemunerationRow, FinancialPeriod
    from models_survey import SurveyCompanyData
    import app as app_module
    _admin(client, db)
    co = _company_with_pay_rows_only(db)
    r = client.put(f'/api/manual/{co.id}/FY2025/metrics', json={'fields': {'board_size': '9', 'chairperson_annual_retainer': '1,200,000'},
                                                               'note': 'p.88', 'unit': 'units'})
    assert r.status_code == 200
    row = SurveyCompanyData.query.filter_by(company_id=co.id, fiscal_year='FY2025').first()
    assert row.board_size == 9 and row.chairperson_annual_retainer == 1200000.0 and row.director_figures_unit == 'units'
    assert row.field_sources['board_size'] == 'entered manually: p.88' and row.field_review_status['board_size'] == 'corrected'
    # rules never overwrite it: reconcile leaves the typed board size alone
    tab_modules.reconcile_tabs(co.id, 'FY2025')
    assert SurveyCompanyData.query.filter_by(company_id=co.id).first().board_size == 9
    # list editors
    assert client.post(f'/api/manual/{co.id}/FY2025/directors', json={'director_name': 'Jane Doe', 'role': 'non_executive', 'gender': 'Female', 'independent': True}).status_code == 200
    assert client.post(f'/api/manual/{co.id}/FY2025/committees', json={'name': 'Audit Committee', 'meetings_held': 4, 'members': 'Jane Doe, John Roe'}).status_code == 200
    assert client.post(f'/api/manual/{co.id}/FY2025/pay-rows', json={'director_name': 'Jane Doe', 'role': 'non_executive', 'total': '5,000', 'components': {'Fees': 5000}}).status_code == 200
    assert client.put(f'/api/manual/{co.id}/FY2025/benefits', json={'ned_benefits': {'MedicalCover': {'provided': True}, 'Bogus': {'provided': True}}}).status_code == 200
    snap = client.get(f'/api/manual/{co.id}/FY2025').get_json()
    assert [d['director_name'] for d in snap['directors']][-1] == 'Jane Doe' and snap['committees'][0]['members'] == ['Jane Doe', 'John Roe']
    assert list(snap['ned_benefits']) == ['MedicalCover']                               # unknown keys are dropped
    # a re-import replaces the extracted pay rows but never the hand-entered one
    period = FinancialPeriod.query.filter_by(company_id=co.id).first()
    app_module._replaceable_pay_rows(period.id).delete(synchronize_session=False)
    db.session.commit()
    left = DirectorRemunerationRow.query.filter_by(period_id=period.id).all()
    assert [r.director_name for r in left] == ['Jane Doe'] and left[0].table_kind == 'manual'


def test_a_bad_manual_number_is_rejected_with_the_field_name(client, db):
    _admin(client, db)
    co = _company_with_pay_rows_only(db)
    r = client.put(f'/api/manual/{co.id}/FY2025/metrics', json={'fields': {'board_size': 'nine'}})
    assert r.status_code == 400 and 'board_size' in r.get_json()['error']


def test_every_file_is_imported_and_pdfs_also_get_a_review_draft(client, db, monkeypatch):
    from models_ai import UploadDraft
    from app import app
    monkeypatch.setattr(review_api, '_run_preview', lambda *a, **k: None)      # the fake bytes are not a readable PDF
    rest, draft_ids = review_api.gate_payloads(app, [('a.pdf', b'%PDF-1.4 fake'), ('b.txt', b'===COMPANY===\nName: X')], None)
    assert [f for f, _ in rest] == ['a.pdf', 'b.txt']                                   # nothing is held back
    assert set(draft_ids) == {'a.pdf'}                                                  # only the PDF gets a draft
    d = UploadDraft.query.get(draft_ids['a.pdf'])
    assert d.filename == 'a.pdf' and d.pdf_bytes.startswith(b'%PDF')
    rest2, draft_ids2 = review_api.gate_payloads(app, [('a-again.pdf', b'%PDF-1.4 fake')], None)
    assert draft_ids2['a-again.pdf'] == d.id                                            # the same file is not queued twice


def test_schema_check_reports_ok_on_a_complete_schema(client, db):
    _admin(client, db)
    assert client.get('/api/system/schema-check').get_json()['ok'] is True


def test_issues_are_recorded_instead_of_swallowed(client, db):
    from models_ai import ExtractionIssue
    co = _company_with_pay_rows_only(db)
    review_api.record_issue(co.id, 'FY2025', 'r.pdf', 'board / register / committees', ValueError('column x does not exist'))
    i = ExtractionIssue.query.first()
    assert i.step.startswith('board') and 'ValueError' in i.error and i.resolved is False


ABSA_EXEC = """Abdi Mohamed, Managing Director Shs Shs
Base Salary 53 399 838 50 445 591
Retirement benefits 5 339 984 5 044 559
Other employee benefits 22 903 454 22 021 754
Total fixed remuneration 81 643 276 77 511 904
Cash bonus (non-def
erred) 22 855 445 19 801 301
Deferred bonus / Cash Value Plan (CVP) 15 599 748 12 468 699
Total remuneration (cost to company) 120 098 469 109 781 904
Yusuf Omari, Chief Financial Officer Shs Shs
Base Salary 40 447 590 38 000 000
Total remuneration (cost to company) 76 524 814 70 000 000"""


def test_every_executive_block_is_read_not_only_the_ceo(monkeypatch):
    monkeypatch.setattr(pe, '_pypdf_page_texts_cached', lambda _b: [(5, ABSA_EXEC)])
    blocks = pe.extract_executive_pay_blocks(b'')
    assert [b['name'] for b in blocks] == ['Abdi Mohamed', 'Yusuf Omari']
    assert blocks[0]['title'] == 'Managing Director' and blocks[0]['components']['incentive_bonus'] == 22855445
    assert pe.extract_ceo_pay(b'')['name'] == 'Abdi Mohamed'


def _ned_company_with_fee_columns(db, extra_rows=()):
    from models import Company, FinancialPeriod, DirectorRemunerationRow
    co = Company(name='Fee Table Co', ticker='FTC', sector='Banking'); db.session.add(co); db.session.flush()
    per = FinancialPeriod(company_id=co.id, period_label='FY2025', period_type='FY', fiscal_year=2025)
    db.session.add(per); db.session.flush()
    fees = [('Mr. Alan Kamau', 1680), ('Ms. Beth Otieno', 1680), ('Dr. Carl Mwangi', 840), ('Mrs. Dina Wafula', 1680)]
    for i, (n, fee) in enumerate(fees):
        db.session.add(DirectorRemunerationRow(
            period_id=per.id, director_name=n, role='non_executive', total=fee + 400.0, order_index=i,
            components=json.dumps({'Directors\u2019 fees': float(fee), 'Sitting allowance': 400.0, 'Total': fee + 400.0})))
    for j, n in enumerate(extra_rows):
        db.session.add(DirectorRemunerationRow(period_id=per.id, director_name=n, role='executive', total=1.0,
                                               order_index=20 + j))
    db.session.commit()
    return co


def test_ned_retainer_is_filled_from_the_fee_column_and_never_from_sitting_allowances(client, db):
    from models_survey import SurveyCompanyData
    co = _ned_company_with_fee_columns(db)
    tab_modules.reconcile_tabs(co.id, 'FY2025')
    row = SurveyCompanyData.query.filter_by(company_id=co.id, fiscal_year='FY2025').first()
    assert row.other_ned_annual_retainer == 1680.0                    # most common fee, not 400 (sitting) or 2,080 (total)
    assert row.field_confidence['other_ned_annual_retainer'] < 0.6      # a paid fee is a stand-in for the rate: flagged
    assert row.field_sources['other_ned_annual_retainer'].startswith('from the director pay table')
    assert row.chairperson_annual_retainer is None                     # nobody is listed as chair: never guessed


def test_pay_table_fallback_never_overwrites_a_value_from_another_source(client, db):
    from models_survey import SurveyCompanyData
    co = _ned_company_with_fee_columns(db)
    row = tab_modules.survey_row(co.id, 'FY2025', create=True)
    row.other_ned_annual_retainer = 999.0
    row.field_sources = {'other_ned_annual_retainer': 'typed in by hand'}
    db.session.commit()
    tab_modules.reconcile_tabs(co.id, 'FY2025')
    assert SurveyCompanyData.query.filter_by(company_id=co.id).first().other_ned_annual_retainer == 999.0


def test_vote_count_rows_are_not_adopted_as_directors(client, db):
    """Kenya Airways: AGM ballot rows ('Against', 'For', '397') were stored as pay rows and became 'directors'."""
    from models_survey import SurveyDirector
    co = _ned_company_with_fee_columns(db, extra_rows=('Against', 'For', '397', 'Abstain'))
    tab_modules.reconcile_tabs(co.id, 'FY2025')
    names = {d.director_name for d in SurveyDirector.query.filter_by(company_id=co.id)}
    assert not names & {'Against', 'For', '397', 'Abstain'} and len(names) == 4


def test_database_export_has_traceable_metrics_and_no_credentials(client, db, tmp_path):
    import sqlite3
    import db_export
    from models import User
    co = _ned_company_with_fee_columns(db)
    u = User(name='x', email='x@y.com', role='admin'); u.set_password('secret'); db.session.add(u); db.session.commit()
    tab_modules.reconcile_tabs(co.id, 'FY2025')
    path, counts = db_export.export_sqlite_snapshot(str(tmp_path / 'snap.sqlite'))
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'app_users' not in tables and {'company', 'field_trace', 'metric_gaps'} <= tables
    v, src = con.execute("SELECT value, source_note FROM field_trace WHERE metric_key='other_ned_annual_retainer'").fetchone()
    assert v == 1680.0 and src.startswith('from the director pay table')
    gaps = {r[0] for r in con.execute("SELECT metric_key FROM metric_gaps")}
    assert 'chairperson_annual_retainer' in gaps                                   # the gap is listed, with its key
