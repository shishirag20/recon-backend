"""Pydantic request/response models for the reconciliation module.

M0: definition/rule CRUD and run enqueue/status. M3 adds: reading engine
output (`MatchGroupOut`, `ExceptionOut`) and resolving an exception
(`ExceptionUpdate`). Sign-off lands in M4; see app/reconciliation/router.py's
module docstring for the milestone map.
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


# -- match_groups / invoice_allocations (M3, read-only) --------------------------
class AllocationOut(BaseModel):
    allocation_id: UUID
    invoice_id: UUID
    payment_id: UUID
    bank_txn_id: UUID | None
    allocated_minor: int


class MatchGroupOut(BaseModel):
    match_group_id: UUID
    run_id: UUID
    match_type: str = Field(description="EXACT | TOLERANCE | PARTIAL | SUBSET_SUM | MANY_TO_ONE | ONE_TO_MANY | MANUAL")
    rule_id: UUID | None
    confidence: int | None
    status: str = Field(description="AUTO_MATCHED | SUGGESTED | CONFIRMED | REJECTED")
    reason: str | None
    created_at: datetime
    allocations: list[AllocationOut] = Field(description="Every invoice this match group settled money against.")

    model_config = {"from_attributes": True}


# -- reconciliation_exceptions (M3) -----------------------------------------------
class ExceptionOut(BaseModel):
    exception_id: UUID
    run_id: UUID
    exception_no: str | None
    exception_type: str = Field(
        description="SHORT_PAY | OVERPAYMENT | UNAPPLIED_CASH | TIMING_DIFFERENCE | GL_VARIANCE | DUPLICATE | "
        "MULTIPLE_INVOICE_MATCH | DOUBLE_COLLISION | SUSPENSE | BANK_CHARGE | GATEWAY_VARIANCE | NO_PAYMENT"
    )
    bank_txn_id: UUID | None
    invoice_id: UUID | None
    customer_id: UUID | None
    discrepancy_minor: int | None
    reason_code: str | None
    status: str = Field(description="OPEN | INVESTIGATING | RESOLVED | AUTO_RESOLVED | DEFERRED | WRITTEN_OFF | ADJUSTED | CARRIED_FORWARD")
    resolution_outcome: str | None = Field(description="WRITEOFF | KEEPOPEN | DISPUTE | JOURNAL | ON_ACCOUNT")
    resolver_id: UUID | None
    resolution_notes: str | None
    resolved_at: datetime | None
    created_at: datetime
    detail: dict | None = Field(description="Candidate lists for MULTIPLE_INVOICE_MATCH/DOUBLE_COLLISION; variance breakdown for GL_VARIANCE.")
    match_group_id: UUID | None

    model_config = {"from_attributes": True}


class ExceptionUpdate(BaseModel):
    status: str | None = Field(default=None, description="Moving away from OPEN/INVESTIGATING stamps resolved_at automatically.")
    resolution_outcome: str | None = Field(default=None, description="WRITEOFF | KEEPOPEN | DISPUTE | JOURNAL | ON_ACCOUNT")
    resolution_notes: str | None = None
