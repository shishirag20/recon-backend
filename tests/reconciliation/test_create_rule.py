"""Service-level tests for ReconciliationService.create_rule
(POST /reconciliations/{id}/rules) - validation, and one end-to-end test
that a newly-created field-match rule actually fires during a real run,
not just that the row lands in the DB.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException

from app.reconciliation import engine
from app.reconciliation.constants import DEFAULT_AR_RULE_CATALOG, PHASE_CUSTOMER_LOCK, PHASE_GL_CHECK
from app.reconciliation.dao import ReconciliationDAO
from app.reconciliation.service import ReconciliationService

pytestmark = pytest.mark.asyncio


async def _any_entity_id(conn) -> str:
    row = await conn.fetchrow("SELECT entity_id FROM entities LIMIT 1")
    if row is None:
        pytest.skip("no entities in the target DB - nothing to test against")
    return str(row["entity_id"])


async def _new_definition(conn, entity_id: str):
    dao = ReconciliationDAO(conn)
    definition = await dao.insert_definition(entity_id=entity_id, name="Rule-create test", recon_type="AR", cadence=None, owner_user_id=None)
    await dao.insert_rules_bulk(definition["definition_id"], list(DEFAULT_AR_RULE_CATALOG))
    return dao, definition["definition_id"]


class TestCreateRuleValidation:
    async def test_valid_field_match_rule_is_created(self, conn):
        entity_id = await _any_entity_id(conn)
        dao, definition_id = await _new_definition(conn, entity_id)
        service = ReconciliationService(dao)
        row = await service.create_rule(
            definition_id, phase=PHASE_CUSTOMER_LOCK, kind="field-match", name="Short Code Match", priority=100,
            confidence=88, config={"matcher": "substring", "bank_field": "narration", "source": "customers", "source_field": "customer_code"},
        )
        assert row["kind"] == "field-match"
        assert row["priority"] == 100

    async def test_unknown_phase_rejected(self, conn):
        entity_id = await _any_entity_id(conn)
        dao, definition_id = await _new_definition(conn, entity_id)
        service = ReconciliationService(dao)
        with pytest.raises(HTTPException) as exc:
            await service.create_rule(definition_id, phase="NOT_A_PHASE", kind="field-match", name="x", priority=1, confidence=None, config={})
        assert exc.value.status_code == 400

    async def test_unregistered_kind_for_phase_rejected(self, conn):
        entity_id = await _any_entity_id(conn)
        dao, definition_id = await _new_definition(conn, entity_id)
        service = ReconciliationService(dao)
        with pytest.raises(HTTPException) as exc:
            await service.create_rule(definition_id, phase=PHASE_CUSTOMER_LOCK, kind="subset-sum", name="x", priority=100, confidence=None, config={})
        assert exc.value.status_code == 400

    async def test_field_match_missing_config_keys_rejected(self, conn):
        entity_id = await _any_entity_id(conn)
        dao, definition_id = await _new_definition(conn, entity_id)
        service = ReconciliationService(dao)
        with pytest.raises(HTTPException) as exc:
            await service.create_rule(
                definition_id, phase=PHASE_CUSTOMER_LOCK, kind="field-match", name="x", priority=100,
                confidence=None, config={"matcher": "exact"},
            )
        assert exc.value.status_code == 400

    async def test_field_match_unknown_matcher_rejected(self, conn):
        entity_id = await _any_entity_id(conn)
        dao, definition_id = await _new_definition(conn, entity_id)
        service = ReconciliationService(dao)
        with pytest.raises(HTTPException) as exc:
            await service.create_rule(
                definition_id, phase=PHASE_CUSTOMER_LOCK, kind="field-match", name="x", priority=100,
                confidence=None, config={"matcher": "regex", "bank_field": "narration", "source": "customers", "source_field": "customer_code"},
            )
        assert exc.value.status_code == 400

    async def test_priority_collision_is_409(self, conn):
        entity_id = await _any_entity_id(conn)
        dao, definition_id = await _new_definition(conn, entity_id)
        service = ReconciliationService(dao)
        existing = next(r for r in await dao.list_rules(definition_id) if r["phase"] == PHASE_CUSTOMER_LOCK)
        with pytest.raises(HTTPException) as exc:
            await service.create_rule(
                definition_id, phase=PHASE_CUSTOMER_LOCK, kind="field-match", name="x", priority=existing["priority"],
                confidence=None, config={"matcher": "exact", "bank_field": "narration", "source": "customers", "source_field": "customer_code"},
            )
        assert exc.value.status_code == 409

    async def test_threshold_only_phase_rejects_other_kinds(self, conn):
        entity_id = await _any_entity_id(conn)
        dao, definition_id = await _new_definition(conn, entity_id)
        service = ReconciliationService(dao)
        with pytest.raises(HTTPException) as exc:
            await service.create_rule(definition_id, phase=PHASE_GL_CHECK, kind="field-match", name="x", priority=2, confidence=None, config={})
        assert exc.value.status_code == 400


class TestCreatedRuleActuallyFires:
    async def test_new_field_match_rule_locks_a_customer_in_a_real_run(self, conn):
        """Not just a DB round-trip - creates a field-match rule via the
        service, then runs the real engine and confirms it's the rule that
        actually locked the customer."""
        entity_id = await _any_entity_id(conn)
        dao, definition_id = await _new_definition(conn, entity_id)
        await dao.seed_gl_account_roles(entity_id)
        service = ReconciliationService(dao)

        customer = await conn.fetchrow(
            "INSERT INTO customers (customer_id, entity_id, customer_code, company_name) "
            "VALUES (gen_random_uuid(), $1, $2, $3) RETURNING customer_id",
            entity_id, "ZCODE99", "Field Match Test Co",
        )
        customer_id = str(customer["customer_id"])

        # Disable every existing CUSTOMER_LOCK rule so only the new one can lock this payment.
        for rule in await dao.list_rules(definition_id):
            if rule["phase"] == PHASE_CUSTOMER_LOCK:
                await dao.update_rule(rule["rule_id"], enabled=False, config=None)

        created = await service.create_rule(
            definition_id, phase=PHASE_CUSTOMER_LOCK, kind="field-match", name="Short Code Match", priority=100,
            confidence=88, config={"matcher": "substring", "bank_field": "narration", "source": "customers", "source_field": "customer_code"},
        )
        assert created["kind"] == "field-match"

        bank_txn = await conn.fetchrow(
            "INSERT INTO bank_statements (bank_txn_id, entity_id, bank_reference, transaction_date, payer_name, "
            "narration, currency, amount_minor, amount_home_minor, dr_cr, recon_status) "
            "VALUES (gen_random_uuid(), $1, 'UTR-FM-1', '2026-07-01', 'Unrelated Payer', "
            "'NEFT TRANSFER REF ZCODE99 PAYMENT', 'INR', 100000, 100000, 'CREDIT', 'PENDING') "
            "RETURNING bank_txn_id",
            entity_id,
        )

        run = await dao.insert_run(definition_id=definition_id, run_no="RUN-PYTEST-FIELD-MATCH", period_start=date(2026, 7, 1), period_end=date(2026, 7, 31))
        run_context = await dao.get_run_context(run["run_id"])
        await engine.run(conn, dao, run["run_id"], run_context)

        payment = await conn.fetchrow("SELECT customer_id, locked_by_rule_id FROM payments WHERE bank_txn_id = $1", bank_txn["bank_txn_id"])
        assert payment is not None
        assert str(payment["customer_id"]) == customer_id
        assert str(payment["locked_by_rule_id"]) == str(created["rule_id"])
