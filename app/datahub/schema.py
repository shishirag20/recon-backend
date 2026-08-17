"""Pydantic request/response models for the data-hub module.

ID fields are typed `UUID`, not `str` - asyncpg returns `uuid.UUID` objects
for uuid columns, and DAO results are passed straight through as dicts (see
dao.py), so response models must accept the native type rather than a string.

Canonical records (bank_statements/invoices/customers rows returned by the
Data Explorer endpoints) are intentionally *not* modeled here - the three
tables have genuinely different shapes, and forcing them through one Pydantic
model would either lose fields or paper over real differences. Those
endpoints return plain dicts; see router.py.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

_TRANSFORM_DESCRIPTION = (
    "One of: NONE (passthrough), TRIM, UPPER, LOWER, "
    "CONST (ignore the raw value, always return transform_param), "
    "TO_MINOR_UNITS (parse a decimal and multiply into integer minor units - "
    "e.g. '1000.00' -> 100000 - pass transform_param='negate' to also flip the sign), "
    "NEGATE (numeric sign flip), "
    "PARSE_DATE (transform_param is a comma-separated list of strptime formats to try in order), "
    "REGEX (transform_param is a pattern with one capture group; the group's text is returned), "
    "PARSE_BOOL (true/false/1/0/yes/no, case-insensitive -> a real bool; use for a boolean-typed "
    "canonical column like is_bank_charge, since TRIM leaves it a string and asyncpg rejects that), "
    "TO_DECIMAL (parse a decimal at face value, no scaling - use for a numeric-typed canonical "
    "column that isn't money, like invoices.tds_rate_pct, since TRIM leaves it a string too). "
    "See app/datahub/transforms.py for the reference implementation - this is the exact "
    "code path both /field-mappings/preview and the ingestion worker call, so a mapping "
    "that previews cleanly will ingest identically."
)


# -- data_sources ------------------------------------------------------------
class DataSourceCreate(BaseModel):
    entity_id: UUID = Field(description="The legal entity this feed belongs to.")
    name: str = Field(min_length=1, max_length=200, description="Human-readable label, e.g. 'HDFC CMRG-1240'.")
    kind: str = Field(description="One of: BANK_FEED, GATEWAY, ERP, MANUAL_UPLOAD.")
    stream: str = Field(
        description=(
            "One of: BANK, INVOICE, CUSTOMER (LEDGER/GATEWAY accepted but not yet implemented). "
            "Fixed for the lifetime of this source - every upload against it writes to this stream's "
            "canonical table, so this is set once here rather than re-specified per upload."
        )
    )


class DataSourceUpdate(BaseModel):
    name: str | None = None
    status: str | None = Field(default=None, description="One of: CONNECTED, PENDING, ERROR.")


class DataSourceOut(BaseModel):
    source_id: UUID
    entity_id: UUID
    name: str
    kind: str
    stream: str
    status: str

    model_config = {"from_attributes": True}


# -- field_mappings ------------------------------------------------------------
class FieldMappingIn(BaseModel):
    source_field: str = Field(min_length=1, description="Column name as it appears in the raw uploaded file.")
    canonical_field: str = Field(
        default="",
        min_length=0,
        description=(
            "Target column on the stream's canonical table. BANK: transaction_date, currency, "
            "amount_minor, amount_home_minor, dr_cr, bank_reference, narration, payer_name, ... "
            "INVOICE: customer_code (resolved by lookup, not stored directly), invoice_number, "
            "issue_date, due_date, currency, total_amount_minor, ... "
            "CUSTOMER: customer_code, company_name, pan, gstin, ... "
            "See app/datahub/canonical.py's KNOWN_FIELDS for the authoritative per-stream list - "
            "anything else is flagged as an issue on the row and ignored."
        ),
    )
    transform: str = Field(default="NONE", description=_TRANSFORM_DESCRIPTION)
    transform_param: str | None = Field(default=None, description="Meaning depends on `transform` - see its description.")


class FieldMappingOut(FieldMappingIn):
    canonical_field: str = Field(default="", min_length=0)
    mapping_id: UUID
    stream: str = Field(description="BANK, INVOICE, CUSTOMER, ... - mappings are shared globally per stream, not per data source.")
    version: int = Field(description="Mapping sets are versioned; only one version per stream is is_active at a time.")
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
                            "canonical_field": "transaction_date",
                            "transform": "PARSE_DATE",
                            "transform_param": "%Y-%m-%d",
                        },
                        {
                            "source_field": "bank_reference_number",
                            "canonical_field": "bank_reference",
                            "transform": "TRIM",
                            "transform_param": None,
                        },
                        {
                            "source_field": "payer_name",
                            "canonical_field": "payer_name",
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
                        {
                            "source_field": "amount",
                            "canonical_field": "dr_cr",
                            "transform": "CONST",
                            "transform_param": "CREDIT",
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


class ResolveHeadersRequest(BaseModel):
    columns: list[str] = Field(
        min_length=1,
        description="Raw column headers from an actual file - typically read client-side before upload.",
    )


class ResolvedHeader(BaseModel):
    source_field: str
    matched: bool = Field(
        description="Whether this header matches an existing synonym (case/whitespace-insensitively) "
        "in the stream's active mapping."
    )


class ResolveHeadersResponse(BaseModel):
    results: list[ResolvedHeader]


class CanonicalFieldsResponse(BaseModel):
    canonical_fields: list[str] = Field(
        description="Valid mapping targets for this stream - see app/datahub/canonical.py's KNOWN_FIELDS."
    )


# -- ingestion_jobs ------------------------------------------------------------
class IngestionJobOut(BaseModel):
    job_id: UUID
    source_id: UUID | None
    stream: str | None = Field(description="BANK, INVOICE, or CUSTOMER - determines which canonical table this job writes to.")
    file_name: str | None
    format: str | None
    status: str = Field(description="PENDING -> RUNNING -> SUCCESS/PARTIAL, or PENDING (retry) / FAILED on error.")
    row_count: int
    error_count: int = Field(description="Rows with issues but still inserted, plus rows in failed_rows that couldn't be inserted at all.")
    attempt_count: int
    max_attempts: int
    last_error: str | None = Field(description="Set when status is FAILED or a retry is pending.")
    mapping_version: int | None = Field(description="Which field_mappings version was active when this job ran (audit trail).")
    unmapped_columns: list[str] | None = Field(
        default=None,
        description="Raw file headers that matched no synonym in the active stream mapping and that the "
        "AI-suggestion stub couldn't resolve either - nothing auto-resolves these yet (see app/datahub/ai_mapping.py).",
    )
    failed_rows: list[dict] | None = Field(
        description="Rows that couldn't satisfy the canonical table's required columns at all - "
        "each is {raw, issues}. These were never inserted anywhere; fixing one means correcting "
        "the source file and re-uploading, not an in-app edit."
    )
    started_at: datetime

    model_config = {"from_attributes": True}
