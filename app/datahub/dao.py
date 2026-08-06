"""Data Access Object for the data-hub module.

Raw SQL only (asyncpg), no ORM - see the schema-design decision this project
follows throughout. Every function takes an `asyncpg.Connection`/`Pool`.

`asyncpg.Record` is neither attribute-accessible nor a `dict` subclass, so it
can't cross into Pydantic response models as-is - every fetch result is
converted to a plain dict at the DAO boundary (`_row`/`_rows`) so nothing
above this module ever touches the driver-specific type.
"""
from __future__ import annotations

import uuid

import asyncpg


def _row(record: asyncpg.Record | None) -> dict | None:
    return dict(record) if record is not None else None


def _rows(records: list[asyncpg.Record]) -> list[dict]:
    return [dict(r) for r in records]


class DataHubDAO:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    # -- entities (read-only sanity check; entities themselves are owned by the domain-schema migrations) --
    async def entity_exists(self, entity_id: str) -> bool:
        row = await self.conn.fetchrow("SELECT 1 FROM entities WHERE entity_id = $1", entity_id)
        return row is not None

    # -- data_sources --------------------------------------------------------
    async def insert_data_source(self, *, entity_id: str, name: str, kind: str) -> dict:
        row = await self.conn.fetchrow(
            "INSERT INTO data_sources (source_id, entity_id, name, kind, status) "
            "VALUES (gen_random_uuid(), $1, $2, $3, 'CONNECTED') "
            "RETURNING source_id, entity_id, name, kind, status",
            entity_id, name, kind,
        )
        return _row(row)

    async def get_data_source(self, source_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT source_id, entity_id, name, kind, status FROM data_sources WHERE source_id = $1",
            source_id,
        )
        return _row(row)

    async def list_data_sources(self, *, entity_id: str | None, kind: str | None) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT source_id, entity_id, name, kind, status FROM data_sources "
            "WHERE ($1::uuid IS NULL OR entity_id = $1) AND ($2::text IS NULL OR kind = $2) "
            "ORDER BY name",
            entity_id, kind,
        )
        return _rows(rows)

    async def update_data_source(self, source_id: str, *, name: str | None, status: str | None) -> dict | None:
        row = await self.conn.fetchrow(
            "UPDATE data_sources SET name = COALESCE($2, name), status = COALESCE($3, status) "
            "WHERE source_id = $1 "
            "RETURNING source_id, entity_id, name, kind, status",
            source_id, name, status,
        )
        return _row(row)

    # -- field_mappings --------------------------------------------------------
    async def get_latest_version(self, source_id: str) -> int:
        row = await self.conn.fetchrow(
            "SELECT COALESCE(MAX(version), 0) AS v FROM field_mappings WHERE source_id = $1", source_id
        )
        return row["v"]

    async def get_active_mappings(self, source_id: str) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT mapping_id, source_id, version, source_field, canonical_field, transform, transform_param, is_active "
            "FROM field_mappings WHERE source_id = $1 AND is_active = true",
            source_id,
        )
        return _rows(rows)

    async def insert_mapping_version(self, source_id: str, mappings: list[dict]) -> list[dict]:
        async with self.conn.transaction():
            await self.conn.execute(
                "UPDATE field_mappings SET is_active = false WHERE source_id = $1 AND is_active = true", source_id
            )
            next_version = await self.get_latest_version(source_id) + 1
            rows = []
            for m in mappings:
                row = await self.conn.fetchrow(
                    "INSERT INTO field_mappings "
                    "(mapping_id, source_id, version, source_field, canonical_field, transform, transform_param, is_active) "
                    "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, true) "
                    "RETURNING mapping_id, source_id, version, source_field, canonical_field, transform, transform_param, is_active",
                    source_id, next_version, m["source_field"], m["canonical_field"], m["transform"], m.get("transform_param"),
                )
                rows.append(_row(row))
            return rows

    # -- ingestion_jobs --------------------------------------------------------
    async def insert_ingest_job(
        self, *, job_id: str, source_id: str, stream: str, file_name: str, file_uri: str, fmt: str, started_by: str | None
    ) -> dict:
        row = await self.conn.fetchrow(
            "INSERT INTO ingestion_jobs "
            "(job_id, source_id, job_type, stream, file_name, file_uri, format, trigger_type, status, started_by) "
            "VALUES ($1, $2, 'INGEST', $3, $4, $5, $6, 'MANUAL', 'PENDING', $7) "
            "RETURNING *",
            job_id, source_id, stream, file_name, file_uri, fmt, started_by,
        )
        return _row(row)

    async def insert_promote_job(self, *, parent_job_id: str, source_id: str | None) -> dict:
        row = await self.conn.fetchrow(
            "INSERT INTO ingestion_jobs "
            "(job_id, source_id, job_type, parent_job_id, status) "
            "VALUES (gen_random_uuid(), $1, 'PROMOTE', $2, 'PENDING') "
            "RETURNING *",
            source_id, parent_job_id,
        )
        return _row(row)

    async def get_job(self, job_id: str) -> dict | None:
        row = await self.conn.fetchrow("SELECT * FROM ingestion_jobs WHERE job_id = $1", job_id)
        return _row(row)

    async def list_jobs(
        self, *, source_id: str | None, status: str | None, job_type: str | None, limit: int, offset: int
    ) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT * FROM ingestion_jobs "
            "WHERE ($1::uuid IS NULL OR source_id = $1) "
            "AND ($2::text IS NULL OR status = $2) "
            "AND ($3::text IS NULL OR job_type = $3) "
            "ORDER BY started_at DESC LIMIT $4 OFFSET $5",
            source_id, status, job_type, limit, offset,
        )
        return _rows(rows)

    async def retry_job(self, job_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            "UPDATE ingestion_jobs SET status = 'PENDING', attempt_count = 0, next_attempt_at = NULL, last_error = NULL "
            "WHERE job_id = $1 AND status = 'FAILED' "
            "RETURNING *",
            job_id,
        )
        return _row(row)

    # -- staging_records --------------------------------------------------------
    async def list_staging_records(
        self, *, job_id: str, valid: bool | None, search: str | None, limit: int, offset: int
    ) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT * FROM staging_records "
            "WHERE job_id = $1 "
            "AND ($2::boolean IS NULL OR valid = $2) "
            "AND ($3::text IS NULL OR reference ILIKE '%' || $3 || '%' OR counterparty ILIKE '%' || $3 || '%') "
            "ORDER BY staging_id LIMIT $4 OFFSET $5",
            job_id, valid, search, limit, offset,
        )
        return _rows(rows)

    async def get_staging_record(self, staging_id: str) -> dict | None:
        row = await self.conn.fetchrow("SELECT * FROM staging_records WHERE staging_id = $1", staging_id)
        return _row(row)

    async def update_staging_record(self, staging_id: str, fields: dict) -> dict | None:
        if not fields:
            return await self.get_staging_record(staging_id)
        set_clauses = []
        params: list = [staging_id]
        for i, (col, value) in enumerate(fields.items(), start=2):
            set_clauses.append(f"{col} = ${i}")
            params.append(value)
        sql = f"UPDATE staging_records SET {', '.join(set_clauses)} WHERE staging_id = $1 RETURNING *"
        row = await self.conn.fetchrow(sql, *params)
        return _row(row)

    async def insert_staging_rows(self, rows: list[tuple]) -> None:
        await self.conn.executemany(
            "INSERT INTO staging_records "
            "(staging_id, job_id, stream, txn_date, reference, counterparty, "
            "amount_minor, amount_home_minor, currency, dr_cr, raw, valid, issues) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
            rows,
        )


def new_id() -> str:
    return str(uuid.uuid4())
