"""GL posting (M3): turns resolved Phase-1/2 outcomes - plus standalone bank
charges, which never go through Phase 1/2 at all - into real double-entry
journal entries, then runs the SL-vs-GL control proof.

One journal entry per bank_txn, not per invoice_allocation - covering every
line that transaction's cash produced: the cash-vs-AR line(s) for each
invoice it settled, a gap line (TDS withheld / bank fee / dust write-off)
for any invoice closed for less than its full balance, and an unapplied/
leftover line (on-account credit if a customer is known, otherwise
suspense) for whatever wasn't allocated to an invoice at all. Every journal
this module builds balances - sum(DEBIT) == sum(CREDIT) - by construction:
each line pair is emitted together from the same source amount, never
computed independently and reconciled after the fact.
"""
from __future__ import annotations

from datetime import date

import asyncpg

from app.reconciliation.constants import (
    GL_ROLE_AR_CONTROL,
    GL_ROLE_BANK_CHARGES,
    GL_ROLE_CASH_CONTROL,
    GL_ROLE_ON_ACCOUNT_ADVANCE,
    GL_ROLE_SUSPENSE,
    PHASE_GL_CHECK,
)
from app.reconciliation.dao import ReconciliationDAO
from app.reconciliation.rules import get_threshold_minor

_REQUIRED_ROLES = (GL_ROLE_CASH_CONTROL, GL_ROLE_AR_CONTROL, GL_ROLE_SUSPENSE, GL_ROLE_ON_ACCOUNT_ADVANCE, GL_ROLE_BANK_CHARGES)


def _line(gl_account_id: str, dr_cr: str, currency: str, amount_minor: int, business_partner_id: str | None = None) -> dict:
    return {
        "gl_account_id": gl_account_id, "dr_cr": dr_cr, "currency": currency,
        "amount_minor": amount_minor, "business_partner_id": business_partner_id,
    }


async def post_run(
    conn: asyncpg.Connection, dao: ReconciliationDAO, run_id: str, run_context: dict,
    suspense_records: list[dict], payment_ledger_records: list[dict],
) -> dict:
    """Posts every payment this run touched - Phase 1's Suspense rows and
    Phase 2's committed/ambiguous/double-collision/unresolved rows alike -
    then every still-pending standalone bank charge, then runs the control
    proof. Raises if the entity is missing a required gl_account_roles entry
    rather than posting an incomplete/unbalanced journal."""
    entity_id = str(run_context["entity_id"])
    period_end = run_context.get("period_end") or date.today()

    rules = await dao.list_rules(str(run_context["definition_id"]))
    gl_variance_tolerance_minor = get_threshold_minor(rules, PHASE_GL_CHECK)

    roles = await dao.get_gl_account_roles_map(entity_id)
    missing = [r for r in _REQUIRED_ROLES if r not in roles]
    if missing:
        raise ValueError(f"entity {entity_id} is missing gl_account_roles for {missing} - cannot post")

    journal_count = 0
    posted_minor = 0

    # Phase 1 Suspense - unidentified receipts, straight to SUSPENSE. No
    # customer known at all, so there's nothing to credit an AR/on-account
    # line to - the whole amount is a single Dr Cash / Cr Suspense pair.
    for rec in suspense_records:
        lines = [
            _line(roles[GL_ROLE_CASH_CONTROL], "DEBIT", rec["currency"], rec["unapplied_minor"]),
            _line(roles[GL_ROLE_SUSPENSE], "CREDIT", rec["currency"], rec["unapplied_minor"]),
        ]
        await dao.insert_journal(
            entity_id=entity_id, run_id=run_id, posting_date=period_end, source_type="CASH_RECEIPT",
            memo=rec.get("memo") or "Suspense receipt - no customer identified", lines=lines,
        )
        journal_count += 1
        posted_minor += rec["unapplied_minor"]

    # Phase 2 outcomes - every payment record uniformly, regardless of how
    # it resolved: `allocations` is empty for ambiguous/double-collision/
    # unresolved payments (nothing committed), `unapplied_minor` covers the
    # rest for those; a fully-committed payment may have both.
    for rec in payment_ledger_records:
        lines: list[dict] = []
        currency = rec["currency"]
        for alloc in rec["allocations"]:
            cash = alloc["cash_minor"]
            gap = alloc["gap_minor"]
            lines.append(_line(roles[GL_ROLE_CASH_CONTROL], "DEBIT", currency, cash))
            lines.append(_line(roles[GL_ROLE_AR_CONTROL], "CREDIT", currency, cash + gap, business_partner_id=rec["customer_id"]))
            if gap > 0 and alloc["gap_role"]:
                if alloc["gap_role"] not in roles:
                    raise ValueError(f"entity {entity_id} is missing a gl_account_roles entry for {alloc['gap_role']}")
                lines.append(_line(roles[alloc["gap_role"]], "DEBIT", currency, gap))
        if rec["unapplied_minor"] > 0:
            lines.append(_line(roles[GL_ROLE_CASH_CONTROL], "DEBIT", currency, rec["unapplied_minor"]))
            if rec["customer_id"]:
                lines.append(_line(roles[GL_ROLE_ON_ACCOUNT_ADVANCE], "CREDIT", currency, rec["unapplied_minor"], business_partner_id=rec["customer_id"]))
            else:
                lines.append(_line(roles[GL_ROLE_SUSPENSE], "CREDIT", currency, rec["unapplied_minor"]))
        if not lines:
            continue  # nothing moved for this payment - never post an empty/unbalanced journal
        journal_id = await dao.insert_journal(
            entity_id=entity_id, run_id=run_id, posting_date=period_end, source_type="CASH_RECEIPT",
            memo=f"Reconciliation posting for bank_txn {rec['bank_txn_id']}", lines=lines,
        )
        for alloc in rec["allocations"]:
            await dao.link_allocation_journal(alloc["invoice_id"], rec["payment_id"], journal_id)
        journal_count += 1
        posted_minor += sum(l["amount_minor"] for l in lines if l["dr_cr"] == "DEBIT")

    # Standalone bank charges - never touched Phase 1/2 at all (excluded
    # from the working set at the query level in run_phase_1).
    charge_count = 0
    for charge in await dao.list_pending_bank_charges(entity_id):
        lines = [
            _line(roles[GL_ROLE_BANK_CHARGES], "DEBIT", charge["currency"], charge["amount_minor"]),
            _line(roles[GL_ROLE_CASH_CONTROL], "CREDIT", charge["currency"], charge["amount_minor"]),
        ]
        await dao.insert_journal(
            entity_id=entity_id, run_id=run_id, posting_date=charge["transaction_date"], source_type="FEE_ADJUSTMENT",
            memo=charge["narration"], lines=lines,
        )
        await dao.mark_bank_statement_status(charge["bank_txn_id"], "BANK_CHARGE")
        charge_count += 1
        journal_count += 1

    gl_variance = await _run_control_proof(dao, entity_id, roles.get(GL_ROLE_AR_CONTROL), period_end, run_id, gl_variance_tolerance_minor)

    return {
        "journal_count": journal_count,
        "posted_minor": posted_minor,
        "standalone_charge_count": charge_count,
        "gl_variance": gl_variance,
    }


async def _run_control_proof(
    dao: ReconciliationDAO, entity_id: str, ar_control_account_id: str | None, period_end, run_id: str, tolerance_minor: int,
) -> dict | None:
    """Compares the sub-ledger's live AR position (sum of every open
    invoice's balance, entity-wide - not scoped to just this run) against
    the GL's own stated control balance for the same period_date. A missing
    `gl_control_balances` row for that exact date means there's nothing to
    compare against yet - skipped, not treated as a mismatch. A variance at
    or below `tolerance_minor` (config'd via the GL_CHECK phase's threshold
    rule, default 0 - exact match required) is also not treated as a
    mismatch. Returns the variance detail (and raises a GL_VARIANCE
    exception) on a real mismatch, or None if it matches / is within
    tolerance / can't be checked."""
    if ar_control_account_id is None:
        return None
    control = await dao.get_gl_control_balance(ar_control_account_id, period_end)
    if control is None:
        return None
    sl_balance = await dao.sum_open_ar_balance(entity_id)
    gl_balance = control["control_balance_minor"]
    variance = sl_balance - gl_balance
    if abs(variance) <= tolerance_minor:
        return None
    detail = {
        "sub_ledger_balance_minor": sl_balance, "gl_control_balance_minor": gl_balance,
        "variance_minor": variance, "tolerance_minor": tolerance_minor,
    }
    sl_fmt = f"₹{sl_balance / 100:,.2f}"
    gl_fmt = f"₹{gl_balance / 100:,.2f}"
    var_fmt = f"₹{abs(variance) / 100:,.2f}"
    reason = f"Sub-ledger {sl_fmt} vs GL {gl_fmt} — unposted variance of {var_fmt}"
    await dao.insert_exception(
        run_id=run_id, exception_type="GL_VARIANCE", bank_txn_id=None, customer_id=None,
        discrepancy_minor=abs(variance), reason_code=reason, detail=detail,
    )
    return detail
