"""Data Access Object for the data-hub module.

Raw SQL only (asyncpg), no ORM - see the schema-design decision this project
follows throughout. Every function takes an `asyncpg.Connection`/`Pool`.

`asyncpg.Record` is neither attribute-accessible nor a `dict` subclass, so it
can't cross into Pydantic response models as-is - every fetch result is
converted to a plain dict at the DAO boundary (`_row`/`_rows`) so nothing
above this module ever touches the driver-specific type.

`list_records`/`get_record`/`update_record` build SQL against a `table`/
`pk_column`/`search_column` - always one of the fixed values in
app/datahub/canonical.py's STREAM_TABLES, never user input, so the
interpolation is safe.
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
    async def insert_data_source(self, *, entity_id: str, name: str, kind: str, stream: str) -> dict:
        row = await self.conn.fetchrow(
            "INSERT INTO data_sources (source_id, entity_id, name, kind, stream, status) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, 'CONNECTED') "
            "RETURNING source_id, entity_id, name, kind, stream, status",
            entity_id, name, kind, stream,
        )
        return _row(row)

    async def get_data_source(self, source_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT source_id, entity_id, name, kind, stream, status FROM data_sources WHERE source_id = $1",
            source_id,
        )
        return _row(row)

    async def list_data_sources(self, *, entity_id: str | None, kind: str | None) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT source_id, entity_id, name, kind, stream, status FROM data_sources "
            "WHERE ($1::uuid IS NULL OR entity_id = $1) AND ($2::text IS NULL OR kind = $2) "
            "ORDER BY name",
            entity_id, kind,
        )
        return _rows(rows)

    async def update_data_source(self, source_id: str, *, name: str | None, status: str | None) -> dict | None:
        row = await self.conn.fetchrow(
            "UPDATE data_sources SET name = COALESCE($2, name), status = COALESCE($3, status) "
            "WHERE source_id = $1 "
            "RETURNING source_id, entity_id, name, kind, stream, status",
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
            "(job_id, source_id, stream, file_name, file_uri, format, trigger_type, status, started_by) "
            "VALUES ($1, $2, $3, $4, $5, $6, 'MANUAL', 'PENDING', $7) "
            "RETURNING *",
            job_id, source_id, stream, file_name, file_uri, fmt, started_by,
        )
        return _row(row)

    async def get_job(self, job_id: str) -> dict | None:
        row = await self.conn.fetchrow("SELECT * FROM ingestion_jobs WHERE job_id = $1", job_id)
        return _row(row)

    async def list_jobs(
        self, *, source_id: str | None, status: str | None, limit: int, offset: int
    ) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT * FROM ingestion_jobs "
            "WHERE ($1::uuid IS NULL OR source_id = $1) "
            "AND ($2::text IS NULL OR status = $2) "
            "ORDER BY started_at DESC LIMIT $3 OFFSET $4",
            source_id, status, limit, offset,
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

    # -- canonical records (Data Explorer) --------------------------------------------------------
    async def _list_records(
        self, *, table: str, pk_column: str, search_column: str | None, scope_column: str, scope_value: str,
        valid: bool | None, search: str | None, limit: int, offset: int,
    ) -> list[dict]:
        conditions = [f"{scope_column} = $1"]
        params: list = [scope_value]
        if valid is not None:
            params.append(valid)
            conditions.append(f"valid = ${len(params)}")
        if search_column and search:
            params.append(f"%{search}%")
            conditions.append(f"{search_column} ILIKE ${len(params)}")
        params.extend([limit, offset])
        sql = (
            f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} "
            f"ORDER BY {pk_column} LIMIT ${len(params) - 1} OFFSET ${len(params)}"
        )
        rows = await self.conn.fetch(sql, *params)
        return _rows(rows)

    async def list_records(
        self, *, table: str, pk_column: str, search_column: str | None,
        job_id: str, valid: bool | None, search: str | None, limit: int, offset: int,
    ) -> list[dict]:
        """All records a single ingestion job produced."""
        return await self._list_records(
            table=table, pk_column=pk_column, search_column=search_column,
            scope_column="source_job_id", scope_value=job_id,
            valid=valid, search=search, limit=limit, offset=offset,
        )

    async def list_records_by_entity(
        self, *, table: str, pk_column: str, search_column: str | None,
        entity_id: str, valid: bool | None, search: str | None, limit: int, offset: int,
    ) -> list[dict]:
        """Every record of this stream ever ingested for an entity, across all jobs/sources."""
        return await self._list_records(
            table=table, pk_column=pk_column, search_column=search_column,
            scope_column="entity_id", scope_value=entity_id,
            valid=valid, search=search, limit=limit, offset=offset,
        )

    async def get_record(self, *, table: str, pk_column: str, record_id: str) -> dict | None:
        row = await self.conn.fetchrow(f"SELECT * FROM {table} WHERE {pk_column} = $1", record_id)
        return _row(row)

    async def update_record(self, *, table: str, pk_column: str, record_id: str, fields: dict) -> dict | None:
        if not fields:
            return await self.get_record(table=table, pk_column=pk_column, record_id=record_id)
        set_clauses = []
        params: list = [record_id]
        for col, value in fields.items():
            params.append(value)
            set_clauses.append(f"{col} = ${len(params)}")
        sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {pk_column} = $1 RETURNING *"
        row = await self.conn.fetchrow(sql, *params)
        return _row(row)


def new_id() -> str:
    return str(uuid.uuid4())
