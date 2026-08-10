"""HTTP layer for the data-hub module - endpoints only.

Permission gating is TODO pending the real auth module (currently stubbed -
see app/auth/router.py). Where it belongs, once `require_permission` exists:
  - datahub:configure -> data source / field mapping writes
  - ingestion:upload   -> POST /ingestion-jobs
  - ingestion:view     -> every GET below
  - ingestion:manage   -> retry, PATCH records
"""
from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from recon.app.datahub.constants import ROUTER_TAGS
from recon.app.datahub.dao import DataHubDAO
from recon.app.datahub.schema import (
    DataSourceCreate,
    DataSourceOut,
    DataSourceUpdate,
    FieldMappingOut,
    FieldMappingVersionCreate,
    IngestionJobOut,
    MappingPreviewRequest,
    MappingPreviewResponse,
)
from recon.app.datahub.service import DataHubService
from recon.app.db.pool import get_connection

router = APIRouter(tags=ROUTER_TAGS)


def get_service(conn: asyncpg.Connection = Depends(get_connection)) -> DataHubService:
    return DataHubService(DataHubDAO(conn))


# -- data_sources --------------------------------------------------------
@router.post(
    "/data-sources",
    response_model=DataSourceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a data source",
)
async def create_data_source(payload: DataSourceCreate, service: DataHubService = Depends(get_service)):
    """Registers a feed (bank connection, ERP export, gateway, or manual-upload
    bucket) for an entity. A data source must exist - and have an active field
    mapping (see `POST /data-sources/{source_id}/field-mappings/versions`) -
    before any file can be uploaded against it."""
    return await service.create_data_source(entity_id=payload.entity_id, name=payload.name, kind=payload.kind)


@router.get("/data-sources", response_model=list[DataSourceOut], summary="List data sources")
async def list_data_sources(
    entity_id: UUID | None = None, kind: str | None = None, service: DataHubService = Depends(get_service)
):
    return await service.list_data_sources(entity_id=entity_id, kind=kind)


@router.get("/data-sources/{source_id}", response_model=DataSourceOut, summary="Get a data source")
async def get_data_source(source_id: UUID, service: DataHubService = Depends(get_service)):
    return await service.get_data_source(source_id)


@router.patch("/data-sources/{source_id}", response_model=DataSourceOut, summary="Update a data source")
async def update_data_source(source_id: UUID, payload: DataSourceUpdate, service: DataHubService = Depends(get_service)):
    return await service.update_data_source(source_id, name=payload.name, status_=payload.status)


# -- field_mappings --------------------------------------------------------
@router.get(
    "/data-sources/{source_id}/field-mappings",
    response_model=list[FieldMappingOut],
    summary="Get the active field mapping",
)
async def get_active_field_mappings(source_id: UUID, service: DataHubService = Depends(get_service)):
    """Returns the currently-active (`is_active=true`) mapping set - i.e. the
    one the ingestion worker will actually use on the next upload against this
    source. Past versions aren't retrievable through this endpoint."""
    return await service.get_active_mappings(source_id)


@router.post(
    "/data-sources/{source_id}/field-mappings/versions",
    response_model=list[FieldMappingOut],
    status_code=status.HTTP_201_CREATED,
    summary="Save a new field-mapping version",
)
async def create_field_mapping_version(
    source_id: UUID, payload: FieldMappingVersionCreate, service: DataHubService = Depends(get_service)
):
    """Replaces the active mapping with a new version, atomically (the
    previous version's rows are deactivated, not deleted). Submit the full
    mapping set every time - this is not a partial update against the
    previous version.

    See the request schema's example for a realistic bank-statement mapping:
    it parses a date, trims two text fields, converts a decimal amount into
    integer minor units, and defaults a currency the source file doesn't
    provide."""
    return await service.create_mapping_version(source_id, [m.model_dump() for m in payload.mappings])


@router.post(
    "/data-sources/{source_id}/field-mappings/preview",
    response_model=MappingPreviewResponse,
    summary="Dry-run a mapping against sample rows",
)
async def preview_field_mapping(
    source_id: UUID, payload: MappingPreviewRequest, service: DataHubService = Depends(get_service)
):
    """Runs sample rows through the mapping's transforms without writing
    anything to the database. Pass `mappings` to test an unsaved draft, or
    omit it to preview the source's currently active mapping. This calls the
    exact same transform code the ingestion worker uses, so whatever this
    endpoint returns is exactly what a real upload would produce."""
    mappings_override = [m.model_dump() for m in payload.mappings] if payload.mappings is not None else None
    rows = await service.preview_mapping(source_id, payload.sample_rows, mappings_override)
    return {"rows": rows}


# -- ingestion_jobs --------------------------------------------------------
@router.post(
    "/ingestion-jobs",
    response_model=IngestionJobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a file for ingestion",
)
async def upload_ingestion_job(
    source_id: UUID = Form(..., description="Must already exist and have an active field mapping."),
    stream: str = Form(..., description="One of: BANK, INVOICE, CUSTOMER (LEDGER/GATEWAY accepted but not yet implemented)."),
    format: str = Form(..., description="Only CSV is parsed today; other values are accepted but the job will fail."),
    file: UploadFile = File(...),
    service: DataHubService = Depends(get_service),
):
    """Saves the file and creates a job with `status=PENDING`, then returns
    immediately - it does **not** wait for parsing. A background worker polls
    for pending jobs (typically picks one up within a few seconds), maps each
    row, and writes it directly into the stream's canonical table
    (bank_statements / invoices / customers) - there's no intermediate
    staging step or separate promote action.

    Poll `GET /ingestion-jobs/{job_id}` until `status` leaves `PENDING`/`RUNNING`
    to see the outcome. Rows that couldn't satisfy a required column end up in
    that response's `failed_rows`, not in any table."""
    # started_by is None until real auth provides the caller's user id
    return await service.create_upload_job(source_id=source_id, stream=stream, fmt=format, file=file, started_by=None)


@router.get("/ingestion-jobs", response_model=list[IngestionJobOut], summary="List ingestion jobs")
async def list_ingestion_jobs(
    source_id: UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    service: DataHubService = Depends(get_service),
):
    return await service.list_jobs(source_id=source_id, status_=status, limit=limit, offset=offset)


@router.get("/ingestion-jobs/{job_id}", response_model=IngestionJobOut, summary="Get an ingestion job")
async def get_ingestion_job(job_id: UUID, service: DataHubService = Depends(get_service)):
    return await service.get_job(job_id)


@router.post(
    "/ingestion-jobs/{job_id}/retry",
    response_model=IngestionJobOut,
    summary="Retry a failed ingestion job",
)
async def retry_ingestion_job(job_id: UUID, service: DataHubService = Depends(get_service)):
    """Only valid when `status=FAILED` (409 otherwise) - resets attempt_count
    and re-queues the job as PENDING for the worker to pick up again."""
    return await service.retry_job(job_id)


# -- Data Explorer: canonical records produced by a job --------------------------------------------------------
@router.get(
    "/ingestion-jobs/{job_id}/records",
    summary="List the canonical rows a job produced",
)
async def list_records(
    job_id: UUID,
    valid: bool | None = None,
    search: str | None = Query(default=None, description="Matches the stream's natural-key-ish column (bank_reference / invoice_number / company_name)."),
    limit: int = 50,
    offset: int = 0,
    service: DataHubService = Depends(get_service),
):
    """Reads directly from whichever canonical table this job's `stream`
    writes to (bank_statements / invoices / customers) - the response shape
    therefore differs by stream, which is why this isn't a typed Pydantic
    model. `valid=false` finds rows that were inserted but flagged with
    issues; rows that couldn't be inserted at all are in the job's
    `failed_rows`, not here."""
    return await service.list_records(job_id, valid=valid, search=search, limit=limit, offset=offset)


@router.get("/ingestion-jobs/{job_id}/records/{record_id}", summary="Get one canonical row")
async def get_record(job_id: UUID, record_id: UUID, service: DataHubService = Depends(get_service)):
    return await service.get_record(job_id, record_id)


@router.patch("/ingestion-jobs/{job_id}/records/{record_id}", summary="Correct a canonical row")
async def update_record(job_id: UUID, record_id: UUID, fields: dict, service: DataHubService = Depends(get_service)):
    """For fixing a row that was inserted with `valid=false` - pass any
    subset of the stream's columns (e.g. `{"amount_minor": 100000, "valid": true}`).
    Not a typed body since the editable columns differ by stream."""
    return await service.update_record(job_id, record_id, fields)
