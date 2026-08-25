"""Phase 1b (CANDIDATE_POOL) rules - only evaluated when every Phase 1a rule
missed. Unlike identification rules, a pooling rule never locks a single
customer - it always returns a *list* of candidates (possibly more than one,
e.g. two customers sharing the same masked account suffix - see BANK-013 in
the golden test data, which deliberately collides Silverline Traders and
Silverline Exports). Disambiguating a multi-candidate pool down to one
customer is a Phase 2 (M2) concern, not this module's job.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from app.reconciliation import fuzzy
from app.reconciliation.extract import account_suffix_matches
from app.reconciliation.rules import RuleContext, matchers

RuleFn = Callable[[dict, RuleContext, dict], Awaitable[list[str]]]


async def masked_account_pool(bank_txn: dict, ctx: RuleContext, config: dict) -> list[str]:
    """1.1b - every customer whose registered bank_account_no shares its last
    N digits (config['suffix_length'], default 4) with a digit run found in
    the bank_txn's account field or narration. Checks *every* registered
    account, not just the first hit, deliberately - a shared suffix across
    two different customers is a real ambiguity the engine must surface, not
    silently resolve by picking whichever came first."""
    suffix_length = config.get("suffix_length", 4)
    haystack = f"{bank_txn.get('payer_account_no') or ''} {bank_txn.get('narration') or ''}"
    candidates: list[str] = []
    for acct in ctx.bank_accounts:
        if account_suffix_matches(haystack, acct["bank_account_no"], suffix_length):
            customer_id = str(acct["customer_id"])
            if customer_id not in candidates:
                candidates.append(customer_id)
    return candidates


async def token_pool(bank_txn: dict, ctx: RuleContext, config: dict) -> list[str]:
    """1.2b - every customer with a significant company-name token
    (fuzzy.significant_tokens) present in the bank_txn's narration."""
    narration = bank_txn.get("narration") or ""
    candidates: list[str] = []
    for customer in ctx.customers:
        if fuzzy.token_overlap_match(customer["company_name"], narration):
            customer_id = str(customer["customer_id"])
            if customer_id not in candidates:
                candidates.append(customer_id)
    return candidates


async def generic_field_pool(bank_txn: dict, ctx: RuleContext, config: dict) -> list[str]:
    """Config-driven Phase 1b rule (`kind="field-match"`) - see
    matchers.find_matches. Unlike identification.py's generic_field_match,
    collects every match rather than stopping at the first - Phase 1b never
    locks, by design (same as masked_account_pool/token_pool above).

    `source="invoices"` matches with no `customer_id` (migration 0031 - an
    invoice ingested without a resolvable customer_code) are skipped, same
    reasoning as generic_field_match's guard - a candidate pool is for
    customer_ids, and `str(None)` isn't one."""
    found = await matchers.find_matches(bank_txn, ctx, config)
    candidates: list[str] = []
    for match in found:
        if match["customer_id"] is None:
            continue
        customer_id = str(match["customer_id"])
        if customer_id not in candidates:
            candidates.append(customer_id)
    return candidates


POOLING_RULES: dict[str, RuleFn] = {
    "account-suffix": masked_account_pool,
    "narration-tokens": token_pool,
    "field-match": generic_field_pool,
}
