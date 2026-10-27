"""Every field the Data Quality tab can flag as missing must also be enterable on the Manual Entry form, and a
filled form must round-trip (form -> upload preview -> approve -> stored). Regression for the six fields that were
in the survey model and the Data Quality list but not in tab_modules (so the PDF form never offered a box for them)."""
import io

from pypdf import PdfReader, PdfWriter

import manual_entry_form as mef
import manual_entry_upload as meu
import tab_modules

NEW_NUMERIC = ['committee_chair_meeting_allowance', 'committee_member_meeting_allowance', 'ceo_monthly_gratuity',
               'ceo_monthly_share_value', 'ceo_annual_salary_as_stated']
NEW_TEXT = ['ceo_monthly_conversion_basis']


def _company(db):
    from models import Company
    co = Company(name='Form Co', ticker='FCO', sector='Banking')
    db.session.add(co)
    db.session.commit()
    return co


def _field_names(pdf_bytes):
    return set((PdfReader(io.BytesIO(pdf_bytes)).get_fields() or {}).keys())


def _fill(pdf_bytes, values):
    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(pdf_bytes)))
    for p in w.pages:
        w.update_page_form_field_values(p, values, auto_regenerate=False)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def test_every_mapping_field_has_a_tab_module_entry():
    """Keeps the Python side in step with the survey model's Data Quality list (MAPPING_FIELDS in index.html)."""
    import re
    html = open('templates/index.html', encoding='utf-8').read()
    block = html.split('const MAPPING_FIELDS = [', 1)[1].split('\n];', 1)[0]
    fields = set(re.findall(r"field:\s*'([a-z_]+)'", block))
    assert len(fields) == 40, f'MAPPING_FIELDS parsed {len(fields)} fields'
    enterable = set(tab_modules.core_metric_labels())
    assert fields == enterable            # exact: nothing flagged on the page can be un-enterable, nothing enterable is unscored
    assert set(tab_modules.all_metric_labels()) - enterable == set(tab_modules.FIELD_KIND)   # the rest are the extended fields


def test_form_has_a_box_for_each_previously_missing_field(client, db):
    co = _company(db)
    buf, _ = mef.build_company_form(co.id, 'FY2025')
    names = _field_names(buf.getvalue())
    for k in NEW_NUMERIC + NEW_TEXT:
        assert f'metric|{k}' in names and f'metricnote|{k}' in names, k


def test_filled_form_round_trips_including_the_text_field(client, db):
    from models_survey import SurveyCompanyData
    co = _company(db)
    buf, _ = mef.build_company_form(co.id, 'FY2025')
    vals = {f'metric|{k}': v for k, v in zip(NEW_NUMERIC, ['1,500', '1,000', '2,148.1', '0', '85,922'])}
    vals['metric|ceo_monthly_conversion_basis'] = 'annual / 12'
    vals['metricnote|ceo_monthly_gratuity'] = 'p.75 gratuity 25,777k / 12'
    filled = _fill(buf.getvalue(), vals)

    prev = meu.preview_upload([('f.pdf', filled)])
    assert not prev['problems'], prev['problems']
    keys = {c['key'] for c in prev['changes']}
    assert keys == set(NEW_NUMERIC + NEW_TEXT)
    res = meu.apply_changes([dict(c, checked=True) for c in prev['changes']])
    row = SurveyCompanyData.query.filter_by(company_id=co.id, fiscal_year='FY2025').first()
    assert row.ceo_monthly_gratuity == 2148.1 and row.ceo_annual_salary_as_stated == 85922.0
    assert row.ceo_monthly_share_value == 0.0            # an explicit 0 ("no share awards") is data, not "missing"
    assert row.committee_chair_meeting_allowance == 1500.0
    assert row.ceo_monthly_conversion_basis == 'annual / 12'
    st = tab_modules.tab_status(co.id, 'FY2025')['remuneration']['missing']
    assert not set(NEW_NUMERIC + NEW_TEXT) & set(st)


def test_a_filled_field_no_longer_appears_on_the_next_form(client, db):
    from models_survey import SurveyCompanyData
    co = _company(db)
    db.session.add(SurveyCompanyData(company_id=co.id, fiscal_year='FY2025', ceo_monthly_gratuity=10.0,
                                     ceo_monthly_conversion_basis='annual / 12'))
    db.session.commit()
    names = _field_names(mef.build_company_form(co.id, 'FY2025')[0].getvalue())
    assert 'metric|ceo_monthly_gratuity' not in names and 'metric|ceo_monthly_conversion_basis' not in names
    assert 'metric|ceo_monthly_share_value' in names


def test_text_field_rejects_junk_on_the_manual_page_only_when_numeric(client, db):
    from models_survey import SurveyCompanyData
    from test_review_and_tabs import _admin
    co = _company(db)
    _admin(client, db)
    r = client.put(f'/api/manual/{co.id}/FY2025/metrics',
                   json={'fields': {'ceo_monthly_conversion_basis': 'annual / 12', 'ceo_monthly_gratuity': '2,148.1'}})
    assert r.status_code == 200
    row = SurveyCompanyData.query.filter_by(company_id=co.id, fiscal_year='FY2025').first()
    assert row.ceo_monthly_conversion_basis == 'annual / 12' and row.ceo_monthly_gratuity == 2148.1
    bad = client.put(f'/api/manual/{co.id}/FY2025/metrics', json={'fields': {'ceo_monthly_gratuity': 'lots'}})
    assert bad.status_code == 400


def test_manual_page_snapshot_lists_the_new_fields_and_flags_the_text_one(client, db):
    from test_review_and_tabs import _admin
    co = _company(db)
    _admin(client, db)
    client.put(f'/api/manual/{co.id}/FY2025/metrics', json={'fields': {'ceo_monthly_conversion_basis': 'annual / 12'}})
    snap = client.get(f'/api/manual/{co.id}/FY2025').get_json()
    ms = {m['key']: m for g in snap['groups'] for m in g['metrics']}
    for k in NEW_NUMERIC + NEW_TEXT:
        assert k in ms, k
    assert ms['ceo_monthly_conversion_basis']['text'] is True and ms['ceo_monthly_conversion_basis']['value'] == 'annual / 12'
    assert ms['ceo_monthly_gratuity']['text'] is False
