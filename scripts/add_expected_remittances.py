import asyncio
import os
import asyncpg

async def main():
    db_url = os.getenv("DATABASE_URL", "postgresql://recon:recon@db:5432/recon")
    conn = await asyncpg.connect(db_url)
    try:
        entity = await conn.fetchval("SELECT entity_id FROM entities LIMIT 1")
        exists = await conn.fetchval("SELECT count(*) FROM data_sources WHERE name = 'Expected Remittances'")
        if exists == 0:
            await conn.execute(
                "INSERT INTO data_sources (source_id, entity_id, name, kind, status, stream) "
                "VALUES (gen_random_uuid(), $1, 'Expected Remittances', 'ERP', 'CONNECTED', 'REMITTANCE')",
                entity
            )
            print("Inserted Expected Remittances into data_sources")
        else:
            print("Expected Remittances already exists in data_sources")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
