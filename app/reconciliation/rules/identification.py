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
    """Rejects a bank_txn whose `bank_reference` either repeats a PENDING row
    already accounted for earlier in this same run (checked in Python - the
    engine precomputes which specific bank_txn_ids are non-first occurrences
    into `ctx.duplicate_bank_txn_ids` before any rule runs - see
    engine.py::run_phase_1) or already belongs to a row
    `recon_status='MATCHED'` from a prior run (checked here, since that's a
    live DB fact this rule alone needs). Only the *later* occurrence(s) of a
    repeated reference are rejected - the first is left alone to identify/
    match normally, matching this rule's own name ("reject an ALREADY-
    matched reference"), not "reject everyone who shares one" (2026-08 fix -
    two genuinely distinct payments that happen to reuse a reference used to
    both get rejected, including the legitimate first one)."""
    reference = bank_txn.get("bank_reference")
    if not reference:
        return IdentificationResult()
    if bank_txn["bank_txn_id"] in ctx.duplicate_bank_txn_ids:
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
    not a new policy, just generalized.

    `source="invoices"` (2026-08 fix) is the one case a match's own
    `customer_id` can be None - an invoice ingested without a resolvable
    customer_code (migration 0031). Skipped, not taken as-is: `str(None)`
    would otherwise silently lock the payment to the literal string
    "None" instead of a real customer_id. Keeps searching `found` rather
    than giving up entirely - the first candidate happening to be
    customer-less doesn't mean a later one in the same list isn't real."""
    found = await matchers.find_matches(bank_txn, ctx, config)
    for match in found:
        if match["customer_id"] is None:
            continue
        return IdentificationResult(
            customer_id=str(match["customer_id"]),
            reason=f"field-match ({config.get('matcher')}): {config.get('bank_field')} ~ {config.get('source')}.{config.get('source_field')}",
        )
    return IdentificationResult()


async def document_number_match(bank_txn: dict, ctx: RuleContext, config: dict) -> IdentificationResult:
    """1.7a - last resort in the CUSTOMER_LOCK cascade, only reached if
    nothing richer (UTR, account+IFSC, VPA, customer-code, GSTIN/PAN,
    fuzzy-name) found anything. For a remittance with none of that data -
    just a bank narration and an invoice/document number in it - this is the
    only signal left: does the narration reference a real open invoice
    (entity-wide, not scoped to any customer - nobody's been identified
    yet), and does *that invoice* already know its own customer (from ERP
    ingestion)? If so, lock the payment to it.

    Deliberately placed last, not first: it's a real signal but a shorter,
    more collision-prone one than the others (document numbers are often
    short digit strings glued into free-text narration with no delimiter -
    see docs/reconciliation.md's cross-check on the real CMR data), so a
    solid identity match earlier in the cascade should always win first.

    Reuses narration_invoice_owner() - the same search the NARRATION_CHECK
    cross-check (Phase 1c) independently performs after this cascade runs;
    if this rule is what locked the payment, that cross-check will find the
    same invoice and record agreement rather than raise a mismatch.
    `config['match_fields']` (Rules Studio's "Compares" picker) genuinely
    selects which field(s) to check - see narration_invoice_owner."""
    match = narration_invoice_owner(
        bank_txn.get("narration") or "", ctx.all_open_invoices, fields=config.get("match_fields")
    )
    if match is None or match["customer_id"] is None:
        # Either no invoice reference found, or it was found but that
        # invoice itself has no customer yet either (migration 0031) - this
        # rule can only *identify*, it can't invent a customer that doesn't
        # exist anywhere. Falls through to Phase 1b / the narration-pool
        # fallback / Suspense, same as today.
        return IdentificationResult()
    return IdentificationResult(
        customer_id=match["customer_id"],
        reason=f"document/invoice number {match['matched_number']!r} in narration (invoice's own customer)",
    )


DEFAULT_NARRATION_MATCH_FIELDS = ("invoice_number", "document_number")


def narration_invoice_owner(
    narration: str, all_open_invoices: list[dict], fields: list[str] | tuple[str, ...] | None = None
) -> dict | None:
    """The "Invoice Number in Narration" cross-check (kind
    `invoice-number-in-narration`). Deliberately NOT in `IDENTIFICATION_RULES`
    - it doesn't compete in the first-match-wins CUSTOMER_LOCK loop, it's
    called directly by `engine.py::run_phase_1` and reconciled against
    whatever that loop separately decides.

    Same substring check as Phase 2's `exact-invoice-num`
    (allocation.py::invoice_number_match), just unscoped: it searches every
    open invoice for this entity, not one customer's, since the whole point
    is to catch a narration referencing a *different* customer's invoice than
    the one Phase 1a is about to lock.

    Checks both invoice_number and document_number (migration 0033) by
    default - some ERP exports label the customer-facing reference "Document
    Number" rather than "Invoice Number" (see CMR_BOOK_DATA.csv), or carry
    both as genuinely different values. `fields` (from the calling rule's own
    `config['match_fields']`, e.g. `invoice-number-in-narration`'s Rules
    Studio "Compares" picker) can narrow this to just one - genuinely
    respected here now, not just displayed (2026-08 fix)."""
    if not narration:
        return None
    for inv in all_open_invoices:
        matched_number = None
        for field in fields or DEFAULT_NARRATION_MATCH_FIELDS:
            value = inv.get(field)
            if value and extract.contains_substring(narration, value):
                matched_number = value
                break
        if matched_number is not None:
            return {
                # None (migration 0031, an invoice ingested without a
                # resolvable customer) must stay None here, not str(None) -
                # engine.py's mismatch check treats a real customer_id that
                # disagrees with Phase 1a's lock very differently from "this
                # invoice doesn't have one yet to disagree with."
                "customer_id": str(inv["customer_id"]) if inv["customer_id"] is not None else None,
                "invoice_id": str(inv["invoice_id"]),
                "invoice_number": inv["invoice_number"],
                "matched_number": matched_number,
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
    "document-number-narration": document_number_match,
    "field-match": generic_field_match,
}
