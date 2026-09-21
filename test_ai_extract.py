"""AI extraction module: everything except the network. A fake client stands in for the
Anthropic SDK, so these tests prove the plumbing (page choice, request shape, grounding
checks, batch flow, apply) - they say NOTHING about how accurate a real model is; that is
measured by eval/ai_eval.py against the gold set with a real key."""
import json
from types import SimpleNamespace as NS

import pytest

import ai_extract as ai

PAGE_113 = """Directors' attendance at scheduled Board and Board Committee meetings
Board        Audit and Risk Committee
Total number of scheduled meetings      4      8
Louis Otieno                            4(4)   8(8)
Fulvio Tonelli                          4(4)   8(8)
Marion Mwangi                           4(4)   6(8)"""

PAY_PAGE = """Directors' remuneration report - year ended 31 December 2025
Non-executive directors                          Total Shs
Charles Muchene                                  8,307,800
Patricia Ithau                                   3,558,000
Total                                            11,865,800"""


def test_page_selection_prefers_the_relevant_pages():
    pages = [(1, "Chairman's statement about our strategy " * 20), (2, PAGE_113 * 3), (3, PAY_PAGE * 3), (4, "Notes to accounts")]
    assert 2 in ai.select_pages(pages, 'committees')
    assert 3 in ai.select_pages(pages, 'pay')
    assert 1 not in ai.select_pages(pages, 'pay') and 4 not in ai.select_pages(pages, 'committees')


def test_request_shape_forces_the_tool_and_carries_page_markers():
    p = ai.build_request('committees', {113: PAGE_113}, 'Absa Bank Kenya', 'FY2025')
    assert p['tool_choice'] == {'type': 'tool', 'name': 'record_findings'}
    assert '=== PAGE 113 ===' in p['messages'][0]['content'] and 'Absa Bank Kenya' in p['messages'][0]['content']
    assert p['tools'][0]['input_schema']['properties']['committees']


def test_cost_estimate_uses_the_batch_discount():
    p = ai.build_request('pay', {3: PAY_PAGE}, 'X', 'FY2025')
    direct = ai.estimate_cost([p], ['pay'], 'claude-sonnet-5', batch=False)
    batch = ai.estimate_cost([p], ['pay'], 'claude-sonnet-5', batch=True)
    assert batch['usd'] == pytest.approx(direct['usd'] / 2, rel=0.01) and direct['usd'] > 0
    assert ai.actual_cost({'input_tokens': 1_000_000, 'output_tokens': 100_000}, 'claude-sonnet-5', batch=False) == 3.0


def test_grounding_rejects_invented_values_and_accepts_printed_ones():
    data = {'tables': [{'title': 'NED pay', 'unit': 'units', 'is_current_year': True, 'printed_total': 11865800, 'rows': [
        {'name': 'Charles Muchene', 'role': 'non_executive', 'total': 8307800, 'page': 3, 'evidence': 'Charles Muchene 8,307,800'},
        {'name': 'Patricia Ithau', 'role': 'non_executive', 'total': 3558000, 'page': 3, 'evidence': 'Patricia Ithau 3,558,000'},
        {'name': 'Invented Person', 'role': 'non_executive', 'total': 999999, 'page': 3, 'evidence': 'Invented Person 999,999'},
        {'name': 'Charles Muchene', 'role': 'non_executive', 'total': 8307801, 'page': 3, 'evidence': 'Charles Muchene 8,307,800'}]}]}
    res = ai.verify_result('pay', data, {3: PAY_PAGE})
    flags = [r['verified'] for r in res['data']['tables'][0]['rows']]
    assert flags == [True, True, False, False]                       # a wrong amount fails even with a real snippet
    assert res['summary']['unverified'] == 2
    assert any(c['check'] == 'rows_sum_to_printed_total' and c['ok'] for c in res['checks'])


def test_a_citation_of_a_page_that_was_not_sent_is_rejected():
    data = {'directors': [{'name': 'Louis Otieno', 'role': 'non_executive', 'page': 99, 'evidence': 'Louis Otieno'}], 'facts': []}
    assert ai.verify_result('board', data, {113: PAGE_113})['data']['directors'][0]['verified'] is False


def _message(tool_input, i=1200, o=300):
    return NS(content=[NS(type='tool_use', name='record_findings', input=tool_input)], usage=NS(input_tokens=i, output_tokens=o))


class FakeClient:
    def __init__(self, tool_input):
        self.tool_input, self.batches = tool_input, {}
        self.messages = NS(create=self._create, batches=NS(create=self._bcreate, retrieve=self._bretrieve, results=self._bresults))

    def _create(self, **params):
        return _message(self.tool_input)

    def _bcreate(self, requests):
        self.batches['b1'] = requests
        return NS(id='b1')

    def _bretrieve(self, bid):
        return NS(processing_status='ended', request_counts=NS(processing=0, succeeded=len(self.batches[bid]), errored=0, canceled=0, expired=0))

    def _bresults(self, bid):
        for r in self.batches[bid]:
            yield NS(custom_id=r['custom_id'], result=NS(type='succeeded', message=_message(self.tool_input)))


def test_batch_submit_and_poll_roundtrip():
    fake = FakeClient({'committees': []})           # one shared instance: the server keeps batch state, not the client
    ai.set_client_factory(lambda: fake)
    try:
        bid = ai.submit_batch([('item-1', ai.build_request('committees', {113: PAGE_113}, 'X', 'FY2025'))])
        polled = ai.poll_batch(bid)
        assert polled['status'] == 'ended' and polled['results']['item-1']['ok'] and polled['results']['item-1']['usage']['input_tokens'] == 1200
    finally:
        ai.set_client_factory(None)


def test_end_to_end_run_collect_apply_writes_only_verified_items(client, db, monkeypatch):
    from app import Company, FinancialPeriod, SurveyCompanyData, SurveyDirector, Committee, AIExtractionItem, User
    co = Company(name='Absa Bank Kenya', ticker='ABSA', sector='Banking')
    db.session.add(co); db.session.flush()
    db.session.add(FinancialPeriod(company_id=co.id, period_label='FY2025', period_type='FY', fiscal_year=2025))
    admin = User(name='a', email='a@a.com', role='admin'); admin.set_password('x'); db.session.add(admin); db.session.flush()
    db.session.add(AIExtractionItem(company_id=co.id, fiscal_year='FY2025', filename='r.pdf', task='committees',
                                    pages_json='[113]', page_text=json.dumps({'113': PAGE_113}), status='pending'))
    db.session.commit()
    with client.session_transaction() as s:
        s['user_id'] = admin.id
    tool = {'board_meetings_held': {'value': 4, 'page': 113, 'evidence': 'Total number of scheduled meetings 4'},
            'committees': [
                {'name': 'Audit and Risk Committee', 'chair': 'Louis Otieno', 'meetings_held': 8, 'page': 113,
                 'evidence': 'Audit and Risk Committee', 'members': [
                     {'name': 'Louis Otieno', 'attended': 8, 'eligible': 8}, {'name': 'Marion Mwangi', 'attended': 6, 'eligible': 8}]},
                {'name': 'Made Up Committee', 'meetings_held': 3, 'page': 113, 'evidence': 'Made Up Committee met three times', 'members': []}]}
    fake = FakeClient(tool)
    ai.set_client_factory(lambda: fake)
    try:
        assert client.get('/api/ai/status').get_json()['items'] == {'pending': 1}
        est = client.post('/api/ai/estimate', json={}).get_json()
        assert est['items'] == 1 and est['batch']['usd'] < est['direct']['usd']
        capped = client.post('/api/ai/run', json={'mode': 'batch', 'max_cost_usd': 0.0000001})
        assert capped.status_code == 400                                             # spend cap enforced before submitting
        job = client.post('/api/ai/run', json={'mode': 'batch'}).get_json()['job']
        got = client.post(f"/api/ai/jobs/{job['id']}/collect").get_json()
        assert got['collected'] == 1 and got['job']['status'] == 'ended' and got['job']['actual_cost_usd'] > 0
        item_id = client.get('/api/ai/items?status=done').get_json()[0]['id']
        assert client.get(f'/api/ai/items/{item_id}').get_json()['summary']['unverified'] == 1
        applied = client.post(f'/api/ai/items/{item_id}/apply').get_json()['applied']
        assert applied['committees'] == 1
        names = [c.name for c in Committee.query.all()]
        assert names == ['Audit and Risk Committee']                                  # the unverified one was not written
        row = SurveyCompanyData.query.filter_by(company_id=co.id, fiscal_year='FY2025').first()
        assert row.board_meetings_per_year == 4 and 'from AI extraction' in row.field_sources['board_meetings_per_year']
    finally:
        ai.set_client_factory(None)


def test_ceo_pay_is_grounded_then_stored_per_month_with_its_derivation(client, db):
    from app import Company, FinancialPeriod, SurveyCompanyData, AIExtractionItem, User, _apply_ai_item
    co = Company(name='Eaagads', ticker='EGAD', sector='Agriculture')
    db.session.add(co); db.session.flush()
    db.session.add(FinancialPeriod(company_id=co.id, period_label='FY2025', period_type='FY', fiscal_year=2025))
    page = "Chief Executive Officer  Salary 12,000  Allowances 1,200  Total 13,200  Shs'000"
    data = {'tables': [], 'ceo': {'name': 'Chief Executive Officer', 'unit': 'thousands', 'is_annual': True, 'salary': 12000,
                                  'allowances': 1200, 'total': 13200, 'page': 5, 'evidence': 'Salary 12,000 Allowances 1,200 Total 13,200'}}
    checked = ai.verify_result('pay', data, {5: page})
    assert checked['data']['ceo']['verified'] is True
    bad = {'tables': [], 'ceo': dict(data['ceo'], salary=99999)}
    assert ai.verify_result('pay', bad, {5: page})['data']['ceo']['verified'] is False       # an amount not on the page
    item = AIExtractionItem(company_id=co.id, fiscal_year='FY2025', task='pay', status='done', page_text='{}',
                            result_json=json.dumps(checked['data']))
    db.session.add(item); db.session.commit()
    applied = _apply_ai_item(item)
    row = SurveyCompanyData.query.filter_by(company_id=co.id, fiscal_year='FY2025').first()
    assert row.ceo_monthly_salary == 1000.0 and row.ceo_monthly_cost_of_employment == 1100.0      # thousands: 12,000 / 12 and 13,200 / 12
    assert row.ceo_annual_salary_as_stated == 12000.0 and row.ceo_monthly_conversion_basis == 'annual / 12'
    assert 'ceo_monthly_salary' in applied['fields']


def _seed_complete_company(db):
    from app import (Company, FinancialPeriod, SurveyCompanyData, SurveyDirector, Committee,
                     DirectorRemunerationRow, User)
    co = Company(name='Full Co', ticker='FULL', sector='Banking'); db.session.add(co); db.session.flush()
    per = FinancialPeriod(company_id=co.id, period_label='FY2025', period_type='FY', fiscal_year=2025)
    db.session.add(per); db.session.flush()
    for i in range(4):
        db.session.add(SurveyDirector(company_id=co.id, fiscal_year='FY2025', director_name=f'Person {i} Name', role='non_executive', order_index=i))
    db.session.add(SurveyCompanyData(company_id=co.id, fiscal_year='FY2025', board_size=4, board_meetings_per_year=4,
                                     executive_directors_count=0, non_executive_directors_count=4,
                                     ned_benefits={'MedicalCover': {'provided': True}}, ceo_monthly_cost_of_employment=1.0))
    db.session.add(Committee(period_id=per.id, name='Audit', meetings_held=4, source_document_id=1))
    db.session.add(DirectorRemunerationRow(period_id=per.id, director_name='Person 0 Name', total=100.0, role='non_executive'))
    db.session.add(DirectorRemunerationRow(period_id=per.id, director_name='Total', total=100.0, is_grand_total=True))
    admin = User(name='a', email='a@a.com', role='admin'); admin.set_password('x'); db.session.add(admin)
    db.session.commit()
    return co, admin


def test_the_ai_is_only_asked_for_gaps_the_rules_left(client, db):
    from app import _compute_ai_gaps, AIExtractionItem
    co, admin = _seed_complete_company(db)
    blobs = {'board': 'independent directors', 'committees': 'attendance', 'pay': 'retainer sitting allowance chief executive medical'}
    gaps = _compute_ai_gaps(co.id, 'FY2025', blobs)
    assert gaps['committees'] == [] and 'register' not in gaps['board'] and 'pay_rows' not in gaps['pay']
    assert gaps['pay'] == ['ned_policy']                                  # the only pay item the rules did not read
    # an item whose gaps are all filled is skipped and never sent, even when the button is pressed
    db.session.add(AIExtractionItem(company_id=co.id, fiscal_year='FY2025', task='committees', pages_json='[113]',
                                    page_text=json.dumps({'113': 'attendance'}), status='pending'))
    db.session.commit()
    with client.session_transaction() as s:
        s['user_id'] = admin.id
    ai.set_client_factory(lambda: FakeClient({'committees': []}))
    try:
        r = client.post('/api/ai/run', json={'mode': 'batch'})
        assert r.status_code == 400 and 'Nothing pending' in r.get_json()['error']
        assert AIExtractionItem.query.first().status == 'skipped'
    finally:
        ai.set_client_factory(None)


def test_ai_never_overwrites_a_value_the_rules_already_have_and_reports_the_disagreement(client, db):
    from app import _apply_ai_item, AIExtractionItem, SurveyCompanyData
    co, _admin = _seed_complete_company(db)
    row = SurveyCompanyData.query.filter_by(company_id=co.id).first()
    row.field_sources = {'board_meetings_per_year': 'from the attendance table on page 9'}
    data = {'board_meetings_held': {'value': 7, 'page': 1, 'evidence': 'x', 'verified': True},
            'facts': [], 'committees': []}
    item = AIExtractionItem(company_id=co.id, fiscal_year='FY2025', task='committees', page_text='{}', status='done',
                            result_json=json.dumps(data))
    db.session.add(item); db.session.commit()
    applied = _apply_ai_item(item)
    assert SurveyCompanyData.query.filter_by(company_id=co.id).first().board_meetings_per_year == 4      # rule value kept
    assert applied['conflicts'] == [{'field': 'board_meetings_per_year', 'kept': 4, 'ai': 7}]
