"""Business logic for the reconciliation module.

Auth/permission checks (recon.run.prepare|approve, recon.exception.resolve -
see the RBAC design) are not wired in yet, same as app/datahub/service.py -
app/auth/ exists but is stubbed. Where each `Depends(require_permission(...))`
belongs is noted in router.py.
"""
from __future__ import annotations

from fastapi import HTTPException, status

from app.reconciliation.constants import (
    DEFAULT_AR_RULE_CATALOG,
    ReconciliationErrors,
    RECON_TYPES,
)
from app.reconciliation.dao import ReconciliationDAO, new_run_no


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
            first_entity = await self.dao.conn.fetchrow("SELECT entity_id FROM entities LIMIT 1")
            if first_entity and first_entity.get("entity_id"):
                entity_id = str(first_entity["entity_id"])
            else:
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
