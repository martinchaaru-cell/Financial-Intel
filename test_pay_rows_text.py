"""Tests for pay_rows_text.py: the text-based fallback reader for per-director NED pay tables
(2026-09-28: BOC, Liberty, Sameer, KPLC and Bamburi all print a per-director table the word-position
extractor in pdf_parse.py found nothing in)."""
import pay_rows_text as P

KPLC_TEXT = """Director's remuneration
For the financial years ended 30 June 2025 and 30 June 2024, the Directors\u2019
fees and remuneration are as below:
Year ended 30 June 2025
Salary/
honoraria
Fees Expense
allowances
Total
Shs\u2019000 Shs\u2019000 Shs\u2019000 Shs\u2019000
Executive Director
Dr. Eng. Joseph Siror- MD &
CEO
17,367 - 6,770 24,137
Non-Executive Directors
Joy Brenda Masinde -
Chairman
960 1,000 3,846 5,806
PS, National Treasury - 1,000 - 1,000
Ezekiel Saina - 1,000 4,582 5,582
Mr. Humphrey Muhu           -          - 658 658
18,327 9,000 31,546 58,873
DIRECTORS\u2019 REMUNERATION REPORT (CONTINUED)"""

BOC_TEXT = """Other terms: Non-Executive Directors
The table below outlines the key components of the Non-Executive Directors remuneration packages
during the year
  2025 2024
Name Category Fees
Sitting
Allowance Total Fees
Sitting
allowance Total
     KShs 000  KShs 000  KShs 000  KShs 000  KShs 000  KShs 000
Robert Mbugua Chairman Non-Executive - - - 1,520 280 1,800
Cosima Wetende Non-Executive 1,680 560 2,240 1,680 630 2,310
Steve Maina Non-Executive 1,680 420 2,100 1,680 420 2,100
Joseph Ramashala Non-Executive 840 210 1,050 - - -
Eckhardt Vorster* Chairman Non-Executive 3,110 420 3,530 - - -
Totals   7,310 1,610 8,920 4,880 1,330 6,210"""


def _pages(monkeypatch, text, module=P):
    monkeypatch.setattr(module, '_pypdf_page_texts_cached', lambda b: [(9, text)])


def test_kplc_fee_schedule_style_table_reads_every_director_and_tags_the_chair(monkeypatch):
    import pdf_parse
    monkeypatch.setattr(pdf_parse, '_pypdf_page_texts_cached', lambda b: [(9, KPLC_TEXT)])
    rows = P.extract_ned_pay_rows_text(b'', 'FY2025', page_texts=[(9, KPLC_TEXT)])
    neds = [r for r in rows if r['role'] == 'non_executive' and not r['is_total_row']]
    assert len(neds) == 4
    assert any(r['director_name'] == 'PS, National Treasury' and r['total'] == 1000.0 for r in neds)  # institutional NED seat kept, not dropped as "not a person"
    chair = next(r for r in neds if 'Masinde' in r['director_name'])
    assert '(Chairman)' in chair['director_name'] and chair['total'] == 5806.0
    exe = next(r for r in rows if r['role'] == 'executive')
    assert exe['total'] == 24137.0 and exe['components']['Salary / Honoraria'] == 17367.0
    # the dash after a name ("Joy Brenda Masinde -") must not be read as a data cell
    assert all(r['director_name'].strip()[-1] != '-' for r in rows)


def test_boc_two_year_table_reads_only_the_2025_block_and_treats_a_bare_dash_as_no_figure(monkeypatch):
    rows = P.extract_ned_pay_rows_text(b'', 'FY2025', page_texts=[(9, BOC_TEXT)])
    by_name = {r['director_name']: r for r in rows}
    assert by_name['Cosima Wetende']['total'] == 2240.0                        # 2025 total, not the 2024 figure (2,310)
    assert by_name['Cosima Wetende']['components'] == {'Fees': 1680.0, 'Sitting Allowance': 560.0, 'Total': 2240.0}
    assert by_name['Robert Mbugua (Chairman)']['total'] is None                # retired before 2025: dash, not 0
    assert by_name['Eckhardt Vorster* (Chairman)']['total'] == 3530.0
    total = next(r for r in rows if r['is_total_row'])
    assert total['total'] == 8920.0


def test_a_prior_year_only_block_is_not_read_as_the_target_year(monkeypatch):
    rows = P.extract_ned_pay_rows_text(b'', 'FY2024', page_texts=[(9, KPLC_TEXT)])
    assert rows == []                       # KPLC_TEXT's only block is headed "Year ended 30 June 2025"


def test_fewer_than_three_named_rows_is_not_treated_as_a_director_table(monkeypatch):
    text = "Non-Executive Directors\nSitting Fees\nShs'000 Shs'000\nOnly One 500 200"
    assert P.extract_ned_pay_rows_text(b'', 'FY2025', page_texts=[(9, text)]) == []
