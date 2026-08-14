"""Generic, reusable building blocks for reconciliation rules - not wired
into any rule, dispatcher, or registry yet. Nothing in this file is called
anywhere else in the codebase today; it exists so these five functions have
a stable name/signature to build against before anything depends on them.

"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher

# Convenience patterns for regex_pattern_extractor's `pattern` argument - not
# exhaustive, just the formats this module already knows how to name. Any
# other regex string works too; these are just named shortcuts.
KNOWN_PATTERNS: dict[str, str] = {
    "vpa": r"\b[\w.\-]+@[a-zA-Z]+\b",
    "gstin": r"\b\d{2}[A-Za-z]{5}\d{4}[A-Za-z]\d[Zz][A-Za-z0-9]\b",
    "pan": r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b",
    "numeric_block": r"\d{4,}",
}


def regex_pattern_extractor(text: str, pattern: str) -> list[str]:
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


def calculate_dynamic_age(reference_date: date, as_of: date | None = None) -> int:
    """How many whole days have elapsed between `reference_date` (e.g. an
    invoice's issue_date or due_date) and `as_of` (defaults to today).
    Negative if `reference_date` is in the future relative to `as_of` - a
    not-yet-due invoice, for instance - callers decide how to treat that,
    this just does the subtraction."""
    return ((as_of or date.today()) - reference_date).days


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


def variance_tolerance_validator(value_a: object, value_b: object, *, tolerance: object, mode: str = "absolute") -> bool:
    """True if `value_a` and `value_b` differ by no more than `tolerance` -
    e.g. a payment landing a few paise short of an invoice's balance
    because of a bank fee. `mode="absolute"` (default) compares the raw
    difference against `tolerance` directly (same units as the values -
    minor currency units, days, whatever the caller is validating).
    `mode="percentage"` compares the difference against `tolerance` as a
    fraction of `value_b` (0.01 = 1%) instead - `value_b` is treated as
    the reference/expected value the percentage is taken against."""
    a, b, tol = Decimal(str(value_a)), Decimal(str(value_b)), Decimal(str(tolerance))
    diff = abs(a - b)
    if mode == "percentage":
        if b == 0:
            return diff == 0
        return diff <= abs(b) * tol
    return diff <= tol


# Catalog for GET /reconciliations/algorithms - mirrors the spec table this
# module was built from verbatim. `wired=False` for every entry here since
# none of these five are called by any rule/dispatcher yet (see module
# docstring) - contrast with rules/matchers.py's MATCHER_CATALOG, whose
# entries ARE live and usable today via kind="field-match".
GENERIC_FUNCTION_CATALOG = [
    {
        "technical_name": "regex_pattern_extractor", "ui_display_name": "Pattern Extractor",
        "ui_action_verb": "Extract by Pattern",
        "description": "Pulls formatted strings (Invoice #, UPI, GSTIN) from text.",
    },
    {
        "technical_name": "exact_value_matcher", "ui_display_name": "Exact Match Checker",
        "ui_action_verb": "Check Exact Match",
        "description": "Checks if two strings, numbers, or amounts match 100%.",
    },
    {
        "technical_name": "calculate_dynamic_age", "ui_display_name": "Document Age Calculator",
        "ui_action_verb": "Calculate Days Outstanding",
        "description": "Determines how many days old an invoice is.",
    },
    {
        "technical_name": "fuzzy_string_matcher", "ui_display_name": "Similar Name Matcher",
        "ui_action_verb": "Match Similar Text",
        "description": "Checks for typos/variations in company names.",
    },
    {
        "technical_name": "variance_tolerance_validator", "ui_display_name": "Tolerance & Fee Checker",
        "ui_action_verb": "Validate Within Limit",
        "description": "Checks if small differences fall within fee/penny limits.",
    },
]
