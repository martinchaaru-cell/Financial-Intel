"""Pure-function tests for board_extract (no PDF needed). The table parsers
themselves were verified against Absa Bank Kenya's 2022-2025 reports."""
from board_extract import (derive_board_composition, profile_cards_look_complete, same_person,
                           _normalise_register_row, _card_role)


def _row(name, category, age, gender, nationality, page=45):
    return _normalise_register_row(0, name, {'category': category, 'age': str(age), 'gender': gender,
                                             'nationality': nationality, 'date': '1 June 2010'}, page)


def test_register_row_reads_category_age_gender():
    r = _row('Fulvio Tonelli', 'Non-Executive', 62, 'Male', 'South African')
    assert (r['role'], r['independent'], r['age'], r['gender']) == ('non_executive', False, 62, 'Male')
    r = _row('Charles Muchene', 'Independent Non-Executive', 65, 'Male', 'Kenyan')
    assert (r['role'], r['independent']) == ('non_executive', True)
    r = _row('Yusuf Omari', 'Executive', 48, 'Male', 'Kenyan')
    assert (r['role'], r['independent']) == ('executive', None)


def test_composition_counts_only_what_every_row_states():
    rows = [_row('A One', 'Independent Non-Executive', 60, 'Female', 'Kenyan'),
            _row('B Two', 'Non-Executive', 50, 'Male', 'South African'),
            _row('C Three', 'Executive', 40, 'Male', 'Kenyan')]
    f = derive_board_composition(rows)['fields']
    assert f['board_size'] == 3 and f['executive_directors_count'] == 1 and f['non_executive_directors_count'] == 2
    assert f['independent_neds_count'] == 1 and f['non_independent_neds_count'] == 1
    assert f['directors_female'] == 1 and f['directors_male'] == 2
    assert f['neds_kenyan_count'] == 1 and f['neds_non_kenyan_count'] == 1
    assert f['avg_age_executive_directors'] == 40.0


def test_a_missing_gender_means_no_gender_count_rather_than_a_wrong_one():
    rows = [_row('A One', 'Executive', 40, 'Female', 'Kenyan'), _row('B Two', 'Executive', 41, '', 'Kenyan'),
            _row('C Three', 'Executive', 42, 'Male', 'Kenyan')]
    f = derive_board_composition(rows)['fields']
    assert 'directors_female' not in f and 'directors_male' not in f and f['board_size'] == 3


def test_same_person_tolerates_middle_names_and_hyphens():
    assert same_person('Louis Otieno', 'Louis Onyango Otieno')
    assert same_person('Marion Mwangi', 'Marion Gathoga-Mwangi')
    assert not same_person('Charles Muchene', 'Charles Murito')


def test_profile_cards_only_trusted_when_they_look_complete():
    cards = [{'director_name': n} for n in ('Louis Onyango Otieno', 'Fulvio Tonelli', 'Marion Gathoga-Mwangi')]
    roster = ['Louis Otieno', 'Fulvio Tonelli', 'Marion Mwangi', 'Charles Muchene', 'Patricia Ithau']
    assert not profile_cards_look_complete(cards, roster)                       # 3 of 5 < 75%
    assert profile_cards_look_complete(cards + [{'director_name': 'Charles Muchene'}], roster)
    assert not profile_cards_look_complete(cards + [{'director_name': 'Somebody Else'}], roster)


def test_card_titles():
    assert _card_role('Chairman and Independent Non-executive director') == ('non_executive', True)
    assert _card_role('Executive Director and Chief Financial Officer')[0] == 'executive'
    assert _card_role('Managing Director and Chief Executive Officer')[0] == 'executive'


# ---- round 3: other companies' layouts -------------------------------------------------
from board_extract import _split_honorific_and_name, _role_from_title


def test_names_lose_honorifics_credentials_and_keep_only_mr_ms_as_gender():
    assert _split_honorific_and_name("Ms. Muthoni Runji-Pertet") == ('Female', 'Muthoni Runji-Pertet')
    assert _split_honorific_and_name("DR. PETER NDEGWA (CBS)") == (None, 'Peter Ndegwa')          # Dr. says nothing about gender
    assert _split_honorific_and_name("Dr. Helen Gichohi MBS") == (None, 'Helen Gichohi')
    assert _split_honorific_and_name("ADIL ARSHED KHAWAJA (MGH)") == (None, 'Adil Arshed Khawaja')
    assert _split_honorific_and_name("Mr. Kiprono Kittony, EBS")[0] == 'Male'


def test_titles_to_role_and_independence():
    assert _role_from_title("Independent, Non-Executive Director") == ('non_executive', True)
    assert _role_from_title("Non \u2013 Independent Director") == ('unknown', False)
    assert _role_from_title("GROUP CHIEF EXECUTIVE OFFICER")[0] == 'executive'
    assert _role_from_title("ALTERNATE DIRECTOR TO CEO")[0] == 'unknown'       # not the CEO himself
    assert _role_from_title("Chairperson Non-Executive Director")[0] == 'non_executive'


def test_same_person_tolerates_a_misspelling_printed_in_the_report():
    assert same_person("Kedibone Imahtu", "Kedibone Imathiu")
    assert same_person("Japhet Olende", "Japheth Olende")
    assert not same_person("Charles Muchene", "Charles Murito")


def test_pdf_parse_helpers_for_other_filers():
    from pdf_parse import (detect_interim_period_label, detect_prior_period_label, detect_statement_unit,
                           match_canonical_label, _coerce_amount)
    cover = [(1, "Kakuzi Plc Interim Financial Statements For the period of six months to 30 June 2025")]
    assert detect_interim_period_label(cover) == 'H1 2025' and detect_prior_period_label('H1 2025') == 'H1 2024'
    assert detect_interim_period_label([(1, "2024 Integrated Report"), (2, "our half-year results")]) is None
    assert detect_statement_unit([(1, "Notes Shs\u2019000 Shs\u2019000\nNotes Shs\u2019000 Shs\u2019000\nShs'000")]) == 'thousands'
    assert detect_statement_unit([(1, "Notes Shs\u2019million Shs\u2019million\nShs\u2019million\nKShs million")]) == 'millions'
    assert match_canonical_label('income_statement', 'Sales') == 'revenue'
    assert match_canonical_label('income_statement', 'Cost of sales') != 'revenue'
    assert [_coerce_amount(x) for x in ('328,920', '(1,234)', '-', 12)] == [328920.0, -1234.0, None, 12.0]
