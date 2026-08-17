"""Business logic for the data-hub module.

Auth/permission checks (datahub:configure, ingestion:upload, ingestion:view,
ingestion:manage - see the RBAC design) are not wired in yet since the auth
module itself is still stubbed; the router notes exactly where each
`Depends(require_permission(...))` belongs once it exists. Likewise the
entity-ownership check here only confirms the entity exists, not that the
caller's organization owns it - that also depends on real auth.
"""

from __future__ import annotations

import hashlib
import os

from fastapi import HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.datahub import transforms
from app.datahub.canonical import (
    EDITABLE_FIELDS,
    KNOWN_FIELDS,
    STREAM_TABLES,
    SEARCH_COLUMNS,
)
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
    async def create_data_source(
        self, *, entity_id: str, name: str, kind: str, stream: str
    ):
        if stream not in STREAM_VALUES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, DataHubErrors.INVALID_STREAM
            )
        if not await self.dao.entity_exists(entity_id):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, DataHubErrors.ENTITY_NOT_FOUND
            )
        return await self.dao.insert_data_source(
            entity_id=entity_id, name=name, kind=kind, stream=stream
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
    @staticmethod
    def _require_valid_stream(stream: str) -> None:
        if stream not in STREAM_VALUES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, DataHubErrors.INVALID_STREAM
            )

    async def get_active_mappings(self, stream: str) -> list[dict]:
        self._require_valid_stream(stream)
        return await self.dao.get_active_mappings(stream)

    async def create_mapping_version(self, stream: str, mappings: list[dict]):
        self._require_valid_stream(stream)

        # 1. Clean, filter and deduplicate incoming mappings
        incoming_by_source: dict[tuple, dict] = {}
        for m in mappings:
            source = str(m.get("source_field", "")).strip()
            canonical = (
                str(m.get("canonical_field", "")).strip()
                if m.get("canonical_field")
                else ""
            )
            if not source or not canonical or canonical == "-":
                continue
            transform = str(m.get("transform", "NONE")).strip().upper() or "NONE"
            transform_param = m.get("transform_param")
            if isinstance(transform_param, str):
                transform_param = transform_param.strip() or None

            norm_src = transforms.normalize_header(source)
            mapping_dict = {
                "source_field": source,
                "canonical_field": canonical,
                "transform": transform,
                "transform_param": transform_param,
            }

            if transform == "CONST":
                dedup_key = (norm_src, canonical.lower(), transform)
            else:
                dedup_key = (norm_src,)

            incoming_by_source[dedup_key] = mapping_dict

        # 2. Merge with existing active mappings to preserve global synonym dictionary
        active_mappings = await self.get_active_mappings(stream)
        merged_mappings: list[dict] = list(incoming_by_source.values())

        for m in active_mappings:
            src = str(m.get("source_field", "")).strip()
            canon = str(m.get("canonical_field", "")).strip()
            tr = str(m.get("transform", "NONE")).strip().upper() or "NONE"
            param = m.get("transform_param")
            if isinstance(param, str):
                param = param.strip() or None

            norm_s = transforms.normalize_header(src)
            if tr == "CONST":
                key = (norm_s, canon.lower(), tr)
            else:
                key = (norm_s,)

            if key not in incoming_by_source:
                merged_mappings.append(
                    {
                        "source_field": src,
                        "canonical_field": canon,
                        "transform": tr,
                        "transform_param": param,
                    }
                )

        # 3. Idempotency check: compare merged mappings against active mappings
        active_keys = {
            (
                transforms.normalize_header(m["source_field"]),
                str(m.get("canonical_field", "")).strip().lower(),
                str(m.get("transform", "NONE")).strip().upper(),
                (
                    str(m.get("transform_param", "")).strip().lower()
                    if m.get("transform_param") is not None
                    else None
                ),
            )
            for m in active_mappings
            if m.get("source_field")
            and m.get("canonical_field")
            and str(m.get("canonical_field")).strip() != "-"
        }
        merged_keys = {
            (
                transforms.normalize_header(m["source_field"]),
                str(m.get("canonical_field", "")).strip().lower(),
                str(m.get("transform", "NONE")).strip().upper(),
                (
                    str(m.get("transform_param", "")).strip().lower()
                    if m.get("transform_param") is not None
                    else None
                ),
            )
            for m in merged_mappings
        }

        # Short-circuit if mapping content is identical
        if merged_keys == active_keys and len(merged_mappings) == len(active_mappings):
            return active_mappings

        return await self.dao.insert_mapping_version(stream, merged_mappings)

    async def preview_mapping(
        self,
        stream: str,
        sample_rows: list[dict],
        mappings_override: list[dict] | None,
    ):
        self._require_valid_stream(stream)
        mappings = (
            mappings_override
            if mappings_override is not None
            else await self.get_active_mappings(stream)
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

    async def resolve_headers(self, stream: str, columns: list[str]) -> list[dict]:
        """For each raw column header, whether it (case/whitespace-insensitively)
        matches an existing synonym in the stream's active mapping - the same
        check `ingestion_worker.py` does to compute `unmapped_columns`, just
        surfaced before upload instead of only after."""
        self._require_valid_stream(stream)
        mappings = await self.get_active_mappings(stream)
        mapped_headers = {
            transforms.normalize_header(m["source_field"]) for m in mappings
        }
        return [
            {
                "source_field": c,
                "matched": transforms.normalize_header(c) in mapped_headers,
            }
            for c in columns
        ]

    async def resolve_mapping(self, stream: str, headers: list[str]) -> dict:
        """Combines header matching, active mappings, and canonical fields into a single atomic
        pre-flight query for a file's headers."""
        self._require_valid_stream(stream)
        active_mappings = await self.get_active_mappings(stream)
        canonical_fields = await self.canonical_fields(stream)
        valid_canonical_set = set(canonical_fields)

        synonym_map: dict[str, list[dict]] = {}
        const_mappings: list[dict] = []
        for m in active_mappings:
            if m.get("transform") == "CONST":
                const_mappings.append(m)
            else:
                norm_src = transforms.normalize_header(m["source_field"])
                synonym_map.setdefault(norm_src, []).append(m)

        resolved_mappings: list[dict] = []
        seen_sources = set()

        for header in headers:
            norm_header = transforms.normalize_header(header)
            if norm_header in seen_sources:
                continue
            seen_sources.add(norm_header)

            if norm_header in synonym_map:
                for m in synonym_map[norm_header]:
                    target = (
                        m.get("canonical_field")
                        if m.get("canonical_field") in valid_canonical_set
                        else ""
                    )
                    resolved_mappings.append(
                        {
                            "source_field": header,
                            "canonical_field": target or None,
                            "transform": m.get("transform", "NONE") if target else "NONE",
                            "transform_param": m.get("transform_param") if target else None,
                            "is_matched": True,
                        }
                    )
            else:
                resolved_mappings.append(
                    {
                        "source_field": header,
                        "canonical_field": None,
                        "transform": "NONE",
                        "transform_param": None,
                        "is_matched": False,
                    }
                )

        for cm in const_mappings:
            target = (
                cm.get("canonical_field")
                if cm.get("canonical_field") in valid_canonical_set
                else ""
            )
            if target:
                resolved_mappings.append(
                    {
                        "source_field": cm.get("source_field", "constant"),
                        "canonical_field": target,
                        "transform": "CONST",
                        "transform_param": cm.get("transform_param"),
                        "is_matched": True,
                    }
                )

        return {
            "stream": stream,
            "canonical_fields": canonical_fields,
            "mappings": resolved_mappings,
        }

    async def canonical_fields(self, stream: str) -> list[str]:
        self._require_valid_stream(stream)
        return sorted(KNOWN_FIELDS.get(stream, set()))

    # -- ingestion_jobs --------------------------------------------------------
    async def create_upload_job(
        self,
        *,
        source_id: str,
        fmt: str,
        file: UploadFile,
        started_by: str | None,
    ):
        if fmt not in SUPPORTED_UPLOAD_FORMATS:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, DataHubErrors.UNSUPPORTED_FORMAT
            )
        source = await self.get_data_source(source_id)  # 404s if missing
        stream = source[
            "stream"
        ]  # derived from the source, never client-supplied - see migration 0023's rationale

        job_id = new_id()
        safe_filename = os.path.basename(file.filename or "upload")
        dest_dir = os.path.join(UPLOAD_ROOT, job_id)
        dest_path = os.path.join(dest_dir, safe_filename)

        content_hash = await self._save_upload(file, dest_dir, dest_path)

        existing = await self.dao.find_job_by_content_hash(
            source_id=source_id, content_hash=content_hash
        )
        if existing is not None:
            await run_in_threadpool(_remove_quietly, dest_path)
            raise HTTPException(
                status.HTTP_409_CONFLICT, DataHubErrors.DUPLICATE_UPLOAD
            )

        return await self.dao.insert_ingest_job(
            job_id=job_id,
            source_id=source_id,
            stream=stream,
            file_name=safe_filename,
            file_uri=dest_path,
            fmt=fmt,
            content_hash=content_hash,
            started_by=started_by,
        )

    async def _save_upload(
        self, file: UploadFile, dest_dir: str, dest_path: str
    ) -> str:
        """Streams the upload to disk and returns its SHA-256 hex digest,
        computed in the same pass so re-reading the file isn't needed just
        to fingerprint it for duplicate-upload detection."""
        await run_in_threadpool(os.makedirs, dest_dir, exist_ok=True)
        handle = await run_in_threadpool(open, dest_path, "wb")
        hasher = hashlib.sha256()
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
                hasher.update(chunk)
                await run_in_threadpool(handle.write, chunk)
        except HTTPException:
            await run_in_threadpool(handle.close)
            await run_in_threadpool(_remove_quietly, dest_path)
            raise
        else:
            await run_in_threadpool(handle.close)
        return hasher.hexdigest()

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

    async def list_records_by_stream(
        self,
        *,
        stream: str,
        entity_id: str,
        valid: bool | None,
        search: str | None,
        limit: int,
        offset: int,
    ):
        """Every record of this stream ever ingested for an entity, across
        every job/data source that ever fed it - not scoped to one upload."""
        if not await self.dao.entity_exists(entity_id):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, DataHubErrors.ENTITY_NOT_FOUND
            )
        table, pk_column = self._stream_table(stream)
        return await self.dao.list_records_by_entity(
            table=table,
            pk_column=pk_column,
            search_column=SEARCH_COLUMNS.get(stream),
            entity_id=entity_id,
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
        allowed = EDITABLE_FIELDS.get(job["stream"], set())
        not_editable = set(fields) - allowed
        if not_editable:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Not editable for stream {job['stream']}: {', '.join(sorted(not_editable))}",
            )
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
