import asyncio, sys
sys.path.insert(0, '/app')
import asyncpg
from datetime import date
from app.reconciliation import engine
from app.reconciliation.dao import ReconciliationDAO
from app.reconciliation.constants import DEFAULT_AR_RULE_CATALOG, PHASE_CUSTOMER_LOCK
from app.reconciliation.service import ReconciliationService

async def main():
    conn = await asyncpg.connect("postgresql://recon:recon@db:5432/recon")
    tx = conn.transaction()
    await tx.start()
    try:
        row = await conn.fetchrow("SELECT entity_id FROM entities LIMIT 1")
        entity_id = str(row["entity_id"])
        dao = ReconciliationDAO(conn)
        definition = await dao.insert_definition(entity_id=entity_id, name="dbg", recon_type="AR", cadence=None, owner_user_id=None)
        definition_id = definition["definition_id"]
        await dao.insert_rules_bulk(definition_id, list(DEFAULT_AR_RULE_CATALOG))
        await dao.seed_gl_account_roles(entity_id)
        service = ReconciliationService(dao)

        customer = await conn.fetchrow(
            "INSERT INTO customers (customer_id, entity_id, customer_code, company_name) "
            "VALUES (gen_random_uuid(), $1, $2, $3) RETURNING customer_id",
            entity_id, "ZCODE99", "Field Match Test Co",
        )

        for rule in await dao.list_rules(definition_id):
            if rule["phase"] == PHASE_CUSTOMER_LOCK:
                await dao.update_rule(rule["rule_id"], enabled=False, config=None)

        created = await service.create_rule(
            definition_id, phase=PHASE_CUSTOMER_LOCK, kind="field-match", name="Short Code Match", priority=100,
            confidence=88, config={"matcher": "substring", "bank_field": "narration", "source": "customers", "source_field": "customer_code"},
        )

        bank_txn = await conn.fetchrow(
            "INSERT INTO bank_statements (bank_txn_id, entity_id, bank_reference, transaction_date, payer_name, "
            "narration, currency, amount_minor, amount_home_minor, dr_cr, recon_status) "
            "VALUES (gen_random_uuid(), $1, 'UTR-FM-1', '2026-07-01', 'Unrelated Payer', "
            "'NEFT TRANSFER REF ZCODE99 PAYMENT', 'INR', 100000, 100000, 'CREDIT', 'PENDING') "
            "RETURNING bank_txn_id",
            entity_id,
        )

        run = await dao.insert_run(definition_id=definition_id, run_no="RUN-DBG", period_start=date(2026, 7, 1), period_end=date(2026, 7, 31))
        run_context = await dao.get_run_context(run["run_id"])

        phase1 = await engine.run_phase_1(conn, dao, run["run_id"], run_context)
        print("OUTCOMES:", phase1["outcomes"])
        print("UNIDENTIFIED:", [{"payment_id": u["payment_id"], "bank_txn_id": u["bank_txn"]["bank_txn_id"], "narration": u["bank_txn"]["narration"]} for u in phase1["unidentified"]])
    finally:
        await tx.rollback()
        await conn.close()

asyncio.run(main())
