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


def _open_invoices(ctx: AllocationContext, customer_id: str) -> list[dict]:
    return ctx.invoices_by_customer.get(customer_id, [])


async def invoice_number_match(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.1 - the full invoice number appears verbatim in narration. Allocates
    whatever the payment can cover (min(payment, balance)) - a shortfall here
    still identifies the right invoice, it just doesn't close it; the engine
    raises Short-Pay for the remainder, not this rule."""
    narration = bank_txn.get("narration") or ""
    amount = payment["total_received_minor"]
    for inv in _open_invoices(ctx, customer_id):
        if extract.contains_substring(narration, inv["invoice_number"]):
            cash = min(amount, inv["balance_due_minor"])
            match_type = "EXACT" if cash == inv["balance_due_minor"] else "PARTIAL"
            return AllocationOutcome(
                allocations=[InvoiceAllocation(inv["invoice_id"], cash)],
                match_type=match_type, reason=f"invoice_number {inv['invoice_number']!r} in narration",
            )
    return AllocationOutcome()


async def truncated_suffix_match(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.2 - only a 4+ digit numeric block from the tail of the invoice
    number appears in narration (e.g. "1046" for INV-2026-1046)."""
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
        return AllocationOutcome(ambiguous=True, ambiguous_invoice_ids=[inv["invoice_id"] for inv in ties], reason=f"{len(ties)} invoices tie on exact balance {amount}")
    return AllocationOutcome()


async def tds_net_match(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.4 - payment equals balance net of TDS withheld at source. The
    effective TDS amount is computed here from `tds_rate_pct` (from invoice or
    configured rule default, e.g. 10.0% or 2.0%) or `allowed_tds_minor`, with
    optional variance tolerance."""
    amount = payment["total_received_minor"]
    default_tds_rate = config.get("tds_rate_pct") or config.get("rate_pct") or config.get("default_tds_rate_pct")
    tds_tolerance = config.get("amount", {}).get("value_minor") or config.get("tolerance_minor", 0)
    for inv in _open_invoices(ctx, customer_id):
        tds_rate = inv.get("tds_rate_pct") or default_tds_rate
        computed_tds = int(round(inv["total_amount_minor"] * float(tds_rate) / 100)) if tds_rate else (inv.get("allowed_tds_minor") or 0)
        if computed_tds <= 0:
            continue
        shortfall = inv["balance_due_minor"] - amount
        if shortfall == computed_tds or (tds_tolerance > 0 and abs(shortfall - computed_tds) <= tds_tolerance):
            return AllocationOutcome(
                allocations=[InvoiceAllocation(inv["invoice_id"], amount, close_full=True)],
                match_type="TOLERANCE", reason=f"amount matches balance net of {computed_tds} minor TDS",
            )
    return AllocationOutcome()


async def subset_sum_fifo(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.5 - a combination of 2+ open invoices (oldest due date first, up to
    `config['max_invoices']`) whose balances sum exactly to the payment. A
    single-invoice exact match is exact-amount's job (earlier
    priority) - this only searches combinations of size >= 2."""
    amount = payment["total_received_minor"]
    max_invoices = config.get("max_invoices") or config.get("max_combo", 10)
    invoices = _open_invoices(ctx, customer_id)[:max_invoices]  # already sorted by due_date
    for size in range(2, len(invoices) + 1):
        for combo in itertools.combinations(invoices, size):
            if sum(inv["balance_due_minor"] for inv in combo) == amount:
                return AllocationOutcome(
                    allocations=[InvoiceAllocation(inv["invoice_id"], inv["balance_due_minor"], close_full=True) for inv in combo],
                    match_type="SUBSET_SUM", reason=f"{size} invoices sum exactly to the payment",
                )
    return AllocationOutcome()


async def fee_tolerance_match(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.6 - the shortfall exactly equals the bank row's own
    `explicit_fee_minor` (the fee is decoupled from the invoice, not treated
    as an unexplained partial payment), or - failing that - a small
    unexplained shortfall within `config['amount']['value_minor']` tolerance.
    Only fires when exactly one open invoice qualifies; 2+ is left
    unresolved rather than guessed."""
    amount = payment["total_received_minor"]
    explicit_fee = bank_txn.get("explicit_fee_minor") or 0
    tolerance = config.get("amount", {}).get("value_minor") or config.get("max_fee_amount") or 500
    invoices = _open_invoices(ctx, customer_id)

    if explicit_fee > 0:
        fee_matches = [inv for inv in invoices if inv["balance_due_minor"] - amount == explicit_fee]
        if len(fee_matches) == 1:
            inv = fee_matches[0]
            return AllocationOutcome(
                allocations=[InvoiceAllocation(inv["invoice_id"], amount, close_full=True)],
                match_type="TOLERANCE", reason=f"shortfall {explicit_fee} matches the row's own bank fee",
            )
        if fee_matches:
            return AllocationOutcome()  # ambiguous among fee-matches - don't guess, don't fall through partially

    tolerance_matches = [inv for inv in invoices if 0 < inv["balance_due_minor"] - amount <= tolerance]
    if len(tolerance_matches) == 1:
        inv = tolerance_matches[0]
        return AllocationOutcome(
            allocations=[InvoiceAllocation(inv["invoice_id"], amount, close_full=True)],
            match_type="TOLERANCE", reason=f"shortfall within {tolerance} minor-unit tolerance",
        )
    return AllocationOutcome()


async def dust_writeoff(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.7 - a residual so small (config['amount']['value_minor'], e.g. ₹5)
    it's not worth carrying forward - write it off rather than leaving the
    invoice open indefinitely for a trivial amount."""
    amount = payment["total_received_minor"]
    threshold = config.get("amount", {}).get("value_minor")
    if threshold is None:
        val = config.get("max_writeoff_amount") or config.get("materiality_threshold") or 500
        threshold = int(val * 100) if val < 50 else int(val)
    matches = [inv for inv in _open_invoices(ctx, customer_id) if 0 < inv["balance_due_minor"] - amount <= threshold]
    if len(matches) == 1:
        inv = matches[0]
        return AllocationOutcome(
            allocations=[InvoiceAllocation(inv["invoice_id"], amount, close_full=True)],
            match_type="TOLERANCE", reason=f"residual within dust threshold ({threshold}), written off",
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
        match_type="TOLERANCE", reason=f"closest invoice fully settled, {excess} minor excess on-account",
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
