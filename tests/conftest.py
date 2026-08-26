"""Shared pytest fixtures.

`conn` gives every test a connection to DATABASE_URL (the same migrated dev
DB the app/worker containers use - `python -m app.db.migrate` must have been
run against it already) wrapped in a transaction that's always rolled back in
teardown, regardless of test outcome. This is deliberately a single
connection + rollback, not a real isolated test database or a pool: it's
enough to let DAO-level integration tests write real rows and assert on them
without a second Postgres service, while guaranteeing nothing a test does is
ever actually persisted. DAOs only ever need one `asyncpg.Connection`, so
this is a drop-in for what a pool-backed fixture would provide.
"""
from __future__ import annotations

import os

import asyncpg
import pytest_asyncio

from app.db.pool import init_connection


@pytest_asyncio.fixture
async def conn():
    database_url = os.environ.get("DATABASE_URL", "postgresql://recon:recon@127.0.0.1:5432/recon")
    # Reuse the exact same jsonb/json codec app/db/pool.py registers on every
    # real connection (dict <-> jsonb text) - without it, a DAO call passing
    # a plain dict for a jsonb param fails here even though it's correct
    # against the app's real pool.
    connection = await asyncpg.connect(database_url)
    await init_connection(connection)
    transaction = connection.transaction()
    await transaction.start()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()
