"""Lease-based background worker for reconciliation_runs.

Structurally identical to app/workers/ingestion_worker.py - same
claim/heartbeat/complete/fail queue mechanics, same self-healing crashed-
worker story (SELECT ... FOR UPDATE SKIP LOCKED + lease expiry), same
exponential-backoff retry to FAILED. See that file's module docstring for the
mechanics; this one only documents what differs.

Runs `engine.run()` (Phase 1 identification then Phase 2 allocation) inside
one transaction, then marks the run COMPUTED. GL posting (M3) and sign-off
(M4) will extend `engine.run()` itself, not this worker - the queue/lease
code below doesn't change per milestone.

Run: python -m app.workers.reconciliation_worker
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import timedelta

import asyncpg

from app.reconciliation import engine
from app.reconciliation.dao import ReconciliationDAO
from app.db.pool import create_pool

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 3
LEASE_DURATION = timedelta(minutes=5)
HEARTBEAT_INTERVAL_SECONDS = LEASE_DURATION.total_seconds() / 3
BASE_BACKOFF_SECONDS = 30

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

_CLAIM_SQL = """
WITH claimed AS (
    SELECT run_id FROM reconciliation_runs
    WHERE (status = 'QUEUED' AND (next_attempt_at IS NULL OR next_attempt_at <= now()))
       OR (status = 'RUNNING' AND lease_expires_at < now())
    ORDER BY started_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE reconciliation_runs r
SET status = 'RUNNING',
    locked_by = $1,
    locked_at = now(),
    lease_expires_at = now() + $2::interval,
    attempt_count = r.attempt_count + 1
FROM claimed
WHERE r.run_id = claimed.run_id
RETURNING r.run_id, r.definition_id, r.attempt_count, r.max_attempts;
"""

_HEARTBEAT_SQL = """
UPDATE reconciliation_runs
SET lease_expires_at = now() + $3::interval
WHERE run_id = $1 AND locked_by = $2 AND status = 'RUNNING'
RETURNING run_id;
"""

_COMPLETE_SQL = """
UPDATE reconciliation_runs
SET status = $3, volume = $4, matched_count = $5, exception_count = $6,
    matched_value_minor = $7, exception_value_minor = $8, unapplied_minor = $9,
    locked_by = NULL, lease_expires_at = NULL
WHERE run_id = $1 AND locked_by = $2
RETURNING run_id;
"""

_FAIL_SQL = """
UPDATE reconciliation_runs
SET status = CASE WHEN attempt_count >= max_attempts THEN 'FAILED' ELSE 'QUEUED' END,
    next_attempt_at = now() + (make_interval(secs => $3) * power(2, attempt_count)),
    last_error = $4,
    locked_by = NULL,
    lease_expires_at = NULL
WHERE run_id = $1 AND locked_by = $2
RETURNING run_id, status;
"""


async def claim_run(pool: asyncpg.Pool) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(_CLAIM_SQL, WORKER_ID, LEASE_DURATION)


async def _heartbeat_loop(pool: asyncpg.Pool, run_id: str, stop_event: asyncio.Event) -> None:
    """Identical fencing story to ingestion_worker._heartbeat_loop: if a
    renewal finds the row no longer locked by this worker, another worker
    decided this run was abandoned and reclaimed it - this worker's eventual
    result must not overwrite whatever the new claimant does."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            return
        async with pool.acquire() as conn:
            renewed = await conn.fetchrow(_HEARTBEAT_SQL, run_id, WORKER_ID, LEASE_DURATION)
        if renewed is None:
            logger.warning("lease lost for run %s — another worker reclaimed it", run_id)
            stop_event.set()
            return


async def process_reconciliation_run(pool: asyncpg.Pool, run: asyncpg.Record) -> dict:
    """Runs Phase 1 for this run inside one transaction - a mid-run crash
    rolls back everything (no partial payments/exceptions), same crash-safety
    story as ingestion_worker.process_ingestion_job's outer transaction."""
    async with pool.acquire() as conn:
        dao = ReconciliationDAO(conn)
        run_context = await dao.get_run_context(run["run_id"])
        if run_context is None:
            raise ValueError(f"run {run['run_id']} has no resolvable definition/entity")

        async with conn.transaction():
            counters = await engine.run(conn, dao, str(run["run_id"]), run_context)
    return counters


async def run_one_run(pool: asyncpg.Pool, run: asyncpg.Record) -> None:
    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(pool, run["run_id"], stop_event))
    try:
        counters = await process_reconciliation_run(pool, run)
        if stop_event.is_set():
            logger.warning("run %s lease was lost mid-processing; discarding result", run["run_id"])
            return
        async with pool.acquire() as conn:
            await conn.execute(
                _COMPLETE_SQL,
                run["run_id"],
                WORKER_ID,
                "COMPUTED",
                counters["volume"],
                counters["matched_count"],
                counters["exception_count"],
                counters["matched_value_minor"],
                counters["exception_value_minor"],
                counters["unapplied_minor"],
            )
        logger.info(
            "run %s computed: volume=%d locked=%d pooled=%d exceptions=%d",
            run["run_id"], counters["volume"], counters["matched_count"],
            counters["pooled_count"], counters["exception_count"],
        )
    except Exception as exc:  # noqa: BLE001 - any failure here is a run-level failure that must be recorded
        logger.exception("run %s failed", run["run_id"])
        async with pool.acquire() as conn:
            result = await conn.fetchrow(_FAIL_SQL, run["run_id"], WORKER_ID, BASE_BACKOFF_SECONDS, str(exc))
        if result:
            logger.info("run %s -> %s (attempt %d/%d)", run["run_id"], result["status"], run["attempt_count"], run["max_attempts"])
    finally:
        stop_event.set()
        await heartbeat_task


async def run_forever(pool: asyncpg.Pool) -> None:
    logger.info("reconciliation worker %s starting", WORKER_ID)
    while True:
        run = await claim_run(pool)
        if run is None:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue
        logger.info("claimed run %s (attempt %d/%d)", run["run_id"], run["attempt_count"], run["max_attempts"])
        await run_one_run(pool, run)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    pool = await create_pool()
    try:
        await run_forever(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
