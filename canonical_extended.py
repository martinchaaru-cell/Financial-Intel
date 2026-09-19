"""
Extended line-item vocabulary for pdf_parse.parse_financials_text().

WHY THIS FILE EXISTS
pdf_parse.CANONICAL_LINE_ITEMS only knows ~40 label patterns (the ones
the ratios/report code reads directly), so on a typical filing 75-85% of
extracted lines came back "unmapped" at confidence 0.5 and every
document's extraction score sat at ~55-60. Most of those lines are
perfectly ordinary IFRS lines ("Loans and advances to customers",
"Lease obligations", "Deferred income tax") that just had no pattern.

HOW IT'S USED (see parse_financials_text)
  1. pdf_parse's own core table runs first, exactly as before (0.9).
  2. Then EXTENDED_CANONICAL below (0.85). These names are additive -
     nothing in ratios.py/report_context.py reads them - and unlike core
     names they may legitimately repeat inside one statement ("Lease
     obligations" as a non-current AND a current liability), so a repeat
     gets a numeric suffix (lease_liabilities, lease_liabilities_2)
     instead of being dropped, keeping normalized_name unique per
     statement (report_context / the period-compare view key on it).
  3. Anything still unmapped keeps normalized_name=None. If it is a
     structurally clean two-column row (real label + current AND
     comparative figure) it scores 0.7 rather than 0.5 - see
     structural_confidence(). A one-figure or odd-looking row stays 0.5.

TO EXTEND FOR A NEW COMPANY / LAYOUT: add a (regex, name) pair to the
right statement list below. Patterns are tried in order, first hit wins,
so put specific patterns before general ones. Nothing else to touch.
"""
import re

# (regex, normalized_name) per statement type. Order matters.
_RAW = {
    'income_statement': [
        # --- bank / financial-institution lines
        (r"net\s+interest\s+income", 'net_interest_income'),
        (r"^interest\s+income|interest\s+and\s+similar\s+income|interest\s+income\s+computed", 'interest_income_line'),
        (r"net\s+fees?\s+and\s+commissions?", 'net_fee_and_commission_income'),
        (r"fees?\s+and\s+commissions?\s+income", 'fee_and_commission_income'),
        (r"fees?\s+and\s+commissions?\s+expense", 'fee_and_commission_expense'),
        (r"insurance\s+revenue", 'insurance_revenue'),
        (r"insurance\s+service\s+(expense|result)|reinsurance", 'insurance_service_lines'),
        (r"foreign\s+exchange", 'net_foreign_exchange'),
        (r"impairment|expected\s+credit\s+loss", 'credit_impairment'),
        (r"net\s+operating\s+income", 'net_operating_income'),
        (r"total\s+operating\s+expenses?|total\s+expenses?", 'total_operating_expenses'),
        (r"employee\s+(benefits?|costs?)|staff\s+(costs?|expenses?)|personnel", 'employee_benefits'),
        (r"other\s+operating\s+expenses?|other\s+expenses?", 'other_operating_expenses'),
        (r"loss\s+on\s+monetary\s+position|monetary\s+(gain|loss)", 'monetary_position'),
        (r"share\s+of\s+(net\s+)?(profit|surplus|loss|results?)", 'share_of_associates'),
        (r"fair\s+value\s+(gain|loss)|net\s+(gain|loss).*fair\s+value|gain\s+arising\s+from\s+changes\s+in\s+fair\s+value", 'fair_value_movements'),
        # --- general corporate lines
        (r"^\s*(net\s+)?sales\s*$|^\s*turnover\s*$", 'sales'),
        (r"cost\s+of\s+production", 'cost_of_production'),
        (r"administrative\s+(expenses?|costs?)|general\s+and\s+administrative", 'administrative_expenses'),
        (r"selling\s+and\s+distribution|distribution\s+(costs?|expenses?)|marketing\s+expenses?", 'distribution_costs'),
        (r"other\s+(operating\s+)?(income|gains?|losses?)|other\s+income/\(losses\)", 'other_income'),
        (r"finance\s+income|interest\s+received", 'finance_income'),
        (r"tax\s+\(?(charge|expense|credit)|income\s+tax|taxation|^tax\b", 'income_tax'),
        (r"total\s+other\s+comprehensive|other\s+comprehensive\s+(income|loss)", 'other_comprehensive_income'),
        (r"total\s+comprehensive", 'total_comprehensive_income'),
        (r"revaluation\s+(of|surplus|gain)", 'revaluation_items'),
        (r"actuarial|remeasurement", 'remeasurement_items'),
        (r"dividends?\s+(from|received)", 'dividend_income'),
        (r"transaction\s+levy|listing\s+fees?|data\s+vending|subscription|market\s+access|platform\s+fees?|licen[cs]e\s+fees?", 'operating_fee_income'),
        (r"system\s+maintenance|building\s+and\s+office|office\s+costs?|premises|rent\b|utilities|repairs?\s+and\s+maintenance", 'premises_and_systems_costs'),
        (r"professional\s+fees?|legal\s+fees?|audit\s+fees?|consultancy", 'professional_fees'),
        (r"directors?['’]?\s*(emoluments?|fees|remuneration)", 'directors_emoluments'),
        (r"marketing|advertis|promotion|donations?|corporate\s+social", 'marketing_and_csr'),
        (r"^profit|^loss\b|surplus|deficit", 'profit_or_loss_line'),
    ],
    'balance_sheet': [
        (r"total\s+equity\s+and\s+(non-?current\s+)?liabilities|total\s+liabilities\s+and\s+(equity|shareholders)|total\s+equity\s+and\s+liabilities", 'total_equity_and_liabilities'),
        (r"total\s+non-?current\s+assets", 'total_non_current_assets'),
        (r"total\s+non-?current\s+liabilities", 'total_non_current_liabilities'),
        (r"net\s+current\s+assets", 'net_current_assets'),
        (r"cash\s+and\s+(balances|bank|deposits)|cash\s+in\s+hand|cash,\s+deposits|bank\s+balances|^cash\s*$", 'cash_and_bank_balances'),
        (r"loans\s+and\s+advances\s+to\s+banks", 'loans_to_banks'),
        (r"loans\s+and\s+advances", 'loans_and_advances'),
        (r"deposits\s+from\s+customers|customer\s+deposits", 'customer_deposits'),
        (r"deposits\s+from\s+(banks|financial|other\s+banks)|due\s+to\s+banks|placements\s+from", 'deposits_from_banks'),
        (r"(fair\s+value\s+through\s+other\s+comprehensive|fvoci)", 'financial_assets_fvoci'),
        (r"(fair\s+value\s+through\s+profit|fvtpl)", 'financial_assets_fvtpl'),
        (r"financial\s+assets.*amortised|amortised\s+cost|government\s+securities|treasury\s+(bills|bonds)|investment\s+securities|held\s+to\s+maturity", 'investment_securities'),
        (r"derivative", 'derivatives'),
        (r"investment\s+propert", 'investment_property'),
        (r"intangible", 'intangible_assets'),
        (r"right-?\s*of-?\s*use", 'right_of_use_assets'),
        (r"biological\s+assets", 'biological_assets'),
        (r"investments?\s+in\s+(subsidiar|associate|joint)|investments?\s+accounted|equity\s+method", 'investments_in_associates'),
        (r"property\s+and\s+equipment|property,\s+plant", 'property_and_equipment'),
        (r"assets\s+classified\s+as\s+held\s+for\s+sale|held\s+for\s+sale", 'held_for_sale'),
        (r"deferred\s+(income\s+)?tax", 'deferred_tax'),
        (r"(current|corporation|income)\s+tax\s+(recoverable|payable|receivable|assets?|liabilit)|tax\s+(recoverable|payable)", 'current_tax'),
        (r"receivables|prepayments|due\s+from\s+(related|group)|amounts\s+due\s+from", 'receivables_and_prepayments'),
        (r"^other\s+assets|other\s+non-?current\s+assets", 'other_assets'),
        (r"lease\s+(liabilit|obligation)|leases?\s+payable", 'lease_liabilities'),
        (r"retirement|post-?\s*employment|long\s+service|employee\s+benefit|gratuity", 'retirement_benefit_obligations'),
        (r"provisions?\b", 'provisions'),
        (r"borrowed\s+funds|borrowings|debentures?|loan\s+notes|bonds\s+payable|crop\s+debenture|term\s+loans?", 'borrowed_funds'),
        (r"payables|accrued|accruals|creditors|due\s+to\s+(related|group)|amounts\s+due\s+to", 'payables_and_accruals'),
        (r"unearned|deferred\s+income|contract\s+liabilit|insurance\s+contract|reinsurance", 'contract_and_insurance_liabilities'),
        (r"dividends?\s+payable|proposed\s+dividend|dividend\s+proposed", 'dividends'),
        (r"share\s+premium", 'share_premium'),
        (r"revaluation\s+(reserve|surplus)|fair\s+value\s+reserve", 'revaluation_reserve'),
        (r"other\s+reserves|statutory\s+reserve|regulatory\s+reserve|reserves", 'other_reserves'),
        (r"non-?\s*controlling", 'non_controlling_interests'),
        (r"treasury\s+shares|preference\s+shares|perpetual", 'other_equity_instruments'),
        (r"total\s+non-?current|non-?current\s+(assets|liabilities)$", 'non_current_subtotal'),
        (r"^total\b", 'other_total'),
    ],
    'cash_flow': [
        (r"net\s+cash\s+(flows?\s+)?(generated|used|from|\(?used).*operating|cash\s+flows?\s+from\s+operating\s+activities", 'operating_cash_flow_total'),
        (r"net\s+cash\s+(flows?\s+)?(generated|used|from|\(?used).*investing|(cash\s+(used|generated).*)?investing\s+activities$", 'investing_cash_flow_total'),
        (r"net\s+cash\s+(flows?\s+)?(generated|used|from|\(?used).*financing|(cash\s+(used|generated).*)?financing\s+activities$", 'financing_cash_flow_total'),
        (r"cash\s+(generated|utili[sz]ed|used).*operations|operating\s+profit\s+before\s+working", 'cash_from_operations'),
        (r"profit\s+before\s+(income\s+)?tax", 'profit_before_tax_cf'),
        (r"adjustments?|non-?cash\s+items", 'non_cash_adjustments'),
        (r"working\s+capital|(increase|decrease).*(receivables|payables|inventor)|movement\s+in", 'working_capital_changes'),
        (r"depreciation|amortisation|amortization", 'depreciation_and_amortisation_cf'),
        (r"income\s+tax(es)?\s+paid|tax\s+paid|taxes\s+paid", 'tax_paid'),
        (r"interest\s+(received|paid)|interest\s+paid|payment\s+of\s+interest", 'interest_flows'),
        (r"dividends?\s+(paid|received)|dividends?\s+from", 'dividend_flows'),
        (r"purchase\s+of|payments?\s+(for|to\s+acquire)\s+(property|intangible|investment)|acquisition\s+of|investment\s+in", 'investing_purchases'),
        (r"proceeds\s+(from|on)\s+(sale|disposal|maturity)|disposal\s+of|sale\s+of|maturity\s+of|redemption", 'investing_proceeds'),
        (r"proceeds\s+from\s+(borrow|issue|loan)|issue\s+of\s+(shares|bonds)|drawdown", 'financing_proceeds'),
        (r"repayment|payment\s+of\s+(principal|lease|borrow)|lease\s+payments?|principal\s+(elements?|portion)", 'financing_repayments'),
        (r"(net\s+)?(increase|decrease).*cash|net\s+change\s+in\s+cash", 'net_change_in_cash'),
        (r"cash\s+and\s+cash\s+equivalents\s+at\s+(the\s+)?(start|beginning)|at\s+(the\s+)?(start|beginning)\s+of|opening\s+cash", 'cash_start_of_period'),
        (r"cash\s+and\s+cash\s+equivalents\s+at\s+(the\s+)?end|at\s+(the\s+)?end\s+of|closing\s+cash", 'cash_end_of_period_line'),
        (r"effects?\s+of\s+(foreign|exchange)|exchange\s+(gains?|losses?|differences?)|foreign\s+(currency|exchange)", 'fx_effect_on_cash'),
        (r"standby|letters?\s+of\s+credit", 'other_financing'),
    ],
    'equity': [
        (r"(at|balance\s+at)\s+(the\s+)?(start|beginning)|opening\s+balance|as\s+previously", 'equity_opening'),
        (r"(at|balance\s+at)\s+(the\s+)?end|closing\s+balance", 'equity_closing'),
        (r"^\s*(total\s+)?comprehensive\s+income|total\s+comprehensive", 'comprehensive_income'),
        (r"profit\s+(for\s+the\s+(year|period))?|loss\s+for\s+the|surplus", 'profit_for_period'),
        (r"other\s+comprehensive|revaluation|actuarial|remeasurement|fair\s+value", 'oci_movements'),
        (r"dividends?", 'dividends_movement'),
        (r"issue\s+of\s+shares|share\s+(issue|capital)|share\s+ownership|employee\s+share", 'share_movements'),
        (r"transfer|excess\s+depreciation|reclassif", 'reserve_transfers'),
        (r"total\s+contributions|contributions\s+and\s+distributions|transactions\s+with\s+(owners|shareholders)", 'owner_transactions'),
        (r"^total\b", 'equity_total_line'),
        (r"for\s+the\s+(year|period)$", 'period_movement_continuation'),
    ],
}

EXTENDED_CANONICAL = {
    stmt: [(re.compile(p, re.IGNORECASE), name) for p, name in pairs]
    for stmt, pairs in _RAW.items()
}


def match_extended(stmt_type, label):
    for rx, name in EXTENDED_CANONICAL.get(stmt_type, ()):
        if rx.search(label):
            return name
    return None


# ---- page furniture that is not a line item at all -------------------
# Running headers/footers, "as at"/"year ended" lines and unit headers
# were being saved as line items (label + a year or 0 as the "amount"):
# "EAAGADS LIMITED ANNUAL REPORT AND FINANCIAL STATEMENTS 2025",
# "The notes on pages 23", "Year ended 2024", "ASSETS Sh’". They are
# wrong data, and each dragged the document's score down at 0.5.
_JUNK_LABEL_RES = [re.compile(p, re.IGNORECASE) for p in [
    r"annual\s+report|integrated\s+report|financial\s+statements?\s*(20\d\d|for|\b)",
    r"^the\s+(notes|financial\s+statements)\b",
    r"\bnotes?\s+(on\s+)?pages?\b|\bon\s+pages?\b|\bpages?\s+\d",
    r"^(as\s+at|at\s+the|for\s+the\s+(year|period)\s+ended|for\s+the\s+period\s+of|year\s+ended|period\s+ended|six\s+months|twelve\s+months)\b",
    r"^(assets|liabilities|equity)\s+(sh|ksh|kshs|shs|usd|us\$)",
    r"\b(sh|ksh|kshs|shs)[’']?\s*(000|m|million)?$",
    r"^note[s]?\s",
]]


def is_page_furniture(label, numbers):
    lab = label.strip()
    if any(rx.search(lab) for rx in _JUNK_LABEL_RES):
        return True
    # A bare year (or year + page no.) as the only "amount" on a
    # non-numeric heading-like label is a date header, not a figure.
    if numbers and len(numbers) <= 2 and all(1990 <= abs(n) <= 2100 and float(n).is_integer() for n in numbers[:1]):
        if len(lab.split()) <= 4 and not re.search(r"(income|profit|loss|cash|assets|equity|dividend|tax|revenue|sales|expenses?|liabilit)", lab, re.I):
            return True
    return False


def structural_confidence(label, amount, prior_amount):
    """0.7 for an unmapped-but-structurally-clean row, else 0.5.
    Clean = a real multi-word alphabetic label AND both a current and a
    comparative figure printed on the same line (the layout of a genuine
    statement row) AND the current figure isn't a year/units artefact."""
    words = [w for w in re.split(r"\s+", label.strip()) if w]
    alpha_words = [w for w in words if re.search(r"[A-Za-z]{2,}", w)]
    if len(alpha_words) < 2 or prior_amount is None:
        return 0.5
    if amount is None or (float(amount).is_integer() and 1990 <= abs(amount) <= 2100):
        return 0.5
    if label.strip().endswith(('-', ',')):
        return 0.5
    return 0.7