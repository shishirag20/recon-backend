"""Phase 0 (INTAKE_VALIDATION) + Phase 1a (CUSTOMER_LOCK) rules - `dup-utr`
plus the 6 identification rules from the plan's default AR rule catalog
(constants.py). First-match-wins within each phase:
app/reconciliation/engine.py's `run_phase_1` evaluates `dup-utr` in its own
pre-pass, before the CUSTOMER_LOCK loop even starts - a reject there means
Phase 1a/1b never run for that bank_txn at all. Both phases share this
module and the `IDENTIFICATION_RULES` registry since `dup-utr` is, in every
other respect, an identification-adjacent rule (same `RuleContext`, same
`IdentificationResult` return type).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from app.reconciliation import extract, fuzzy
from app.reconciliation.rules import matchers
from app.reconciliation.rules import IdentificationResult, RuleContext

RuleFn = Callable[[dict, RuleContext, dict], Awaitable[IdentificationResult]]


async def dup_utr_check(bank_txn: dict, ctx: RuleContext, config: dict) -> IdentificationResult:
    """Rejects a bank_txn whose `bank_reference` either collides with another
    PENDING row in this same run (checked in Python - the whole candidate set
    is already loaded into `ctx.duplicate_refs_in_run` by the engine before
    any rule runs) or already belongs to a row `recon_status='MATCHED'` from
    a prior run (checked here, since that's a live DB fact this rule alone
    needs)."""
    reference = bank_txn.get("bank_reference")
    if not reference:
        return IdentificationResult()
    if reference in ctx.duplicate_refs_in_run:
        return IdentificationResult(reject=True, reason=f"duplicate bank_reference {reference!r} in this run")
    if await ctx.dao.bank_reference_already_matched(ctx.entity_id, reference, bank_txn["bank_txn_id"]):
        return IdentificationResult(reject=True, reason=f"bank_reference {reference!r} already MATCHED in a prior run")
    return IdentificationResult()


async def utr_match(bank_txn: dict, ctx: RuleContext, config: dict) -> IdentificationResult:
    """1.1a - bank_reference exactly equals a customer's expected_remittances.utr_number."""
    reference = bank_txn.get("bank_reference")
    if not reference:
        return IdentificationResult()
    for remittance in ctx.expected_remittances:
        if remittance["utr_number"] and remittance["utr_number"].strip().upper() == reference.strip().upper():
            return IdentificationResult(customer_id=str(remittance["customer_id"]), reason="expected_remittances.utr_number exact match")
    return IdentificationResult()


async def bank_account_match(bank_txn: dict, ctx: RuleContext, config: dict) -> IdentificationResult:
    """1.2a - payer's account number AND IFSC both exactly match a registered
    customer_bank_accounts row. Both fields required - an account-number-only
    match (no IFSC, or a differing one) is deliberately left for 1.1b's
    suffix pooling instead of guessed here."""
    account_no = (bank_txn.get("payer_account_no") or "").strip()
    ifsc = (bank_txn.get("payer_ifsc") or "").strip()
    if not account_no or not ifsc:
        return IdentificationResult()
    for acct in ctx.bank_accounts:
        if (acct["bank_account_no"] or "").strip() == account_no and (acct["ifsc_code"] or "").strip().upper() == ifsc.upper():
            return IdentificationResult(customer_id=str(acct["customer_id"]), reason="customer_bank_accounts exact account+IFSC match")
    return IdentificationResult()


async def vpa_match(bank_txn: dict, ctx: RuleContext, config: dict) -> IdentificationResult:
    """1.3a - a UPI VPA extracted from narration matches customers.vpa_handle."""
    vpa = extract.extract_vpa(bank_txn.get("narration") or "")
    if not vpa:
        return IdentificationResult()
    for customer in ctx.customers:
        if customer["vpa_handle"] and customer["vpa_handle"].strip().lower() == vpa.strip().lower():
            return IdentificationResult(customer_id=str(customer["customer_id"]), reason=f"VPA {vpa!r} in narration matches customers.vpa_handle")
    return IdentificationResult()


async def reference_code_match(bank_txn: dict, ctx: RuleContext, config: dict) -> IdentificationResult:
    """1.4a - a known customer code found as a substring of narration.
    Checks `customers.customer_code` directly (the primary natural key, per
    app/datahub/canonical.py's customer-resolution order) and
    `customer_reference_codes.code_value` (the ERP-alternate-code fallback)."""
    narration = bank_txn.get("narration") or ""
    for customer in ctx.customers:
        if customer["customer_code"] and extract.contains_substring(narration, customer["customer_code"]):
            return IdentificationResult(customer_id=str(customer["customer_id"]), reason=f"customer_code {customer['customer_code']!r} in narration")
    for ref in ctx.reference_codes:
        if extract.contains_substring(narration, ref["code_value"]):
            return IdentificationResult(customer_id=str(ref["customer_id"]), reason=f"reference code {ref['code_value']!r} in narration")
    return IdentificationResult()


async def gstin_pan_match(bank_txn: dict, ctx: RuleContext, config: dict) -> IdentificationResult:
    """1.5a - a GSTIN (preferred) or PAN extracted from narration matches customers.gstin/.pan."""
    narration = bank_txn.get("narration") or ""
    gstin = extract.extract_gstin(narration)
    if gstin:
        for customer in ctx.customers:
            if customer["gstin"] and customer["gstin"].strip().upper() == gstin:
                return IdentificationResult(customer_id=str(customer["customer_id"]), reason=f"GSTIN {gstin!r} in narration")
    pan = extract.extract_pan(narration)
    if pan:
        for customer in ctx.customers:
            if customer["pan"] and customer["pan"].strip().upper() == pan:
                return IdentificationResult(customer_id=str(customer["customer_id"]), reason=f"PAN {pan!r} in narration")
    return IdentificationResult()


async def fuzzy_name_match(bank_txn: dict, ctx: RuleContext, config: dict) -> IdentificationResult:
    """1.6a - trigram similarity between the payer name and customers.company_name."""
    payer_name = bank_txn.get("payer_name")
    if not payer_name:
        return IdentificationResult()
    min_similarity = config.get("min_similarity", 0.85)
    match = await fuzzy.best_fuzzy_match(ctx.conn, entity_id=ctx.entity_id, probe_name=payer_name, min_similarity=min_similarity)
    if match:
        return IdentificationResult(
            customer_id=str(match["customer_id"]),
            reason=f"fuzzy match {payer_name!r} ~ {match['company_name']!r} (score={match['score']:.2f})",
        )
    return IdentificationResult()


async def generic_field_match(bank_txn: dict, ctx: RuleContext, config: dict) -> IdentificationResult:
    """Config-driven Phase 0/1a rule (`kind="field-match"`) - see
    matchers.find_matches for what `config` needs. Stops at the first
    match, same "first-match-wins, no ambiguity check" behavior the
    existing simple rules (utr_match, vpa_match, ...) already have - it's
    not a new policy, just generalized."""
    found = await matchers.find_matches(bank_txn, ctx, config)
    if not found:
        return IdentificationResult()
    match = found[0]
    return IdentificationResult(
        customer_id=str(match["customer_id"]),
        reason=f"field-match ({config.get('matcher')}): {config.get('bank_field')} ~ {config.get('source')}.{config.get('source_field')}",
    )


def narration_invoice_owner(narration: str, all_open_invoices: list[dict]) -> dict | None:
    """The "Invoice Number in Narration" cross-check (kind
    `invoice-number-in-narration`). Deliberately NOT in `IDENTIFICATION_RULES`
    - it doesn't compete in the first-match-wins CUSTOMER_LOCK loop, it's
    called directly by `engine.py::run_phase_1` and reconciled against
    whatever that loop separately decides.

    Same substring check as Phase 2's `exact-invoice-num`
    (allocation.py::invoice_number_match), just unscoped: it searches every
    open invoice for this entity, not one customer's, since the whole point
    is to catch a narration referencing a *different* customer's invoice than
    the one Phase 1a is about to lock."""
    if not narration:
        return None
    for inv in all_open_invoices:
        if extract.contains_substring(narration, inv["invoice_number"]):
            return {
                "customer_id": str(inv["customer_id"]),
                "invoice_id": str(inv["invoice_id"]),
                "invoice_number": inv["invoice_number"],
            }
    return None


IDENTIFICATION_RULES: dict[str, RuleFn] = {
    "dup-utr": dup_utr_check,
    "expected-utr": utr_match,
    "account-ifsc": bank_account_match,
    "upi": vpa_match,
    "customer-code": reference_code_match,
    "gstin-pan": gstin_pan_match,
    "fuzzy-name": fuzzy_name_match,
    "field-match": generic_field_match,
}
