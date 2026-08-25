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
    PHASE_ALLOCATION,
    PHASE_CANDIDATE_POOL,
    PHASE_CUSTOMER_LOCK,
    PHASE_INTAKE_VALIDATION,
    PHASE_NARRATION_CHECK,
    PHASE_SHORT_PAY,
    PHASE_UNAPPLIED,
)
from app.reconciliation.dao import ReconciliationDAO
from app.reconciliation.rules import AllocationContext, RuleContext, get_threshold_minor
from app.reconciliation.rules.allocation import ALLOCATION_RULES, resolve_invoice_settlement, sequential_waterfall, waterfall_outcome
from app.reconciliation.rules.identification import IDENTIFICATION_RULES, narration_group_match, narration_invoice_owner
from app.reconciliation.rules.pooling import POOLING_RULES


def _ref_str(bank_txn: dict) -> str:
    """" (ref XXX)" suffix for exception/short-pay reason text - prefers the
    source file's own transaction id (bank_txn_source_id, e.g. 'BANK-001',
    pulled from bank_statements.raw) over bank_reference (the UTR/reference
    number), since that's what a user actually recognizes their row by when
    scanning the exceptions list against their upload - bank_reference is
    the internal reconciliation key, not what's printed on the row (2026-08
    fix). Empty string if neither is present."""
    ref = bank_txn.get("bank_txn_source_id") or bank_txn.get("bank_reference")
    return f" (ref {ref})" if ref else ""


async def run(
    conn: asyncpg.Connection, dao: ReconciliationDAO, run_id: str, run_context: dict
) -> dict:
    """Runs Phase 1, Phase 2, then GL posting (M3) for this run and returns
    the combined counters `reconciliation_worker` writes onto
    `reconciliation_runs`. A `GL_VARIANCE` raised by the control proof is
    reflected in `exception_count` but does not fail the run - it's a
    finding to investigate, not a reason to leave the run unposted."""
    phase1 = await run_phase_1(conn, dao, run_id, run_context)
    phase2 = await run_phase_2(conn, dao, run_id, run_context, phase1["outcomes"])
    gl = await gl_posting.post_run(
        conn,
        dao,
        run_id,
        run_context,
        phase1["suspense_records"],
        phase2["payment_ledger_records"],
    )
    return {
        "volume": phase1["volume"],
        "matched_count": phase2["matched_count"],
        "exception_count": (
            phase1["duplicate_count"]
            + phase1["suspense_count"]
            + phase1["customer_invoice_mismatch_count"]
            + phase2["exception_count"]
            + (1 if gl["gl_variance"] else 0)
        ),
        "matched_value_minor": phase2["matched_value_minor"],
        "exception_value_minor": phase1["exception_value_minor"]
        + phase2["exception_value_minor"],
        "unapplied_minor": phase2["unapplied_minor"],
        "pooled_count": phase2[
            "unresolved_pool_count"
        ],  # not persisted on reconciliation_runs - log line only
        "journal_count": gl[
            "journal_count"
        ],  # not persisted either - log line only, same as pooled_count
        "gl_variance": gl["gl_variance"],
    }


async def run_phase_1(
    conn: asyncpg.Connection, dao: ReconciliationDAO, run_id: str, run_context: dict
) -> dict:
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
    period_end = run_context.get("period_end")

    rules = await dao.list_rules(definition_id)
    intake_rules = sorted(
        (r for r in rules if r["phase"] == PHASE_INTAKE_VALIDATION and r["enabled"]),
        key=lambda r: r["priority"],
    )
    identification_rules = sorted(
        (r for r in rules if r["phase"] == PHASE_CUSTOMER_LOCK and r["enabled"]),
        key=lambda r: r["priority"],
    )
    # Its own phase (NARRATION_CHECK), not CUSTOMER_LOCK - it runs after both
    # 1a and 1b have had their chance for a row, not inside either's
    # first-match-wins loop. Two kinds live here: `invoice-number-in-
    # narration` (crosscheck_rule below - the single-invoice mismatch-
    # detection role against an already-locked customer, unchanged since
    # 2026-08) and `sequential-narration-match` (group_rules - 2026-08d,
    # only ever consulted in the no-customer-anywhere branch further down,
    # never in the mismatch check). crosscheck_rule is filtered to its own
    # kind specifically so an enabled sequential-narration-match row at a
    # lower priority number can never accidentally become "the" crosscheck
    # rule and silently disable the mismatch check.
    crosscheck_rules = sorted(
        (r for r in rules if r["phase"] == PHASE_NARRATION_CHECK and r["enabled"]),
        key=lambda r: r["priority"],
    )
    crosscheck_rule = next((r for r in crosscheck_rules if r["kind"] == "invoice-number-in-narration"), None)
    group_rules = [r for r in crosscheck_rules if r["kind"] == "sequential-narration-match"]
    pooling_rules = sorted(
        (r for r in rules if r["phase"] == PHASE_CANDIDATE_POOL and r["enabled"]),
        key=lambda r: r["priority"],
    )

    # Ordered by (transaction_date, bank_txn_id) - list_candidate_bank_inflows's
    # own ORDER BY - so "first occurrence" here is deterministic, even though
    # bank_txn_id (a random UUID) has no real relationship to which payment
    # actually arrived first; there's no reliable file/insertion order tracked
    # today (line_number is only populated when a source file maps one).
    bank_inflows = await dao.list_candidate_bank_inflows(entity_id)
    seen_refs_in_run: set[str] = set()
    duplicate_bank_txn_ids: set[str] = set()
    for b in bank_inflows:
        ref = b["bank_reference"]
        if not ref:
            continue
        if ref in seen_refs_in_run:
            duplicate_bank_txn_ids.add(b["bank_txn_id"])
        else:
            seen_refs_in_run.add(ref)

    all_open_invoices = await dao.load_open_invoices(entity_id, period_end)
    customer_master = await dao.load_customer_master(entity_id)
    cust_name_map = {str(c["customer_id"]): c["company_name"] for c in customer_master}

    ctx = RuleContext(
        entity_id=entity_id,
        dao=dao,
        conn=conn,
        customers=customer_master,
        bank_accounts=await dao.load_customer_bank_accounts(entity_id),
        reference_codes=await dao.load_customer_reference_codes(entity_id),
        expected_remittances=await dao.load_expected_remittances(entity_id),
        duplicate_bank_txn_ids=duplicate_bank_txn_ids,
        all_open_invoices=all_open_invoices,
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
            bank_ref = bank_txn.get("bank_txn_source_id") or bank_txn.get("bank_reference") or "N/A"
            amt_fmt = f"₹{amount_minor / 100:,.2f}"
            reason = (
                f"Duplicate payment {amt_fmt} with reference '{bank_ref}' in this run"
            )
            await dao.insert_exception(
                run_id=run_id,
                exception_type="DUPLICATE",
                bank_txn_id=bank_txn_id,
                customer_id=None,
                discrepancy_minor=amount_minor,
                reason_code=reason,
                detail={"bank_reference": bank_ref, "amount_minor": amount_minor},
            )
            await dao.mark_bank_statement_status(bank_txn_id, "EXCEPTION")
            exception_minor += amount_minor
            counts["duplicate"] += 1
            continue

        # Phase 1a (CUSTOMER_LOCK) - first match wins.
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

        # Phase 1b (CANDIDATE_POOL) - only reached if 1a locked nothing.
        candidates: list[str] = []
        if result is None or not result.customer_id:
            for rule in pooling_rules:
                rule_fn = POOLING_RULES.get(rule["kind"])
                if rule_fn is None:
                    continue
                candidates = await rule_fn(bank_txn, ctx, rule["config"])
                if candidates:
                    break

        # Phase 1c (NARRATION_CHECK) - runs after both 1a and 1b have had
        # their chance, reconciled against whichever (if either) actually
        # produced something. Searches every customer's open invoices (not
        # just whoever 1a/1b already identified) for one the narration
        # references.
        narration_match = (
            narration_invoice_owner(
                bank_txn.get("narration") or "",
                ctx.all_open_invoices,
                fields=crosscheck_rule["config"].get("match_fields"),
            )
            if crosscheck_rule is not None
            else None
        )

        if (
            narration_match
            and result is not None
            and result.customer_id
            and narration_match["customer_id"] is not None
            and narration_match["customer_id"] != result.customer_id
        ):
            # Two independently-confirmed answers disagree - don't let the
            # lock stand unquestioned. No payments row locks to either
            # candidate; money is still tracked (Dr CASH_CONTROL / Cr
            # SUSPENSE, same GL shape as Suspense/Double-Collision) pending a
            # human decision.
            locked_name = cust_name_map.get(result.customer_id, result.customer_id[:8])
            narration_name = cust_name_map.get(narration_match["customer_id"], narration_match["customer_id"][:8])
            amt_fmt = f"₹{amount_minor / 100:,.2f}"
            reason = (
                f"{amt_fmt} payment locked to {locked_name} via {fired_rule['kind']}, but narration "
                f"references {narration_name}'s invoice {narration_match['invoice_number']}"
            )
            payment = await dao.insert_payment(
                bank_txn_id=bank_txn_id,
                customer_id=None,
                total_received_minor=amount_minor,
                locked_by_rule_id=None,
                candidate_pool=None,
            )
            await dao.insert_exception(
                run_id=run_id,
                exception_type="CUSTOMER_INVOICE_MISMATCH",
                bank_txn_id=bank_txn_id,
                customer_id=None,
                discrepancy_minor=amount_minor,
                reason_code=reason,
                detail={
                    "locked_customer_id": result.customer_id,
                    "locked_customer_name": locked_name,
                    "locked_via_rule": fired_rule["kind"],
                    "narration_customer_id": narration_match["customer_id"],
                    "narration_customer_name": narration_name,
                    "narration_invoice_id": narration_match["invoice_id"],
                    "narration_invoice_number": narration_match["invoice_number"],
                },
            )
            await dao.mark_bank_statement_status(bank_txn_id, "EXCEPTION")
            exception_minor += amount_minor
            counts["customer_invoice_mismatch"] += 1
            suspense_records.append(
                {
                    "payment_id": payment["payment_id"],
                    "bank_txn_id": bank_txn_id,
                    "currency": bank_txn["currency"],
                    "unapplied_minor": amount_minor,
                    "memo": "Customer/invoice mismatch receipt - narration disagrees with the identified customer",
                }
            )
            continue

        if result is not None and result.customer_id:
            crosscheck_confirmed = bool(
                narration_match and narration_match["customer_id"] == result.customer_id
            )
            payment = await dao.insert_payment(
                bank_txn_id=bank_txn_id,
                customer_id=result.customer_id,
                total_received_minor=amount_minor,
                locked_by_rule_id=fired_rule["rule_id"],
                candidate_pool=None,
                narration_crosscheck_rule_id=crosscheck_rule["rule_id"] if crosscheck_confirmed else None,
            )
            outcomes.append(
                {
                    "payment_id": payment["payment_id"],
                    "bank_txn": bank_txn,
                    "customer_id": result.customer_id,
                    "candidate_pool": None,
                }
            )
            counts["locked"] += 1
            continue

        if not candidates and narration_match and narration_match["customer_id"] is not None:
            # Phase 1a locked nobody and the pooling rules found nothing
            # either - fall back to the cross-check's own candidate rather
            # than dropping straight to Suspense-with-no-suggestion. Still
            # just a hint (single-candidate pool) at this point, not an
            # identification-phase lock - but Pass A in run_phase_2 will
            # commit it for real if it ends up as the pool's only candidate
            # with a clean rule match (2026-08 change), same as any other
            # single-candidate pool.
            # (The None guard matters now that document-number-narration
            # exists as a real rule above this: if it already tried and
            # declined - the referenced invoice has no customer either -
            # narration_match["customer_id"] is None here, and appending
            # that would silently push a dead-end [None] candidate into Pass
            # A, which can never resolve to anything.)
            candidates = [narration_match["customer_id"]]

        if candidates:
            payment = await dao.insert_payment(
                bank_txn_id=bank_txn_id,
                customer_id=None,
                total_received_minor=amount_minor,
                locked_by_rule_id=None,
                candidate_pool=candidates,
            )
            outcomes.append(
                {
                    "payment_id": payment["payment_id"],
                    "bank_txn": bank_txn,
                    "customer_id": None,
                    "candidate_pool": candidates,
                }
            )
            counts["pooled"] += 1
            continue

        # Truly no customer anywhere - not on the payment side (Phase 1a/1b
        # found nobody), not on the invoice side either (narration_match's
        # own customer_id is None, migration 0031). Try every enabled
        # sequential-narration-match rule first (priority order, first hit
        # wins) - when a narration-matched field genuinely groups more than
        # one open invoice (e.g. raw:Business_Partner_Code), that's a
        # strictly richer answer than the single-invoice check below, so it
        # gets first refusal (2026-08d). Falls through to the existing
        # single-invoice check unchanged when no group rule is enabled or
        # none of them find a clean (unambiguous) group.
        group_match = None
        firing_group_rule = None
        for rule in group_rules:
            group_match = await narration_group_match(bank_txn, ctx, rule["config"])
            if group_match:
                firing_group_rule = rule
                break

        if group_match:
            payment = await dao.insert_payment(
                bank_txn_id=bank_txn_id,
                customer_id=None,
                total_received_minor=amount_minor,
                locked_by_rule_id=None,
                candidate_pool=None,
            )
            outcomes.append(
                {
                    "payment_id": payment["payment_id"],
                    "bank_txn": bank_txn,
                    "customer_id": None,
                    "candidate_pool": None,
                    "direct_invoice_group": group_match["invoice_ids"],
                    "direct_invoice_group_rule": firing_group_rule,
                }
            )
            counts["direct_match"] += 1
            continue

        # The invoice number/document number itself is still self-sufficient
        # evidence of which invoice this is, even with no identity to attach
        # it to - hand it to Phase 2 as a direct match instead of dropping
        # straight to Suspense. customer_id stays NULL on the payment
        # (there's nothing to backfill it with); run_phase_2's direct-match
        # pass allocates against exactly this invoice, bypassing customer
        # scoping entirely.
        if narration_match:
            payment = await dao.insert_payment(
                bank_txn_id=bank_txn_id,
                customer_id=None,
                total_received_minor=amount_minor,
                locked_by_rule_id=None,
                candidate_pool=None,
            )
            outcomes.append(
                {
                    "payment_id": payment["payment_id"],
                    "bank_txn": bank_txn,
                    "customer_id": None,
                    "candidate_pool": None,
                    "direct_invoice_id": narration_match["invoice_id"],
                }
            )
            counts["direct_match"] += 1
            continue

        # Neither phase found anything at all - Suspense. Still gets a
        # payments row (money tracked), but nothing for Phase 2 to do.
        payer = bank_txn.get("payer_name") or "Unidentified Remitter"
        ref = bank_txn.get("bank_txn_source_id") or bank_txn.get("bank_reference") or bank_txn_id[:8]
        amt_fmt = f"₹{amount_minor / 100:,.2f}"
        reason = f"{amt_fmt} payment from {payer} (ref {ref}) could not be identified to any customer"
        payment = await dao.insert_payment(
            bank_txn_id=bank_txn_id,
            customer_id=None,
            total_received_minor=amount_minor,
            locked_by_rule_id=None,
            candidate_pool=None,
        )
        await dao.insert_exception(
            run_id=run_id,
            exception_type="SUSPENSE",
            bank_txn_id=bank_txn_id,
            customer_id=None,
            discrepancy_minor=amount_minor,
            reason_code=reason,
            detail={
                "payer_name": payer,
                "bank_reference": ref,
                "amount_minor": amount_minor,
            },
        )
        await dao.mark_bank_statement_status(bank_txn_id, "EXCEPTION")
        exception_minor += amount_minor
        counts["suspense"] += 1
        suspense_records.append(
            {
                "payment_id": payment["payment_id"],
                "bank_txn_id": bank_txn_id,
                "currency": bank_txn["currency"],
                "unapplied_minor": amount_minor,
            }
        )

    return {
        "volume": len(bank_inflows),
        "outcomes": outcomes,
        "duplicate_count": counts["duplicate"],
        "suspense_count": counts["suspense"],
        "customer_invoice_mismatch_count": counts["customer_invoice_mismatch"],
        "exception_value_minor": exception_minor,
        # M3: gl_posting.py posts these straight to SUSPENSE (no customer_id at all).
        "suspense_records": suspense_records,
    }


async def run_phase_2(
    conn: asyncpg.Connection,
    dao: ReconciliationDAO,
    run_id: str,
    run_context: dict,
    outcomes: list[dict],
) -> dict:
    """Processes every Phase-1 outcome (locked or pooled payment): scopes to
    the candidate customer's(s') open invoices, tries the 9 allocation rules,
    writes `match_groups`/`invoice_allocations`, decrements invoice/payment
    balances, and sets each bank row's final `recon_status`. Finishes with a
    sweep for invoices that received no allocation at all this run (No-Payment).

    Two passes, matching the prototype's own two-stage design
    (index copy.html's arReconcile: pool resolution happens once, before its
    per-customer `allocRules.forEach(rule => payments.forEach(...))` loop):

    Pass A resolves every pooled (candidate_pool, Phase 1b) payment entirely
    on its own: try every candidate through all 9 rules, first-match-wins per
    candidate. 2+ clean matches -> Double-Collision, a genuine ambiguity (the
    same amount legitimately matches two different customers' invoices, and
    nothing here can safely pick one). Exactly 1 clean match -> committed for
    real via `_commit`, the same path Pass B uses (2026-08 change - a
    single-candidate pool with a clean rule match used to always be
    downgraded to a Suspense suggestion, never auto-committed, regardless of
    how clean the match was; the prototype's own `exactAmountRule` probe did
    the same, but that call was revisited - a unique candidate plus a
    non-ambiguous rule match is no weaker a signal than most identification
    rules already auto-lock on). 0 matches -> Suspense with no suggestion.
    Only a payment Phase 1a locked outright skips straight into Pass B.

    Pass B resolves WHICH INVOICE for every payment Phase 1a actually locked.
    Grouped by customer, it runs **rule-outer / payment-inner**: rule 1
    (exact-invoice-num) gets a complete pass over every one of that
    customer's still-unresolved payments before rule 2 is tried on anyone,
    and so on down the priority list. This is the actual fix - the previous
    single-pass, payment-outer design let an *earlier* payment's low-priority
    catch-all (partial-payment, the last rule) permanently consume an invoice
    that a *later* payment's higher-priority, more specific rule
    (exact-invoice-num) needed, simply because it happened to be evaluated
    first. Every payment now gets first crack at the specific/exact rules
    across the whole batch before anyone is allowed to fall through to a
    generic one.

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
        (r for r in rules if r["phase"] == PHASE_ALLOCATION and r["enabled"]),
        key=lambda r: r["priority"],
    )
    # Not in ALLOCATION_RULES (never dispatched through the per-customer
    # cascade - see its catalog comment, constants.py) - it exists as a row
    # purely so _commit_direct_match's match_groups get a real rule_id/
    # confidence instead of always None, and so disabling it in Rules Studio
    # actually stops the no-customer direct-match path from firing (gated at
    # the Pass A dispatch below), instead of it running unconditionally with
    # no way to turn it off.
    direct_match_rule = next((r for r in rules if r["kind"] == "direct-invoice-match" and r["enabled"]), None)
    short_pay_tolerance_minor = get_threshold_minor(rules, PHASE_SHORT_PAY)
    unapplied_tolerance_minor = get_threshold_minor(rules, PHASE_UNAPPLIED)

    invoices = await dao.load_open_invoices(entity_id, period_end)
    customer_master = await dao.load_customer_master(entity_id)
    cust_name_map = {str(c["customer_id"]): c["company_name"] for c in customer_master}
    invoices_by_customer: dict[str, list[dict]] = defaultdict(list)
    # Ingested (migration 0031) but never linked to a customer - not "this
    # customer's invoice" to any payment yet, so it's kept out of
    # invoices_by_customer entirely rather than under a bogus str(None) =
    # "None" key that no real customer_id would ever look up. Tracked here so
    # it's still visible (see the unresolved-customer sweep below) instead of
    # silently invisible to every allocation rule forever.
    unresolved_invoices: list[dict] = []
    for inv in invoices:
        # Normalize once here (asyncpg returns uuid.UUID, not str) so every
        # downstream consumer - allocation.py's rules, this function's own
        # invoice lookups, and exception `detail` jsonb payloads - sees
        # plain strings consistently, instead of each having to remember to
        # convert (see docs/reconciliation.md's UUID-vs-str bug history).
        inv["invoice_id"] = str(inv["invoice_id"])
        if inv["customer_id"] is None:
            unresolved_invoices.append(inv)
            continue
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
                inv["balance_due_minor"] = max(
                    0, inv["balance_due_minor"] + sign * memo["amount_minor"]
                )

    ctx = AllocationContext(
        entity_id=entity_id,
        dao=dao,
        conn=conn,
        invoices_by_customer=invoices_by_customer,
        memos_by_customer=memos_by_customer,
        unresolved_invoices=unresolved_invoices,
    )

    money = {"matched": 0, "exception": 0, "unapplied": 0}
    counts = Counter()
    touched_invoice_ids: set[str] = set()
    flagged_invoice_ids: set[str] = (
        set()
    )  # already covered by their own exception - skip in the No-Payment sweep
    # One entry per payment processed this phase, regardless of outcome -
    # gl_posting.py (M3) needs a uniform view to post every payment's cash
    # correctly, not just the cleanly-committed ones. See its `allocations`/
    # `unapplied_minor` fields' meaning per branch below.
    payment_ledger_records: list[dict] = []

    async def _commit(
        item: dict, customer_id: str, rule: dict | None, alloc_result
    ) -> None:
        """Writes one committed match: match_group, invoice_allocations,
        balance decrements, Short-Pay check, and the payment_ledger_records
        entry. Only ever called from Pass B, so `item` always has a genuine
        Phase 1a lock behind it - a pool never reaches here (see `_suspense`
        above; Pass A resolves every pool outcome itself, auto-commit or
        not). Mutates the enclosing function's `money`/`counts`/
        `touched_invoice_ids`/`payment_ledger_records` in place - all
        mutable containers, so this closure needs no `nonlocal`."""
        payment_id = item["payment_id"]
        bank_txn = item["bank_txn"]
        bank_txn_id = bank_txn["bank_txn_id"]
        amount = bank_txn["amount_minor"]

        match_group = await dao.insert_match_group(
            run_id=run_id,
            match_type=alloc_result.match_type,
            rule_id=rule["rule_id"] if rule else None,
            confidence=rule["confidence"] if rule else None,
            status="AUTO_MATCHED",
            reason=alloc_result.reason,
        )

        cash_applied = 0
        still_open: list[str] = []
        shortfall_total_minor = 0
        posted_allocations: list[dict] = []
        for alloc in alloc_result.allocations:
            if alloc.cash_minor <= 0:
                continue  # invoice_allocations.allocated_minor has CHECK(> 0) - nothing real moved, nothing to record
            invoice = next(
                i
                for i in invoices_by_customer[customer_id]
                if i["invoice_id"] == alloc.invoice_id
            )
            close_amount = (
                invoice["balance_due_minor"] if alloc.close_full else alloc.cash_minor
            )
            await dao.apply_invoice_allocation(alloc.invoice_id, close_amount)
            await dao.insert_invoice_allocation(
                match_group_id=match_group["match_group_id"],
                invoice_id=alloc.invoice_id,
                payment_id=payment_id,
                bank_txn_id=bank_txn_id,
                allocated_minor=alloc.cash_minor,
            )
            gap_minor = (
                close_amount - alloc.cash_minor
            )  # >0 only for close_full allocations that absorbed a shortfall (TDS/fee/write-off)
            posted_allocations.append(
                {
                    "invoice_id": alloc.invoice_id,
                    "cash_minor": alloc.cash_minor,
                    "gap_minor": gap_minor,
                    # Per-allocation, not per-rule - a single subset-sum combo
                    # can mix a raw-balance invoice with a TDS-adjusted one,
                    # each needing its own gap destination.
                    "gap_role": alloc.gap_role if gap_minor > 0 else None,
                }
            )
            invoice["balance_due_minor"] = max(
                0, invoice["balance_due_minor"] - close_amount
            )
            touched_invoice_ids.add(alloc.invoice_id)
            cash_applied += alloc.cash_minor
            if invoice["balance_due_minor"] <= 0:
                # Remove it from the in-memory working set now, not just in
                # the DB - otherwise a later rule pass (or a later payment
                # within the same rule pass) can still "find" it via
                # invoice-number/truncated-suffix match (neither rule checks
                # balance > 0) and produce a zero-amount allocation, which
                # the CHECK constraint rejects.
                invoices_by_customer[customer_id] = [
                    i
                    for i in invoices_by_customer[customer_id]
                    if i["invoice_id"] != alloc.invoice_id
                ]
            else:
                still_open.append(
                    alloc.invoice_id
                )  # recorded here, not re-looked-up below - it may since have been removed from the list above
                shortfall_total_minor += invoice["balance_due_minor"]

        await dao.apply_payment_allocation(payment_id, cash_applied)
        leftover = amount - cash_applied

        # Short-Pay: an allocation left an invoice still open with a balance.
        if still_open:
            if shortfall_total_minor > short_pay_tolerance_minor:
                cust_name = cust_name_map.get(customer_id) or "Customer"
                inv_nums = [
                    next(
                        (
                            i["invoice_number"]
                            for i in invoices_by_customer.get(customer_id, [])
                            if i["invoice_id"] == inv_id
                        ),
                        inv_id[:8],
                    )
                    for inv_id in still_open
                ]
                inv_str = ", ".join(inv_nums) if inv_nums else "invoice"
                ref_str = _ref_str(bank_txn)
                shortfall_fmt = f"₹{shortfall_total_minor / 100:,.2f}"
                reason = f"{cust_name} short-paid {inv_str} by {shortfall_fmt}{ref_str}"
                await dao.insert_exception(
                    run_id=run_id,
                    exception_type="SHORT_PAY",
                    bank_txn_id=bank_txn_id,
                    customer_id=customer_id,
                    discrepancy_minor=shortfall_total_minor,
                    reason_code=reason,
                    detail={
                        "invoice_ids": still_open,
                        "shortfall_minor": shortfall_total_minor,
                        "tolerance_minor": short_pay_tolerance_minor,
                        "customer_name": cust_name,
                        "invoice_numbers": inv_nums,
                    },
                    match_group_id=match_group["match_group_id"],
                )
                money["exception"] += amount
                counts["short_pay"] += 1
            else:
                money["matched"] += amount
                counts["matched"] += 1
            await dao.mark_bank_statement_status(bank_txn_id, "PARTIAL")
        else:
            await dao.mark_bank_statement_status(bank_txn_id, "MATCHED")
            money["matched"] += amount
            counts["matched"] += 1

        money["unapplied"] += max(0, leftover)
        payment_ledger_records.append(
            {
                "payment_id": payment_id,
                "bank_txn_id": bank_txn_id,
                "customer_id": customer_id,
                "currency": bank_txn["currency"],
                "unapplied_minor": max(0, leftover),
                "allocations": posted_allocations,
            }
        )

    async def _unapplied(item: dict, customer_id: str | None) -> None:
        """Nothing ever matched this payment - to any candidate (pool never
        resolved) or to any open invoice of its one known customer."""
        bank_txn = item["bank_txn"]
        bank_txn_id = bank_txn["bank_txn_id"]
        amount = bank_txn["amount_minor"]
        if amount > unapplied_tolerance_minor:
            cust_name = (
                cust_name_map.get(customer_id)
                if customer_id
                else (bank_txn.get("payer_name") or "Unidentified Remitter")
            )
            amt_fmt = f"₹{amount / 100:,.2f}"
            ref_str = _ref_str(bank_txn)
            reason = (
                f"{amt_fmt} payment from {cust_name}{ref_str} matched no open invoice"
            )
            await dao.insert_exception(
                run_id=run_id,
                exception_type="UNAPPLIED_CASH",
                bank_txn_id=bank_txn_id,
                customer_id=customer_id,
                discrepancy_minor=amount,
                reason_code=reason,
                detail={"amount_minor": amount, "customer_name": cust_name},
            )
            money["exception"] += amount
            counts["unresolved"] += 1
        await dao.mark_bank_statement_status(bank_txn_id, "EXCEPTION")
        money["unapplied"] += amount
        payment_ledger_records.append(
            {
                "payment_id": item["payment_id"],
                "bank_txn_id": bank_txn_id,
                "customer_id": customer_id,
                "currency": bank_txn["currency"],
                "unapplied_minor": amount,
                "allocations": [],
            }
        )

    async def _suspense(item: dict, reason_code: str, detail: dict | None) -> None:
        """A candidate-pool payment (Phase 1b - no independently confirmed
        identity, only a weak hint like a narration-token overlap) never
        auto-commits, no matter how clean the eventual invoice match looks -
        matching the prototype exactly (index copy.html's own
        `exactAmountRule` probe: even a single-candidate exact-amount hit
        only ever sets `suggestedCustomerId`/`suggestedInvoiceId` and still
        raises a Suspense exception for a human to confirm). `detail` carries
        the suggestion so it isn't lost - a future "confirm this" action can
        read it back."""
        bank_txn = item["bank_txn"]
        bank_txn_id = bank_txn["bank_txn_id"]
        amount = bank_txn["amount_minor"]
        await dao.insert_exception(
            run_id=run_id,
            exception_type="SUSPENSE",
            bank_txn_id=bank_txn_id,
            customer_id=None,
            reason_code=reason_code,
            detail=detail,
        )
        await dao.mark_bank_statement_status(bank_txn_id, "EXCEPTION")
        money["exception"] += amount
        money["unapplied"] += amount
        counts["unresolved"] += 1
        payment_ledger_records.append(
            {
                "payment_id": item["payment_id"],
                "bank_txn_id": bank_txn_id,
                "customer_id": None,
                "currency": bank_txn["currency"],
                "unapplied_minor": amount,
                "allocations": [],
            }
        )

    async def _commit_direct_match(item: dict, invoice: dict) -> None:
        """Writes a direct invoice match for a payment with no customer
        identified anywhere - not on the payment side (Phase 1a/1b found
        nobody) and not on the invoice side either (migration 0031, no
        ERP-linked customer_id to backfill from). The invoice/document
        number in narration is self-sufficient evidence of which invoice
        this is, so it's allocated directly against exactly that one
        invoice, bypassing customer scoping entirely. Unlike `_commit`
        there's no subset-sum cascade here (that genuinely needs a bounded,
        known customer's invoice set to search) - but the same settlement
        classifier every other Phase 2 rule uses
        (`allocation.py::resolve_invoice_settlement`) is run against this
        one invoice, so a payment short by TDS/a bank fee/a dust residual
        still closes it, and an overpayment is reported as such instead of
        being mislabeled an exact match. customer_id stays NULL throughout -
        on the payment, the match_group, and the GL posting (gl_posting.py
        routes any leftover to SUSPENSE rather than ON_ACCOUNT_ADVANCE
        whenever a payment_ledger_records entry's customer_id is None, same
        as an ordinary unidentified receipt)."""
        payment_id = item["payment_id"]
        bank_txn = item["bank_txn"]
        bank_txn_id = bank_txn["bank_txn_id"]
        amount = bank_txn["amount_minor"]

        if min(amount, invoice["balance_due_minor"]) <= 0:
            # Already fully closed by something else this run - nothing left
            # to allocate; treat like any other payment that matched nothing.
            await _unapplied(item, None)
            return

        inv_label = invoice.get("document_number") or invoice["invoice_number"]
        settle = resolve_invoice_settlement(amount, invoice, bank_txn)
        cash = settle.cash_minor
        close_amount = invoice["balance_due_minor"] if settle.close_full else cash
        gap_minor = close_amount - cash
        gap_role = settle.gap_role
        base_reason = f"{inv_label!r} in narration - no customer identified"
        reason = (
            f"{base_reason}, matched directly"
            if settle.status in ("EXACT", "PARTIAL")
            else f"{base_reason}, {settle.reason}"
        )

        match_group = await dao.insert_match_group(
            run_id=run_id,
            match_type="EXACT" if settle.status == "EXACT" else ("PARTIAL" if settle.status == "PARTIAL" else "TOLERANCE"),
            rule_id=direct_match_rule["rule_id"] if direct_match_rule else None,
            confidence=direct_match_rule["confidence"] if direct_match_rule else None,
            status="AUTO_MATCHED",
            reason=reason,
        )
        await dao.apply_invoice_allocation(invoice["invoice_id"], close_amount)
        await dao.insert_invoice_allocation(
            match_group_id=match_group["match_group_id"],
            invoice_id=invoice["invoice_id"],
            payment_id=payment_id,
            bank_txn_id=bank_txn_id,
            allocated_minor=cash,
        )
        invoice["balance_due_minor"] = max(0, invoice["balance_due_minor"] - close_amount)
        touched_invoice_ids.add(invoice["invoice_id"])
        if invoice["balance_due_minor"] <= 0:
            # Same in-memory-removal reasoning as _commit: stop a later
            # payment in this same run from "finding" it again.
            unresolved_invoices[:] = [
                i for i in unresolved_invoices if i["invoice_id"] != invoice["invoice_id"]
            ]

        await dao.apply_payment_allocation(payment_id, cash)
        leftover = amount - cash

        if invoice["balance_due_minor"] > 0:
            if invoice["balance_due_minor"] > short_pay_tolerance_minor:
                shortfall_fmt = f"₹{invoice['balance_due_minor'] / 100:,.2f}"
                reason = f"Unidentified payment short-paid {inv_label} by {shortfall_fmt}"
                await dao.insert_exception(
                    run_id=run_id,
                    exception_type="SHORT_PAY",
                    bank_txn_id=bank_txn_id,
                    customer_id=None,
                    discrepancy_minor=invoice["balance_due_minor"],
                    reason_code=reason,
                    detail={
                        "invoice_ids": [invoice["invoice_id"]],
                        "shortfall_minor": invoice["balance_due_minor"],
                        "tolerance_minor": short_pay_tolerance_minor,
                        "invoice_numbers": [inv_label],
                    },
                    match_group_id=match_group["match_group_id"],
                )
                money["exception"] += amount
                counts["short_pay"] += 1
            else:
                money["matched"] += amount
                counts["matched"] += 1
            await dao.mark_bank_statement_status(bank_txn_id, "PARTIAL")
        else:
            await dao.mark_bank_statement_status(bank_txn_id, "MATCHED")
            money["matched"] += amount
            counts["matched"] += 1

        money["unapplied"] += max(0, leftover)
        payment_ledger_records.append(
            {
                "payment_id": payment_id,
                "bank_txn_id": bank_txn_id,
                "customer_id": None,
                "currency": bank_txn["currency"],
                "unapplied_minor": max(0, leftover),
                "allocations": [
                    {
                        "invoice_id": invoice["invoice_id"],
                        "cash_minor": cash,
                        "gap_minor": gap_minor if gap_minor > 0 else 0,
                        "gap_role": gap_role if gap_minor > 0 else None,
                    }
                ],
            }
        )

    async def _commit_direct_group_match(item: dict, invoices: list[dict], rule: dict) -> None:
        """Writes a Sequential Narration Match for a payment with no
        customer identified anywhere, same precondition as
        `_commit_direct_match`, but for a GROUP of invoices sharing one
        narration-matched field value (e.g. raw:Business_Partner_Code)
        instead of a single invoice number (2026-08d). Runs the same
        oldest-due-first waterfall `sequential_amount_match` uses
        (`allocation.sequential_waterfall`) across the whole group, so one
        payment can settle several customer-less invoices at once instead
        of dumping its entire amount on just the first one. `invoices` is
        already the *live* subset of `unresolved_invoices` matching this
        outcome's group (some may have been closed by an earlier payment
        this same run - already excluded by the caller); `rule` is the
        specific `sequential-narration-match` NARRATION_CHECK row that
        found this group, used for the match_group's rule_id/confidence -
        unlike `_commit_direct_match` there's no separate ALLOCATION-phase
        gate row for this path, the firing rule's own enabled state is the
        only gate needed."""
        payment_id = item["payment_id"]
        bank_txn = item["bank_txn"]
        bank_txn_id = bank_txn["bank_txn_id"]
        amount = bank_txn["amount_minor"]

        open_invoices = [inv for inv in invoices if inv["balance_due_minor"] > 0]
        if amount <= 0 or not open_invoices:
            await _unapplied(item, None)
            return

        allocations, settles = sequential_waterfall(amount, open_invoices, bank_txn)
        if not allocations:
            await _unapplied(item, None)
            return

        base = waterfall_outcome(allocations, settles)
        labels = [inv.get("document_number") or inv["invoice_number"] for inv in open_invoices[: len(allocations)]]
        if len(allocations) == 1:
            reason = f"{labels[0]!r} in narration - no customer identified, {base.reason}"
        else:
            label_str = ", ".join(repr(label) for label in labels)
            reason = f"{label_str} share the same narration reference - no customer identified, {base.reason}"

        match_group = await dao.insert_match_group(
            run_id=run_id,
            match_type=base.match_type,
            rule_id=rule["rule_id"],
            confidence=rule["confidence"],
            status="AUTO_MATCHED",
            reason=reason,
        )

        cash_applied = 0
        still_open: list[str] = []
        shortfall_total_minor = 0
        posted_allocations: list[dict] = []
        for alloc in allocations:
            if alloc.cash_minor <= 0:
                continue
            invoice = next(i for i in open_invoices if i["invoice_id"] == alloc.invoice_id)
            close_amount = invoice["balance_due_minor"] if alloc.close_full else alloc.cash_minor
            await dao.apply_invoice_allocation(alloc.invoice_id, close_amount)
            await dao.insert_invoice_allocation(
                match_group_id=match_group["match_group_id"],
                invoice_id=alloc.invoice_id,
                payment_id=payment_id,
                bank_txn_id=bank_txn_id,
                allocated_minor=alloc.cash_minor,
            )
            gap_minor = close_amount - alloc.cash_minor
            posted_allocations.append(
                {
                    "invoice_id": alloc.invoice_id,
                    "cash_minor": alloc.cash_minor,
                    "gap_minor": gap_minor,
                    "gap_role": alloc.gap_role if gap_minor > 0 else None,
                }
            )
            invoice["balance_due_minor"] = max(0, invoice["balance_due_minor"] - close_amount)
            touched_invoice_ids.add(alloc.invoice_id)
            cash_applied += alloc.cash_minor
            if invoice["balance_due_minor"] <= 0:
                unresolved_invoices[:] = [
                    i for i in unresolved_invoices if i["invoice_id"] != alloc.invoice_id
                ]
            else:
                still_open.append(alloc.invoice_id)
                shortfall_total_minor += invoice["balance_due_minor"]

        await dao.apply_payment_allocation(payment_id, cash_applied)
        leftover = amount - cash_applied

        if still_open:
            if shortfall_total_minor > short_pay_tolerance_minor:
                inv_nums = [
                    next((i["invoice_number"] for i in open_invoices if i["invoice_id"] == inv_id), inv_id[:8])
                    for inv_id in still_open
                ]
                inv_str = ", ".join(inv_nums)
                ref_str = _ref_str(bank_txn)
                shortfall_fmt = f"₹{shortfall_total_minor / 100:,.2f}"
                short_pay_reason = f"Unidentified payment short-paid {inv_str} by {shortfall_fmt}{ref_str}"
                await dao.insert_exception(
                    run_id=run_id,
                    exception_type="SHORT_PAY",
                    bank_txn_id=bank_txn_id,
                    customer_id=None,
                    discrepancy_minor=shortfall_total_minor,
                    reason_code=short_pay_reason,
                    detail={
                        "invoice_ids": still_open,
                        "shortfall_minor": shortfall_total_minor,
                        "tolerance_minor": short_pay_tolerance_minor,
                        "invoice_numbers": inv_nums,
                    },
                    match_group_id=match_group["match_group_id"],
                )
                money["exception"] += amount
                counts["short_pay"] += 1
            else:
                money["matched"] += amount
                counts["matched"] += 1
            await dao.mark_bank_statement_status(bank_txn_id, "PARTIAL")
        else:
            await dao.mark_bank_statement_status(bank_txn_id, "MATCHED")
            money["matched"] += amount
            counts["matched"] += 1

        money["unapplied"] += max(0, leftover)
        payment_ledger_records.append(
            {
                "payment_id": payment_id,
                "bank_txn_id": bank_txn_id,
                "customer_id": None,
                "currency": bank_txn["currency"],
                "unapplied_minor": max(0, leftover),
                "allocations": posted_allocations,
            }
        )

    # --- Pass A: pooled payments only (candidate_pool, from Phase 1b) - a
    # locked payment (outcome["customer_id"] already set by Phase 1a, an
    # independently confirmed identity) skips straight into `pending` for
    # Pass B. A pool is always resolved right here, never deferred into Pass
    # B: 2+ clean matches -> Double-Collision (genuine ambiguity, no safe
    # pick), exactly 1 clean match -> committed for real (2026-08 change -
    # see run_phase_2's docstring), 0 matches -> Suspense with no suggestion.
    pending: list[tuple[dict, str]] = []  # (item, resolved_customer_id) for Pass B
    for outcome in outcomes:
        if outcome.get("direct_invoice_group"):
            # No customer anywhere - resolved by run_phase_1's
            # sequential-narration-match group fallback (2026-08d). Bypasses
            # Pass A/B's customer-scoped machinery entirely, same as
            # direct_invoice_id below, but settles the whole group via the
            # same oldest-due-first waterfall sequential_amount_match uses.
            group_invoices = [
                i for i in unresolved_invoices if i["invoice_id"] in outcome["direct_invoice_group"]
            ]
            if not group_invoices:
                # Every invoice in the group was already closed by another
                # payment earlier in this same run.
                await _unapplied(outcome, None)
            else:
                await _commit_direct_group_match(outcome, group_invoices, outcome["direct_invoice_group_rule"])
            continue

        if outcome.get("direct_invoice_id"):
            # No customer anywhere (payment or invoice) - resolved by
            # run_phase_1's direct-match fallback. Bypasses Pass A/B's
            # customer-scoped machinery entirely; see _commit_direct_match.
            if direct_match_rule is None:
                # The "Document Number in Narration Match" (ALLOCATION)
                # catalog row itself is disabled - this definition has opted
                # out of the no-customer direct-match path entirely, so
                # there's nothing left to resolve this payment with.
                await _unapplied(outcome, None)
                continue
            invoice = next(
                (i for i in unresolved_invoices if i["invoice_id"] == outcome["direct_invoice_id"]),
                None,
            )
            if invoice is None:
                # Already closed by another direct match earlier in this
                # same run (two payments' narration both named it) - can't
                # double-allocate it, so this one matched nothing.
                await _unapplied(outcome, None)
            else:
                await _commit_direct_match(outcome, invoice)
            continue

        if outcome["customer_id"] is not None:
            pending.append((outcome, outcome["customer_id"]))
            continue

        candidates = outcome["candidate_pool"]
        bank_txn = outcome["bank_txn"]
        amount = bank_txn["amount_minor"]
        per_candidate_matches: list[tuple[str, dict | None, object]] = (
            []
        )  # (customer_id, rule, alloc_result)
        for candidate_id in candidates:
            alloc_result = None
            fired_rule = None
            for rule in allocation_rules:
                rule_fn = ALLOCATION_RULES.get(rule["kind"])
                if rule_fn is None:
                    continue
                alloc_result = await rule_fn(
                    {"total_received_minor": amount},
                    bank_txn,
                    candidate_id,
                    ctx,
                    rule["config"],
                )
                if alloc_result.matched:
                    fired_rule = rule
                    break
            if (
                alloc_result is not None
                and alloc_result.allocations
                and not alloc_result.ambiguous
            ):
                per_candidate_matches.append((candidate_id, fired_rule, alloc_result))

        if len(per_candidate_matches) >= 2:
            # Double-Collision: 2+ different candidates each produced a clean match.
            amt_fmt = f"₹{amount / 100:,.2f}"
            ref_str = _ref_str(bank_txn)
            candidate_names = [
                cust_name_map.get(cid, cid[:8]) for cid, _, _ in per_candidate_matches
            ]
            names_str = ", ".join(candidate_names)
            reason = f"{amt_fmt} payment{ref_str} produced valid matches for {len(per_candidate_matches)} candidates ({names_str})"
            detail = {
                "candidates": [
                    {"customer_id": cid, "customer_name": cust_name_map.get(cid)}
                    for cid, _, _ in per_candidate_matches
                ],
                "amount_minor": amount,
            }
            await dao.insert_exception(
                run_id=run_id,
                exception_type="DOUBLE_COLLISION",
                bank_txn_id=bank_txn["bank_txn_id"],
                customer_id=None,
                discrepancy_minor=amount,
                reason_code=reason,
                detail=detail,
            )
            await dao.mark_bank_statement_status(bank_txn["bank_txn_id"], "EXCEPTION")
            money["exception"] += amount
            money["unapplied"] += amount
            counts["double_collision"] += 1
            payment_ledger_records.append(
                {
                    "payment_id": outcome["payment_id"],
                    "bank_txn_id": bank_txn["bank_txn_id"],
                    "customer_id": None,
                    "currency": bank_txn["currency"],
                    "unapplied_minor": amount,
                    "allocations": [],
                }
            )
            continue

        if len(per_candidate_matches) == 1:
            # Exactly one pooled candidate, and it produced a clean
            # (non-ambiguous) rule match - the same "unique answer" bar
            # Double-Collision uses to decide *not* to trust a pool, just
            # inverted: 2+ clean matches means real ambiguity, but a single
            # clean match against a single candidate is no longer treated as
            # merely a suggestion (2026-08 change - was always Suspense
            # before, regardless of how clean the match was; see this run's
            # git history for the prior behavior/rationale if that's ever
            # needed again). Committed exactly like a Pass B match - same
            # `_commit`, same match_group/GL treatment.
            cid, rule, alloc_result = per_candidate_matches[0]
            await _commit(outcome, cid, rule, alloc_result)
            continue

        # 0 of the pool's candidates produced any match at all.
        await _suspense(
            outcome,
            reason_code=f"candidate pool of {len(candidates)} customers but none resolved by exact amount",
            detail={"candidate_customer_ids": candidates},
        )

    # --- Pass B: rule-outer / payment-inner, grouped by resolved customer -
    # every item here came from a genuine Phase 1a lock (Pass A never defers
    # a pool resolution into this pass).
    by_customer: dict[str, list[dict]] = defaultdict(list)
    for item, customer_id in pending:
        by_customer[customer_id].append(item)

    for customer_id, items in by_customer.items():
        remaining = items  # preserves `outcomes`' original (transaction_date) order
        for rule in allocation_rules:
            rule_fn = ALLOCATION_RULES.get(rule["kind"])
            if rule_fn is None or not remaining:
                continue
            still_remaining = []
            for item in remaining:
                bank_txn = item["bank_txn"]
                amount = bank_txn["amount_minor"]
                alloc_result = await rule_fn(
                    {"total_received_minor": amount},
                    bank_txn,
                    customer_id,
                    ctx,
                    rule["config"],
                )
                if alloc_result.ambiguous:
                    cust_name = cust_name_map.get(customer_id) or "Customer"
                    amt_fmt = f"₹{amount / 100:,.2f}"
                    ref_str = _ref_str(bank_txn)
                    reason = f"{cust_name} payment {amt_fmt}{ref_str} matches more than one open invoice ({alloc_result.reason})"
                    await dao.insert_exception(
                        run_id=run_id,
                        exception_type="MULTIPLE_INVOICE_MATCH",
                        bank_txn_id=bank_txn["bank_txn_id"],
                        customer_id=customer_id,
                        discrepancy_minor=amount,
                        reason_code=reason,
                        detail={
                            "invoice_ids": alloc_result.ambiguous_invoice_ids,
                            "amount_minor": amount,
                            "customer_name": cust_name,
                        },
                    )
                    await dao.mark_bank_statement_status(
                        bank_txn["bank_txn_id"], "EXCEPTION"
                    )
                    flagged_invoice_ids.update(alloc_result.ambiguous_invoice_ids)
                    money["exception"] += amount
                    money["unapplied"] += amount
                    counts["ambiguous"] += 1
                    payment_ledger_records.append(
                        {
                            "payment_id": item["payment_id"],
                            "bank_txn_id": bank_txn["bank_txn_id"],
                            "customer_id": customer_id,
                            "currency": bank_txn["currency"],
                            "unapplied_minor": amount,
                            "allocations": [],
                        }
                    )
                    continue
                if alloc_result.allocations:
                    await _commit(item, customer_id, rule, alloc_result)
                    continue
                still_remaining.append(item)
            remaining = still_remaining
        # Nothing in `remaining` matched any of the 9 rules for this customer.
        for item in remaining:
            await _unapplied(item, customer_id)

    # No-Payment sweep: open invoices nothing in this run touched or flagged at all.
    no_payment_count = 0
    for inv_list in invoices_by_customer.values():
        for inv in inv_list:
            if (
                inv["balance_due_minor"] > 0
                and inv["invoice_id"] not in touched_invoice_ids
                and inv["invoice_id"] not in flagged_invoice_ids
            ):
                cust_name = cust_name_map.get(inv["customer_id"]) or "Customer"
                bal_fmt = f"₹{inv['balance_due_minor'] / 100:,.2f}"
                reason = f"{cust_name} invoice {inv['invoice_number']} ({bal_fmt}) has received no payment"
                await dao.insert_exception(
                    run_id=run_id,
                    exception_type="NO_PAYMENT",
                    bank_txn_id=None,
                    customer_id=inv["customer_id"],
                    invoice_id=inv["invoice_id"],
                    discrepancy_minor=inv["balance_due_minor"],
                    reason_code=reason,
                    detail={
                        "invoice_number": inv["invoice_number"],
                        "balance_due_minor": inv["balance_due_minor"],
                        "customer_name": cust_name,
                    },
                )
                no_payment_count += 1

    # Unresolved-customer sweep: invoices ingested without a resolvable
    # customer_code (migration 0031) - a distinct problem from an ordinary
    # No-Payment (that customer's own invoice, just not paid yet). Flagged
    # every run until a narration-based invoice-number match (or a human)
    # resolves the customer, at which point the invoice moves into the
    # normal invoices_by_customer working set and stops appearing here.
    # Skips anything already in touched_invoice_ids (2026-08e fix) - a
    # customer-less invoice that received a *partial* settlement this run
    # (via _commit_direct_match/_commit_direct_group_match) already raises
    # its own SHORT_PAY exception; without this guard it also fell through
    # here and got a second, redundant "has no linked customer" exception
    # for the exact same event, same reasoning as the No-Payment sweep
    # above (which already checks touched_invoice_ids for the
    # customer-locked case) - this sweep just never had the same guard.
    for inv in unresolved_invoices:
        if inv["balance_due_minor"] <= 0 or inv["invoice_id"] in touched_invoice_ids:
            continue
        bal_fmt = f"₹{inv['balance_due_minor'] / 100:,.2f}"
        reason = f"Invoice {inv['invoice_number']} ({bal_fmt}) has no linked customer - cannot be reconciled"
        await dao.insert_exception(
            run_id=run_id,
            exception_type="NO_PAYMENT",
            bank_txn_id=None,
            customer_id=None,
            invoice_id=inv["invoice_id"],
            discrepancy_minor=inv["balance_due_minor"],
            reason_code=reason,
            detail={
                "invoice_number": inv["invoice_number"],
                "balance_due_minor": inv["balance_due_minor"],
                "unresolved_customer": True,
            },
        )
        no_payment_count += 1

    return {
        "matched_count": counts["matched"],
        "exception_count": counts["short_pay"]
        + counts["ambiguous"]
        + counts["double_collision"]
        + counts["unresolved"]
        + no_payment_count,
        "matched_value_minor": money["matched"],
        "exception_value_minor": money["exception"],
        "unapplied_minor": money["unapplied"],
        "unresolved_pool_count": counts["ambiguous"]
        + counts["double_collision"]
        + counts["unresolved"],
        # M3: gl_posting.py's single source of truth for what to post - one
        # entry per payment processed this phase, every outcome type included.
        "payment_ledger_records": payment_ledger_records,
    }
