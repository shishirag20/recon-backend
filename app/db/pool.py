"""Shared asyncpg connection pool creation.

Used by the FastAPI app (request-scoped connections) and by background
workers (long-lived polling loop) alike, so both talk to Postgres through
the same connection-pooling settings.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import asyncpg
from fastapi import Request


async def init_connection(conn: asyncpg.Connection) -> None:
    """Registers the jsonb/json <-> dict codec every real connection needs.

    Public (no leading underscore) because tests/conftest.py's `conn` fixture
    also calls this directly on a bare `asyncpg.connect()` - a DAO call
    passing a plain dict for a jsonb param is only valid once this codec is
    registered, so any connection a DAO touches needs it, pool-backed or not.
    """
    # Without this, jsonb columns round-trip as raw JSON text instead of
    # dict/list - every caller would otherwise need to json.dumps/loads by hand.
    for typename in ("jsonb", "json"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog", format="text"
        )


async def create_pool(min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://recon:recon@127.0.0.1:5432/recon"
    )
    return await asyncpg.create_pool(
        database_url, min_size=min_size, max_size=max_size, init=init_connection
    )


async def get_connection(request: Request) -> AsyncIterator[asyncpg.Connection]:
    """FastAPI dependency: acquires a connection from the pool on app.state."""
    async with request.app.state.pool.acquire() as conn:
        yield conn
