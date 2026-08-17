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


class MatcherInfo(BaseModel):
    kind: str
    label: str
    description: str
    config_keys: list[str] = Field(description="Optional config keys this matcher reads, beyond bank_field/source/source_field.")


class SourceInfo(BaseModel):
    source: str
    fields: list[str] = Field(description="Valid config.source_field values for this source - the only columns find_matches can actually read.")


class MatcherCatalogResponse(BaseModel):
    matchers: list[MatcherInfo]
    sources: list[SourceInfo]
    bank_fields: list[str] = Field(description="Valid config.bank_field values - direct bank_statements columns, plus extract:vpa/gstin/pan sentinels.")


class AlgorithmInfo(BaseModel):
    name: str = Field(description="Technical/callable name - a MATCHER_REGISTRY key for category='matcher', a function name in generic_functions.py for category='generic_function'.")
    category: str = Field(description="'matcher' (usable today via kind='field-match') or 'generic_function' (standalone, not wired into any rule yet).")
    label: str
    description: str
    action_verb: str | None = Field(default=None, description="UI action verb, only set for generic_function entries.")
    wired: bool = Field(description="True if this algorithm is actually reachable from a real reconciliation run today.")


class AlgorithmCatalogResponse(BaseModel):
    algorithms: list[AlgorithmInfo]


class RuleCreate(BaseModel):
    phase: str = Field(description="One of RECON_PHASES, e.g. 'CUSTOMER_LOCK'.")
    kind: str = Field(description="Must already be registered for `phase` - GET .../rules to see what's seeded. 'field-match' composes a new identification/pooling rule from an existing matcher, no code change needed - see the config fields below.")
    name: str = Field(min_length=1, max_length=200)
    priority: int = Field(description="Evaluation order within phase, ascending. Must be unused for this (definition_id, phase) - a collision 409s.")
    confidence: int | None = Field(default=None, ge=0, le=100)
    config: dict = Field(
        default_factory=dict,
        description="For kind='field-match': {matcher, bank_field, source, source_field, ...}. "
        "matcher: one of 'exact'|'substring'|'numeric_suffix'|'token_overlap'|'trigram_similarity'. "
        "bank_field: a bank_statements column name, or 'extract:vpa'|'extract:gstin'|'extract:pan' to "
        "regex-extract it from narration first. source: one of 'customers'|'customer_bank_accounts'|"
        "'customer_reference_codes'|'expected_remittances'. source_field: the column on `source` to compare "
        "against. Every other kind's config shape matches its existing seeded rows - see docs/reconciliation.md §6.",
    )


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
    invoice_number: str | None = Field(description="The real, human-readable invoice number (invoices.invoice_number) - invoice_id is the internal row UUID.")
    invoice_amount_minor: int | None = Field(description="The invoice's own total (invoices.total_amount_minor) - what was owed, not necessarily what this allocation actually applied.")
    payment_id: UUID
    payment_amount_minor: int | None = Field(description="The payment's own total received (payments.total_received_minor) - what the bank transaction actually brought in, which may exceed or fall short of allocated_minor (overpayment/short-pay/fee cases).")
    bank_txn_id: UUID | None
    bank_reference: str | None = Field(description="The real bank reference/UTR from the source file (bank_statements.bank_reference) - bank_txn_id is the internal generated row UUID.")
    allocated_minor: int = Field(description="How much of this payment was actually applied to this invoice - may differ from both invoice_amount_minor and payment_amount_minor.")


class MatchGroupOut(BaseModel):
    match_group_id: UUID
    run_id: UUID
    match_type: str = Field(description="EXACT | TOLERANCE | PARTIAL | SUBSET_SUM | MANY_TO_ONE | ONE_TO_MANY | MANUAL")
    rule_id: UUID | None = Field(description="The ALLOCATION-phase rule that committed this match group.")
    locked_by_rule_id: UUID | None = Field(description="The CUSTOMER_LOCK-phase rule that identified the payment's customer.")
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
    exception_no: str | None = None
    exception_type: str = Field(
        description="SHORT_PAY | OVERPAYMENT | UNAPPLIED_CASH | TIMING_DIFFERENCE | GL_VARIANCE | DUPLICATE | "
        "MULTIPLE_INVOICE_MATCH | DOUBLE_COLLISION | SUSPENSE | BANK_CHARGE | GATEWAY_VARIANCE | NO_PAYMENT"
    )
    bank_txn_id: UUID | None = None
    invoice_id: UUID | None = None
    customer_id: UUID | None = None
    customer_name: str | None = Field(default=None, description="Human-readable customer/remitter name.")
    customer_code: str | None = None
    invoice_number: str | None = None
    bank_reference: str | None = None
    discrepancy_minor: int | None = None
    amount_minor: int | None = None
    reason_code: str | None = None
    status: str = Field(description="OPEN | INVESTIGATING | RESOLVED | AUTO_RESOLVED | DEFERRED | WRITTEN_OFF | ADJUSTED | CARRIED_FORWARD")
    resolution_outcome: str | None = Field(default=None, description="WRITEOFF | KEEPOPEN | DISPUTE | JOURNAL | ON_ACCOUNT")
    resolver_id: UUID | None = None
    resolution_notes: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime
    detail: dict | None = Field(default=None, description="Candidate lists for MULTIPLE_INVOICE_MATCH/DOUBLE_COLLISION; variance breakdown for GL_VARIANCE.")
    match_group_id: UUID | None = None

    model_config = {"from_attributes": True}


class ExceptionUpdate(BaseModel):
    status: str | None = Field(default=None, description="Moving away from OPEN/INVESTIGATING stamps resolved_at automatically.")
    resolution_outcome: str | None = Field(default=None, description="WRITEOFF | KEEPOPEN | DISPUTE | JOURNAL | ON_ACCOUNT | MANUAL_MATCH")
    resolution_notes: str | None = None


class PaymentOut(BaseModel):
    payment_id: UUID
    bank_txn_id: UUID
    bank_reference: str | None = None
    customer_id: UUID | None = Field(default=None, description="None means this payment never locked/resolved to a customer.")
    customer_name: str | None = None
    total_received_minor: int
    unapplied_minor: int = Field(description="Cash from this payment not yet applied to any invoice - what's actually available to manually match.")
    created_at: datetime

    model_config = {"from_attributes": True}


class ResolveNoPaymentRequest(BaseModel):
    payment_ids: list[UUID] = Field(min_length=1, description="Open/unapplied payments to apply against this exception's invoice, in the order to fill it.")
    note: str | None = Field(default=None, description="Optional reviewer note - stored as the match's reason and the exception's resolution_notes.")
