"""Generic, config-driven matcher primitives shared by
rules/identification.py's `generic_field_match` (Phase 0/1a - stops at the
first match, matching the existing simple rules' own first-match-wins
behavior) and rules/pooling.py's `generic_field_pool` (Phase 1b - collects
every match, since Phase 1b never locks).

Lets a new rule be created via `POST /reconciliations/{id}/rules` with any
(matcher, bank_field, source, source_field) combination below, without
writing new Python - `kind="field-match"`.

Compound rules - two fields ANDed together (account-ifsc), multiple
sources/extractors tried in sequence (customer-code, gstin-pan) - aren't
expressible here by design; those stay their own bespoke functions. See
docs/reconciliation.md.
"""
from __future__ import annotations

from typing import Callable

from app.reconciliation import extract, fuzzy
from app.reconciliation.rules import RuleContext

PredicateFn = Callable[[str, str, dict], bool]


def _exact(bank_value: str, candidate_value: str, config: dict) -> bool:
    return bank_value.strip().upper() == candidate_value.strip().upper()


def _substring(bank_value: str, candidate_value: str, config: dict) -> bool:
    return extract.contains_substring(bank_value, candidate_value)


def _numeric_suffix(bank_value: str, candidate_value: str, config: dict) -> bool:
    return extract.account_suffix_matches(bank_value, candidate_value, config.get("suffix_length", 4))


def _token_overlap(bank_value: str, candidate_value: str, config: dict) -> bool:
    # fuzzy.token_overlap_match's own signature is (company_name, narration)
    # - candidate first, bank value second - the reverse of every other
    # matcher's (bank_value, candidate_value) convention, so flip here
    # rather than push that inconsistency up into find_matches.
    return fuzzy.token_overlap_match(candidate_value, bank_value) is not None


MATCHER_REGISTRY: dict[str, PredicateFn] = {
    "exact": _exact,
    "substring": _substring,
    "numeric_suffix": _numeric_suffix,
    "token_overlap": _token_overlap,
}

# Handled separately in find_matches, not through MATCHER_REGISTRY - a
# pg_trgm search across every candidate at once (fuzzy.best_fuzzy_match),
# not a pairwise (bank_value, candidate_value) -> bool predicate the loop
# below can call once per candidate.
TRIGRAM_MATCHER = "trigram_similarity"

MATCHER_KINDS = frozenset(MATCHER_REGISTRY) | {TRIGRAM_MATCHER}

# Human-readable catalog for GET /reconciliations/matchers - the frontend's
# source of truth for the MATCHER/source/field pickers, so it never hardcodes
# a list that can drift from what find_matches actually accepts.
MATCHER_CATALOG = [
    {"kind": "exact", "label": "Exact match", "description": "Case-insensitive exact string equality.", "config_keys": []},
    {"kind": "substring", "label": "Contains (substring)", "description": "Case-insensitive substring containment - the source value appears somewhere in the bank value.", "config_keys": []},
    {"kind": "numeric_suffix", "label": "Numeric suffix", "description": "The source value's last N digits (config.suffix_length, default 4) appear as a trailing digit run in the bank value.", "config_keys": ["suffix_length"]},
    {"kind": "token_overlap", "label": "Token overlap", "description": "A significant word from the source value (stopwords/short noise dropped) appears in the bank value.", "config_keys": []},
    {
        "kind": TRIGRAM_MATCHER, "label": "Fuzzy similarity (trigram)",
        "description": "Postgres pg_trgm similarity search - only supported for source='customers', source_field='company_name' (the only trigram-indexed column). config.min_similarity (default 0.85) sets the threshold.",
        "config_keys": ["min_similarity"],
    },
]

# Only the four Phase-1 working-set lists RuleContext already loads once per
# run - no new queries. Phase 2's `invoices` is deliberately not here; that
# phase's AllocationContext/rules stay their own bespoke functions (see
# module docstring and docs/reconciliation.md's scoping note).
_SOURCE_ATTR = {
    "customers": "customers",
    "customer_bank_accounts": "bank_accounts",
    "customer_reference_codes": "reference_codes",
    "expected_remittances": "expected_remittances",
}
SOURCE_KINDS = frozenset(_SOURCE_ATTR)

# The exact columns each source's loader (app/reconciliation/dao.py) selects
# - the only fields find_matches can actually read off a candidate row.
# customer_id is excluded (never a meaningful source_field to match against).
SOURCE_FIELDS = {
    "customers": ["company_name", "customer_code", "pan", "gstin", "vpa_handle"],
    "customer_bank_accounts": ["bank_account_no", "ifsc_code"],
    "customer_reference_codes": ["code_value", "code_type"],
    "expected_remittances": ["utr_number"],
}

# The exact bank_statements columns list_candidate_bank_inflows selects,
# plus the extract:* sentinels extract_bank_value understands - the only
# valid values for config.bank_field.
BANK_FIELDS = [
    "bank_reference", "narration", "payer_name", "payer_account_no", "payer_ifsc",
    "extract:vpa", "extract:gstin", "extract:pan",
]


def extract_bank_value(bank_txn: dict, bank_field: str) -> str | None:
    """`bank_field` is either a direct bank_statements column name, or an
    `extract:vpa`/`extract:gstin`/`extract:pan` sentinel that regex-extracts
    it from narration first (see extract.py)."""
    if bank_field.startswith("extract:"):
        kind = bank_field.split(":", 1)[1]
        narration = bank_txn.get("narration") or ""
        if kind == "vpa":
            return extract.extract_vpa(narration)
        if kind == "gstin":
            return extract.extract_gstin(narration)
        if kind == "pan":
            return extract.extract_pan(narration)
        return None
    return bank_txn.get(bank_field)


async def find_matches(bank_txn: dict, ctx: RuleContext, config: dict) -> list[dict]:
    """Every candidate row (from `config['source']`, already loaded on
    `ctx`) whose `config['source_field']` matches the bank_txn's
    `config['bank_field']` per `config['matcher']`. Empty list on any
    missing/malformed config key or an empty bank-side value - never
    raises, matching every other rule's "no match" convention (config
    validity is checked once, at rule-creation time, by
    ReconciliationService.create_rule - not re-validated per bank_txn)."""
    matcher_name = config.get("matcher")
    bank_field = config.get("bank_field")
    source = config.get("source")
    source_field = config.get("source_field")
    if not (matcher_name and bank_field and source and source_field):
        return []

    bank_value = extract_bank_value(bank_txn, bank_field)
    if not bank_value:
        return []

    if matcher_name == TRIGRAM_MATCHER:
        if source != "customers" or source_field != "company_name":
            return []  # the only pg_trgm-indexed column (migration 0027)
        match = await fuzzy.best_fuzzy_match(
            ctx.conn, entity_id=ctx.entity_id, probe_name=bank_value,
            min_similarity=config.get("min_similarity", 0.85),
        )
        return [match] if match else []

    matcher_fn = MATCHER_REGISTRY.get(matcher_name)
    if matcher_fn is None:
        return []
    candidates = getattr(ctx, _SOURCE_ATTR.get(source, ""), [])
    return [
        cand for cand in candidates
        if cand.get(source_field) and matcher_fn(bank_value, str(cand[source_field]), config)
    ]
