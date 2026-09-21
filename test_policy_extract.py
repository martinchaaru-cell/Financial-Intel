"""Rule-based readers (tier 1): CEO pay block, NED fee sentence, NED benefits, committee sentences.
Text is copied from the real Absa / Eaagads / NSE pages they were built against."""
import policy_extract as pe


def _pages(monkeypatch, text, pn=5):
    monkeypatch.setattr(pe, '_pypdf_page_texts_cached', lambda _b: [(pn, text)])


ABSA_CEO = """Abdi Mohamed, Managing Director Shs Shs
Base Salary 53 399 838 50 445 591
Retirement benefits 5 339 984 5 044 559
Other employee benefits 22 903 454 22 021 754
Total fixed remuneration 81 643 276 77 511 904
Cash bonus (non-def
erred) 22 855 445 19 801 301
Deferred bonus / Cash Value Plan (CVP) 15 599 748 12 468 699
Total variable remuneration 38 455 193 32 270 000
Total remuneration (cost to company) 120 098 469 109 781 904"""


def test_ceo_block_is_read_with_wrapped_labels_and_current_year_column(monkeypatch):
    _pages(monkeypatch, "Managing Director\n" + ABSA_CEO)
    ceo = pe.extract_ceo_pay(b'')
    c = ceo['components']
    assert ceo['name'] == 'Abdi Mohamed' and ceo['unit'] == 'units'
    assert c['salary'] == 53399838 and c['incentive_bonus'] == 22855445 and c['deferred_incentive'] == 15599748
    assert c['cost_of_employment'] == 120098469                        # not the "total fixed" / "total variable" sub-totals
    assert c['salary'] + c['pension'] + c['non_cash_benefits'] + c['incentive_bonus'] + c['deferred_incentive'] == c['cost_of_employment']


def test_ned_policy_sentence_gives_the_chairman_retainer_and_never_guesses_committee_rates(monkeypatch):
    _pages(monkeypatch, "Non-executive directors fees. The Board Chairman is entitled to an annual retainer of Shs 10,650,400 "
                        "for performing all the duties of the chairman of the board. Audit Committee  Chairman 1 761 800  Member 880 900")
    pol = pe.extract_ned_policy(b'')
    assert pol['chairperson_annual_retainer']['value'] == 10650400
    assert 'committee_chair_annual_retainer' not in pol


def test_benefits_yes_and_no_and_employee_sentences_ignored(monkeypatch):
    _pages(monkeypatch, "Directors' remuneration report. Non-Executive directors have the benefit of indemnity in relation to liability. "
                        "Share Option Scheme The Company does not operate a share option scheme for Directors. "
                        "Core benefits are provided to all employees and include medical insurance and life assurance. "
                        "Long Term Incentives There were no long-term incentives granted to Non-Executive Directors.")
    ben = pe.extract_ned_benefits(b'')
    assert ben['IndemnityInsurance']['provided'] is True
    assert ben['ShareSchemeParticipation']['provided'] is False
    assert 'MedicalCover' not in ben                                    # that sentence is about employees


def test_committee_members_sentence_survives_honorific_periods(monkeypatch):
    _pages(monkeypatch, "The members of the Nominations and Remuneration Committee (the \u201cCommittee\u201d) during the year were "
                        "Ms. Muthoni Runji-Pertet, Mr. George Kapanadze, Mr. Nicholas Kathiari, and Amb. Harry Mutuma Kathurima. "
                        "The Committee is responsible for reviewing remuneration.")
    c = pe.extract_committee_prose(b'')[0]
    assert c['name'] == 'Nominations and Remuneration Committee' and len(c['members']) == 4
