"""Extended survey fields: stored, offered on the Manual Entry form with the right units, round-trip through an upload
(including Replace), per-director attendance and holdings, and kept out of the core data-quality score."""
import io

from pypdf import PdfReader, PdfWriter

import data_quality
import manual_entry_form as mef
import manual_entry_upload as meu
import tab_modules
from models import Company
from models_survey import SurveyCompanyData, SurveyDirector


def _company(db, with_row=True):
    co = Company(name='Field Co', ticker='FLD', sector='Banking')
    db.session.add(co)
    db.session.commit()
    if with_row:
        db.session.add(SurveyCompanyData(company_id=co.id, fiscal_year='FY2025', currency='KES', unit='millions',
                                         director_figures_unit='thousands', board_size=9))
        db.session.add(SurveyDirector(company_id=co.id, fiscal_year='FY2025', director_name='Jane Mwangi', role='non_executive', order_index=0))
        db.session.add(SurveyDirector(company_id=co.id, fiscal_year='FY2025', director_name='Paul Otieno', role='executive', order_index=1))
        db.session.commit()
    return co


def _fields(pdf_bytes):
    return {k: (v.get('/V') or '') for k, v in (PdfReader(io.BytesIO(pdf_bytes)).get_fields() or {}).items()}


def _fill(pdf_bytes, values):
    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(pdf_bytes)))
    for p in w.pages:
        w.update_page_form_field_values(p, values, auto_regenerate=False)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def test_registry_has_35_extended_fields_with_units():
    ext = [k for m in tab_modules.TAB_MODULES if m.get('extended') for k, _ in list(m['metrics']) + list(m.get('text_metrics', []))]
    assert len(ext) == 35 and set(ext) == set(tab_modules.FIELD_KIND)
    assert len(tab_modules.core_metric_labels()) == 40
    for k in ext:
        assert hasattr(SurveyCompanyData, k), k
    assert tab_modules.field_kind('kmp_total_compensation') == 'pay' and tab_modules.field_kind('staff_costs') == 'headline'
    assert tab_modules.normalise_yes_no(' y ') == 'Yes' and tab_modules.normalise_yes_no('No.') == 'No'
    assert tab_modules.normalise_yes_no('Partly') == 'Partly'


def test_form_offers_the_new_boxes_with_units(client, db):
    co = _company(db)
    pdf = mef.build_company_form(co.id, 'FY2025')[0].getvalue()
    names = set(_fields(pdf))
    for k in ('kmp_total_compensation', 'audit_fee', 'auditor_name', 'chair_ceo_separate', 'employees_count', 'dividend_per_share'):
        assert f'metric|{k}' in names, k
    d = SurveyDirector.query.filter_by(director_name='Jane Mwangi').one()
    for a in ('board_meetings_attended', 'board_meetings_eligible', 'other_directorships', 'shares_held'):
        assert f'dirx|{d.id}|{a}' in names
    row = SurveyCompanyData.query.one()
    assert mef._unit_of(row, 'kmp_total_compensation') == 'KES thousands'
    assert mef._unit_of(row, 'audit_fee') == 'KES millions'
    assert mef._unit_of(row, 'earnings_per_share') == 'KES per share'
    assert mef._unit_of(row, 'board_attendance_pct') == '%'


def test_new_fields_round_trip_through_an_upload(client, db):
    co = _company(db)
    pdf = mef.build_company_form(co.id, 'FY2025')[0].getvalue()
    jane = SurveyDirector.query.filter_by(director_name='Jane Mwangi').one()
    vals = {'metric|kmp_count': '7', 'metric|kmp_total_compensation': '412,350', 'metric|audit_fee': '48.2',
            'metric|auditor_name': 'KPMG Kenya', 'metric|chair_ceo_separate': 'y', 'metric|going_concern_uncertainty': 'no',
            'metric|employees_count': '6,241', 'metric|women_in_workforce_pct': '47.5', 'metric|earnings_per_share': '7.62',
            f'dirx|{jane.id}|board_meetings_attended': '5', f'dirx|{jane.id}|board_meetings_eligible': '6',
            f'dirx|{jane.id}|shares_held': '12,500'}
    prev = meu.preview_upload([('f.pdf', _fill(pdf, vals))])
    assert not prev['problems'], prev['problems']
    meu.apply_changes([dict(c, checked=True) for c in prev['changes']])
    row = SurveyCompanyData.query.one()
    assert row.kmp_count == 7 and row.kmp_total_compensation == 412350 and row.audit_fee == 48.2
    assert row.auditor_name == 'KPMG Kenya' and row.chair_ceo_separate == 'Yes' and row.going_concern_uncertainty == 'No'
    assert row.employees_count == 6241 and row.women_in_workforce_pct == 47.5 and row.earnings_per_share == 7.62
    jane = db.session.get(SurveyDirector, jane.id)
    assert (jane.board_meetings_attended, jane.board_meetings_eligible, jane.shares_held) == (5, 6, 12500.0)
    assert jane.other_directorships is None


def test_replace_sets_new_fields_and_leaves_older_forms_harmless(client, db):
    co = _company(db)
    jane = SurveyDirector.query.filter_by(director_name='Jane Mwangi').one()
    pdf = mef.build_company_form(co.id, 'FY2025')[0].getvalue()
    vals = {'metric|audit_fee': '50', f'dirx|{jane.id}|board_meetings_attended': '4', f'dirx|{jane.id}|board_meetings_eligible': '6'}
    prev = meu.preview_upload([('f.pdf', _fill(pdf, vals))], replace=True)
    assert not prev['problems'], prev['problems']
    meu.apply_changes([dict(c, checked=True) for c in prev['changes']])
    row = SurveyCompanyData.query.one()
    jane = db.session.get(SurveyDirector, jane.id)
    assert row.audit_fee == 50 and jane.board_meetings_attended == 4 and jane.board_meetings_eligible == 6
    assert row.board_size == 9                                   # pre-filled and unchanged: kept

    # an older form (no extended boxes at all) replacing the year must not wipe the new fields
    old_values = {k: v for k, v in _fields(pdf).items() if not k.startswith('dirx|') and not any(
        k == f'metric|{x}' or k == f'metricnote|{x}' for x in tab_modules.FIELD_KIND)}
    ch, _ = meu.replace_change(co.id, 'FY2025', {k: str(v) for k, v in old_values.items() if k != 'meta'}, 'old.pdf', co.name)
    if ch:
        meu.apply_changes([dict(ch, id='c0', checked=True)])
    row = SurveyCompanyData.query.one()
    jane = db.session.get(SurveyDirector, jane.id)
    assert row.audit_fee == 50 and jane.board_meetings_attended == 4


def test_bad_attendance_number_is_reported_not_stored(client, db):
    co = _company(db)
    jane = SurveyDirector.query.filter_by(director_name='Jane Mwangi').one()
    pdf = mef.build_company_form(co.id, 'FY2025')[0].getvalue()
    prev = meu.preview_upload([('f.pdf', _fill(pdf, {f'dirx|{jane.id}|board_meetings_attended': 'five'}))])
    assert any('not a number' in p for p in prev['problems'])


def test_extended_fields_do_not_change_the_quality_score(client, db):
    co = _company(db)
    row = SurveyCompanyData.query.one()
    before = data_quality.company_quality(row)
    assert before['total'] == 40
    row.audit_fee, row.auditor_name = 10.0, 'EY'
    db.session.commit()
    assert data_quality.company_quality(row)['score'] == before['score']


def test_company_report_returns_the_new_fields(signed_in, db):
    co = _company(db)
    row = SurveyCompanyData.query.one()
    row.employees_count, row.audit_opinion = 1200, 'Unmodified'
    d = SurveyDirector.query.filter_by(director_name='Jane Mwangi').one()
    d.board_meetings_attended, d.board_meetings_eligible = 6, 6
    db.session.commit()
    body = signed_in.get(f'/api/companies/{co.id}/survey-report?fiscal_year=FY2025').get_json()
    assert body['survey']['employees_count'] == 1200 and body['survey']['audit_opinion'] == 'Unmodified'
    jane = next(x for x in body['survey_director_rows'] if x['director_name'] == 'Jane Mwangi')
    assert jane['board_meetings_attended'] == 6 and jane['board_meetings_eligible'] == 6
