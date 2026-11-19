# Financial data integrity V3

Corrects the disagreement between director-row/benefit display and existing survey policy normalization. NULL pay scale now means whole currency, independently of the financial headline scale. Explicit pay scales remain supported. AI pay tables are converted to the existing pay scale without retagging other stored pay fields. Newly detected explicit remuneration units are retained even when equal to headline units.

Existing data is not divided or rewritten. Legacy records with genuinely scaled pay and no explicit unit require source review. Run `python audit_remuneration.py > remuneration-audit.csv` with the existing database configuration. The script issues queries only (application initialization may perform its normal startup work); protect the CSV as confidential. Flags are review candidates, not corrections. Verify each flagged company-year against the original filing before changing amounts or unit metadata. Do not publish unverified exports. Existing PDF downloads must be regenerated.

Tests: `python -m unittest test_monetary_integrity`; JavaScript syntax checks with node. Full Flask/PostgreSQL integration, browser, original annual report comparison and live data audit not performed here.
