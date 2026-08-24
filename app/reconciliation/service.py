"""Business logic for the reconciliation module.

Auth/permission checks (recon.run.prepare|approve, recon.exception.resolve -
see the RBAC design) are not wired in yet, same as app/datahub/service.py -
app/auth/ exists but is stubbed. Where each `Depends(require_permission(...))`
belongs is noted in router.py.
"""

from __future__ import annotations

import asyncpg
from fastapi import HTTPException, status

from app.reconciliation.constants import (
    DEFAULT_AR_RULE_CATALOG,
    EXCEPTION_RESOLUTION_OUTCOMES,
    EXCEPTION_STATUSES,
    PHASE_ALLOCATION,
    PHASE_CANDIDATE_POOL,
    PHASE_CUSTOMER_LOCK,
    PHASE_GL_CHECK,
    PHASE_INTAKE_VALIDATION,
    PHASE_SHORT_PAY,
    PHASE_UNAPPLIED,
    ReconciliationErrors,
    RECON_PHASES,
    RECON_TYPES,
    RULE_DATA_CATEGORIES,
)
from app.reconciliation.schema import (
    RuleCategoriesResponse,
    ExceptionResolveRequest,
    ExceptionOut,
)
from app.reconciliation.dao import ReconciliationDAO, new_run_no
from app.reconciliation.rules.allocation import ALLOCATION_RULES
from app.reconciliation.rules.identification import IDENTIFICATION_RULES
from app.reconciliation.rules import generic_functions, matchers
from app.reconciliation.rules.matchers import MATCHER_KINDS, SOURCE_KINDS
from app.reconciliation.rules.pooling import POOLING_RULES

# Which rule-dispatch registry validates `kind` for a given phase - the two
# threshold-only phases (SHORT_PAY/UNAPPLIED/GL_CHECK aren't in here, they're
# checked separately below) since they're read directly, not dispatched
# through a per-kind registry (see rules.get_threshold_minor).
_REGISTRY_BY_PHASE = {
    PHASE_INTAKE_VALIDATION: IDENTIFICATION_RULES,
    PHASE_CUSTOMER_LOCK: IDENTIFICATION_RULES,
    PHASE_CANDIDATE_POOL: POOLING_RULES,
    PHASE_ALLOCATION: ALLOCATION_RULES,
}
_THRESHOLD_ONLY_PHASES = frozenset({PHASE_SHORT_PAY, PHASE_UNAPPLIED, PHASE_GL_CHECK})


def _validate_field_match_config(config: dict) -> None:
    matcher = config.get("matcher")
    bank_field = config.get("bank_field")
    source = config.get("source")
    source_field = config.get("source_field")
    if not (matcher and bank_field and source and source_field):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_FIELD_MATCH_CONFIG
        )
    if matcher not in MATCHER_KINDS or source not in SOURCE_KINDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_FIELD_MATCH_CONFIG
        )


class ReconciliationService:
    def __init__(self, dao: ReconciliationDAO) -> None:
        self.dao = dao

    # -- reconciliation_definitions ------------------------------------------------
    async def create_definition(
        self,
        *,
        entity_id: str,
        name: str,
        recon_type: str,
        cadence: str | None,
        owner_user_id: str | None,
    ):
        if recon_type not in RECON_TYPES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_RECON_TYPE
            )
        if not await self.dao.entity_exists(entity_id):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.ENTITY_NOT_FOUND
            )

        definition = await self.dao.insert_definition(
            entity_id=entity_id,
            name=name,
            recon_type=recon_type,
            cadence=cadence,
            owner_user_id=owner_user_id,
        )
        if recon_type == "AR":
            # AR is the only recon_type with an implemented engine/rule catalog
            # today (AP/BANK are reserved schema values - see constants.py).
            # GL roles are seeded here, not left as a separate manual step,
            # so a definition is never left unable to post once M3 lands.
            await self.dao.insert_rules_bulk(
                definition["definition_id"], list(DEFAULT_AR_RULE_CATALOG)
            )
            await self.dao.seed_gl_account_roles(entity_id)
        return definition

    async def get_definition(self, definition_id: str):
        row = await self.dao.get_definition(definition_id)
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.DEFINITION_NOT_FOUND
            )
        return row

    async def list_definitions(self, *, entity_id: str | None):
        return await self.dao.list_definitions(entity_id=entity_id)

    # -- reconciliation_rules ------------------------------------------------------
    async def list_rules(self, definition_id: str):
        await self.get_definition(definition_id)  # 404s if missing
        return await self.dao.list_rules(definition_id)

    async def update_rule(
        self,
        definition_id: str,
        rule_id: str,
        *,
        enabled: bool | None,
        confidence: int | None = None,
        config: dict | None,
    ):
        await self.get_definition(definition_id)
        existing = await self.dao.get_rule(rule_id)
        # asyncpg returns uuid columns as uuid.UUID, not str - compare as str
        # on both sides or this always fails even for the correct owner.
        if existing is None or str(existing["definition_id"]) != definition_id:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.RULE_NOT_FOUND
            )
        return await self.dao.update_rule(
            rule_id, enabled=enabled, confidence=confidence, config=config
        )

    async def delete_rule(self, definition_id: str, rule_id: str):
        await self.get_definition(definition_id)
        existing = await self.dao.get_rule(rule_id)
        if existing is None or str(existing["definition_id"]) != definition_id:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.RULE_NOT_FOUND
            )
        try:
            return await self.dao.delete_rule(rule_id)
        except asyncpg.exceptions.ForeignKeyViolationError:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Cannot delete rule: it is referenced by existing matches or payments.",
            )

    def list_matcher_catalog(self) -> dict:
        """Static reference data (no DB) for the `kind="field-match"`
        picker - the frontend's source of truth for valid matcher/source/
        bank_field values, so it never hardcodes a list that can drift from
        what rules.matchers.find_matches actually accepts."""
        return {
            "matchers": matchers.MATCHER_CATALOG,
            "sources": [
                {"source": source, "fields": fields}
                for source, fields in matchers.SOURCE_FIELDS.items()
            ],
            "bank_fields": matchers.BANK_FIELDS,
        }

    def list_algorithm_catalog(self) -> dict:
        """Every comparison/extraction algorithm in the reconciliation
        module, in one place - rules.matchers.MATCHER_CATALOG (wired, usable
        today via kind='field-match') plus
        rules.generic_functions.GENERIC_FUNCTION_CATALOG (standalone, not
        called by any rule/dispatcher yet)."""
        algorithms = [
            {
                "name": entry["kind"],
                "category": "matcher",
                "label": entry["label"],
                "description": entry["description"],
                "action_verb": None,
                "wired": True,
            }
            for entry in matchers.MATCHER_CATALOG
        ] + [
            {
                "name": entry["technical_name"],
                "category": "generic_function",
                "label": entry["ui_display_name"],
                "description": entry["description"],
                "action_verb": entry["ui_action_verb"],
                "wired": False,
            }
            for entry in generic_functions.GENERIC_FUNCTION_CATALOG
        ]
        return {"algorithms": algorithms}

    async def create_rule(
        self,
        definition_id: str,
        *,
        phase: str,
        kind: str,
        name: str,
        priority: int,
        confidence: int | None,
        config: dict,
    ):
        await self.get_definition(definition_id)  # 404s if missing
        if phase not in RECON_PHASES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_PHASE
            )

        if phase in _THRESHOLD_ONLY_PHASES:
            if kind != "threshold":
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_RULE_KIND
                )
        else:
            registry = _REGISTRY_BY_PHASE[phase]
            if kind not in registry:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_RULE_KIND
                )

        if kind == "field-match":
            _validate_field_match_config(config)

        try:
            return await self.dao.insert_rule(
                definition_id,
                phase=phase,
                kind=kind,
                name=name,
                priority=priority,
                confidence=confidence,
                config=config,
            )
        except asyncpg.exceptions.UniqueViolationError:
            raise HTTPException(
                status.HTTP_409_CONFLICT, ReconciliationErrors.DUPLICATE_PRIORITY
            )

    # -- reconciliation_runs (enqueue only - execution is the M1+ worker) ------------
    async def create_run(self, definition_id: str, *, period_start, period_end):
        await self.get_definition(definition_id)  # 404s if missing
        return await self.dao.insert_run(
            definition_id=definition_id,
            run_no=new_run_no(),
            period_start=period_start,
            period_end=period_end,
        )

    # DEV-ONLY: see ReconciliationDAO.reset_definition. Grep "DEV-ONLY" to
    # find every piece of this (this method, the router endpoint, the DAO
    # method) if/when it's time to remove it.
    async def rerun(self, definition_id: str, *, period_start, period_end):
        definition = await self.get_definition(definition_id)  # 404s if missing
        await self.dao.reset_definition(definition_id, definition["entity_id"])
        return await self.dao.insert_run(
            definition_id=definition_id,
            run_no=new_run_no(),
            period_start=period_start,
            period_end=period_end,
        )

    async def get_run(self, run_id: str):
        row = await self.dao.get_run(run_id)
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.RUN_NOT_FOUND
            )
        return row

    async def list_runs(self, *, definition_id: str | None, status_: str | None):
        return await self.dao.list_runs(definition_id=definition_id, status=status_)

    async def retry_run(self, run_id: str):
        await self.get_run(run_id)  # 404s if missing
        row = await self.dao.retry_run(run_id)
        if row is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, ReconciliationErrors.RUN_NOT_RETRYABLE
            )
        return row

    # -- match_groups / reconciliation_exceptions (M3, run results) ------------------
    async def list_matches(self, run_id: str):
        await self.get_run(run_id)  # 404s if missing
        return await self.dao.list_match_groups_for_run(run_id)

    async def list_exceptions(self, run_id: str, *, status_: str | None):
        await self.get_run(run_id)  # 404s if missing
        if status_ is not None and status_ not in EXCEPTION_STATUSES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                ReconciliationErrors.INVALID_EXCEPTION_STATUS,
            )
        return await self.dao.list_exceptions_for_run(run_id, status_)

    async def update_exception(
        self,
        exception_id: str,
        *,
        status_: str | None,
        resolution_outcome: str | None,
        resolution_notes: str | None,
    ):
        existing = await self.dao.get_exception(exception_id)
        if existing is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.EXCEPTION_NOT_FOUND
            )
        if status_ is not None and status_ not in EXCEPTION_STATUSES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                ReconciliationErrors.INVALID_EXCEPTION_STATUS,
            )
        if (
            resolution_outcome is not None
            and resolution_outcome not in EXCEPTION_RESOLUTION_OUTCOMES
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                ReconciliationErrors.INVALID_RESOLUTION_OUTCOME,
            )
        # resolver_id is None until real auth provides the caller's user id
        return await self.dao.update_exception(
            exception_id,
            status=status_,
            resolution_outcome=resolution_outcome,
            resolution_notes=resolution_notes,
            resolver_id=None,
        )

    async def list_open_invoices(
        self, run_id: str, *, search: str | None, limit: int = 50
    ):
        """The Suspense resolution panel's "match to a different invoice"
        fallback - every open invoice for this run's entity, optionally
        filtered by `search` (invoice_number or customer name)."""
        run_context = await self.dao.get_run_context(run_id)
        if run_context is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.RUN_NOT_FOUND
            )
        return await self.dao.list_open_invoices_for_entity(
            str(run_context["entity_id"]), search, limit
        )

    async def list_open_payments(self, run_id: str):
        """The candidate pool for the No-Payment-Received resolution panel -
        every payment in this run's entity that still has real leftover cash
        (unapplied_minor > 0), regardless of which run originally processed
        it (see dao.list_open_payments_for_entity)."""
        run_context = await self.dao.get_run_context(run_id)
        if run_context is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.RUN_NOT_FOUND
            )
        return await self.dao.list_open_payments_for_entity(
            str(run_context["entity_id"])
        )

    async def resolve_no_payment(
        self, exception_id: str, *, payment_ids: list[str], note: str | None
    ):
        """Matches the prototype's `arNoPaymentPanel` "Match selected
        payment(s)" action exactly: fills this exception's invoice from the
        selected payments in the order given (each capped at its own
        unapplied_minor), writes one MANUAL match_group + one
        invoice_allocations row per payment that contributed cash, and
        cross-resolves any of those payments' own open Suspense exceptions -
        a reviewer picking payments here already answered "who is this
        money from," so a separate Suspense review for the same payment
        would be redundant."""
        exc = await self.dao.get_exception(exception_id)
        if exc is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.EXCEPTION_NOT_FOUND
            )
        if exc["exception_type"] != "NO_PAYMENT" or exc["invoice_id"] is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                ReconciliationErrors.NOT_A_NO_PAYMENT_EXCEPTION,
            )
        if not payment_ids:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                ReconciliationErrors.NO_PAYMENT_IDS_SELECTED,
            )

        invoice = await self.dao.get_invoice(str(exc["invoice_id"]))
        if invoice is None or invoice["balance_due_minor"] <= 0:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVOICE_NOT_OPEN
            )

        payments = []
        for payment_id in payment_ids:
            payment = await self.dao.get_payment(payment_id)
            if payment is None or payment["unapplied_minor"] <= 0:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    ReconciliationErrors.PAYMENT_NOT_FOUND_OR_NOT_OPEN,
                )
            payments.append(payment)

        match_group = await self.dao.insert_match_group(
            run_id=str(exc["run_id"]),
            match_type="MANUAL",
            rule_id=None,
            confidence=None,
            status="CONFIRMED",
            reason=note or "Manually matched from No Payment Received",
        )

        remaining = invoice["balance_due_minor"]
        cash_applied = 0
        for payment in payments:
            if remaining <= 0:
                break
            take = min(payment["unapplied_minor"], remaining)
            if take <= 0:
                continue
            await self.dao.insert_invoice_allocation(
                match_group_id=match_group["match_group_id"],
                invoice_id=invoice["invoice_id"],
                payment_id=payment["payment_id"],
                bank_txn_id=payment["bank_txn_id"],
                allocated_minor=take,
            )
            await self.dao.apply_payment_allocation(payment["payment_id"], take)
            await self.dao.auto_resolve_suspense_for_payment(
                payment["payment_id"], note
            )
            remaining -= take
            cash_applied += take

        if cash_applied > 0:
            await self.dao.apply_invoice_allocation(invoice["invoice_id"], cash_applied)

        await self.dao.update_exception(
            exception_id,
            status="RESOLVED",
            resolution_outcome="MANUAL_MATCH",
            resolution_notes=note,
            resolver_id=None,
            match_group_id=match_group["match_group_id"],
        )
        return await self.dao.get_exception(exception_id)

    async def list_open_invoices_for_customer(self, customer_id: str):
        """The Suspense resolution panel's invoice picker, once a candidate
        customer is selected - see dao.list_open_invoices_for_customer."""
        customer = await self.dao.get_customer(customer_id)
        if customer is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.CUSTOMER_NOT_FOUND
            )
        return await self.dao.list_open_invoices_for_customer(customer_id)

    async def resolve_suspense(
        self,
        exception_id: str,
        *,
        customer_id: str,
        invoice_ids: list[str],
        note: str | None,
    ):
        """Matches the prototype's `arSuspensePanel` resolution actions -
        "Likely Match (Exact Amount)", a candidate-pool pick, and the manual
        invoice picker are all the same underlying decision here: confirm
        which customer this unidentified payment belongs to, and optionally
        which of their open invoices it settles. Locks the payment to
        `customer_id` (if not already), applies its cash across
        `invoice_ids` in order, and resolves the exception. Empty
        `invoice_ids` matches the prototype's "found a candidate but no
        exact invoice match - apply to their account as unapplied cash"
        case - the payment is confirmed to belong to this customer but
        stays fully unapplied."""
        exc = await self.dao.get_exception(exception_id)
        if exc is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.EXCEPTION_NOT_FOUND
            )
        if exc["exception_type"] != "SUSPENSE" or exc["bank_txn_id"] is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                ReconciliationErrors.NOT_A_SUSPENSE_EXCEPTION,
            )

        customer = await self.dao.get_customer(customer_id)
        if customer is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.CUSTOMER_NOT_FOUND
            )

        payment = await self.dao.get_payment_by_bank_txn(str(exc["bank_txn_id"]))
        if payment is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                ReconciliationErrors.SUSPENSE_PAYMENT_NOT_FOUND,
            )

        if payment["customer_id"] is None or str(payment["customer_id"]) != str(
            customer_id
        ):
            await self.dao.lock_payment_customer(
                payment["payment_id"], customer_id, None
            )

        match_group_id = None
        cash_applied = 0
        if invoice_ids:
            invoices = []
            for invoice_id in invoice_ids:
                invoice = await self.dao.get_invoice(invoice_id)
                if (
                    invoice is None
                    or invoice["balance_due_minor"] <= 0
                    or str(invoice["customer_id"]) != str(customer_id)
                ):
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        ReconciliationErrors.INVOICE_NOT_FOUND_FOR_CUSTOMER,
                    )
                invoices.append(invoice)

            match_group = await self.dao.insert_match_group(
                run_id=str(exc["run_id"]),
                match_type="MANUAL",
                rule_id=None,
                confidence=None,
                status="CONFIRMED",
                reason=note or "Manually matched from Suspense",
            )
            match_group_id = match_group["match_group_id"]

            remaining = payment["unapplied_minor"]
            for invoice in invoices:
                if remaining <= 0:
                    break
                take = min(invoice["balance_due_minor"], remaining)
                if take <= 0:
                    continue
                await self.dao.insert_invoice_allocation(
                    match_group_id=match_group_id,
                    invoice_id=invoice["invoice_id"],
                    payment_id=payment["payment_id"],
                    bank_txn_id=payment["bank_txn_id"],
                    allocated_minor=take,
                )
                await self.dao.apply_invoice_allocation(invoice["invoice_id"], take)
                remaining -= take
                cash_applied += take

            if cash_applied > 0:
                await self.dao.apply_payment_allocation(
                    payment["payment_id"], cash_applied
                )
                if remaining <= 0 and cash_applied >= payment["unapplied_minor"]:
                    await self.dao.mark_bank_statement_status(
                        payment["bank_txn_id"], "MATCHED"
                    )

        await self.dao.update_exception(
            exception_id,
            status="RESOLVED",
            resolution_outcome="MANUAL_MATCH" if cash_applied > 0 else "ON_ACCOUNT",
            resolution_notes=note,
            resolver_id=None,
            match_group_id=match_group_id,
        )
        return await self.dao.get_exception(exception_id)

    async def resolve_multiple_match(
        self,
        exception_id: str,
        *,
        customer_id: str,
        invoice_ids: list[str],
        note: str | None,
    ):
        """Resolves a `MULTIPLE_INVOICE_MATCH` exception by allocating payment cash
        to the selected invoice_ids for the confirmed customer."""
        exc = await self.dao.get_exception(exception_id)
        if exc is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.EXCEPTION_NOT_FOUND
            )
        if (
            exc["exception_type"] != "MULTIPLE_INVOICE_MATCH"
            or exc["bank_txn_id"] is None
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                ReconciliationErrors.NOT_A_MULTIPLE_MATCH_EXCEPTION,
            )

        customer = await self.dao.get_customer(customer_id)
        if customer is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.CUSTOMER_NOT_FOUND
            )

        payment = await self.dao.get_payment_by_bank_txn(str(exc["bank_txn_id"]))
        if payment is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                ReconciliationErrors.SUSPENSE_PAYMENT_NOT_FOUND,
            )

        if payment["customer_id"] is None or str(payment["customer_id"]) != str(
            customer_id
        ):
            await self.dao.lock_payment_customer(
                payment["payment_id"], customer_id, None
            )

        match_group_id = None
        cash_applied = 0
        if invoice_ids:
            invoices = []
            for invoice_id in invoice_ids:
                invoice = await self.dao.get_invoice(invoice_id)
                if (
                    invoice is None
                    or invoice["balance_due_minor"] <= 0
                    or str(invoice["customer_id"]) != str(customer_id)
                ):
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        ReconciliationErrors.INVOICE_NOT_FOUND_FOR_CUSTOMER,
                    )
                invoices.append(invoice)

            match_group = await self.dao.insert_match_group(
                run_id=str(exc["run_id"]),
                match_type="MANUAL",
                rule_id=None,
                confidence=None,
                status="CONFIRMED",
                reason=note or "Manually matched from Multiple Invoice Match tie-break",
            )
            match_group_id = match_group["match_group_id"]

            remaining = payment["unapplied_minor"]
            for invoice in invoices:
                if remaining <= 0:
                    break
                take = min(invoice["balance_due_minor"], remaining)
                if take <= 0:
                    continue
                await self.dao.insert_invoice_allocation(
                    match_group_id=match_group_id,
                    invoice_id=invoice["invoice_id"],
                    payment_id=payment["payment_id"],
                    bank_txn_id=payment["bank_txn_id"],
                    allocated_minor=take,
                )
                await self.dao.apply_invoice_allocation(invoice["invoice_id"], take)
                remaining -= take
                cash_applied += take

            if cash_applied > 0:
                await self.dao.apply_payment_allocation(
                    payment["payment_id"], cash_applied
                )
                if remaining <= 0 and cash_applied >= payment["unapplied_minor"]:
                    await self.dao.mark_bank_statement_status(
                        payment["bank_txn_id"], "MATCHED"
                    )

        await self.dao.update_exception(
            exception_id,
            status="RESOLVED",
            resolution_outcome="MANUAL_MATCH" if cash_applied > 0 else "ON_ACCOUNT",
            resolution_notes=note,
            resolver_id=None,
            match_group_id=match_group_id,
        )
        return await self.dao.get_exception(exception_id)

    async def resolve_exception_unified(
        self, payload: ExceptionResolveRequest
    ) -> ExceptionOut:
        """Unified exception resolution dispatcher. Looks up exception_type from DB using exception_id passed in body."""
        exception_id_str = str(payload.exception_id)
        exc = await self.dao.get_exception(exception_id_str)
        if exc is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, ReconciliationErrors.EXCEPTION_NOT_FOUND
            )

        exc_type = exc.get("exception_type")
        note = payload.note or payload.resolution_notes

        match exc_type:
            case "NO_PAYMENT":
                if payload.payment_ids:
                    return await self.resolve_no_payment(
                        exception_id_str,
                        payment_ids=[str(pid) for pid in payload.payment_ids],
                        note=note,
                    )
                status_to_apply = payload.status or "CARRIED_FORWARD"
                return await self.update_exception(
                    exception_id_str,
                    status_=status_to_apply,
                    resolution_outcome=payload.resolution_outcome,
                    resolution_notes=note,
                )

            case "MULTIPLE_INVOICE_MATCH":
                if payload.customer_id:
                    return await self.resolve_multiple_match(
                        exception_id_str,
                        customer_id=str(payload.customer_id),
                        invoice_ids=[str(iid) for iid in (payload.invoice_ids or [])],
                        note=note,
                    )
                status_to_apply = payload.status or "RESOLVED"
                return await self.update_exception(
                    exception_id_str,
                    status_=status_to_apply,
                    resolution_outcome=payload.resolution_outcome,
                    resolution_notes=note,
                )

            case "SUSPENSE":
                if payload.customer_id:
                    return await self.resolve_suspense(
                        exception_id_str,
                        customer_id=str(payload.customer_id),
                        invoice_ids=[str(iid) for iid in (payload.invoice_ids or [])],
                        note=note,
                    )
                status_to_apply = payload.status or "RESOLVED"
                return await self.update_exception(
                    exception_id_str,
                    status_=status_to_apply,
                    resolution_outcome=payload.resolution_outcome or "JOURNAL",
                    resolution_notes=note,
                )

            case "SHORT_PAY":
                status_to_apply = payload.status or "RESOLVED"
                outcome_to_apply = payload.resolution_outcome or "WRITEOFF"
                return await self.update_exception(
                    exception_id_str,
                    status_=status_to_apply,
                    resolution_outcome=outcome_to_apply,
                    resolution_notes=note,
                )

            case "GL_VARIANCE":
                status_to_apply = payload.status or "RESOLVED"
                outcome_to_apply = payload.resolution_outcome or "JOURNAL"
                return await self.update_exception(
                    exception_id_str,
                    status_=status_to_apply,
                    resolution_outcome=outcome_to_apply,
                    resolution_notes=note,
                )

            case "DOUBLE_COLLISION":
                status_to_apply = payload.status or "RESOLVED"
                return await self.update_exception(
                    exception_id_str,
                    status_=status_to_apply,
                    resolution_outcome=payload.resolution_outcome,
                    resolution_notes=note,
                )

            case _:
                status_to_apply = payload.status or "RESOLVED"
                return await self.update_exception(
                    exception_id_str,
                    status_=status_to_apply,
                    resolution_outcome=payload.resolution_outcome,
                    resolution_notes=note,
                )

    async def get_rule_categories(self) -> RuleCategoriesResponse:
        return RuleCategoriesResponse(categories=RULE_DATA_CATEGORIES)
