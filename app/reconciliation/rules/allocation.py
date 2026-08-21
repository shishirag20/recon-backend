"""Phase 2 (ALLOCATION) rules - the 9 rules from the plan's default AR rule
catalog (constants.py), evaluated per (payment, candidate customer) pair in
`(phase, priority)` order, first-match-wins, same pattern as
identification.py/pooling.py.

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

import itertools
from typing import Awaitable, Callable

from app.reconciliation import extract
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


def _matched_number(narration: str, inv: dict) -> str | None:
    """invoice_number or document_number (migration 0033), whichever
    actually appears in narration - either is equally strong evidence of
    which invoice this is (see narration_invoice_owner's docstring)."""
    if extract.contains_substring(narration, inv["invoice_number"]):
        return inv["invoice_number"]
    if inv.get("document_number") and extract.contains_substring(narration, inv["document_number"]):
        return inv["document_number"]
    return None


async def invoice_number_match(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.1 - the full invoice number (or document_number) appears verbatim
    in narration. Allocates whatever the payment can cover
    (min(payment, balance)) - a shortfall here still identifies the right
    invoice, it just doesn't close it; the engine raises Short-Pay for the
    remainder, not this rule.

    Also searches ctx.unresolved_invoices (entity-wide, invoices ingested
    without a resolvable customer_code - migration 0031) if this customer's
    own invoices come up empty. A literal number match in narration is
    self-sufficient evidence of ownership, so a hit there backfills the
    invoice's customer_id rather than leaving it permanently orphaned."""
    narration = bank_txn.get("narration") or ""
    amount = payment["total_received_minor"]
    for inv in _open_invoices(ctx, customer_id):
        matched = _matched_number(narration, inv)
        if matched is not None:
            cash = min(amount, inv["balance_due_minor"])
            match_type = "EXACT" if cash == inv["balance_due_minor"] else "PARTIAL"
            return AllocationOutcome(
                allocations=[InvoiceAllocation(inv["invoice_id"], cash)],
                match_type=match_type, reason=f"{matched!r} in narration",
            )
    for inv in ctx.unresolved_invoices:
        matched = _matched_number(narration, inv)
        if matched is not None:
            await _promote_unresolved_invoice(ctx, inv, customer_id)
            cash = min(amount, inv["balance_due_minor"])
            match_type = "EXACT" if cash == inv["balance_due_minor"] else "PARTIAL"
            return AllocationOutcome(
                allocations=[InvoiceAllocation(inv["invoice_id"], cash)],
                match_type=match_type,
                reason=f"{matched!r} in narration (customer resolved via this match)",
            )
    return AllocationOutcome()


async def truncated_suffix_match(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.2 - only a 4+ digit numeric block from the tail of the invoice
    number appears in narration (e.g. "1046" for INV-2026-1046). Also
    searches ctx.unresolved_invoices once this customer's own invoices come
    up empty - see invoice_number_match's docstring for why that's safe here
    specifically (unlike the balance-based rules below)."""
    min_length = config.get("min_length", 4)
    narration = bank_txn.get("narration") or ""
    blocks = extract.extract_numeric_blocks(narration, min_length=min_length)
    amount = payment["total_received_minor"]
    for inv in _open_invoices(ctx, customer_id):
        if any(inv["invoice_number"].endswith(block) for block in blocks):
            cash = min(amount, inv["balance_due_minor"])
            match_type = "EXACT" if cash == inv["balance_due_minor"] else "PARTIAL"
            return AllocationOutcome(
                allocations=[InvoiceAllocation(inv["invoice_id"], cash)],
                match_type=match_type, reason=f"invoice number suffix in narration",
            )
    for inv in ctx.unresolved_invoices:
        if any(inv["invoice_number"].endswith(block) for block in blocks):
            await _promote_unresolved_invoice(ctx, inv, customer_id)
            cash = min(amount, inv["balance_due_minor"])
            match_type = "EXACT" if cash == inv["balance_due_minor"] else "PARTIAL"
            return AllocationOutcome(
                allocations=[InvoiceAllocation(inv["invoice_id"], cash)],
                match_type=match_type,
                reason="invoice number suffix in narration (customer resolved via this match)",
            )
    return AllocationOutcome()


async def exact_balance_match(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.3 - the payment exactly equals exactly one open invoice's balance.
    A tie between two+ invoices of the same balance is a deliberate refusal
    to guess (`ambiguous=True`) - the engine turns that into a
    MULTIPLE_INVOICE_MATCH exception, per `config['tie_break']`."""
    amount = payment["total_received_minor"]
    ties = [inv for inv in _open_invoices(ctx, customer_id) if inv["balance_due_minor"] == amount]
    if len(ties) == 1:
        inv = ties[0]
        return AllocationOutcome(allocations=[InvoiceAllocation(inv["invoice_id"], amount)], match_type="EXACT", reason="exact balance match")
    if len(ties) > 1:
        return AllocationOutcome(ambiguous=True, ambiguous_invoice_ids=[inv["invoice_id"] for inv in ties], reason=f"{len(ties)} invoices tie on exact balance {_rupees(amount)}")
    return AllocationOutcome()


def tds_adjusted_close(amount: int, inv: dict) -> tuple[int, str] | None:
    """`(computed_tds, reason)` if `amount` equals this one invoice's
    balance net of TDS withheld at source, else None. The effective TDS
    amount is computed here from `tds_rate_pct` (a percentage) rather than
    trusting a pre-populated `allowed_tds_minor`, since no ingestion mapping
    can derive that product today (docs/reconciliation.md §8) -
    `allowed_tds_minor` is used as a fallback if it's already set. Pulled
    out of tds_net_match so engine.py's no-customer direct-match path
    (_commit_direct_match) can apply the same check to a single already-
    identified invoice, without needing a customer-scoped invoice list to
    search."""
    tds_rate = inv.get("tds_rate_pct")
    computed_tds = int(round(inv["total_amount_minor"] * float(tds_rate) / 100)) if tds_rate else (inv.get("allowed_tds_minor") or 0)
    if computed_tds <= 0:
        return None
    if inv["balance_due_minor"] - computed_tds == amount:
        return computed_tds, f"amount matches balance net of {_rupees(computed_tds)} TDS"
    return None


async def tds_net_match(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.4 - payment equals balance net of TDS withheld at source."""
    amount = payment["total_received_minor"]
    for inv in _open_invoices(ctx, customer_id):
        result = tds_adjusted_close(amount, inv)
        if result is not None:
            _, reason = result
            return AllocationOutcome(
                allocations=[InvoiceAllocation(inv["invoice_id"], amount, close_full=True)],
                match_type="TOLERANCE", reason=reason,
            )
    return AllocationOutcome()


async def subset_sum_fifo(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.5 - a combination of 2+ open invoices (oldest due date first, up to
    `config['max_invoices']`) whose balances sum exactly to the payment. A
    single-invoice exact match is exact-amount's job (earlier
    priority) - this only searches combinations of size >= 2."""
    amount = payment["total_received_minor"]
    max_invoices = config.get("max_invoices", 10)
    invoices = _open_invoices(ctx, customer_id)[:max_invoices]  # already sorted by due_date
    for size in range(2, len(invoices) + 1):
        for combo in itertools.combinations(invoices, size):
            if sum(inv["balance_due_minor"] for inv in combo) == amount:
                return AllocationOutcome(
                    allocations=[InvoiceAllocation(inv["invoice_id"], inv["balance_due_minor"], close_full=True) for inv in combo],
                    match_type="SUBSET_SUM", reason=f"{size} invoices sum exactly to the payment",
                )
    return AllocationOutcome()


def fee_tolerance_close(amount: int, inv: dict, explicit_fee: int) -> str | None:
    """Reason string if the shortfall on this one invoice exactly equals
    `explicit_fee`, else None. Pulled out of fee_tolerance_match for the
    same reason as tds_adjusted_close above - see its docstring."""
    if explicit_fee > 0 and inv["balance_due_minor"] - amount == explicit_fee:
        return f"shortfall {_rupees(explicit_fee)} matches the row's own bank fee"
    return None


async def fee_tolerance_match(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.6 - the shortfall exactly equals the bank row's own
    `explicit_fee_minor` (the fee is decoupled from the invoice, not treated
    as an unexplained partial payment). Only fires when the bank row actually
    declares a fee - an unexplained residual with no `explicit_fee_minor` is
    deliberately left to `write-off` (priority 7) instead of being guessed at
    here. `config['amount']['value_minor']` is unused; kept in the catalog
    row for backward compatibility with any saved config, not read. Only
    fires when exactly one open invoice qualifies; 2+ is left unresolved
    rather than guessed."""
    amount = payment["total_received_minor"]
    explicit_fee = bank_txn.get("explicit_fee_minor") or 0
    if explicit_fee <= 0:
        return AllocationOutcome()

    invoices = _open_invoices(ctx, customer_id)
    fee_matches = [inv for inv in invoices if fee_tolerance_close(amount, inv, explicit_fee) is not None]
    if len(fee_matches) == 1:
        inv = fee_matches[0]
        return AllocationOutcome(
            allocations=[InvoiceAllocation(inv["invoice_id"], amount, close_full=True)],
            match_type="TOLERANCE", reason=fee_tolerance_close(amount, inv, explicit_fee),
        )
    return AllocationOutcome()  # 0 or 2+ fee-matches - ambiguous or no match, don't guess


def dust_writeoff_close(amount: int, inv: dict, threshold: int) -> str | None:
    """Reason string if the residual on this one invoice is small enough to
    write off, else None. Pulled out of dust_writeoff for the same reason
    as tds_adjusted_close above - see its docstring."""
    if 0 < inv["balance_due_minor"] - amount <= threshold:
        return f"residual within threshold ({_rupees(threshold)}), written off"
    return None


def resolve_dust_threshold(config: dict) -> int:
    """Same fallback chain dust_writeoff itself uses, pulled out so
    engine.py's direct-match path reads the identical threshold the normal
    write-off rule would (its own `reconciliation_rules.config`), not a
    second hardcoded default that could drift from it."""
    threshold = config.get("amount", {}).get("value_minor")
    if threshold is None:
        val = config.get("max_writeoff_amount") or config.get("materiality_threshold") or 500
        threshold = int(val * 100) if val < 50 else int(val)
    return threshold


async def dust_writeoff(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.7 - a residual so small (config['amount']['value_minor'], e.g. ₹5)
    it's not worth carrying forward - write it off rather than leaving the
    invoice open indefinitely for a trivial amount."""
    amount = payment["total_received_minor"]
    threshold = resolve_dust_threshold(config)
    matches = [inv for inv in _open_invoices(ctx, customer_id) if dust_writeoff_close(amount, inv, threshold) is not None]
    if len(matches) == 1:
        inv = matches[0]
        return AllocationOutcome(
            allocations=[InvoiceAllocation(inv["invoice_id"], amount, close_full=True)],
            match_type="TOLERANCE", reason=dust_writeoff_close(amount, inv, threshold),
        )
    return AllocationOutcome()


async def overpay_on_account(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.8 - the payment exceeds an open invoice's balance. Targets whichever
    invoice has the *smallest* excess (closest match) rather than an
    arbitrary one - the invoice closes fully, and the excess is left as
    on-account credit via the normal payments.unapplied_minor bookkeeping
    (no separate exception - an overpayment isn't a problem needing review)."""
    amount = payment["total_received_minor"]
    overpaid = [(inv, amount - inv["balance_due_minor"]) for inv in _open_invoices(ctx, customer_id) if inv["balance_due_minor"] < amount]
    if not overpaid:
        return AllocationOutcome()
    inv, excess = min(overpaid, key=lambda pair: pair[1])
    return AllocationOutcome(
        allocations=[InvoiceAllocation(inv["invoice_id"], inv["balance_due_minor"])],
        match_type="TOLERANCE", reason=f"closest invoice fully settled, {_rupees(excess)} excess on-account",
    )


async def partial_pay(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.9 - universal fallback: nothing else identified a target invoice at
    all, so apply the payment to the customer's oldest open invoice (by due
    date). Always leaves the invoice open (or the engine raises Short-Pay
    for the remainder) - this rule doesn't invent a full match."""
    invoices = _open_invoices(ctx, customer_id)
    if not invoices:
        return AllocationOutcome()
    inv = invoices[0]  # already sorted oldest-due-first
    cash = min(payment["total_received_minor"], inv["balance_due_minor"])
    return AllocationOutcome(
        allocations=[InvoiceAllocation(inv["invoice_id"], cash)],
        match_type="PARTIAL", reason="universal partial-payment fallback (oldest open invoice)",
    )


ALLOCATION_RULES: dict[str, RuleFn] = {
    "exact-invoice-num": invoice_number_match,
    "invoice-suffix": truncated_suffix_match,
    "exact-amount": exact_balance_match,
    "tds-match": tds_net_match,
    "subset-sum": subset_sum_fifo,
    "bank-fee": fee_tolerance_match,
    "write-off": dust_writeoff,
    "overpayment": overpay_on_account,
    "partial-payment": partial_pay,
}
