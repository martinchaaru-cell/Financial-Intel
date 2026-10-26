"""Data-quality average (Survey Analytics), the score printed on every Manual Entry PDF, and the summary inside the zip."""
import csv
import io
import zipfile

from pypdf import PdfReader

import data_quality
import manual_entry_form as mef
import manual_entry_upload as meu
import tab_modules

N = len(tab_modules.all_metric_labels())        # 40 hand-enterable survey fields


def _co(db, name, ticker):
    from models import Company
    c = Company(name=name, ticker=ticker, sector='Banking')
    db.session.add(c)
    db.session.commit()
    return c


def _row(db, co, fy='FY2025', conf=None, **vals):
    from models_survey import SurveyCompanyData
    r = SurveyCompanyData(company_id=co.id, fiscal_year=fy, field_confidence=conf or {}, **vals)
    db.session.add(r)
    db.session.commit()
    return r


def _pdf_text(blob):
    return '\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(blob)).pages)


def test_score_rules_match_the_company_data_quality_tab(client, db):
    co = _co(db, 'A Bank', 'AAA')
    row = _row(db, co, board_size=9, board_meetings_per_year=7, market_cap=100.0, ceo_monthly_conversion_basis='annual / 12',
               conf={'board_size': 0.6, 'market_cap': 0.75})
    q = data_quality.company_quality(row)
    assert q['total'] == N
    assert q['needs_review'] == 1                   # board_size, confidence 0.60 < 0.75
    assert q['complete'] == 3                       # board_meetings, market_cap (0.75 is enough), text basis
    assert q['missing'] == N - 4
    assert q['score'] == round(3 / N * 100)
    blank = _row(db, _co(db, 'B Bank', 'BBB'), ceo_monthly_conversion_basis='   ')
    assert data_quality.company_quality(blank)['complete'] == 0            # blank text is missing, not filled
    assert data_quality.company_quality(None)['score'] == 0 and not data_quality.company_quality(None)['has_data']


def test_average_is_mean_of_company_scores_and_lists_unscored(client, db):
    a, b, c = _co(db, 'A Bank', 'AAA'), _co(db, 'B Bank', 'BBB'), _co(db, 'C Bank', 'CCC')
    _row(db, a, board_size=9, board_meetings_per_year=7, market_cap=1.0, turnover=1.0)            # 4/40 = 10%
    _row(db, b, **{k: 1 for k in list(tab_modules.all_metric_labels())[:20]
                   if k != 'ceo_monthly_conversion_basis'})                                          # ~50%
    d = data_quality.portfolio_quality('FY2025')
    assert d['has_data'] and d['companies_counted'] == 2 and d['not_on_file'] == ['C Bank']
    scores = sorted(x['score'] for x in d['companies'])
    assert d['average_score'] == round(sum(scores) / 2)
    assert d['best']['score'] == scores[-1] and d['lowest']['score'] == scores[0]
    assert d['totals']['total'] == 2 * N
    assert {s['section'] for s in d['sections']} >= {'Board Composition', 'Remuneration'}


def test_average_api_and_empty_state(client, db):
    assert client.get('/api/survey/data-quality-average').get_json()['has_data'] is False
    _row(db, _co(db, 'A Bank', 'AAA'), board_size=9)
    d = client.get('/api/survey/data-quality-average?fiscal_year=FY2025').get_json()
    assert d['has_data'] and d['fiscal_year'] == 'FY2025' and d['companies'][0]['name'] == 'A Bank'
    assert client.get('/api/survey/data-quality-average?fiscal_year=FY1999').get_json()['fiscal_year'] == 'FY2025'   # unknown year -> newest


def test_analytics_page_has_the_data_quality_tab():
    html = open('templates/index.html', encoding='utf-8').read()
    assert "{id:'data-quality', label:'Data Quality'}," in html.split('const SA_TABS')[1].split('];')[0]
    assert "saTab === 'data-quality'" in html and '/api/survey/data-quality-average' in html


def test_every_form_pdf_prints_its_data_quality(client, db):
    co = _co(db, 'A Bank', 'AAA')
    row = _row(db, co, board_size=9, market_cap=1.0)
    buf, _ = mef.build_company_form(co.id, 'FY2025')
    txt = _pdf_text(buf.getvalue())
    q = data_quality.company_quality(row)
    assert 'Data quality' in txt and f"{q['score']}%" in txt and 'Board Composition' in txt
    empty = _co(db, 'Empty Co', 'EEE')
    assert '0%' in _pdf_text(mef.build_company_form(empty.id, 'FY2025')[0].getvalue())     # no survey data at all -> still shown


def _form_names(blob):
    from pypdf import PdfReader as R
    return set((R(io.BytesIO(blob)).get_fields() or {}).keys())


def test_zip_is_ranked_from_most_missing_and_forms_still_upload(client, db):
    a, b, c = _co(db, 'A Bank', 'AAA'), _co(db, 'B Bank', 'BBB'), _co(db, 'C Bank', 'CCC')
    _row(db, a, board_size=9, market_cap=1.0, turnover=2.0, net_profit=1.0)        # 4 filled  -> 36 missing
    _row(db, b, board_size=5)                                                      # 1 filled  -> 39 missing
    #                                                                                C: no survey data -> 40 missing
    buf, name = mef.build_all_forms_zip('FY2025')
    z = zipfile.ZipFile(buf)
    names = z.namelist()
    assert names[:2] == ['00_Data_Quality_Summary.pdf', '00_Data_Quality_Summary.csv']
    forms = [n for n in names if n.endswith('.pdf') and not n.startswith('00_')]
    assert [f.split('_Manual_Entry_Form_')[0] for f in forms] == ['01', '02', '03']
    # every form is for the chosen year; companies with data first (most problems, then most missing), no-data last
    assert [('B_Bank' in f, 'A_Bank' in f, 'C_Bank' in f) for f in forms] == [(True, False, False), (False, True, False), (False, False, True)]
    assert all(f.endswith('_FY2025.pdf') for f in forms)
    rows = list(csv.reader(io.StringIO(z.read('00_Data_Quality_Summary.csv').decode('utf-8-sig'))))
    body = [r for r in rows[1:] if r and r[0].isdigit()]
    assert [r[1] for r in body] == ['B Bank', 'A Bank', 'C Bank'] and [int(r[8]) for r in body] == [39, 36, 40]
    assert [r[11] for r in body] == forms                                          # CSV rows point at the matching file
    assert len(body[2][15].split('; ')) == 40                                      # the missing field NAMES are listed
    avg = data_quality.portfolio_quality('FY2025')['average_score']                # average = the 2 companies with data
    assert rows[-1][0].startswith('AVERAGE') and int(rows[-1][10]) == avg
    assert f'{avg}%' in _pdf_text(z.read('00_Data_Quality_Summary.pdf'))
    buf.seek(0)
    prev = meu.preview_upload([(name, buf.getvalue())])                            # summary skipped, all 3 forms read
    assert not prev['notes'], prev['notes']
    assert len(prev['files']) == 3


def test_every_form_has_a_box_for_every_missing_field(client, db):
    """The guarantee behind 'no more manual entry': whatever the data-quality score says is missing, the company's form
    has a fillable box for it - for a partly filled company AND for one with no survey data / no year at all."""
    a, e = _co(db, 'A Bank', 'AAA'), _co(db, 'Empty Co', 'EEE')
    _row(db, a, board_size=9, ceo_monthly_conversion_basis='annual / 12', market_cap=1.0)
    for co, fy in ((a, 'FY2025'), (e, None)):
        d = mef.collect(co.id, fy or mef.latest_year(co.id) or 'No year yet')
        form_keys = {k for _, ms in d['missing_metrics'] for k, _ in ms}
        row = tab_modules.survey_row(co.id, fy) if fy else None
        q = data_quality.company_quality(row)
        want = {k for k, lbl in tab_modules.all_metric_labels().items() if lbl in q['missing_fields']}
        assert want == form_keys, (co.name, want ^ form_keys)
        blob = mef.build_company_form(co.id, fy)[0].getvalue()
        names = _form_names(blob)
        assert all(f'metric|{k}' in names for k in want)
        assert len(want) == q['missing']


def test_report_dropdown_ranking_fewest_missing_first(client, db):
    a, b, c = _co(db, 'A Bank', 'AAA'), _co(db, 'B Bank', 'BBB'), _co(db, 'C Bank', 'CCC')
    _row(db, a, board_size=9, market_cap=1.0)                  # 38 missing
    _row(db, b, board_size=5, market_cap=1.0, turnover=1.0)    # 37 missing
    _row(db, a, fy='FY2024')                                   # older year, all missing - must NOT count (latest year is used)
    r = client.get('/api/survey-report/company-ranking').get_json()
    assert [x['name'] for x in r] == ['B Bank', 'A Bank', 'C Bank']                # C: no data -> 40 missing, last
    assert [x['missing'] for x in r] == [37, 38, 40] and [x['rank'] for x in r] == [1, 2, 3]
    html = open('templates/index.html', encoding='utf-8').read()
    assert '/api/survey-report/company-ranking' in html
    assert "' missing'" not in html and "+ ' \\u2014 '" not in html      # drop-down shows names only, no counts


def test_ties_break_by_name(client, db):
    _row(db, _co(db, 'Zeta', 'ZZZ'), board_size=1)
    _row(db, _co(db, 'Alpha', 'AAA'), board_size=1)
    assert [x['name'] for x in client.get('/api/survey-report/company-ranking').get_json()] == ['Alpha', 'Zeta']