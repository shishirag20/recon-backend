"""Pydantic request/response models for the reconciliation module.

Scoped to what M0 actually implements - definition/rule CRUD and run
enqueue/status. Endpoints that read engine output (matches, exceptions,
sign-off) land in M1-M4 alongside the code that produces that data; see
app/reconciliation/router.py's module docstring for the milestone map.
"""
from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field


# -- reconciliation_definitions ------------------------------------------------
class DefinitionCreate(BaseModel):
    entity_id: UUID
    name: str = Field(min_length=1, max_length=200, description="e.g. 'AR Reconciliation - Default Entity'.")
    recon_type: str = Field(default="AR", description="One of: AR, AP, BANK. Only AR has a seeded rule catalog today.")
    cadence: str | None = Field(default=None, description="Free text, e.g. 'MONTHLY' - not enforced.")


class DefinitionOut(BaseModel):
    definition_id: UUID
    entity_id: UUID
    name: str
    recon_type: str
    cadence: str | None
    owner_user_id: UUID | None

    model_config = {"from_attributes": True}


# -- reconciliation_rules ------------------------------------------------------
class RuleOut(BaseModel):
    rule_id: UUID
    definition_id: UUID
    phase: str
    kind: str
    name: str
    priority: int
    enabled: bool
    confidence: int | None
    config: dict

    model_config = {"from_attributes": True}


class RuleUpdate(BaseModel):
    enabled: bool | None = None
    config: dict | None = Field(default=None, description="Full replacement of the rule's config, not a merge.")


# -- reconciliation_runs --------------------------------------------------------
class RunCreate(BaseModel):
    period_start: date | None = None
    period_end: date | None = Field(
        default=None,
        description="Authoritative period cutoff used by the period-cutoff-guard rule - not derived from today() at compute time.",
    )


class RunOut(BaseModel):
    run_id: UUID
    definition_id: UUID
    run_no: str
    period_start: date | None
    period_end: date | None
    status: str = Field(description="DRAFT -> QUEUED -> RUNNING -> COMPUTED -> APPROVED -> CLOSED, or FAILED.")
    volume: int | None
    matched_count: int | None
    exception_count: int | None
    matched_value_minor: int | None
    exception_value_minor: int | None
    unapplied_minor: int | None
    prepared_by: UUID | None
    reviewed_by: UUID | None
    signed_at: datetime | None
    run_hash: str | None
    attempt_count: int
    max_attempts: int
    last_error: str | None
    started_at: datetime

    model_config = {"from_attributes": True}
