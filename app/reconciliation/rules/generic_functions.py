"""Generic, reusable building blocks for reconciliation rules - not wired
into any rule, dispatcher, or registry yet. Nothing in this file is called
anywhere else in the codebase today; it exists so these functions have a
stable name/signature to build against before anything depends on them.

Names/technical identifiers match the finalized "Core Engine Primitives"
function-library spec exactly (dynamic_age_calc, pattern_extractor,
tolerance_validator, aggregate_sum, relational_comparator,
fifo_waterfall_allocator, filter_dataset, net_open_credits,
execute_settlement, plus exact_value_matcher/fuzzy_string_matcher which
were already correct).
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher

# Convenience patterns for pattern_extractor's `pattern` argument - not
# exhaustive, just the formats this module already knows how to name. Any
# other regex string works too; these are just named shortcuts.
KNOWN_PATTERNS: dict[str, str] = {
    "vpa": r"\b[\w.\-]+@[a-zA-Z]+\b",
    "gstin": r"\b\d{2}[A-Za-z]{5}\d{4}[A-Za-z]\d[Zz][A-Za-z0-9]\b",
    "pan": r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b",
    "numeric_block": r"\d{4,}",
}

# Supported operators for filter_dataset/relational_comparator - shared so
# both stay in sync rather than each hardcoding its own smaller set.
_RELATIONAL_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
}


# -- 1. Pre-Processing & Scoping ---------------------------------------------
def filter_dataset(records: list[dict], *, field: str, operator: str, value: object) -> list[dict]:
    """Restricts `records` (open invoices, bank rows, etc.) to those where
    `record[field]` satisfies `operator` against `value` -
    e.g. `filter_dataset(invoices, field="status", operator="in", value=["OPEN", "PARTIALLY_SETTLED"])`.
    A record missing `field` entirely, or an unrecognized `operator`, is
    excluded rather than raising - same "never raise on a plain bad input"
    convention as the rest of this module."""
    compare = _RELATIONAL_OPS.get(operator)
    if compare is None:
        return []
    return [r for r in records if field in r and r[field] is not None and compare(r[field], value)]


def net_open_credits(gross_amount: object, memos: list[dict], *, amount_field: str = "amount_minor", type_field: str = "memo_type") -> Decimal:
    """Nets a list of open credit/debit memos off `gross_amount`. Each memo
    dict needs `type_field` ("CREDIT" reduces the balance, anything else -
    "DEBIT" - increases it) and `amount_field`. Floored at 0 - netting
    memos alone never pushes a balance negative."""
    net = Decimal(str(gross_amount))
    for memo in memos:
        sign = -1 if memo.get(type_field) == "CREDIT" else 1
        net += sign * Decimal(str(memo.get(amount_field) or 0))
    return max(net, Decimal(0))


def dynamic_age_calc(reference_date: date, as_of: date | None = None) -> int:
    """How many whole days have elapsed between `reference_date` (e.g. an
    invoice's issue_date or due_date) and `as_of` (defaults to today, or
    pass the reconciliation period's end date as the "As-Of Anchor").
    Negative if `reference_date` is in the future relative to `as_of` - a
    not-yet-due invoice, for instance - callers decide how to treat that,
    this just does the subtraction."""
    return ((as_of or date.today()) - reference_date).days


# -- 2. Data Extraction -------------------------------------------------------
def pattern_extractor(text: str, pattern: str) -> list[str]:
    """Pulls every match of `pattern` out of `text`, in order of appearance.

    `pattern` is either a raw regex string, or one of KNOWN_PATTERNS'
    shortcut names ("vpa", "gstin", "pan", "numeric_block") - resolved to
    its regex before matching. Returns an empty list for no matches or
    empty input, never raises on a "no match" case (only on a genuinely
    malformed regex string)."""
    if not text:
        return []
    resolved = KNOWN_PATTERNS.get(pattern, pattern)
    return re.findall(resolved, text)


# -- 3. Comparison, Aggregation & Math ----------------------------------------
def aggregate_sum(records: list[dict], field: str) -> Decimal:
    """Sums `record[field]` across every record in `records`, into a single
    total - a record missing `field` (or with it set to None) contributes
    0, not an error."""
    return sum((Decimal(str(r[field])) for r in records if r.get(field) is not None), Decimal(0))


def relational_comparator(value_a: object, operator: str, value_b: object) -> bool:
    """Evaluates `value_a <operator> value_b` - operator is one of
    "=="|"!="|">"|"<"|">="|"<=" (also "in"/"not_in" for membership checks,
    shared with filter_dataset). Numeric-aware via Decimal when both sides
    parse as numbers; falls back to comparing the raw values directly
    otherwise (so "in"/"not_in" against a list/set still works). An
    unrecognized operator returns False rather than raising, matching this
    module's other functions' "never raise on a plain bad input" convention."""
    compare = _RELATIONAL_OPS.get(operator)
    if compare is None:
        return False
    try:
        return compare(Decimal(str(value_a)), Decimal(str(value_b)))
    except (ArithmeticError, ValueError, TypeError):
        return compare(value_a, value_b)


def exact_value_matcher(value_a: object, value_b: object, *, case_sensitive: bool = False) -> bool:
    """True if `value_a` and `value_b` are the same string, number, or
    amount. Tries a numeric comparison first (via Decimal) so "100", 100,
    and 100.0 are all equal regardless of type on either side; only falls
    back to a whitespace-trimmed string comparison (case-insensitive by
    default) if either side isn't parseable as a number."""
    if value_a is None or value_b is None:
        return False
    try:
        return Decimal(str(value_a)) == Decimal(str(value_b))
    except (ArithmeticError, ValueError):
        pass
    text_a, text_b = str(value_a).strip(), str(value_b).strip()
    if not case_sensitive:
        text_a, text_b = text_a.upper(), text_b.upper()
    return text_a == text_b


def fuzzy_string_matcher(value_a: str, value_b: str, *, min_similarity: float = 0.85) -> bool:
    """True if `value_a` and `value_b` are similar enough to plausibly be
    the same name despite typos/variations (e.g. "Acme Corp" vs "Acme
    Corporation"), per a pure-Python SequenceMatcher ratio in [0, 1] -
    not Postgres pg_trgm's similarity(), which scores differently and
    needs a live DB connection (see rules/matchers.py's
    TRIGRAM_MATCHER for that path). Empty/missing input never matches."""
    if not value_a or not value_b:
        return False
    ratio = SequenceMatcher(None, value_a.strip().lower(), value_b.strip().lower()).ratio()
    return ratio >= min_similarity


def tolerance_validator(value_a: object, value_b: object, *, tolerance: object, mode: str = "absolute") -> bool:
    """True if `value_a` and `value_b` differ by no more than `tolerance` -
    e.g. a payment landing a few paise short of an invoice's balance
    because of a bank fee, or a TDS-adjusted shortfall (2%/10% of the
    invoice). `mode="absolute"` (default) compares the raw difference
    against `tolerance` directly (same units as the values - minor
    currency units, days, whatever the caller is validating).
    `mode="percentage"` compares the difference against `tolerance` as a
    fraction of `value_b` (0.01 = 1%, 0.02 = 2% TDS, etc.) instead -
    `value_b` is treated as the reference/expected value the percentage is
    taken against."""
    a, b, tol = Decimal(str(value_a)), Decimal(str(value_b)), Decimal(str(tolerance))
    diff = abs(a - b)
    if mode == "percentage":
        if b == 0:
            return diff == 0
        return diff <= abs(b) * tol
    return diff <= tol


# -- 4. Allocation & Settlement Actions ---------------------------------------
def fifo_waterfall_allocator(cash_available: object, invoices: list[dict], *, balance_field: str = "balance_due_minor", id_field: str = "invoice_id") -> list[dict]:
    """Applies `cash_available` sequentially across `invoices` - the caller
    is responsible for pre-sorting them oldest-due-first, this doesn't
    sort. Fully closes each invoice in turn until the cash runs out, then
    partially closes whichever invoice it ran out on. Returns one
    `{id_field: ..., "allocated_minor": ...}` dict per invoice that
    actually received money - invoices past the point the cash ran out
    aren't included at all, and one already at/below a zero balance is
    skipped without consuming any cash."""
    remaining = Decimal(str(cash_available))
    allocations: list[dict] = []
    for inv in invoices:
        if remaining <= 0:
            break
        balance = Decimal(str(inv[balance_field]))
        if balance <= 0:
            continue
        applied = min(remaining, balance)
        allocations.append({id_field: inv[id_field], "allocated_minor": applied})
        remaining -= applied
    return allocations


def execute_settlement(cash_applied: object, invoice_balance: object) -> dict:
    """Pure decision logic for one (cash applied, invoice balance) pair -
    no DB writes (see engine.py::run_phase_2 for where an equivalent
    decision maps onto real invoice/payment updates today). Returns
    `{"status": "FULLY_SETTLED"|"PARTIALLY_SETTLED"|"UNSETTLED",
    "excess_minor": ..., "shortfall_minor": ...}` - `excess_minor` is > 0
    only when cash exceeded the balance (on-account credit candidate),
    `shortfall_minor` is > 0 only when cash fell short (what's still owed)."""
    cash, balance = Decimal(str(cash_applied)), Decimal(str(invoice_balance))
    if cash <= 0:
        return {"status": "UNSETTLED", "excess_minor": Decimal(0), "shortfall_minor": balance}
    if cash >= balance:
        return {"status": "FULLY_SETTLED", "excess_minor": cash - balance, "shortfall_minor": Decimal(0)}
    return {"status": "PARTIALLY_SETTLED", "excess_minor": Decimal(0), "shortfall_minor": balance - cash}


# Catalog for GET /reconciliations/algorithms - mirrors the finalized
# function-library spec verbatim. `wired=False` for every entry here since
# none of these are called by any rule/dispatcher yet (see module
# docstring) - contrast with rules/matchers.py's MATCHER_CATALOG, whose
# entries ARE live and usable today via kind="field-match". `ui_action_verb`
# is None where the spec didn't give one (the spec's second pass added six
# functions with a Description/Example but no explicit action-verb column).
GENERIC_FUNCTION_CATALOG = [
    {
        "technical_name": "filter_dataset", "ui_display_name": "Filter Dataset", "ui_action_verb": None,
        "description": "Restricts records based on status, dates, or standard ledger attributes before matching begins.",
    },
    {
        "technical_name": "net_open_credits", "ui_display_name": "Net Open Credits", "ui_action_verb": None,
        "description": "Subtracts open credit/debit notes from gross invoice balances to calculate the true net amount due.",
    },
    {
        "technical_name": "dynamic_age_calc", "ui_display_name": "Document Age Calculator",
        "ui_action_verb": "Calculate Days Outstanding",
        "description": "Computes dynamic aging in days relative to the As-Of Anchor Date (Reconciliation Period End Date).",
    },
    {
        "technical_name": "pattern_extractor", "ui_display_name": "Pattern Extractor",
        "ui_action_verb": "Extract by Pattern",
        "description": "Rips formatted strings (Invoice numbers, UPI handles, GSTIN tax IDs) out of messy, unstructured narration text.",
    },
    {
        "technical_name": "aggregate_sum", "ui_display_name": "Sum Dataset", "ui_action_verb": None,
        "description": "Aggregates and totals a specific numeric field across a filtered dataset, saving the output to a temporary variable.",
    },
    {
        "technical_name": "relational_comparator", "ui_display_name": "Compare Values", "ui_action_verb": None,
        "description": "Evaluates two numerical values or variables using relational operators (==, >, <, >=, <=) to trigger logical branching.",
    },
    {
        "technical_name": "exact_value_matcher", "ui_display_name": "Exact Match Checker",
        "ui_action_verb": "Check Exact Match",
        "description": "Asserts 100% strict normalized equality on text strings, account numbers, or rounded currency amounts.",
    },
    {
        "technical_name": "fuzzy_string_matcher", "ui_display_name": "Similar Name Matcher",
        "ui_action_verb": "Match Similar Text",
        "description": "Fuzzy matches company names to handle typos or spelling variations (>85% similarity threshold).",
    },
    {
        "technical_name": "tolerance_validator", "ui_display_name": "Tolerance & Fee Checker",
        "ui_action_verb": "Validate Within Limit",
        "description": "Checks if financial differences match statutory tax rules (TDS 2%/10%), early discounts, or penny limits.",
    },
    {
        "technical_name": "fifo_waterfall_allocator", "ui_display_name": "Apply FIFO Allocation", "ui_action_verb": None,
        "description": "An atomic execution block that takes an unallocated cash amount and an aging-sorted dataset, applying funds sequentially (Oldest Due First) until the money runs out.",
    },
    {
        "technical_name": "execute_settlement", "ui_display_name": "Execute Settlement", "ui_action_verb": None,
        "description": "Final state-change block that applies status updates (Fully/Partially Settled), routes excess cash to On-Account Advance ledgers, or flags residual shortfalls for exceptions.",
    },
]
