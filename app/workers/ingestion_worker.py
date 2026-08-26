"""Lease-based background worker for ingestion_jobs.

Any number of worker processes/containers can run this loop concurrently with
no external coordination: `SELECT ... FOR UPDATE SKIP LOCKED` lets each one
claim a different pending job with zero contention. A crashed worker's job is
recovered automatically once its lease expires (self-healing, not a stuck
row) rather than needing manual intervention.

Parses an uploaded file and writes each row directly into its stream's real
canonical table (bank_statements / invoices / customers) - see
app/datahub/canonical.py. A row that can't be turned into a canonical record
doesn't fail the batch; it's collected into the job's `failed_rows` instead.

Run: python -m app.workers.ingestion_worker
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import socket
import uuid
from datetime import timedelta

import asyncpg

from app.datahub import ai_mapping
from app.datahub.canonical import KNOWN_FIELDS, STREAM_INSERTERS, RowRejected, row_hash, unknown_field_issues
from app.datahub.dao import DataHubDAO
from app.datahub.transforms import apply_fill_down, apply_mapping, normalize_header
from app.db.pool import create_pool

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 3
LEASE_DURATION = timedelta(minutes=5)
HEARTBEAT_INTERVAL_SECONDS = LEASE_DURATION.total_seconds() / 3
BASE_BACKOFF_SECONDS = 30

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

_CLAIM_SQL = """
WITH claimed AS (
    SELECT job_id FROM ingestion_jobs
    WHERE (status = 'PENDING' AND (next_attempt_at IS NULL OR next_attempt_at <= now()))
       OR (status = 'RUNNING' AND lease_expires_at < now())
    ORDER BY started_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE ingestion_jobs j
SET status = 'RUNNING',
    locked_by = $1,
    locked_at = now(),
    lease_expires_at = now() + $2::interval,
    attempt_count = j.attempt_count + 1
FROM claimed
WHERE j.job_id = claimed.job_id
RETURNING j.job_id, j.file_uri, j.file_name, j.format, j.source_id, j.stream,
          j.attempt_count, j.max_attempts;
"""

_HEARTBEAT_SQL = """
UPDATE ingestion_jobs
SET lease_expires_at = now() + $3::interval
WHERE job_id = $1 AND locked_by = $2 AND status = 'RUNNING'
RETURNING job_id;
"""

_COMPLETE_SQL = """
UPDATE ingestion_jobs
SET status = $3, row_count = $4, error_count = $5, failed_rows = $6,
    locked_by = NULL, lease_expires_at = NULL
WHERE job_id = $1 AND locked_by = $2
RETURNING job_id;
"""

_FAIL_SQL = """
UPDATE ingestion_jobs
SET status = CASE WHEN attempt_count >= max_attempts THEN 'FAILED' ELSE 'PENDING' END,
    next_attempt_at = now() + (make_interval(secs => $3) * power(2, attempt_count)),
    last_error = $4,
    locked_by = NULL,
    lease_expires_at = NULL
WHERE job_id = $1 AND locked_by = $2
RETURNING job_id, status;
"""


async def claim_job(pool: asyncpg.Pool) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(_CLAIM_SQL, WORKER_ID, LEASE_DURATION)


async def _heartbeat_loop(
    pool: asyncpg.Pool, job_id: str, stop_event: asyncio.Event
) -> None:
    """Extends the lease periodically; sets stop_event if the lease was stolen.

    A stolen lease means another worker decided this job was abandoned and
    reclaimed it — this worker must not let its eventual result overwrite
    whatever the new claimant does (fencing).
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS
            )
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            return
        async with pool.acquire() as conn:
            renewed = await conn.fetchrow(
                _HEARTBEAT_SQL, job_id, WORKER_ID, LEASE_DURATION
            )
        if renewed is None:
            logger.warning(
                "lease lost for job %s — another worker reclaimed it", job_id
            )
            stop_event.set()
            return


async def process_ingestion_job(
    pool: asyncpg.Pool, job: asyncpg.Record
) -> tuple[int, int, list[dict]]:
    """Parses the uploaded file at job['file_uri'] and writes each row
    directly into its stream's canonical table.

    The whole batch runs inside one outer transaction, with each row's
    insert in a nested transaction (a Postgres savepoint, since asyncpg
    detects the nesting automatically). A row-level failure only rolls back
    that row - normal `failed_rows` behavior, unchanged. But if the worker
    process itself dies mid-file, nothing has been committed at all, so a
    retry starting from row 1 is correct instead of duplicating whatever
    happened to have been inserted before the crash.

    Only CSV is implemented (see SUPPORTED_UPLOAD_FORMATS), and only the
    BANK/INVOICE/CUSTOMER streams have a canonical inserter - anything else
    raises, which run_one_job turns into a normal retry/dead-letter failure.
    """
    if job["format"] != "CSV":
        raise ValueError(
            f"unsupported format {job['format']!r} (only CSV implemented so far)"
        )
    insert_fn = STREAM_INSERTERS.get(job["stream"])
    if insert_fn is None:
        raise ValueError(
            f"unsupported stream {job['stream']!r} for direct-to-canonical ingestion"
        )
    if not job["file_uri"] or not os.path.exists(job["file_uri"]):
        raise FileNotFoundError(f"no file at {job['file_uri']!r}")

    with open(job["file_uri"], newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        headers = reader.fieldnames or []

    async with pool.acquire() as conn:
        dao = DataHubDAO(conn)
        mappings = await dao.get_active_mappings(job["stream"])
        if not mappings:
            raise ValueError(
                f"stream {job['stream']!r} has no active field mapping"
            )
        # Must run once, over the whole file, before any row's own mapping -
        # a no-op unless the active mapping actually uses FILL_DOWN.
        rows = apply_fill_down(rows, mappings)
        source = await dao.get_data_source(job["source_id"])
        entity_id = source["entity_id"]
        home_currency = await dao.get_entity_home_currency(entity_id)
        if home_currency is None:
            raise ValueError(f"entity {entity_id} not found or has no home_currency")

        # Any file header that matches no synonym in the shared mapping is a
        # candidate for the AI-suggestion stub (currently a no-op - see
        # app/datahub/ai_mapping.py); anything still unresolved after that is
        # recorded on the job instead of silently failing every row.
        mapped_headers = {
            normalize_header(m["source_field"])
            for m in mappings
            if m.get("canonical_field") and str(m["canonical_field"]).strip() not in ("", "-")
        }
        unmapped_columns = [h for h in headers if normalize_header(h) not in mapped_headers]
        if unmapped_columns:
            suggestions = ai_mapping.suggest_canonical_fields(
                job["stream"], unmapped_columns, KNOWN_FIELDS.get(job["stream"], set())
            )
            if suggestions:
                mappings = await dao.save_mapping(
                    job["stream"], [dict(m) for m in mappings] + suggestions
                )
                mapped_headers = {
                    normalize_header(m["source_field"])
                    for m in mappings
                    if m.get("canonical_field") and str(m["canonical_field"]).strip() not in ("", "-")
                }
                unmapped_columns = [h for h in headers if normalize_header(h) not in mapped_headers]

        row_count = 0
        error_count = 0
        failed_rows: list[dict] = []

        async with conn.transaction():
            for raw_row in rows:
                row_count += 1
                canonical, issues = apply_mapping(raw_row, mappings)
                issues = issues + unknown_field_issues(job["stream"], canonical)
                # `raw` on the canonical row stores only what the mapping
                # *didn't* capture - fields already sitting in a typed column
                # would just be a redundant copy otherwise. The duplicate-row
                # fingerprint still hashes the full row, not this trimmed
                # version - two rows with different mapped values but the
                # same (or no) leftover fields must not hash identically.
                extra_raw = {
                    k: v for k, v in raw_row.items() if normalize_header(k) not in mapped_headers
                } or None
                # Snapshot before insert_fn runs: issues added here are genuine
                # mapping/transform problems (a bad date, an unknown field, ...).
                # An inserter (insert_invoice_row, for an unresolved
                # customer_code - migration 0031) may append its own issue
                # afterward for a row that landed fine but isn't fully linked
                # yet - that's an expected, resolvable state, not a processing
                # failure, so it shouldn't flip the whole job to FAILED/PARTIAL.
                # The row itself still gets `valid=false` either way (insert_fn
                # receives and stores the full, possibly-grown `issues` list) -
                # only this job-level error_count ignores what's added here.
                pre_insert_issue_count = len(issues)
                try:
                    async with conn.transaction():  # nested -> savepoint, isolates this row only
                        await insert_fn(
                            conn,
                            entity_id=entity_id,
                            source_job_id=job["job_id"],
                            canonical=canonical,
                            raw=extra_raw,
                            issues=issues,
                            home_currency=home_currency,
                            row_hash_value=row_hash(raw_row),
                        )
                    if pre_insert_issue_count > 0:
                        error_count += 1
                except RowRejected as exc:
                    error_count += 1
                    failed_rows.append({"raw": raw_row, "issues": issues + [str(exc)]})

            await conn.execute(
                "UPDATE ingestion_jobs SET unmapped_columns = $2 WHERE job_id = $1",
                job["job_id"],
                unmapped_columns or None,
            )

    return row_count, error_count, failed_rows


async def run_one_job(pool: asyncpg.Pool, job: asyncpg.Record) -> None:
    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(pool, job["job_id"], stop_event)
    )
    try:
        row_count, error_count, failed_rows = await process_ingestion_job(pool, job)
        if stop_event.is_set():
            logger.warning(
                "job %s lease was lost mid-processing; discarding result", job["job_id"]
            )
            return
        if error_count == 0:
            status = "SUCCESS"
        elif error_count == row_count:
            status = "FAILED"
        else:
            status = "PARTIAL"
        async with pool.acquire() as conn:
            await conn.execute(
                _COMPLETE_SQL,
                job["job_id"],
                WORKER_ID,
                status,
                row_count,
                error_count,
                failed_rows or None,
            )
        logger.info(
            "job %s completed: %s (%d rows, %d errors)",
            job["job_id"],
            status,
            row_count,
            error_count,
        )
    except (
        Exception
    ) as exc:  # noqa: BLE001 - any failure here is a job-level failure that must be recorded
        logger.exception("job %s failed", job["job_id"])
        async with pool.acquire() as conn:
            result = await conn.fetchrow(
                _FAIL_SQL, job["job_id"], WORKER_ID, BASE_BACKOFF_SECONDS, str(exc)
            )
        if result:
            logger.info(
                "job %s -> %s (attempt %d/%d)",
                job["job_id"],
                result["status"],
                job["attempt_count"],
                job["max_attempts"],
            )
    finally:
        stop_event.set()
        await heartbeat_task


async def run_forever(pool: asyncpg.Pool) -> None:
    logger.info("ingestion worker %s starting", WORKER_ID)
    while True:
        job = await claim_job(pool)
        if job is None:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue
        logger.info(
            "claimed job %s (attempt %d/%d)",
            job["job_id"],
            job["attempt_count"],
            job["max_attempts"],
        )
        await run_one_job(pool, job)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    pool = await create_pool()
    try:
        await run_forever(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
