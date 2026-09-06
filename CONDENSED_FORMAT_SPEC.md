# FinSight Standard Condensed Filing Format (v1)

One plain-text file per fiscal year, per company. Every section is
optional except COMPANY, PERIOD, and INCOME_STATEMENT/BALANCE_SHEET
(a report with neither has nothing for the app to show). Missing
sections/fields are simply omitted - never filled with a guess,
zero, or placeholder text. The app treats an omitted field as
"not disclosed in this filing," exactly as it does today for a
real PDF that doesn't contain that data point.

## Grammar

    ===SECTION_NAME===
    Field Label: value

Numbers are plain (no currency symbol, no thousands separators
required but allowed): `1234.5` or `1,234.5`. Two-number lines
("current, prior") are comma-separated: `Revenue: 349447.2, 310904.8`.
A single-number line has just one value: `DividendPerShare: 4.25`.
All financial figures are in the SAME unit throughout one file -
state the unit once in the COMPANY section (e.g. "Shs millions").

Lines starting with `#` are comments, ignored by the parser.

---

## ===COMPANY===
    Name: <string>
    Ticker: <string>
    Sector: <string>                # must match another uploaded company's Sector EXACTLY (case-sensitive string match) for Peer Comparison to group them together - no fuzzy matching
    Exchange: <string>              # e.g. NSE
    Currency: <string>              # e.g. KES
    Unit: <string>                  # e.g. "millions" - every amount in this file is in this unit

## ===PERIOD===
    Label: <e.g. FY2025>
    FiscalYearEnd: <e.g. 31 March 2025>
    PriorLabel: <e.g. FY2024>        # what the "prior" column in every 2-number line means

## ===INCOME_STATEMENT===
Field labels map 1:1 to the app's `normalized_name` values - use
these exact labels so the parser needs no fuzzy matching:

    Revenue: <current>, <prior>
    InterestIncome: <current>, <prior>
    InterestExpense: <current>, <prior>
    CostOfSales: <current>, <prior>
    GrossProfit: <current>, <prior>
    EmployeeCosts: <current>, <prior>
    Depreciation: <current>, <prior>
    Amortisation: <current>, <prior>
    FinanceCosts: <current>, <prior>
    OperatingProfit: <current>, <prior>
    ProfitBeforeTax: <current>, <prior>
    TaxExpense: <current>, <prior>
    NetIncome: <current>, <prior>
    EPS: <current>, <prior>
    SharesOutstanding: <current>, <prior>

## ===BALANCE_SHEET===
    TotalAssets: <current>, <prior>
    TotalLiabilities: <current>, <prior>
    TotalEquity: <current>, <prior>
    CashAndEquivalents: <current>, <prior>
    Receivables: <current>, <prior>
    Payables: <current>, <prior>
    Inventory: <current>, <prior>
    CurrentAssets: <current>, <prior>
    CurrentLiabilities: <current>, <prior>
    PPE: <current>, <prior>
    Borrowings: <current>, <prior>
    ShareCapital: <current>, <prior>
    RetainedEarnings: <current>, <prior>

## ===CASH_FLOW===
    OperatingCashFlow: <current>, <prior>
    InvestingCashFlow: <current>, <prior>
    FinancingCashFlow: <current>, <prior>
    CashEndOfPeriod: <current>, <prior>
    Capex: <current>, <prior>

## ===MARKET_DATA===
(all single-value unless noted; this is this period's own point-in-
time investor information, not a current/prior pair, except where noted)

    SharePrice: <current>, <prior>
    MarketCap: <current>, <prior>
    SharesIssued: <value>
    SharesAuthorized: <value>
    ShareholderCount: <current>, <prior>
    FreeFloatPct: <value>
    DividendPerShare: <value>          # total for the year, as filed
    InterimDividendPerShare: <value>
    FinalDividendPerShare: <value>
    SpecialDividendPerShare: <value>
    DividendYieldPct: <value>
    TotalShareholderReturnPct: <value>
    LocalInstitutionalPct: <value>
    LocalIndividualPct: <value>
    ForeignInvestorPct: <value>

## ===MANAGEMENT_GUIDANCE===
One line per forward-looking KPI, from the filing's own guidance
table/commentary for the NEXT fiscal year. Optional `GuidancePeriod`
line sets the label shown on the Outlook tab for every row below it
(e.g. "FY2026"); optional trailing `commentary=` on any row is shown
next to that row. Format:
`<MetricName>: current=<x>, low=<x>, high=<x>[, commentary=<text>]`

    GuidancePeriod: FY2026
    ROE: current=26.7, low=24.0, high=27.0, commentary=Driven by cost discipline
    CostToIncomeRatio: current=51.0, low=48.0, high=51.0

## ===PRINCIPAL_RISKS===
One block per named risk category, in the filing's own order:

    [Credit Risk]
    Description: <as filed>
    Mitigation: <as filed>

    [Technology and Cybersecurity]
    Description: <as filed>
    Mitigation: <as filed>

## ===DIRECTOR_REMUNERATION===
One line per row the filing itself prints a total for:

    [GRAND TOTAL - Non-Executive Directors] Total: <value>
    [Dr. James Mwangi, Executive] Total: <value>
    [Executive Director Name, Executive] Total: <value>

## ===SOURCE===
Optional, for traceability - human-readable notes on where each
section's figures came from in the original filing (page numbers),
kept as free text since this is a condensed file, not the original PDF.

    IncomeStatement: page 138
    BalanceSheet: page 141
    ...

## Known limits of this format (v1)

- **Peer Comparison**: works automatically once 2+ companies with the
  exact same `Sector` string are uploaded - the app computes sector
  averages/rankings server-side across all uploaded companies. This
  format doesn't need to (and can't) carry peer data itself; just make
  sure `Sector` is spelled identically across every company's file.
- **Source Evidence tab**: this format has no per-line-item page/
  confidence data beyond the one free-text ===SOURCE=== block (whole
  sections, not individual figures) - the Source Evidence tab's
  per-data-point table is a separate, not-yet-built feature this
  format doesn't drive.
- **MANAGEMENT_GUIDANCE / PRINCIPAL_RISKS / DIRECTOR_REMUNERATION**:
  each requires a person to read and condense the filing's own prose/
  table into this format's grammar - there's no way to paste raw
  filing text into these sections and have it auto-parse, unlike the
  numeric statement sections which are closer to copy-paste from a
  clean table.
