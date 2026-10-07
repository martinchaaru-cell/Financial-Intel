"""Extraction report evidence: the report's text is stored at import/diagnose time, so afterwards every field can be
explained (passed: where the value is in the report; failed: what the report says about the topic) with no PDF to hand."""
import io

import extraction_report as er


def _pdf(pages):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for lines in pages:
        y = 800
        # real report pages carry plenty of text; thin pages would (correctly) be read as a scan
        lines = list(lines) + ['This page of the report also contains ordinary narrative text about the business.'] * 4
        for ln in lines:
            c.drawString(50, y, ln)
            y -= 18
        c.showPage()
    c.save()
    return buf.getvalue()


PAGES = [
    ['Annual Report and Financial Statements 2026', 'Alpha Limited'],
    ['Corporate governance', 'The Board of Directors comprises eleven directors, of whom two are executive directors.',
     'The Board met seven times during the year.'],
    ['Statement of profit or loss', 'Revenue from contracts with customers 1,250,000', 'Profit for the year 210,500'],
    ['Shareholder information', 'Market capitalisation at year end was Ksh 9.8 billion.'],
]


def _seed(db):
    from models import Company
    from models_survey import SurveyCompanyData
    co = Company(name='Alpha Ltd', ticker='ALP')
    db.session.add(co)
    db.session.commit()
    row = SurveyCompanyData(company_id=co.id, fiscal_year='FY2026', source_filename='alpha.pdf', board_size=11,
                            board_meetings_per_year=7, executive_directors_count=4)
    row.field_sources = {'board_size': 'from the board table (page 2)', 'board_meetings_per_year': 'from the document scan (page 2)',
                         'executive_directors_count': 'from the document scan (page 2)'}
    row.field_confidence = {'board_size': 0.9, 'board_meetings_per_year': 0.9, 'executive_directors_count': 0.6}
    db.session.add(row)
    db.session.commit()
    return co.id


def test_text_is_stored_and_explains_without_the_pdf(app, db):
    cid = _seed(db)
    assert er.record_import(cid, 'FY2026', 'alpha.pdf', None, _pdf(PAGES), kind='diagnose')
    assert er.load_pages(cid, 'FY2026') is not None and len(er.load_pages(cid, 'FY2026')) == 4

    # no PDF is passed from here on
    rep = er.build_report(company_id=cid, fiscal_year='FY2026')
    f = rep['files'][0]
    assert f['text_stored'] and f['text_pages'] == 4 and rep['coverage'] == {'files': 1, 'with_text': 1, 'without_text': 0}
    it = {i['field']: i for i in f['fields']}

    # passed: the value is found on the page, beside the right wording
    b = it['board_size']
    assert b['status'] == 'filled' and b['verified'] is True and b['evidence'][0]['page'] == 2
    assert 'Cross-check passed' in b['reason'] and 'page 2' in b['reason']
    assert it['board_meetings_per_year']['verified'] is True

    # passed but the number is not printed anywhere (4 executives was counted, the text says "two") -> says so honestly
    e = it['executive_directors_count']
    assert e['verified'] is False and 'does not appear word-for-word' in e['reason']

    # failed: the topic IS in the report -> pages and the sentence are shown
    mc = it['market_cap']
    assert mc['status'] == 'missing' and mc['reason_code'] == 'topic_found'
    assert mc['evidence'] and mc['evidence'][0]['page'] == 4 and 'Market capitalisation' in mc['evidence'][0]['snippet']
    assert mc['action']

    # failed: the topic is nowhere in the report -> says it was searched and found nothing
    g = it['ceo_monthly_gratuity']
    assert g['reason_code'] == 'not_in_text' and not g['evidence'] and g['action']


def test_old_file_without_text_says_what_to_do(app, db):
    cid = _seed(db)
    f = er.build_report(company_id=cid, fiscal_year='FY2026')['files'][0]
    it = {i['field']: i for i in f['fields']}
    assert f['text_stored'] is False
    assert it['board_size']['verified'] is None and 'not stored yet' in it['board_size']['reason']
    assert 'Run Diagnose once' in it['market_cap']['action']


def test_explain_endpoint_and_big_scopes_skip_evidence(app, client, db):
    from app import User
    cid = _seed(db)
    er.record_import(cid, 'FY2026', 'alpha.pdf', None, _pdf(PAGES), kind='diagnose')
    u = User(name='adm', email='adm@x.com', role='admin')
    u.set_password('secret1')
    db.session.add(u)
    db.session.commit()
    client.post('/api/login', json={'email': 'adm@x.com', 'password': 'secret1'})
    r = client.get(f'/api/extraction-report/explain?company_id={cid}&fiscal_year=FY2026')
    assert r.status_code == 200 and r.get_json()['evidence_loaded'] is True
    assert client.get('/api/extraction-report/explain').status_code == 400
    # a scope bigger than EVIDENCE_MAX_FILES loads no evidence up front
    lite = er.build_report(company_id=cid, fiscal_year='FY2026', evidence=False)['files'][0]
    assert lite['evidence_loaded'] is False and 'evidence' not in lite['fields'][0]
