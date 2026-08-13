"""Orchestrates one reconciliation run.

`run()` is the entry point the worker calls: Phase 1 (identification) then
Phase 2 (allocation), inside the same DB transaction the worker already
wraps both in. `run_phase_1` and `run_phase_2` are also exported separately
for focused testing.

GL posting (M3) runs after Phase 2, in the same transaction; sign-off is
M4 - see the milestone map in app/reconciliation/router.py.
"""
from __future__ import annotations

from collections import Counter, defaultdict

import asyncpg

from app.reconciliation import gl_posting
from app.reconciliation.constants import (
    GAP_ROLE_BY_RULE_KIND, PHASE_ALLOCATION, PHASE_CANDIDATE_POOL, PHASE_CUSTOMER_LOCK, PHASE_INTAKE_VALIDATION,
)
from app.reconciliation.dao import ReconciliationDAO
from app.reconciliation.rules import AllocationContext, RuleContext
from app.reconciliation.rules.allocation import ALLOCATION_RULES, GUARDRAIL_KINDS
from app.reconciliation.rules.identification import IDENTIFICATION_RULES
from app.reconciliation.rules.pooling import POOLING_RULES


async def run(conn: asyncpg.Connection, dao: ReconciliationDAO, run_id: str, run_context: dict) -> dict:
    """Runs Phase 1, Phase 2, then GL posting (M3) for this run and returns
    the combined counters `reconciliation_worker` writes onto
    `reconciliation_runs`. A `GL_VARIANCE` raised by the control proof is
    reflected in `exception_count` but does not fail the run - it's a
    finding to investigate, not a reason to leave the run unposted."""
    phase1 = await run_phase_1(conn, dao, run_id, run_context)
    phase2 = await run_phase_2(conn, dao, run_id, run_context, phase1["outcomes"])
    gl = await gl_posting.post_run(conn, dao, run_id, run_context, phase1["suspense_records"], phase2["payment_ledger_records"])
    return {
        "volume": phase1["volume"],
        "matched_count": phase2["matched_count"],
        "exception_count": (
            phase1["duplicate_count"] + phase1["suspense_count"] + phase2["exception_count"]
            + (1 if gl["gl_variance"] else 0)
        ),
        "matched_value_minor": phase2["matched_value_minor"],
        "exception_value_minor": phase1["exception_value_minor"] + phase2["exception_value_minor"],
        "unapplied_minor": phase2["unapplied_minor"],
        "pooled_count": phase2["unresolved_pool_count"],  # not persisted on reconciliation_runs - log line only
        "journal_count": gl["journal_count"],  # not persisted either - log line only, same as pooled_count
        "gl_variance": gl["gl_variance"],
    }


async def run_phase_1(conn: asyncpg.Connection, dao: ReconciliationDAO, run_id: str, run_context: dict) -> dict:
    """Loads the Phase-1 working set, evaluates every candidate bank inflow
    against Phase 0 (intake validation - `dup-utr`'s reject-before-anything-else
    pre-check) then Phase 1a (lock) then Phase 1b (pool), writes `payments` and
    `reconciliation_exceptions`.

    Returns `outcomes` (one dict per locked/pooled payment, for `run_phase_2`
    to process - `{"payment_id", "bank_txn", "customer_id", "candidate_pool"}`)
    plus raw stats. A duplicate `bank_reference` short-circuits before any
    rule runs and never gets a `payments` row - excluded from `outcomes`
    entirely, not just left unidentified. Suspense rows get a `payments` row
    (so the money is tracked) but aren't in `outcomes` either - Phase 2 has
    nothing to do for a row with no customer and no candidates. They're in
    `suspense_records` instead, for `gl_posting.py` (M3) to post directly.
    """
    entity_id = str(run_context["entity_id"])
    definition_id = str(run_context["definition_id"])

    rules = await dao.list_rules(definition_id)
    intake_rules = sorted(
        (r for r in rules if r["phase"] == PHASE_INTAKE_VALIDATION and r["enabled"]), key=lambda r: r["priority"]
    )
    identification_rules = sorted(
        (r for r in rules if r["phase"] == PHASE_CUSTOMER_LOCK and r["enabled"]), key=lambda r: r["priority"]
    )
    pooling_rules = sorted(
        (r for r in rules if r["phase"] == PHASE_CANDIDATE_POOL and r["enabled"]), key=lambda r: r["priority"]
    )

    bank_inflows = await dao.list_candidate_bank_inflows(entity_id)
    ref_counts = Counter(b["bank_reference"] for b in bank_inflows if b["bank_reference"])
    duplicate_refs_in_run = {ref for ref, count in ref_counts.items() if count > 1}

    ctx = RuleContext(
        entity_id=entity_id,
        dao=dao,
        conn=conn,
        customers=await dao.load_customer_master(entity_id),
        bank_accounts=await dao.load_customer_bank_accounts(entity_id),
        reference_codes=await dao.load_customer_reference_codes(entity_id),
        expected_remittances=await dao.load_expected_remittances(entity_id),
        duplicate_refs_in_run=duplicate_refs_in_run,
    )

    outcomes: list[dict] = []
    suspense_records: list[dict] = []
    exception_minor = 0
    counts = Counter()

    for bank_txn in bank_inflows:
        bank_txn_id = bank_txn["bank_txn_id"]
        amount_minor = bank_txn["amount_minor"]

        # Phase 0 (INTAKE_VALIDATION) - runs before customer identification
        # even starts; a reject here (dup-utr) means Phase 1a/1b never see
        # this row at all.
        intake_result = None
        for rule in intake_rules:
            rule_fn = IDENTIFICATION_RULES.get(rule["kind"])
            if rule_fn is None:
                continue  # an unregistered kind - config error, not a crash
            intake_result = await rule_fn(bank_txn, ctx, rule["config"])
            if intake_result.matched:
                break

        if intake_result is not None and intake_result.reject:
            await dao.insert_exception(
                run_id=run_id, exception_type="DUPLICATE", bank_txn_id=bank_txn_id,
                customer_id=None, reason_code=intake_result.reason, detail=None,
            )
            await dao.mark_bank_statement_status(bank_txn_id, "EXCEPTION")
            exception_minor += amount_minor
            counts["duplicate"] += 1
            continue

        fired_rule = None
        result = None
        for rule in identification_rules:
            rule_fn = IDENTIFICATION_RULES.get(rule["kind"])
            if rule_fn is None:
                continue  # an unregistered kind - config error, not a crash
            result = await rule_fn(bank_txn, ctx, rule["config"])
            if result.matched:
                fired_rule = rule
                break

        if result is not None and result.customer_id:
            payment = await dao.insert_payment(
                bank_txn_id=bank_txn_id, customer_id=result.customer_id, total_received_minor=amount_minor,
                locked_by_rule_id=fired_rule["rule_id"], candidate_pool=None,
            )
            outcomes.append({"payment_id": payment["payment_id"], "bank_txn": bank_txn, "customer_id": result.customer_id, "candidate_pool": None})
            counts["locked"] += 1
            continue

        candidates: list[str] = []
        for rule in pooling_rules:
            rule_fn = POOLING_RULES.get(rule["kind"])
            if rule_fn is None:
                continue
            candidates = await rule_fn(bank_txn, ctx, rule["config"])
            if candidates:
                break

        if candidates:
            payment = await dao.insert_payment(
                bank_txn_id=bank_txn_id, customer_id=None, total_received_minor=amount_minor,
                locked_by_rule_id=None, candidate_pool=candidates,
            )
            outcomes.append({"payment_id": payment["payment_id"], "bank_txn": bank_txn, "customer_id": None, "candidate_pool": candidates})
            counts["pooled"] += 1
            continue

        # Neither phase found anything at all - Suspense. Still gets a
        # payments row (money tracked), but nothing for Phase 2 to do.
        payment = await dao.insert_payment(
            bank_txn_id=bank_txn_id, customer_id=None, total_received_minor=amount_minor,
            locked_by_rule_id=None, candidate_pool=None,
        )
        await dao.insert_exception(
            run_id=run_id, exception_type="SUSPENSE", bank_txn_id=bank_txn_id, customer_id=None,
            reason_code="no Phase 1a/1b rule matched", detail=None,
        )
        await dao.mark_bank_statement_status(bank_txn_id, "EXCEPTION")
        exception_minor += amount_minor
        counts["suspense"] += 1
        suspense_records.append({
            "payment_id": payment["payment_id"], "bank_txn_id": bank_txn_id,
            "currency": bank_txn["currency"], "unapplied_minor": amount_minor,
        })

    return {
        "volume": len(bank_inflows),
        "outcomes": outcomes,
        "duplicate_count": counts["duplicate"],
        "suspense_count": counts["suspense"],
        "exception_value_minor": exception_minor,
        # M3: gl_posting.py posts these straight to SUSPENSE (no customer_id at all).
        "suspense_records": suspense_records,
    }


async def run_phase_2(conn: asyncpg.Connection, dao: ReconciliationDAO, run_id: str, run_context: dict, outcomes: list[dict]) -> dict:
    """Processes every Phase-1 outcome (locked or pooled payment): scopes to
    the candidate customer's(s') open invoices, tries the 9 allocation rules
    first-match-wins, writes `match_groups`/`invoice_allocations`, decrements
    invoice/payment balances, and sets each bank row's final `recon_status`.
    A locked payment tries exactly one customer; a pooled payment tries every
    candidate and only commits if exactly one produces an unambiguous match
    (2+ -> Double-Collision, 0 -> left unresolved). Finishes with a sweep for
    invoices that received no allocation at all this run (No-Payment).

    Returns `payment_ledger_records` (one entry per payment processed here,
    every outcome type - committed, ambiguous, double-collision, unresolved)
    for `gl_posting.py` (M3) to post from directly, same "pass the outcome
    forward in memory" pattern `run_phase_1` uses to hand off to this
    function - no run_id column exists on payments/invoice_allocations to
    re-query this by later.
    """
    entity_id = str(run_context["entity_id"])
    definition_id = str(run_context["definition_id"])
    period_end = run_context.get("period_end")

    rules = await dao.list_rules(definition_id)
    allocation_rules = sorted(
        (r for r in rules if r["phase"] == PHASE_ALLOCATION and r["enabled"] and r["kind"] not in GUARDRAIL_KINDS),
        key=lambda r: r["priority"],
    )

    invoices = await dao.load_open_invoices(entity_id, period_end)
    invoices_by_customer: dict[str, list[dict]] = defaultdict(list)
    for inv in invoices:
        # Normalize once here (asyncpg returns uuid.UUID, not str) so every
        # downstream consumer - allocation.py's rules, this function's own
        # invoice lookups, and exception `detail` jsonb payloads - sees
        # plain strings consistently, instead of each having to remember to
        # convert (see docs/reconciliation.md's UUID-vs-str bug history).
        inv["invoice_id"] = str(inv["invoice_id"])
        inv["customer_id"] = str(inv["customer_id"])
        invoices_by_customer[inv["customer_id"]].append(inv)

    memos = await dao.load_open_memos(entity_id)
    memos_by_customer: dict[str, list[dict]] = defaultdict(list)
    for memo in memos:
        memos_by_customer[str(memo["customer_id"])].append(memo)
    # 2.0b guardrail: net open credit/debit memos off the relevant invoice's
    # balance before any rule sees it - a credit memo genuinely reduces what
    # a customer owes. Best-effort (no golden-data case exercises this yet):
    # only memos linked to a specific invoice are applied; customer-level
    # memos with no invoice_id are left for a human/M3 to net off.
    for memo in memos:
        if not memo["invoice_id"]:
            continue
        sign = -1 if memo["memo_type"] == "CREDIT" else 1
        for inv in invoices_by_customer.get(str(memo["customer_id"]), []):
            if inv["invoice_id"] == memo["invoice_id"]:
                inv["balance_due_minor"] = max(0, inv["balance_due_minor"] + sign * memo["amount_minor"])

    ctx = AllocationContext(
        entity_id=entity_id, dao=dao, conn=conn,
        invoices_by_customer=invoices_by_customer, memos_by_customer=memos_by_customer,
    )

    matched_minor = 0
    exception_minor = 0
    unapplied_minor = 0
    counts = Counter()
    touched_invoice_ids: set[str] = set()
    flagged_invoice_ids: set[str] = set()  # already covered by their own exception - skip in the No-Payment sweep
    # One entry per payment processed this phase, regardless of outcome -
    # gl_posting.py (M3) needs a uniform view to post every payment's cash
    # correctly, not just the cleanly-committed ones. See its `allocations`/
    # `unapplied_minor` fields' meaning per branch below.
    payment_ledger_records: list[dict] = []

    for outcome in outcomes:
        payment_id = outcome["payment_id"]
        bank_txn = outcome["bank_txn"]
        bank_txn_id = bank_txn["bank_txn_id"]
        amount = bank_txn["amount_minor"]
        candidates = [outcome["customer_id"]] if outcome["customer_id"] else outcome["candidate_pool"]

        per_candidate_results = []
        ambiguous_same_customer = None
        for candidate_id in candidates:
            alloc_result = None
            fired_rule = None
            for rule in allocation_rules:
                rule_fn = ALLOCATION_RULES.get(rule["kind"])
                if rule_fn is None:
                    continue
                alloc_result = await rule_fn({"total_received_minor": amount}, bank_txn, candidate_id, ctx, rule["config"])
                if alloc_result.matched:
                    fired_rule = rule
                    break
            if alloc_result is not None and alloc_result.ambiguous:
                ambiguous_same_customer = (candidate_id, alloc_result)
            elif alloc_result is not None and alloc_result.allocations:
                per_candidate_results.append((candidate_id, fired_rule, alloc_result))

        resolved_customer_id = None
        resolved_rule = None
        resolved_outcome = None

        if len(candidates) > 1 and len(per_candidate_results) >= 2:
            # Double-Collision: 2+ different candidates each produced a clean match.
            detail = {"candidates": [
                {"customer_id": cid, "invoice_ids": [a.invoice_id for a in res.allocations], "reason": res.reason}
                for cid, _, res in per_candidate_results
            ]}
            await dao.insert_exception(
                run_id=run_id, exception_type="DOUBLE_COLLISION", bank_txn_id=bank_txn_id, customer_id=None,
                reason_code=f"{len(per_candidate_results)} candidates each produced a valid match", detail=detail,
            )
            await dao.mark_bank_statement_status(bank_txn_id, "EXCEPTION")
            exception_minor += amount
            unapplied_minor += amount
            counts["double_collision"] += 1
            # customer_id=None: gl_posting.py can't credit any one customer's
            # on-account - this goes to SUSPENSE, not ON_ACCOUNT_ADVANCE.
            payment_ledger_records.append({
                "payment_id": payment_id, "bank_txn_id": bank_txn_id, "customer_id": None,
                "currency": bank_txn["currency"], "unapplied_minor": amount, "allocations": [],
            })
            continue

        if per_candidate_results:
            resolved_customer_id, resolved_rule, resolved_outcome = per_candidate_results[0]
        elif ambiguous_same_customer is not None:
            candidate_id, alloc_result = ambiguous_same_customer
            await dao.insert_exception(
                run_id=run_id, exception_type="MULTIPLE_INVOICE_MATCH", bank_txn_id=bank_txn_id, customer_id=candidate_id,
                reason_code=alloc_result.reason, detail={"invoice_ids": alloc_result.ambiguous_invoice_ids},
            )
            await dao.mark_bank_statement_status(bank_txn_id, "EXCEPTION")
            flagged_invoice_ids.update(alloc_result.ambiguous_invoice_ids)
            exception_minor += amount
            unapplied_minor += amount
            counts["ambiguous"] += 1
            # customer_id IS known here (the tie is over which invoice, not
            # which customer) - gl_posting.py credits this customer's
            # ON_ACCOUNT_ADVANCE, not SUSPENSE.
            payment_ledger_records.append({
                "payment_id": payment_id, "bank_txn_id": bank_txn_id, "customer_id": candidate_id,
                "currency": bank_txn["currency"], "unapplied_minor": amount, "allocations": [],
            })
            continue

        if resolved_customer_id is None:
            # Pool never resolved (0 candidates matched anything), or a
            # locked customer had literally no open invoice to apply to.
            await dao.insert_exception(
                run_id=run_id, exception_type="UNAPPLIED_CASH", bank_txn_id=bank_txn_id, customer_id=outcome["customer_id"],
                reason_code="no open invoice matched for any candidate", detail=None,
            )
            await dao.mark_bank_statement_status(bank_txn_id, "EXCEPTION")
            exception_minor += amount
            unapplied_minor += amount
            counts["unresolved"] += 1
            # outcome["customer_id"] may itself be None (an unresolved pool)
            # or set (a locked customer with no open invoice at all) -
            # gl_posting.py's SUSPENSE-vs-ON_ACCOUNT_ADVANCE choice follows
            # the same "is there a known customer_id" rule as every branch.
            payment_ledger_records.append({
                "payment_id": payment_id, "bank_txn_id": bank_txn_id, "customer_id": outcome["customer_id"],
                "currency": bank_txn["currency"], "unapplied_minor": amount, "allocations": [],
            })
            continue

        # Committed: retroactively lock a resolved pool, write the match, apply allocations.
        if outcome["customer_id"] is None:
            await dao.lock_payment_customer(payment_id, resolved_customer_id, resolved_rule["rule_id"] if resolved_rule else None)

        match_group = await dao.insert_match_group(
            run_id=run_id, match_type=resolved_outcome.match_type,
            rule_id=resolved_rule["rule_id"] if resolved_rule else None,
            confidence=resolved_rule["confidence"] if resolved_rule else None,
            status="AUTO_MATCHED", reason=resolved_outcome.reason,
        )

        cash_applied = 0
        still_open: list[str] = []
        posted_allocations: list[dict] = []  # for gl_posting.py (M3) - see payment_ledger_records below
        gap_role = GAP_ROLE_BY_RULE_KIND.get(resolved_rule["kind"]) if resolved_rule else None
        for alloc in resolved_outcome.allocations:
            if alloc.cash_minor <= 0:
                continue  # invoice_allocations.allocated_minor has CHECK(> 0) - nothing real moved, nothing to record
            invoice = next(i for i in invoices_by_customer[resolved_customer_id] if i["invoice_id"] == alloc.invoice_id)
            close_amount = invoice["balance_due_minor"] if alloc.close_full else alloc.cash_minor
            await dao.apply_invoice_allocation(alloc.invoice_id, close_amount)
            await dao.insert_invoice_allocation(
                match_group_id=match_group["match_group_id"], invoice_id=alloc.invoice_id,
                payment_id=payment_id, bank_txn_id=bank_txn_id, allocated_minor=alloc.cash_minor,
            )
            gap_minor = close_amount - alloc.cash_minor  # >0 only for close_full rules that absorbed a shortfall (TDS/fee/write-off)
            posted_allocations.append({
                "invoice_id": alloc.invoice_id, "cash_minor": alloc.cash_minor,
                "gap_minor": gap_minor, "gap_role": gap_role if gap_minor > 0 else None,
            })
            invoice["balance_due_minor"] = max(0, invoice["balance_due_minor"] - close_amount)
            touched_invoice_ids.add(alloc.invoice_id)
            cash_applied += alloc.cash_minor
            if invoice["balance_due_minor"] <= 0:
                # Remove it from the in-memory working set now, not just in
                # the DB - otherwise a later payment in this same run can
                # still "find" it via invoice-number/truncated-suffix match
                # (neither rule checks balance > 0) and produce a
                # zero-amount allocation, which the CHECK constraint rejects.
                invoices_by_customer[resolved_customer_id] = [
                    i for i in invoices_by_customer[resolved_customer_id] if i["invoice_id"] != alloc.invoice_id
                ]
            else:
                still_open.append(alloc.invoice_id)  # recorded here, not re-looked-up below - it may since have been removed from the list above

        await dao.apply_payment_allocation(payment_id, cash_applied)
        leftover = amount - cash_applied

        # Short-Pay: an allocation left an invoice still open with a balance.
        if still_open:
            await dao.insert_exception(
                run_id=run_id, exception_type="SHORT_PAY", bank_txn_id=bank_txn_id, customer_id=resolved_customer_id,
                reason_code=f"payment left {len(still_open)} invoice(s) with a remaining balance", detail={"invoice_ids": still_open},
                match_group_id=match_group["match_group_id"],
            )
            await dao.mark_bank_statement_status(bank_txn_id, "PARTIAL")
            exception_minor += amount
            counts["short_pay"] += 1
        else:
            await dao.mark_bank_statement_status(bank_txn_id, "MATCHED")
            matched_minor += amount
            counts["matched"] += 1

        unapplied_minor += max(0, leftover)
        payment_ledger_records.append({
            "payment_id": payment_id, "bank_txn_id": bank_txn_id, "customer_id": resolved_customer_id,
            "currency": bank_txn["currency"], "unapplied_minor": max(0, leftover), "allocations": posted_allocations,
        })

    # No-Payment sweep: open invoices nothing in this run touched or flagged at all.
    no_payment_count = 0
    for inv_list in invoices_by_customer.values():
        for inv in inv_list:
            if inv["balance_due_minor"] > 0 and inv["invoice_id"] not in touched_invoice_ids and inv["invoice_id"] not in flagged_invoice_ids:
                await dao.insert_exception(
                    run_id=run_id, exception_type="NO_PAYMENT", bank_txn_id=None, customer_id=inv["customer_id"],
                    invoice_id=inv["invoice_id"],
                    reason_code=f"invoice {inv['invoice_number']} received no allocation this run", detail=None,
                )
                no_payment_count += 1

    return {
        "matched_count": counts["matched"],
        "exception_count": counts["short_pay"] + counts["ambiguous"] + counts["double_collision"] + counts["unresolved"] + no_payment_count,
        "matched_value_minor": matched_minor,
        "exception_value_minor": exception_minor,
        "unapplied_minor": unapplied_minor,
        "unresolved_pool_count": counts["ambiguous"] + counts["double_collision"] + counts["unresolved"],
        # M3: gl_posting.py's single source of truth for what to post - one
        # entry per payment processed this phase, every outcome type included.
        "payment_ledger_records": payment_ledger_records,
    }
