"""Pydantic request/response models for the data-hub module.

ID fields are typed `UUID`, not `str` - asyncpg returns `uuid.UUID` objects
for uuid columns, and DAO results are passed straight through as dicts (see
dao.py), so response models must accept the native type rather than a string.
"""
from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

_TRANSFORM_DESCRIPTION = (
    "One of: NONE (passthrough), TRIM, UPPER, LOWER, "
    "CONST (ignore the raw value, always return transform_param), "
    "TO_MINOR_UNITS (parse a decimal and multiply into integer minor units - "
    "e.g. '1000.00' -> 100000 - pass transform_param='negate' to also flip the sign), "
    "NEGATE (numeric sign flip), "
    "PARSE_DATE (transform_param is a comma-separated list of strptime formats to try in order), "
    "REGEX (transform_param is a pattern with one capture group; the group's text is returned). "
    "See app/datahub/transforms.py for the reference implementation - this is the exact "
    "code path both /field-mappings/preview and the ingestion worker call, so a mapping "
    "that previews cleanly will ingest identically."
)


# -- data_sources ------------------------------------------------------------
class DataSourceCreate(BaseModel):
    entity_id: UUID = Field(description="The legal entity this feed belongs to.")
    name: str = Field(min_length=1, max_length=200, description="Human-readable label, e.g. 'HDFC CMRG-1240'.")
    kind: str = Field(description="One of: BANK_FEED, GATEWAY, ERP, MANUAL_UPLOAD.")


class DataSourceUpdate(BaseModel):
    name: str | None = None
    status: str | None = Field(default=None, description="One of: CONNECTED, PENDING, ERROR.")


class DataSourceOut(BaseModel):
    source_id: UUID
    entity_id: UUID
    name: str
    kind: str
    status: str

    model_config = {"from_attributes": True}


# -- field_mappings ------------------------------------------------------------
class FieldMappingIn(BaseModel):
    source_field: str = Field(min_length=1, description="Column name as it appears in the raw uploaded file.")
    canonical_field: str = Field(
        min_length=1,
        description=(
            "Target column on staging_records, e.g. txn_date, reference, counterparty, "
            "amount_minor, amount_home_minor, currency, dr_cr."
        ),
    )
    transform: str = Field(default="NONE", description=_TRANSFORM_DESCRIPTION)
    transform_param: str | None = Field(default=None, description="Meaning depends on `transform` - see its description.")


class FieldMappingOut(FieldMappingIn):
    mapping_id: UUID
    source_id: UUID
    version: int = Field(description="Mapping sets are versioned; only one version per source is is_active at a time.")
    is_active: bool

    model_config = {"from_attributes": True}


class FieldMappingVersionCreate(BaseModel):
    mappings: list[FieldMappingIn] = Field(
        min_length=1,
        description="The complete mapping set for this version - not a diff against the previous version.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "mappings": [
                        {
                            "source_field": "transaction_date",
                            "canonical_field": "txn_date",
                            "transform": "PARSE_DATE",
                            "transform_param": "%Y-%m-%d",
                        },
                        {
                            "source_field": "bank_reference_number",
                            "canonical_field": "reference",
                            "transform": "TRIM",
                            "transform_param": None,
                        },
                        {
                            "source_field": "payer_name",
                            "canonical_field": "counterparty",
                            "transform": "TRIM",
                            "transform_param": None,
                        },
                        {
                            "source_field": "amount",
                            "canonical_field": "amount_minor",
                            "transform": "TO_MINOR_UNITS",
                            "transform_param": None,
                        },
                        {
                            "source_field": "currency",
                            "canonical_field": "currency",
                            "transform": "CONST",
                            "transform_param": "INR",
                        },
                    ]
                }
            ]
        }
    }


class MappingPreviewRequest(BaseModel):
    sample_rows: list[dict] = Field(
        min_length=1, max_length=50, description="Raw rows to test, e.g. [{'amount': '1000.00', ...}, ...]."
    )
    mappings: list[FieldMappingIn] | None = Field(
        default=None,
        description="Test a draft mapping without saving it. Omit to preview against the source's currently active mapping instead.",
    )


class MappingPreviewRow(BaseModel):
    raw: dict
    canonical: dict = Field(description="Result of applying each mapping's transform to `raw`.")
    issues: list[str] = Field(description="One entry per field whose transform failed, formatted 'source -> canonical: error'.")


class MappingPreviewResponse(BaseModel):
    rows: list[MappingPreviewRow]


# -- ingestion_jobs ------------------------------------------------------------
class IngestionJobOut(BaseModel):
    job_id: UUID
    source_id: UUID | None
    job_type: str = Field(description="INGEST (parse a file into staging_records) or PROMOTE (write reviewed staging_records into canonical tables).")
    parent_job_id: UUID | None = Field(description="For a PROMOTE job, the INGEST job it's promoting.")
    stream: str | None = Field(description="BANK, LEDGER, INVOICE, GATEWAY, or CUSTOMER - only set on INGEST jobs.")
    file_name: str | None
    format: str | None
    status: str = Field(description="PENDING -> RUNNING -> SUCCESS/PARTIAL, or PENDING (retry) / FAILED on error.")
    row_count: int
    error_count: int
    attempt_count: int
    max_attempts: int
    last_error: str | None = Field(description="Set when status is FAILED or a retry is pending.")
    mapping_version: int | None = Field(description="Which field_mappings version was active when this job ran (audit trail).")
    started_at: datetime

    model_config = {"from_attributes": True}


# -- staging_records ------------------------------------------------------------
class StagingRecordOut(BaseModel):
    staging_id: UUID
    job_id: UUID
    stream: str
    txn_date: date | None
    reference: str | None
    counterparty: str | None
    amount_minor: int | None
    amount_home_minor: int | None
    currency: str | None
    dr_cr: str | None
    raw: dict = Field(description="The original source row, verbatim, including any column not covered by a mapping.")
    valid: bool = Field(description="False if any field's transform failed - see `issues`.")
    issues: list[str] | None

    model_config = {"from_attributes": True}


class StagingRecordUpdate(BaseModel):
    txn_date: date | None = None
    reference: str | None = None
    counterparty: str | None = None
    amount_minor: int | None = None
    amount_home_minor: int | None = None
    currency: str | None = None
    dr_cr: str | None = None
    valid: bool | None = Field(default=None, description="Manually mark a corrected row valid so it's eligible for promotion.")
