"""Phase 2 (ALLOCATION) rules - the 5 rules from the plan's default AR rule
catalog (constants.py), evaluated per (payment, candidate customer) pair in
`(phase, priority)` order, first-match-wins, same pattern as
identification.py/pooling.py.

TDS/bank-fee/dust-write-off variance and overpayment used to be four
separate standalone rules here (tds-match, bank-fee, write-off, overpayment)
that only ever fired as their own late-priority fallback pass over the whole
open-invoice set. They're gone now (2026-08 note) - every rule below runs
the same settlement check (`resolve_invoice_settlement`) against whichever
invoice(s) it identifies by number/suffix/amount/combo, so a narration match
that's short by a withheld TDS amount, or a payment that overshoots a
balance, is handled inline by whichever rule found the invoice, not by a
separate rule several priorities later. See `resolve_invoice_settlement`'s
docstring.

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


def _matched_number(narration: str, inv: dict) -> str | None:
    """invoice_number or document_number (migration 0033), whichever
    actually appears in narration - either is equally strong evidence of
    which invoice this is (see narration_invoice_owner's docstring)."""
    if extract.contains_substring(narration, inv["invoice_number"]):
        return inv["invoice_number"]
    if inv.get("document_number") and extract.contains_substring(narration, inv["document_number"]):
        return inv["document_number"]
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
    exact-amount, subset-sum, and engine.py's no-customer direct-match) asks
    "how does `amount` settle this invoice" - replaces tds-match/bank-fee/
    write-off/overpayment as standalone catalog rules (2026-08 note). Checked
    in priority order (TDS, then bank fee, then dust write-off, then
    overpayment) so the first applicable variance wins, same ordering those
    four rules used to have relative to each other."""
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
    for inv in _open_invoices(ctx, customer_id):
        matched = _matched_number(narration, inv)
        if matched is not None:
            return _settled_outcome(inv, amount, bank_txn, f"{matched!r} in narration")
    for inv in ctx.unresolved_invoices:
        matched = _matched_number(narration, inv)
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
    specifically (unlike the balance-based rules below)."""
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


async def exact_balance_match(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.3 - the payment fully closes exactly one open invoice, at par or
    via an absorbed TDS/bank-fee/dust-write-off variance (previously three
    separate standalone rules - see module docstring). A tie between two+
    invoices that would each close exactly (or via the same kind of
    variance) is a deliberate refusal to guess (`ambiguous=True`) - the
    engine turns that into a MULTIPLE_INVOICE_MATCH exception.

    Overpayment is handled separately below: unlike an exact-balance tie,
    "which invoice is closest to being fully settled" always has a
    deterministic answer (the smallest excess), so it never needs the
    ambiguity refusal - unchanged from the old standalone overpay-on-account
    rule's behavior."""
    amount = payment["total_received_minor"]
    closes = [(inv, resolve_invoice_settlement(amount, inv, bank_txn)) for inv in _open_invoices(ctx, customer_id)]

    exacts = [(inv, s) for inv, s in closes if s.status in ("EXACT", "VARIANCE")]
    if len(exacts) == 1:
        inv, settle = exacts[0]
        reason = "exact balance match" if settle.status == "EXACT" else settle.reason
        match_type = "EXACT" if settle.status == "EXACT" else "TOLERANCE"
        return AllocationOutcome(
            allocations=[
                InvoiceAllocation(inv["invoice_id"], settle.cash_minor, close_full=settle.close_full, gap_role=settle.gap_role)
            ],
            match_type=match_type,
            reason=reason,
        )
    if len(exacts) > 1:
        return AllocationOutcome(
            ambiguous=True,
            ambiguous_invoice_ids=[inv["invoice_id"] for inv, _ in exacts],
            reason=f"{len(exacts)} invoices tie on {_rupees(amount)}",
        )

    overpaid = [(inv, s) for inv, s in closes if s.status == "OVERPAID"]
    if overpaid:
        inv, settle = min(overpaid, key=lambda pair: amount - pair[0]["balance_due_minor"])
        return AllocationOutcome(
            allocations=[
                InvoiceAllocation(inv["invoice_id"], settle.cash_minor, close_full=settle.close_full, gap_role=settle.gap_role)
            ],
            match_type="TOLERANCE",
            reason=settle.reason,
        )
    return AllocationOutcome()


def _subset_sum_variants(inv: dict, bank_txn: dict) -> list[tuple[int, str | None]]:
    """`(contribution_minor, gap_role)` options this one invoice can
    contribute to a subset-sum combo's target total - its full balance
    (`gap_role=None`), or (if applicable) its TDS/bank-fee-adjusted net
    amount. Lets a combo containing e.g. one TDS-withheld invoice still sum
    exactly to the payment, instead of subset-sum only ever seeing raw
    balances - the example that prompted this: a remittance where one of
    several invoices being paid together already had TDS deducted at
    source."""
    balance = inv["balance_due_minor"]
    variants: list[tuple[int, str | None]] = [(balance, None)]

    tds = _tds_net_amount(inv)
    if tds is not None:
        _, net_amount = tds
        variants.append((net_amount, GAP_ROLE_BY_RULE_KIND["tds-match"]))

    explicit_fee = bank_txn.get("explicit_fee_minor") or 0
    if explicit_fee > 0:
        variants.append((balance - explicit_fee, GAP_ROLE_BY_RULE_KIND["bank-fee"]))

    return variants


async def subset_sum_fifo(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.4 - a combination of 2+ open invoices (oldest due date first, up to
    `config['max_invoices']`) whose balances sum exactly to the payment. A
    single-invoice exact match is exact-amount's job (earlier priority) -
    this only searches combinations of size >= 2. Each invoice in a combo
    can contribute either its raw balance or its TDS/bank-fee-adjusted
    amount to the target sum (`_subset_sum_variants`), so a combo with one
    TDS-withheld invoice still matches - bounded by `max_invoices` since the
    per-invoice variant count multiplies the search."""
    amount = payment["total_received_minor"]
    max_invoices = config.get("max_invoices", 10)
    invoices = _open_invoices(ctx, customer_id)[:max_invoices]  # already sorted by due_date
    for size in range(2, len(invoices) + 1):
        for combo in itertools.combinations(invoices, size):
            variant_lists = [_subset_sum_variants(inv, bank_txn) for inv in combo]
            for picks in itertools.product(*variant_lists):
                if sum(contrib for contrib, _ in picks) == amount:
                    allocations = [
                        InvoiceAllocation(inv["invoice_id"], contrib, close_full=True, gap_role=gap_role)
                        for inv, (contrib, gap_role) in zip(combo, picks)
                    ]
                    variance_count = sum(1 for _, gap_role in picks if gap_role)
                    reason = f"{size} invoices sum exactly to the payment"
                    if variance_count:
                        reason += f" ({variance_count} with a TDS/fee variance absorbed)"
                    return AllocationOutcome(allocations=allocations, match_type="SUBSET_SUM", reason=reason)
    return AllocationOutcome()


async def partial_pay(payment: dict, bank_txn: dict, customer_id: str, ctx: AllocationContext, config: dict) -> AllocationOutcome:
    """2.5 - universal fallback: nothing else identified a target invoice at
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
    "subset-sum": subset_sum_fifo,
    "partial-payment": partial_pay,
}
