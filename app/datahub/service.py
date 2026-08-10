"""Business logic for the data-hub module.

Auth/permission checks (datahub:configure, ingestion:upload, ingestion:view,
ingestion:manage - see the RBAC design) are not wired in yet since the auth
module itself is still stubbed; the router notes exactly where each
`Depends(require_permission(...))` belongs once it exists. Likewise the
entity-ownership check here only confirms the entity exists, not that the
caller's organization owns it - that also depends on real auth.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.datahub import transforms
from app.datahub.canonical import STREAM_TABLES, SEARCH_COLUMNS
from app.datahub.constants import (
    DataHubErrors,
    MAX_UPLOAD_BYTES,
    STREAM_VALUES,
    SUPPORTED_UPLOAD_FORMATS,
    UPLOAD_CHUNK_BYTES,
)
from app.datahub.dao import DataHubDAO, new_id

UPLOAD_ROOT = "/data/uploads"


class DataHubService:
    def __init__(self, dao: DataHubDAO) -> None:
        self.dao = dao

    # -- data_sources --------------------------------------------------------
    async def create_data_source(self, *, entity_id: str, name: str, kind: str):
        if not await self.dao.entity_exists(entity_id):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, DataHubErrors.ENTITY_NOT_FOUND
            )
        return await self.dao.insert_data_source(
            entity_id=entity_id, name=name, kind=kind
        )

    async def get_data_source(self, source_id: str):
        row = await self.dao.get_data_source(source_id)
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, DataHubErrors.DATA_SOURCE_NOT_FOUND
            )
        return row

    async def list_data_sources(self, *, entity_id: str | None, kind: str | None):
        return await self.dao.list_data_sources(entity_id=entity_id, kind=kind)

    async def update_data_source(
        self, source_id: str, *, name: str | None, status_: str | None
    ):
        row = await self.dao.update_data_source(source_id, name=name, status=status_)
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, DataHubErrors.DATA_SOURCE_NOT_FOUND
            )
        return row

    # -- field_mappings --------------------------------------------------------
    async def get_active_mappings(self, source_id: str):
        await self.get_data_source(source_id)  # 404s if missing
        return await self.dao.get_active_mappings(source_id)

    async def create_mapping_version(self, source_id: str, mappings: list[dict]):
        await self.get_data_source(source_id)
        return await self.dao.insert_mapping_version(source_id, mappings)

    async def preview_mapping(
        self,
        source_id: str,
        sample_rows: list[dict],
        mappings_override: list[dict] | None,
    ):
        await self.get_data_source(source_id)
        mappings = (
            mappings_override
            if mappings_override is not None
            else await self.dao.get_active_mappings(source_id)
        )
        if not mappings:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, DataHubErrors.NO_ACTIVE_MAPPING
            )
        results = []
        for raw_row in sample_rows:
            canonical, issues = transforms.apply_mapping(raw_row, mappings)
            results.append({"raw": raw_row, "canonical": canonical, "issues": issues})
        return results

    # -- ingestion_jobs --------------------------------------------------------
    async def create_upload_job(
        self,
        *,
        source_id: str,
        stream: str,
        fmt: str,
        file: UploadFile,
        started_by: str | None,
    ):
        if stream not in STREAM_VALUES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, DataHubErrors.INVALID_STREAM
            )
        if fmt not in SUPPORTED_UPLOAD_FORMATS:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, DataHubErrors.UNSUPPORTED_FORMAT
            )
        await self.get_data_source(source_id)  # 404s if missing

        job_id = new_id()
        safe_filename = os.path.basename(file.filename or "upload")
        dest_dir = os.path.join(UPLOAD_ROOT, job_id)
        dest_path = os.path.join(dest_dir, safe_filename)

        await self._save_upload(file, dest_dir, dest_path)

        return await self.dao.insert_ingest_job(
            job_id=job_id,
            source_id=source_id,
            stream=stream,
            file_name=safe_filename,
            file_uri=dest_path,
            fmt=fmt,
            started_by=started_by,
        )

    async def _save_upload(
        self, file: UploadFile, dest_dir: str, dest_path: str
    ) -> None:
        await run_in_threadpool(os.makedirs, dest_dir, exist_ok=True)
        handle = await run_in_threadpool(open, dest_path, "wb")
        total = 0
        try:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        DataHubErrors.FILE_TOO_LARGE,
                    )
                await run_in_threadpool(handle.write, chunk)
        except HTTPException:
            await run_in_threadpool(handle.close)
            await run_in_threadpool(_remove_quietly, dest_path)
            raise
        else:
            await run_in_threadpool(handle.close)

    async def get_job(self, job_id: str):
        row = await self.dao.get_job(job_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, DataHubErrors.JOB_NOT_FOUND)
        return row

    async def list_jobs(
        self, *, source_id: str | None, status_: str | None, limit: int, offset: int
    ):
        return await self.dao.list_jobs(
            source_id=source_id, status=status_, limit=limit, offset=offset
        )

    async def retry_job(self, job_id: str):
        await self.get_job(job_id)  # 404s if missing
        row = await self.dao.retry_job(job_id)
        if row is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, DataHubErrors.JOB_NOT_RETRYABLE
            )
        return row

    # -- canonical records (Data Explorer) --------------------------------------------------------
    async def list_records(
        self,
        job_id: str,
        *,
        valid: bool | None,
        search: str | None,
        limit: int,
        offset: int,
    ):
        job = await self.get_job(job_id)
        table, pk_column = self._stream_table(job["stream"])
        return await self.dao.list_records(
            table=table,
            pk_column=pk_column,
            search_column=SEARCH_COLUMNS.get(job["stream"]),
            job_id=job_id,
            valid=valid,
            search=search,
            limit=limit,
            offset=offset,
        )

    async def get_record(self, job_id: str, record_id: str):
        job = await self.get_job(job_id)
        table, pk_column = self._stream_table(job["stream"])
        row = await self.dao.get_record(
            table=table, pk_column=pk_column, record_id=record_id
        )
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, DataHubErrors.RECORD_NOT_FOUND
            )
        return row

    async def update_record(self, job_id: str, record_id: str, fields: dict):
        job = await self.get_job(job_id)
        table, pk_column = self._stream_table(job["stream"])
        fields = {k: v for k, v in fields.items() if v is not None}
        row = await self.dao.update_record(
            table=table, pk_column=pk_column, record_id=record_id, fields=fields
        )
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, DataHubErrors.RECORD_NOT_FOUND
            )
        return row

    @staticmethod
    def _stream_table(stream: str) -> tuple[str, str]:
        table = STREAM_TABLES.get(stream)
        if table is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                DataHubErrors.UNSUPPORTED_STREAM_FOR_EXPLORER,
            )
        return table


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
