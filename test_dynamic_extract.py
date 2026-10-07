"""Tests for dynamic_extract.py - synthetic page text, no PDF needed. Each test names the LAYOUT it protects."""
import dynamic_extract as D


def _run(pages, **kw):
    return D.extract_survey_dynamic(pages=pages, fiscal_year='FY2025', company_name='Acme Holdings PLC', **kw)['fields']


def test_stated_sentences_with_word_numbers():
    f = _run(["Board of Directors\nThe Board comprised nine members, of whom one is an Executive Director.\n"
              "During the year the Board held six Board meetings.\n"])
    assert f['board_size']['value'] == 9
    assert f['board_meetings_per_year']['value'] == 6


def test_gender_stat_block_split_over_lines():
    f = _run(["BOARD DEMOGRAPHICS\n7\nMale\n2\nFemale\nThe Board directors met often.\nGender diversity\n"])
    assert (f['directors_male']['value'], f['directors_female']['value']) == (7, 2)


def test_workforce_gender_block_is_not_read_as_board():
    f = _run(["Our employees and staff: gender\n60\nMale\n40\nFemale\nBoard directors are elsewhere\n"])
    assert 'directors_male' not in f


def test_header_splitter_keeps_two_line_columns_together():
    labels = D._split_header_labels(['Salary', 'Fees', 'Pension', 'Bonus', 'Gratuity', 'Expense ', 'allowances ',
                                     'Leave pay and ', 'leave allowance', 'Estimated value ', 'for non-cash ', 'benefits', 'Total'])
    assert len(labels) == 9
    assert [D._classify_label(x) for x in labels][5:] == ['allowances', 'leave', 'noncash', 'total']


def _pay_page(rows):
    hdr = ['DIRECTORS REMUNERATION REPORT', 'Salary', 'Fees', 'Pension', 'Bonus', 'Total',
           'Year ended 31 December 2025', "Shs' 000", "Shs' 000", "Shs' 000", "Shs' 000", "Shs' 000"]
    return '\n'.join(hdr + rows)


def test_pay_table_one_value_per_line_and_ceo_monthly_conversion():
    page = _pay_page(['Dr. Ann Kamau*', '120,000', '-', '60', '30,000', '150,060',
                      'Prof. Bob Otieno', '-', '9,000', '-', '-', '9,000'])
    f = _run([page, ''])
    assert f['ceo_monthly_salary']['value'] == 120_000 * 1000 / 12
    assert f['ceo_monthly_incentive_bonus']['value'] == 30_000 * 1000 / 12
    assert f['ceo_monthly_cost_of_employment']['value'] == 150_060 * 1000 / 12
    assert f['ceo_monthly_conversion_basis']['value'] == 'annual / 12'


def test_pay_row_that_lost_a_dash_is_realigned_using_the_total():
    # 4 tokens instead of 5: the missing dash must be the Fees column of an executive (salary first)
    page = _pay_page(['Dr. Ann Kamau*', '120,000', '60', '30,000', '150,060',
                      'Prof. Bob Otieno', '-', '9,000', '-', '-', '9,000'])
    f = _run([page, ''])
    assert f['ceo_monthly_salary']['value'] == 120_000 * 1000 / 12


def test_last_director_row_running_into_the_total_line_is_trusted_only_if_it_adds_up():
    rows = ['Dr. Ann Kamau*', '120,000', '-', '60', '30,000', '150,060',
            'Prof. Bob Otieno', '-', '9,000', '-', '-', '9,000',
            '120,000', '9,000', '60', '30,000', '159,060']          # grand-total line, no name
    tables = D.extract_pay_tables([_pay_page(rows)], 'FY2025')
    assert [r['name'] for r in tables[0]['rows']] == ['Dr. Ann Kamau', 'Prof. Bob Otieno']


def test_executive_directors_not_paid_fees_sentence_gives_zero_retainer():
    f = _run(["Executive Directors are not entitled to receive fees or allowances for attending meetings.\n"])
    assert f['executive_director_annual_retainer']['value'] == 0.0
    assert f['executive_director_meeting_allowance']['value'] == 0.0


def test_non_executive_sentence_does_not_trigger_the_executive_zero_rule():
    f = _run(["Non-Executive Directors are not entitled to a salary but receive fees of KShs 20,000 per meeting.\n"])
    assert 'executive_director_annual_retainer' not in f


def test_stated_rates():
    f = _run(["The Chairman receives an annual retainer of KShs 2.4 million. "
              "Non-Executive Directors receive a sitting allowance of KShs 20,000 per meeting.\n"])
    assert f['chairperson_annual_retainer']['value'] == 2_400_000
    assert f['other_ned_meeting_allowance']['value'] == 20_000


def test_committee_blocks_count_committees_and_meetings():
    def block(title, ratios):
        return title + '\nCOMMITTEE MEMBERS AND ATTENDANCE\nMeeting Eligibility\n' + '\n'.join(ratios)
    pages = [block('Audit Committee', ['3/3', '5/5', '3/3']), block('THE RISK COMMITTEE', ['2/3', '3/3'])]
    f = _run(pages)
    assert f['committees_per_board']['value'] == 2
    assert f['committee_meetings_per_year']['value'] == 4          # average of 5 and 3


def test_cross_check_male_female_must_match_board_size():
    f = _run(["The Board comprised ten members.\nBOARD DEMOGRAPHICS\n7\nMale\n2\nFemale\nBoard directors\n"])
    assert f['directors_male']['confidence'] <= 0.5          # 7 + 2 != 10: not trusted


def test_nothing_is_invented_when_the_report_is_silent():
    assert _run(["Chairman's statement. We had a good year.", "Notes to the financial statements"]) == {}
