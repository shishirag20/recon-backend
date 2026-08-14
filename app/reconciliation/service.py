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
)
from app.reconciliation.dao import ReconciliationDAO, new_run_no
from app.reconciliation.rules.allocation import ALLOCATION_RULES
from app.reconciliation.rules.identification import IDENTIFICATION_RULES
from app.reconciliation.rules import matchers
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
        raise HTTPException(status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_FIELD_MATCH_CONFIG)
    if matcher not in MATCHER_KINDS or source not in SOURCE_KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_FIELD_MATCH_CONFIG)


class ReconciliationService:
    def __init__(self, dao: ReconciliationDAO) -> None:
        self.dao = dao

    # -- reconciliation_definitions ------------------------------------------------
    async def create_definition(
        self, *, entity_id: str, name: str, recon_type: str, cadence: str | None, owner_user_id: str | None
    ):
        if recon_type not in RECON_TYPES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_RECON_TYPE)
        if not await self.dao.entity_exists(entity_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, ReconciliationErrors.ENTITY_NOT_FOUND)

        definition = await self.dao.insert_definition(
            entity_id=entity_id, name=name, recon_type=recon_type, cadence=cadence, owner_user_id=owner_user_id
        )
        if recon_type == "AR":
            # AR is the only recon_type with an implemented engine/rule catalog
            # today (AP/BANK are reserved schema values - see constants.py).
            # GL roles are seeded here, not left as a separate manual step,
            # so a definition is never left unable to post once M3 lands.
            await self.dao.insert_rules_bulk(definition["definition_id"], list(DEFAULT_AR_RULE_CATALOG))
            await self.dao.seed_gl_account_roles(entity_id)
        return definition

    async def get_definition(self, definition_id: str):
        row = await self.dao.get_definition(definition_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, ReconciliationErrors.DEFINITION_NOT_FOUND)
        return row

    async def list_definitions(self, *, entity_id: str | None):
        return await self.dao.list_definitions(entity_id=entity_id)

    # -- reconciliation_rules ------------------------------------------------------
    async def list_rules(self, definition_id: str):
        await self.get_definition(definition_id)  # 404s if missing
        return await self.dao.list_rules(definition_id)

    async def update_rule(self, definition_id: str, rule_id: str, *, enabled: bool | None, config: dict | None):
        await self.get_definition(definition_id)
        existing = await self.dao.get_rule(rule_id)
        # asyncpg returns uuid columns as uuid.UUID, not str - compare as str
        # on both sides or this always fails even for the correct owner.
        if existing is None or str(existing["definition_id"]) != definition_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, ReconciliationErrors.RULE_NOT_FOUND)
        return await self.dao.update_rule(rule_id, enabled=enabled, config=config)

    def list_matcher_catalog(self) -> dict:
        """Static reference data (no DB) for the `kind="field-match"`
        picker - the frontend's source of truth for valid matcher/source/
        bank_field values, so it never hardcodes a list that can drift from
        what rules.matchers.find_matches actually accepts."""
        return {
            "matchers": matchers.MATCHER_CATALOG,
            "sources": [{"source": source, "fields": fields} for source, fields in matchers.SOURCE_FIELDS.items()],
            "bank_fields": matchers.BANK_FIELDS,
        }

    async def create_rule(
        self, definition_id: str, *, phase: str, kind: str, name: str, priority: int,
        confidence: int | None, config: dict,
    ):
        await self.get_definition(definition_id)  # 404s if missing
        if phase not in RECON_PHASES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_PHASE)

        if phase in _THRESHOLD_ONLY_PHASES:
            if kind != "threshold":
                raise HTTPException(status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_RULE_KIND)
        else:
            registry = _REGISTRY_BY_PHASE[phase]
            if kind not in registry:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_RULE_KIND)

        if kind == "field-match":
            _validate_field_match_config(config)

        try:
            return await self.dao.insert_rule(
                definition_id, phase=phase, kind=kind, name=name, priority=priority,
                confidence=confidence, config=config,
            )
        except asyncpg.exceptions.UniqueViolationError:
            raise HTTPException(status.HTTP_409_CONFLICT, ReconciliationErrors.DUPLICATE_PRIORITY)

    # -- reconciliation_runs (enqueue only - execution is the M1+ worker) ------------
    async def create_run(self, definition_id: str, *, period_start, period_end):
        await self.get_definition(definition_id)  # 404s if missing
        return await self.dao.insert_run(
            definition_id=definition_id, run_no=new_run_no(), period_start=period_start, period_end=period_end
        )

    async def get_run(self, run_id: str):
        row = await self.dao.get_run(run_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, ReconciliationErrors.RUN_NOT_FOUND)
        return row

    async def list_runs(self, *, definition_id: str | None, status_: str | None):
        return await self.dao.list_runs(definition_id=definition_id, status=status_)

    async def retry_run(self, run_id: str):
        await self.get_run(run_id)  # 404s if missing
        row = await self.dao.retry_run(run_id)
        if row is None:
            raise HTTPException(status.HTTP_409_CONFLICT, ReconciliationErrors.RUN_NOT_RETRYABLE)
        return row

    # -- match_groups / reconciliation_exceptions (M3, run results) ------------------
    async def list_matches(self, run_id: str):
        await self.get_run(run_id)  # 404s if missing
        return await self.dao.list_match_groups_for_run(run_id)

    async def list_exceptions(self, run_id: str, *, status_: str | None):
        await self.get_run(run_id)  # 404s if missing
        if status_ is not None and status_ not in EXCEPTION_STATUSES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_EXCEPTION_STATUS)
        return await self.dao.list_exceptions_for_run(run_id, status_)

    async def update_exception(
        self, exception_id: str, *, status_: str | None, resolution_outcome: str | None, resolution_notes: str | None
    ):
        existing = await self.dao.get_exception(exception_id)
        if existing is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, ReconciliationErrors.EXCEPTION_NOT_FOUND)
        if status_ is not None and status_ not in EXCEPTION_STATUSES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_EXCEPTION_STATUS)
        if resolution_outcome is not None and resolution_outcome not in EXCEPTION_RESOLUTION_OUTCOMES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, ReconciliationErrors.INVALID_RESOLUTION_OUTCOME)
        # resolver_id is None until real auth provides the caller's user id
        return await self.dao.update_exception(
            exception_id, status=status_, resolution_outcome=resolution_outcome,
            resolution_notes=resolution_notes, resolver_id=None,
        )
