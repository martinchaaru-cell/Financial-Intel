"""Restoring a forms zip into another installation: forms are matched to their company by name / ticker, never by the
internal number alone, and companies that are missing can be added on approval."""
import manual_entry_form as mef
import manual_entry_upload as meu


def _setup_source(db):
    from models import Company
    from models_survey import SurveyCompanyData
    a = Company(name='Alpha Bank Plc', ticker='ALP', sector='Banking')
    b = Company(name='Beta Insurance', ticker='BTA', sector='Insurance')
    db.session.add_all([a, b])
    db.session.commit()
    db.session.add_all([SurveyCompanyData(company_id=a.id, fiscal_year='FY2025', board_size=9, currency='KES', unit='millions'),
                        SurveyCompanyData(company_id=b.id, fiscal_year='FY2025', board_size=7, currency='KES', unit='millions')])
    db.session.commit()
    zbuf, _ = mef.build_all_forms_zip('FY2025')
    return zbuf.getvalue(), {a.id: a.name, b.id: b.name}


def _fresh(db):
    db.session.remove()
    db.drop_all()
    db.create_all()


def _board(name):
    from models import Company
    from models_survey import SurveyCompanyData
    c = Company.query.filter_by(name=name).first()
    r = SurveyCompanyData.query.filter_by(company_id=c.id, fiscal_year='FY2025').first() if c else None
    return r.board_size if r else None


def test_new_installation_forms_never_land_on_the_company_that_has_their_number(client, db):
    from models import Company
    blob, _ = _setup_source(db)
    _fresh(db)
    g = Company(name='Gamma Ltd', ticker='GAM')            # takes internal number 1 - Alpha's number in the old app
    db.session.add(g)
    db.session.commit()
    prev = meu.preview_upload([('forms.zip', blob)], replace=True)
    assert prev['changes'] == []
    assert any('not in this app' in n or 'belongs to "Gamma Ltd"' in n for n in prev['notes'])
    assert any('Add companies that are not in the app' in n for n in prev['notes'])


def test_missing_companies_are_added_on_approval_and_values_restored(client, db):
    from models import Company
    blob, _ = _setup_source(db)
    _fresh(db)
    db.session.add(Company(name='Gamma Ltd', ticker='GAM'))
    db.session.commit()
    prev = meu.preview_upload([('forms.zip', blob)], replace=True, add_missing=True)
    assert sorted(n['name'] for n in prev['new_companies']) == ['Alpha Bank Plc', 'Beta Insurance']
    assert all(c['company_id'] < 0 and c['new_company'] for c in prev['changes'])
    assert Company.query.count() == 1                      # preview writes nothing
    res = meu.apply_changes([dict(c, checked=True) for c in prev['changes']])
    assert all(r['ok'] for r in res['results']), res
    assert sorted(x['name'] for x in res['companies_added']) == ['Alpha Bank Plc', 'Beta Insurance']
    assert _board('Alpha Bank Plc') == 9 and _board('Beta Insurance') == 7 and _board('Gamma Ltd') is None
    alpha = Company.query.filter_by(name='Alpha Bank Plc').first()
    assert alpha.ticker == 'ALP' and alpha.sector == 'Banking' and alpha.slug
    # uploading the same zip again adds no second copy of either company
    again = meu.preview_upload([('forms.zip', blob)], replace=True, add_missing=True)
    assert again['new_companies'] == []
    assert Company.query.count() == 3


def test_companies_already_present_under_other_numbers_are_matched_by_name(client, db):
    from models import Company
    blob, _ = _setup_source(db)
    _fresh(db)
    db.session.add_all([Company(name='Beta Insurance Limited'), Company(name='ALPHA BANK')])   # reversed order, other spelling
    db.session.commit()
    prev = meu.preview_upload([('forms.zip', blob)], replace=True)
    assert prev['new_companies'] == [] and prev['changes']
    meu.apply_changes([dict(c, checked=True) for c in prev['changes']])
    assert _board('ALPHA BANK') == 9 and _board('Beta Insurance Limited') == 7


def test_same_installation_still_matches_by_number(client, db):
    blob, ids = _setup_source(db)
    prev = meu.preview_upload([('forms.zip', blob)], replace=True)
    assert prev['new_companies'] == []
    assert not [n for n in prev['notes'] if 'matched' in n or 'not in this app' in n]


def test_name_matching_rules():
    assert meu.norm_name('I&M Group') == meu.norm_name('I and M group')
    assert meu.loose_name('Equity Group Holdings Plc') == meu.loose_name('Equity')
    assert meu.loose_name('Standard Group') != meu.loose_name('Standard Chartered Bank Kenya')


# ------------------------------------------------------------------ v4: every row of every table is on the form
import io
import zipfile

from pypdf import PdfReader, PdfWriter


def _full_source(db):
    from models import Company, DirectorRemunerationRow, Committee, CommitteeMember
    from models_survey import SurveyCompanyData, SurveyDirector
    import tab_modules
    co = Company(name='Delta Holdings Plc', ticker='DLT', sector='Investment')
    db.session.add(co)
    db.session.commit()
    db.session.add(SurveyCompanyData(company_id=co.id, fiscal_year='FY2025', board_size=3, currency='KES', unit='millions',
                                     ned_benefits={'MedicalCover': {'provided': True, 'detail': 'p.40'},
                                                   'ClubMembership': {'provided': False, 'detail': None}}))
    for i, (n, role, ind) in enumerate([('Jane Mwangi', 'non_executive', True), ('Peter Otieno', 'executive', False),
                                        ('Ann Kamau', 'non_executive', False)]):
        db.session.add(SurveyDirector(company_id=co.id, fiscal_year='FY2025', director_name=n, role=role, independent=ind,
                                      gender='Female' if n != 'Peter Otieno' else 'Male', nationality='Kenyan',
                                      board_meetings_attended=4 + i, board_meetings_eligible=6, order_index=i))
    p = tab_modules.ensure_period(co.id, 'FY2025')
    c1 = Committee(period_id=p.id, name='Audit Committee', chairperson_name='Jane Mwangi', meetings_held=5, order_index=0)
    c2 = Committee(period_id=p.id, name='Nominations Committee', chairperson_name=None, meetings_held=2, order_index=1)
    db.session.add_all([c1, c2])
    db.session.flush()
    for j, m in enumerate(['Jane Mwangi', 'Ann Kamau']):
        db.session.add(CommitteeMember(committee_id=c1.id, director_name=m, order_index=j))
    db.session.add_all([
        DirectorRemunerationRow(period_id=p.id, director_name='Jane Mwangi', role='non_executive', total=3200.0,
                                components='{"Fees": 2000.0, "Sitting allowance": 1200.0}', order_index=0),
        DirectorRemunerationRow(period_id=p.id, director_name='Peter Otieno', role='executive', total=45000.5,
                                components='{"Salary": 40000.0, "Bonus": 5000.5}', order_index=1),
        DirectorRemunerationRow(period_id=p.id, director_name='Total', role='unknown', total=48200.5, is_grand_total=True,
                                order_index=2)])
    db.session.commit()
    return co


def _snapshot(name):
    """Everything about a company's FY2025 that a form carries, keyed by names (not row numbers)."""
    from models import Company, DirectorRemunerationRow, Committee, CommitteeMember, FinancialPeriod
    from models_survey import SurveyCompanyData, SurveyDirector
    co = Company.query.filter_by(name=name).first()
    row = SurveyCompanyData.query.filter_by(company_id=co.id, fiscal_year='FY2025').first()
    p = FinancialPeriod.query.filter_by(company_id=co.id, period_label='FY2025').first()
    dirs = sorted((d.director_name, d.role, d.independent, d.gender, d.nationality, d.board_meetings_attended,
                   d.board_meetings_eligible) for d in SurveyDirector.query.filter_by(company_id=co.id, fiscal_year='FY2025'))
    coms = sorted((c.name, c.chairperson_name, c.meetings_held,
                   tuple(m.director_name for m in CommitteeMember.query.filter_by(committee_id=c.id).order_by(CommitteeMember.order_index)))
                  for c in Committee.query.filter_by(period_id=p.id)) if p else []
    pay = sorted((r.director_name, r.role, r.total, bool(r.is_grand_total), bool(r.is_total_row), meu.load_components(r.components) and
                  tuple(sorted(meu.load_components(r.components).items())))
                 for r in DirectorRemunerationRow.query.filter_by(period_id=p.id)) if p else []
    return {'board_size': row.board_size, 'ben': {k: (v['provided'], v.get('detail')) for k, v in (row.ned_benefits or {}).items()},
            'dirs': dirs, 'coms': coms, 'pay': pay}


def test_a_full_year_is_restored_into_an_empty_installation(client, db):
    _full_source(db)
    before = _snapshot('Delta Holdings Plc')
    blob = mef.build_all_forms_zip('FY2025')[0].getvalue()
    _fresh(db)
    prev = meu.preview_upload([('forms.zip', blob)], replace=True, add_missing=True)
    assert not prev['problems'], prev['problems']
    res = meu.apply_changes([dict(c, checked=True) for c in prev['changes']])
    assert all(r['ok'] for r in res['results']), [r for r in res['results'] if not r['ok']]
    assert _snapshot('Delta Holdings Plc') == before
    # a second upload of the same zip changes nothing
    again = meu.preview_upload([('forms.zip', blob)], replace=True, add_missing=True)
    assert again['new_companies'] == []
    assert [c for c in again['changes'] if c['op'] != 'replace'] == [], [c['label'] for c in again['changes']]


def _form_values(pdf):
    return {k: (v.get('/V') if isinstance(v, dict) else v) for k, v in (PdfReader(io.BytesIO(pdf)).get_fields() or {}).items()}


def _edit(pdf, values):
    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(pdf)))
    for pg in w.pages:
        w.update_page_form_field_values(pg, values, auto_regenerate=False)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def test_corrections_and_removals_of_table_rows(client, db):
    from models import Committee, DirectorRemunerationRow
    co = _full_source(db)
    pdf = mef.build_company_form(co.id, 'FY2025')[0].getvalue()
    names = set(_form_values(pdf))
    audit = Committee.query.filter_by(name='Audit Committee').first()
    nom = Committee.query.filter_by(name='Nominations Committee').first()
    jane = DirectorRemunerationRow.query.filter_by(director_name='Jane Mwangi').first()
    assert {f'comname|{audit.id}', f'comfix|{audit.id}|chair', f'com|{nom.id}|chair', f'payname|{jane.id}',
            f'payfix|{jane.id}|components', 'benv|MedicalCover', 'benv|TelephoneAllowance'} <= names
    filled = _edit(pdf, {f'comfix|{audit.id}|meetings': '6', f'comname|{nom.id}': '',
                         f'payfix|{jane.id}|components': 'Fees=2,500; Sitting allowance=1,200', f'payfix|{jane.id}|total': '3,700',
                         'benv|ClubMembership': 'Provided'})
    # without Replace: corrections only - the emptied committee name does not remove anything
    prev = meu.preview_upload([('f.pdf', filled)])
    ops = sorted(c['op'] for c in prev['changes'])
    assert ops == ['ben', 'com_update', 'pay_update'], [(c['op'], c['label']) for c in prev['changes']]
    assert all(c['checked'] for c in prev['changes'])
    # with Replace: the committee whose name was emptied is removed as well
    prev = meu.preview_upload([('f.pdf', filled)], replace=True)
    assert 'com_delete' in {c['op'] for c in prev['changes']}
    res = meu.apply_changes([dict(c, checked=True) for c in prev['changes']])
    assert all(r['ok'] for r in res['results']), res
    snap = _snapshot('Delta Holdings Plc')
    assert [c[0] for c in snap['coms']] == ['Audit Committee'] and snap['coms'][0][2] == 6
    jane_pay = [p for p in snap['pay'] if p[0] == 'Jane Mwangi'][0]
    assert jane_pay[2] == 3700.0 and dict(jane_pay[5]) == {'Fees': 2500.0, 'Sitting allowance': 1200.0}
    assert snap['ben']['ClubMembership'][0] is True


def test_an_unchanged_form_proposes_nothing_for_the_tables(client, db):
    co = _full_source(db)
    pdf = mef.build_company_form(co.id, 'FY2025')[0].getvalue()
    prev = meu.preview_upload([('f.pdf', pdf)])
    assert prev['changes'] == [], [(c['op'], c['label'], c.get('current'), c.get('new')) for c in prev['changes']]
