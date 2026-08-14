"""HTTP layer for the reconciliation module - endpoints only.

Milestone map (see the approved plan "Production-Ready AR Reconciliation
Engine" for full detail):
  M0 (this file, current) - definitions, rule catalog CRUD, run enqueue/status/retry.
  M1 - Phase 1a/1b customer identification actually runs (engine.py + rules/identification.py,
       rules/pooling.py) via a new lease-based app/workers/reconciliation_worker.py.
  M2 - Phase 2 scoped allocation (rules/allocation.py) - match_groups/invoice_allocations land.
  M3 - GL posting + control proof + exception resolution -> GET .../matches,
       GET .../exceptions, PATCH /exceptions/{id} land here.
  M4 - Sign-off + hash-chained audit -> POST /runs/{run_id}/sign-off lands here.

Until M1's worker exists, POST .../runs enqueues a run (status=QUEUED) exactly
like POST /ingestion-jobs enqueues an upload - nothing currently claims it.

Permission gating is TODO pending the real auth module (see app/auth/router.py
and app/datahub/router.py's identical note). Reserved slugs, once
`require_permission` exists: recon.run.prepare -> run/definition writes,
recon.run.approve -> sign-off (M4), recon.exception.resolve -> PATCH /exceptions (M3).
"""
from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, status

from app.reconciliation.constants import ROUTER_TAGS
from app.reconciliation.dao import ReconciliationDAO
from app.reconciliation.schema import (
    DefinitionCreate,
    DefinitionOut,
    ExceptionOut,
    ExceptionUpdate,
    MatcherCatalogResponse,
    MatchGroupOut,
    RuleCreate,
    RuleOut,
    RuleUpdate,
    RunCreate,
    RunOut,
)
from app.reconciliation.service import ReconciliationService
from app.db.pool import get_connection

router = APIRouter(tags=ROUTER_TAGS)


def get_service(conn: asyncpg.Connection = Depends(get_connection)) -> ReconciliationService:
    return ReconciliationService(ReconciliationDAO(conn))


# -- reconciliation_definitions ------------------------------------------------
@router.post(
    "/reconciliations",
    response_model=DefinitionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a reconciliation definition (and seed its rule catalog)",
)
async def create_definition(payload: DefinitionCreate, service: ReconciliationService = Depends(get_service)):
    """For `recon_type=AR` this also seeds the default AR rule catalog
    (`constants.DEFAULT_AR_RULE_CATALOG`) onto the new definition and the
    entity's `gl_account_roles` (idempotent - safe even if this entity already
    has another AR definition). `AP`/`BANK` are reserved schema values with no
    rule catalog or engine yet."""
    # owner_user_id is None until real auth provides the caller's user id
    return await service.create_definition(
        entity_id=str(payload.entity_id), name=payload.name, recon_type=payload.recon_type,
        cadence=payload.cadence, owner_user_id=None,
    )


@router.get("/reconciliations", response_model=list[DefinitionOut], summary="List reconciliation definitions")
async def list_definitions(entity_id: UUID | None = None, service: ReconciliationService = Depends(get_service)):
    return await service.list_definitions(entity_id=str(entity_id) if entity_id else None)


# Registered before /reconciliations/{definition_id} deliberately - a static
# path must come first, or Starlette tries to parse "matchers" as that
# route's UUID definition_id and 422s before this handler ever runs.
@router.get(
    "/reconciliations/matchers",
    response_model=MatcherCatalogResponse,
    summary="List available matchers/sources/bank_fields for kind='field-match' rules",
)
async def list_matchers(service: ReconciliationService = Depends(get_service)):
    """Static reference data - not scoped to any definition. The frontend's
    source of truth for the MATCHER/source/field pickers when creating a
    `POST /reconciliations/{id}/rules` request with `kind="field-match"`."""
    return service.list_matcher_catalog()


@router.get("/reconciliations/{definition_id}", response_model=DefinitionOut, summary="Get a reconciliation definition")
async def get_definition(definition_id: UUID, service: ReconciliationService = Depends(get_service)):
    return await service.get_definition(str(definition_id))


# -- reconciliation_rules (Rules Studio) -----------------------------------------
@router.get(
    "/reconciliations/{definition_id}/rules",
    response_model=list[RuleOut],
    summary="List a definition's rules",
)
async def list_rules(definition_id: UUID, service: ReconciliationService = Depends(get_service)):
    """Ordered by `(phase, priority)` - the same order the engine evaluates
    them in (first-match-wins within a phase, once M1/M2 land)."""
    return await service.list_rules(str(definition_id))


@router.patch(
    "/reconciliations/{definition_id}/rules/{rule_id}",
    response_model=RuleOut,
    summary="Enable/disable or retune a rule",
)
async def update_rule(
    definition_id: UUID, rule_id: UUID, payload: RuleUpdate, service: ReconciliationService = Depends(get_service)
):
    """`config` is a full replacement, not a merge - submit the rule's
    complete config, not just the keys you're changing."""
    return await service.update_rule(str(definition_id), str(rule_id), enabled=payload.enabled, config=payload.config)


@router.post(
    "/reconciliations/{definition_id}/rules",
    response_model=RuleOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a new rule to a definition's catalog",
)
async def create_rule(definition_id: UUID, payload: RuleCreate, service: ReconciliationService = Depends(get_service)):
    """`kind="field-match"` composes a new CUSTOMER_LOCK/CANDIDATE_POOL rule
    from an existing matcher (`config.matcher`) and field pair
    (`config.bank_field`/`config.source`/`config.source_field`) - no code
    change needed. Every other `kind` must already be registered for
    `phase` (see `GET .../rules` for what's seeded); an unregistered kind
    404s here rather than silently being skipped at run time. Rules are
    never deleted, only disabled via `PATCH .../rules/{rule_id}`."""
    return await service.create_rule(
        str(definition_id), phase=payload.phase, kind=payload.kind, name=payload.name,
        priority=payload.priority, confidence=payload.confidence, config=payload.config,
    )


# -- reconciliation_runs --------------------------------------------------------
@router.post(
    "/reconciliations/{definition_id}/runs",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue a reconciliation run",
)
async def create_run(definition_id: UUID, payload: RunCreate, service: ReconciliationService = Depends(get_service)):
    """Creates a run with `status=QUEUED` and returns immediately - it does
    **not** execute the engine. Until the M1 `reconciliation_worker` exists,
    nothing claims a queued run yet. Poll `GET /runs/{run_id}` for status."""
    return await service.create_run(str(definition_id), period_start=payload.period_start, period_end=payload.period_end)


@router.get("/reconciliations/{definition_id}/runs", response_model=list[RunOut], summary="List a definition's runs")
async def list_runs(
    definition_id: UUID, status_filter: str | None = None, service: ReconciliationService = Depends(get_service)
):
    return await service.list_runs(definition_id=str(definition_id), status_=status_filter)


@router.get("/runs/{run_id}", response_model=RunOut, summary="Get a reconciliation run")
async def get_run(run_id: UUID, service: ReconciliationService = Depends(get_service)):
    """`matched_count`/`exception_count`/etc. stay `NULL` until the engine
    (M1+) actually computes a run - this only reflects queue/lifecycle state
    today."""
    return await service.get_run(str(run_id))


@router.post("/runs/{run_id}/retry", response_model=RunOut, summary="Retry a failed reconciliation run")
async def retry_run(run_id: UUID, service: ReconciliationService = Depends(get_service)):
    """Only valid when `status=FAILED` (409 otherwise) - mirrors
    `POST /ingestion-jobs/{job_id}/retry`."""
    return await service.retry_run(str(run_id))


# -- match_groups / reconciliation_exceptions (M3, run results) --------------------
@router.get("/runs/{run_id}/matches", response_model=list[MatchGroupOut], summary="List a run's match groups")
async def list_matches(run_id: UUID, service: ReconciliationService = Depends(get_service)):
    """Every match group Phase 2 committed for this run, each with its
    nested `allocations` - the invoices it settled money against."""
    return await service.list_matches(str(run_id))


@router.get("/runs/{run_id}/exceptions", response_model=list[ExceptionOut], summary="List a run's exceptions")
async def list_exceptions(run_id: UUID, status_filter: str | None = None, service: ReconciliationService = Depends(get_service)):
    """Optionally filter by `status_filter` (one of `EXCEPTION_STATUSES`,
    e.g. `OPEN`). Includes GL_VARIANCE exceptions raised by the M3 control
    proof, not just Phase 1/2 exceptions."""
    return await service.list_exceptions(str(run_id), status_=status_filter)


@router.patch("/exceptions/{exception_id}", response_model=ExceptionOut, summary="Resolve or annotate an exception")
async def update_exception(exception_id: UUID, payload: ExceptionUpdate, service: ReconciliationService = Depends(get_service)):
    """`resolved_at` is stamped automatically the moment `status` moves away
    from `OPEN`/`INVESTIGATING` - not settable directly. TODO(recon.exception.resolve):
    gate behind that permission once app/auth/ is real; `resolver_id` is
    `None` until then."""
    return await service.update_exception(
        str(exception_id), status_=payload.status, resolution_outcome=payload.resolution_outcome,
        resolution_notes=payload.resolution_notes,
    )
