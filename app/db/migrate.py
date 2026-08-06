"""Applies pending SQL migrations from migrations/ against DATABASE_URL.

Run: python -m recon.app.db.migrate
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


async def run() -> None:
    database_url = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = {row["filename"] for row in await conn.fetch("SELECT filename FROM schema_migrations")}

        pending = sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.name not in applied)
        for path in pending:
            sql = path.read_text()
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations (filename) VALUES ($1)", path.name)
            print(f"applied {path.name}")

        if not pending:
            print("no pending migrations")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())
