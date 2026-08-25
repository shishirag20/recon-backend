"""Phase 2 (ALLOCATION) rules - the 3 rules from the plan's default AR rule
catalog (constants.py), evaluated per (payment, candidate customer) pair in
`(phase, priority)` order, first-match-wins, same pattern as
identification.py/pooling.py.

TDS/bank-fee/dust-write-off variance used to be three separate standalone
rules here (tds-match, bank-fee, write-off) that only ever fired as their
own late-priority fallback pass over the whole open-invoice set. They're
gone (2026-08a) - every rule below runs the same settlement check
(`resolve_invoice_settlement`) against whichever invoice(s) it identifies,
so a narration match that's short by a withheld TDS amount is handled
inline by whichever rule found the invoice, not by a separate rule several
priorities later.

exact-amount/subset-sum/overpayment/partial-payment were later folded into
one rule, `sequential_amount_match` (2026-08b) - see its own docstring for
why a single deterministic oldest-due-first waterfall replaced all four:
searching for a unique exact match, then a unique exact combination, then a
closest-overpay guess, then a bare fallback, all as separate priorities
turned out to just be four increasingly-desperate ways of answering the
same question ("how does this amount distribute across this customer's
open invoices"), each capable of stealing a payment from a later, better
answer.

The period-cutoff and memo-net-off checks the original plan called "Phase
2.0a/2.0b guardrails" are NOT rule callables here, and no longer have a
catalog row at all (see constants.py's `DEFAULT_AR_RULE_CATALOG` comment) -
the period cutoff is baked into `ReconciliationDAO.load_open_invoices`'s
query; the memo net-off is applied once when `engine.py` builds the
`AllocationContext`. Both run unconditionally, same as before - only the
inert, never-actually-read catalog rows were removed.

Every rule receives the *candidate* customer_id explicitly, separate from
`payment['customer_id']` - a pooled payment has no locked customer yet, so
the engine tries each candidate in turn and the rule doesn't need to know
which case it's in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from app.reconciliation import extract
from app.reconciliation.constants import GAP_ROLE_BY_RULE_KIND
from app.reconciliation.rules import AllocationContext, AllocationOutcome, InvoiceAllocation

RuleFn = Callable[[dict, dict, str, AllocationContext, dict], Awaitable[AllocationOutcome]]


def _rupees(amount_minor: int) -> str:
    """Every `reason` string below is user-facing (shown verbatim in the
    frontend's "Resolved Via" column) - amounts must be formatted here, not
    left as raw minor units, or ₹20 renders as "2000"."""
    return f"₹{amount_minor / 100:,.2f}"


def _open_invoices(ctx: AllocationContext, customer_id: str) -> list[dict]:
    return ctx.invoices_by_customer.get(customer_id, [])


async def _promote_unresolved_invoice(ctx: AllocationContext, inv: dict, customer_id: str) -> None:
    """Backfills customer_id on an invoice found via ctx.unresolved_invoices
    (migration 0031) and folds it into the normal per-customer working set,
    so _commit()'s later `invoices_by_customer[customer_id]` lookup finds it
    like any other invoice. Only called once a narration-based invoice-number
    match already ties it to this specific payment's already-identified
    customer - the invoice number itself is the evidence, nothing here is
    guessing who the customer is."""
    await ctx.dao.link_invoice_customer(inv["invoice_id"], customer_id)
    ctx.unresolved_invoices.remove(inv)
    inv["customer_id"] = customer_id
    ctx.invoices_by_customer.setdefault(customer_id, []).append(inv)


DEFAULT_NARRATION_MATCH_FIELDS = ("invoice_number", "document_number")


def _matched_number(narration: str, inv: dict, fields: list[str] | tuple[str, ...] | None = None) -> str | None:
    """Whichever of `fields` (default: invoice_number, then document_number
    - migration 0033) actually appears in narration first - either is
    equally strong evidence of which invoice this is (see
    narration_invoice_owner's docstring). `fields` comes from the calling
    rule's own `config['match_fields']` (Rules Studio's "Compares" picker) -
    genuinely respected here, not just displayed (2026-08 fix)."""
    for field in fields or DEFAULT_NARRATION_MATCH_FIELDS:
        value = inv.get(field)
        if value and extract.contains_substring(narration, value):
            return value
    return None


def _tds_net_amount(inv: dict) -> tuple[int, int] | None:
    """`(computed_tds, balance_net_of_tds)` if this invoice has a TDS rate
    (or a pre-populated `allowed_tds_minor` fallback) applicable, else None.
    The effective TDS amount is computed here from `tds_rate_pct` (a
    percentage) rather than trusting a pre-populated `allowed_tds_minor`,
    since no ingestion mapping can derive that product today
    (docs/reconciliation.md §8)."""
    tds_rate = inv.get("tds_rate_pct")
    computed_tds = (
        int(round(inv["total_amount_minor"] * float(tds_rate) / 100))
        if tds_rate
        else (inv.get("allowed_tds_minor") or 0)
    )
    if computed_tds <= 0:
        return None
    return computed_tds, inv["balance_due_minor"] - computed_tds


def tds_adjusted_close(amount: int, inv: dict) -> tuple[int, str] | None:
    """`(computed_tds, reason)` if `amount` equals this one invoice's
    balance net of TDS withheld at source, else None."""
    net = _tds_net_amount(inv)
    if net is None:
        return None
    computed_tds, net_amount = net
    if net_amount == amount:
        return computed_tds, f"amount matches balance net of {_rupees(computed_tds)} TDS"
    return None


def fee_tolerance_close(amount: int, inv: dict, explicit_fee: int) -> str | None:
    """Reason string if the shortfall on this one invoice exactly equals
    `explicit_fee`, else None. Only meaningful when the bank row actually
    declares a fee (`explicit_fee_minor > 0`) - an unexplained residual with
    no declared fee is left to `dust_writeoff_close` instead of being
    guessed at here."""
    if explicit_fee > 0 and inv["balance_due_minor"] - amount == explicit_fee:
        return f"shortfall {_rupees(explicit_fee)} matches the row's own bank fee"
    return None


def dust_writeoff_close(amount: int, inv: dict, threshold: int) -> str | None:
    """Reason string if the residual on this one invoice is small enough to
    write off, else None."""
    if 0 < inv["balance_due_minor"] - amount <= threshold:
        return f"residual within threshold ({_rupees(threshold)}), written off"
    return None


def resolve_dust_threshold(config: dict) -> int:
    """₹5 (500 minor) default, or `config['amount']['value_minor']`/
    `max_writeoff_amount`/`materiality_threshold` if a caller has one to
    hand - kept as a function (not a bare constant) so a future per-
    definition config source is a one-line change here instead of touching
    every call site."""
    threshold = config.get("amount", {}).get("value_minor")
    if threshold is None:
        val = config.get("max_writeoff_amount") or config.get("materiality_threshold") or 500
        threshold = int(val * 100) if val < 50 else int(val)
    return threshold


@dataclass
class SettlementClose:
    """How one received `amount` settles one invoice - the single
    classification every Phase 2 rule below asks for once it has identified
    its candidate invoice(s), instead of each rule (or a separate later-
    priority rule) re-implementing its own slice of "does this amount close
    the balance" logic:

    - EXACT: amount == balance, no variance.
    - VARIANCE: amount is short of balance by exactly a TDS/bank-fee/dust
      amount - the invoice still closes fully (`close_full=True`), and
      `gap_role` says which GL account absorbs the difference.
    - OVERPAID: amount exceeds balance - the invoice closes fully at its
      balance (`cash_minor` is capped there), and the excess is left as
      unapplied/on-account cash by the caller, same as before.
    - PARTIAL: none of the above - amount only partially covers the
      balance; the invoice stays open for whatever the caller leaves
      unclosed.
    """
    status: str
    cash_minor: int
    close_full: bool
    reason: str
    gap_role: str | None = None


def resolve_invoice_settlement(amount: int, inv: dict, bank_txn: dict) -> SettlementClose:
    """The one place every Phase 2 rule (exact-invoice-num, invoice-suffix,
    sequential-amount-match, and engine.py's no-customer direct-match) asks
    "how does `amount` settle this invoice" - replaces tds-match/bank-fee/
    write-off as standalone catalog rules (2026-08a). Checked in priority
    order (TDS, then bank fee, then dust write-off, then overpayment) so the
    first applicable variance wins, same ordering those rules used to have
    relative to each other."""
    balance = inv["balance_due_minor"]
    if amount == balance:
        return SettlementClose("EXACT", amount, False, "amount matches balance exactly")

    tds = tds_adjusted_close(amount, inv)
    if tds is not None:
        _, reason = tds
        return SettlementClose("VARIANCE", amount, True, reason, GAP_ROLE_BY_RULE_KIND["tds-match"])

    explicit_fee = bank_txn.get("explicit_fee_minor") or 0
    if explicit_fee > 0:
        reason = fee_tolerance_close(amount, inv, explicit_fee)
        if reason is not None:
            return SettlementClose("VARIANCE", amount, True, reason, GAP_ROLE_BY_RULE_KIND["bank-fee"])

    reason = dust_writeoff_close(amount, inv, resolve_dust_threshold({}))
    if reason is not None:
        return SettlementClose("VARIANCE", amount, True, reason, GAP_ROLE_BY_RULE_KIND["write-off"])

    if amount > balance:
        excess_fmt = _rupees(amount - balance)
        return SettlementClose("OVERPAID", balance, True, f"invoice fully settled, {excess_fmt} excess on-account")

    return SettlementClose("PARTIAL", amount, False, "partial payment")


def _settled_outcome(inv: dict, amount: int, bank_txn: dict, label: str) -> AllocationOutcome:
    """Runs `resolve_invoice_settlement` against one already-identified
    invoice and builds the AllocationOutcome every single-invoice rule below
    returns. EXACT/PARTIAL keep `label` (how the invoice was identified,
    e.g. "'INV-107' in narration") as the reason verbatim; VARIANCE/OVERPAID
    append what the settlement classifier found, so a TDS-short or
    overpaid match is never silently mislabeled as a plain exact/partial
    hit."""
    settle = resolve_invoice_settlement(amount, inv, bank_txn)
    reason = label if settle.status in ("EXACT", "PARTIAL") else f"{label} - {settle.reason}"
    match_type = "EXACT" if settle.status == "EXACT" else ("PARTIAL" if settle.status == "PARTIAL" else "TOLERANCE")
    return AllocationOutcome(
        allocations=[
            InvoiceAllocation(inv["invoice_id"], settle.cash_minor, close_full=settle.close_full, gap_role=settle.gap_role)
        ],
        match_type=match_type,
        reason=reason,
    )


async def invoice_number_match(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.1 - the full invoice number (or document_number) appears verbatim
    in narration. Settlement (exact/TDS/fee/dust/overpayment) is decided by
    `_settled_outcome` - a shortfall here still identifies the right
    invoice even if it doesn't fully close it; the engine raises Short-Pay
    for whatever's genuinely left over, not this rule.

    Also searches ctx.unresolved_invoices (entity-wide, invoices ingested
    without a resolvable customer_code - migration 0031) if this customer's
    own invoices come up empty. A literal number match in narration is
    self-sufficient evidence of ownership, so a hit there backfills the
    invoice's customer_id rather than leaving it permanently orphaned."""
    narration = bank_txn.get("narration") or ""
    amount = payment["total_received_minor"]
    fields = config.get("match_fields")
    for inv in _open_invoices(ctx, customer_id):
        matched = _matched_number(narration, inv, fields)
        if matched is not None:
            return _settled_outcome(inv, amount, bank_txn, f"{matched!r} in narration")
    for inv in ctx.unresolved_invoices:
        matched = _matched_number(narration, inv, fields)
        if matched is not None:
            await _promote_unresolved_invoice(ctx, inv, customer_id)
            return _settled_outcome(
                inv, amount, bank_txn, f"{matched!r} in narration (customer resolved via this match)"
            )
    return AllocationOutcome()


async def truncated_suffix_match(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.2 - only a 4+ digit numeric block from the tail of the invoice
    number appears in narration (e.g. "1046" for INV-2026-1046). Also
    searches ctx.unresolved_invoices once this customer's own invoices come
    up empty - see invoice_number_match's docstring for why that's safe here
    specifically (unlike the balance-based rule below)."""
    min_length = config.get("min_length", 4)
    narration = bank_txn.get("narration") or ""
    blocks = extract.extract_numeric_blocks(narration, min_length=min_length)
    amount = payment["total_received_minor"]
    for inv in _open_invoices(ctx, customer_id):
        if any(inv["invoice_number"].endswith(block) for block in blocks):
            return _settled_outcome(inv, amount, bank_txn, "invoice number suffix in narration")
    for inv in ctx.unresolved_invoices:
        if any(inv["invoice_number"].endswith(block) for block in blocks):
            await _promote_unresolved_invoice(ctx, inv, customer_id)
            return _settled_outcome(
                inv, amount, bank_txn, "invoice number suffix in narration (customer resolved via this match)"
            )
    return AllocationOutcome()


def sequential_waterfall(amount: int, invoices: list[dict], bank_txn: dict) -> tuple[list[InvoiceAllocation], list[SettlementClose]]:
    """The actual waterfall step, pulled out of `sequential_amount_match` so
    engine.py's no-customer group-match path (Sequential Narration Match)
    can drive the identical algorithm across a narration-matched invoice
    group instead of a customer's own invoice list (2026-08d) - this
    function doesn't know or care where `invoices` came from, only that
    it's ordered oldest-due-first. At each invoice, `resolve_invoice_
    settlement` is asked "how does whatever's left of `amount` settle this
    one" - its four-way answer already *is* the waterfall step: EXACT/
    VARIANCE closes the invoice and consumes the rest of the amount (loop
    ends), OVERPAID closes the invoice at its balance and carries the
    excess into the next invoice (loop continues), PARTIAL consumes the
    rest without closing the invoice (loop ends, the caller raises
    Short-Pay for the gap)."""
    allocations: list[InvoiceAllocation] = []
    settles: list[SettlementClose] = []
    remaining = amount
    for inv in invoices:
        if remaining <= 0:
            break
        settle = resolve_invoice_settlement(remaining, inv, bank_txn)
        allocations.append(
            InvoiceAllocation(inv["invoice_id"], settle.cash_minor, close_full=settle.close_full, gap_role=settle.gap_role)
        )
        settles.append(settle)
        remaining -= settle.cash_minor
    return allocations, settles


def waterfall_outcome(allocations: list[InvoiceAllocation], settles: list[SettlementClose]) -> AllocationOutcome:
    """Shapes `sequential_waterfall`'s raw output into an `AllocationOutcome`
    - shared so the customer-scoped rule below and engine.py's no-customer
    group-match commit produce identically-worded reasons/match_types for
    the same underlying settlement pattern, instead of two hand-written
    copies of this reason-string logic drifting apart."""
    if not allocations:
        return AllocationOutcome()

    if len(allocations) == 1:
        settle = settles[0]
        reason = "exact balance match" if settle.status == "EXACT" else settle.reason
        match_type = "EXACT" if settle.status == "EXACT" else ("PARTIAL" if settle.status == "PARTIAL" else "TOLERANCE")
        return AllocationOutcome(allocations=allocations, match_type=match_type, reason=reason)

    variance_count = sum(1 for s in settles if s.status == "VARIANCE")
    reason = f"{len(allocations)} invoices settled sequentially, oldest-due first"
    if variance_count:
        reason += f" ({variance_count} with a TDS/fee/write-off variance absorbed)"
    if settles[-1].status == "PARTIAL":
        reason += ", last one short-paid"
    return AllocationOutcome(allocations=allocations, match_type="ONE_TO_MANY", reason=reason)


async def sequential_amount_match(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.3 - walks this customer's open invoices oldest-due-date-first,
    applying the payment sequentially via `sequential_waterfall`. Replaces
    exact-amount/subset-sum/overpayment/partial-payment (2026-08
    consolidation) - see module docstring for why. Deliberately no
    ambiguity/tie-break refusal here, unlike the old exact_balance_match:
    since invoices are walked in a fixed order rather than searched for a
    unique match, "which invoice does this amount belong to" always has one
    deterministic answer - the oldest one it reaches - even on the rare
    occasion a different invoice elsewhere in the list would also have
    matched exactly on its own."""
    amount = payment["total_received_minor"]
    invoices = _open_invoices(ctx, customer_id)  # already sorted oldest-due-first
    if amount <= 0 or not invoices:
        return AllocationOutcome()
    allocations, settles = sequential_waterfall(amount, invoices, bank_txn)
    return waterfall_outcome(allocations, settles)


ALLOCATION_RULES: dict[str, RuleFn] = {
    "exact-invoice-num": invoice_number_match,
    "invoice-suffix": truncated_suffix_match,
    "sequential-amount-match": sequential_amount_match,
}
