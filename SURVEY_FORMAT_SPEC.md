# FinSight Survey Data Format (v1)

One plain-text file per company, per fiscal year. Feeds the Survey page
ONLY — completely separate from CONDENSED_FORMAT_SPEC.md (which feeds a
company's own financial-statement/director-remuneration-table pages).
A company can have one, both, or neither.

Same discipline as the financial condensed format: every field is
optional except COMPANY.Name and PERIOD.FiscalYear. Missing data is
omitted, never filled with a guess, zero, or placeholder. The Survey
page's cross-company averages skip a company for any metric it didn't
supply, and never present a 1-company or 2-company "average" as if it
were a market benchmark — see the app's own aggregation logic for the
minimum-company-count rule.

## Grammar

    ===SECTION===
    Field Label: value

Lines starting with `#` are comments, ignored by the parser. Numbers
are plain (`203199` or `203,199`). All amounts in ===PERFORMANCE===
are in the SAME unit, stated once in COMPANY.Unit.

---

## ===COMPANY===
    Name: <string>                  # must match (or closely match) an existing Company.name to link automatically
    Sector: <string>                # must match another survey file's Sector EXACTLY for sector grouping - no fuzzy matching
    Currency: <string>              # e.g. KES
    Unit: <string>                  # e.g. "millions" - scale of Turnover/NetProfit/MarketCap below

## ===PERIOD===
    FiscalYear: <e.g. FY2025>       # the Survey page's own grouping label

## ===PERFORMANCE===
(Executive Summary page)

    Turnover: <value>
    NetProfit: <value>
    MarketCap: <value>

## ===BOARD_COMPOSITION===
(Board Overview / Executive Summary pages)

    BoardSize: <integer>
    BoardMeetingsPerYear: <integer>
    CommitteesPerBoard: <integer>
    CommitteeMeetingsPerYear: <integer>
    DirectorsFemale: <integer>
    DirectorsMale: <integer>
    ExecutiveDirectorsCount: <integer>
    NonExecutiveDirectorsCount: <integer>
    IndependentNEDsCount: <integer>
    NonIndependentNEDsCount: <integer>
    NEDsKenyanCount: <integer>
    NEDsNonKenyanCount: <integer>
    AvgAgeExecutiveDirectors: <value>
    AvgAgeNonExecutiveDirectors: <value>
    AvgAgeIndependentNEDs: <value>
    AvgAgeNonIndependentNEDs: <value>

## ===DIRECTOR_PAY===
(Directors' Remuneration / NED / Executive Directors tabs — company-level
figures only, not named individuals; if a filing names individuals, pick
out or compute the chairperson/average figure yourself)

    ChairpersonAnnualRetainer: <value>
    OtherNEDAnnualRetainer: <value>
    ChairpersonMeetingAllowance: <value>       # per board meeting
    OtherNEDMeetingAllowance: <value>          # per board meeting
    ExecutiveDirectorAnnualRetainer: <value>
    ExecutiveDirectorMeetingAllowance: <value>

## ===COMMITTEE_PAY===
(Committee Remuneration tab)

    CommitteeChairAnnualRetainer: <value>
    CommitteeMemberAnnualRetainer: <value>
    CommitteeChairMeetingAllowance: <value>    # per committee meeting
    CommitteeMemberMeetingAllowance: <value>

## ===CEO_PAY===
(Chief Executive Officer / Managing Director tab — company-level MONTHLY
figures only, same unit/currency as COMPANY.Unit/Currency above. If a
filing states an ANNUAL figure or names the CEO individually, convert to
monthly / pick out the company-level figure yourself, same as
DIRECTOR_PAY asks for the chairperson/average NED figure)

    CEOMonthlySalary: <value>
    CEOMonthlyAllowances: <value>
    CEOMonthlyIncentiveBonus: <value>
    CEOMonthlyDeferredIncentive: <value>
    CEOMonthlyNonCashBenefits: <value>
    CEOMonthlyPension: <value>
    CEOMonthlyGratuity: <value>
    CEOMonthlyShareValue: <value>
    CEOMonthlyCostOfEmployment: <value>        # total, only if the filing states one directly - do not compute by summing the fields above yourself, since a filing's own total may use a different basis (e.g. annual components spread differently across months)

## ===SOURCE===
Optional, free text — page numbers / where each section's figures came
from in the original filing, for traceability.

    Performance: pages 2, 5
    BoardComposition: pages 16-17, 33-35, 72
    DirectorPay: pages 74-75

## Notes

- A field that requires interpretation, inference from prose, or isn't
  explicitly printed anywhere in the filing is left out entirely - it is
  NOT approximated from a related disclosure (e.g. don't infer a
  director's nationality from where they worked; don't infer a board's
  annual meeting count from unrelated governance narrative).
- Gender counts may be transcribed from the filing's own stated
  honorifics/titles (Mr./Mrs./Ms./Dr.) next to each director's name,
  since that is text the filing itself prints - not an inference.
- A filing's "current board" (as of publication) and its actual
  in-year (fiscal-year) board can differ due to mid-year
  appointments/retirements. Use the fiscal-year population (matching
  whichever roster the filing itself ties to that specific fiscal
  year, e.g. its own remuneration table), not the publication-date
  "current board" list, when the two disagree.
