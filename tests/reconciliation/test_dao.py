"""DAO-level integration tests against a real (rolled-back) DB connection -
see tests/conftest.py's `conn` fixture for the transaction-rollback contract.
"""
from __future__ import annotations

import pytest

from app.reconciliation.constants import DEFAULT_AR_RULE_CATALOG, GL_ROLE_CODES
from app.reconciliation.dao import ReconciliationDAO


async def _any_entity_id(conn) -> str:
    row = await conn.fetchrow("SELECT entity_id FROM entities LIMIT 1")
    if row is None:
        pytest.skip("no entities in the target DB - nothing to test against")
    return str(row["entity_id"])


async def test_definition_and_rule_seeding_round_trip(conn):
    dao = ReconciliationDAO(conn)
    entity_id = await _any_entity_id(conn)

    definition = await dao.insert_definition(
        entity_id=entity_id, name="Test AR Definition", recon_type="AR", cadence=None, owner_user_id=None
    )
    # asyncpg returns uuid columns as uuid.UUID, not str - compare as str.
    assert str(definition["entity_id"]) == entity_id

    fetched = await dao.get_definition(definition["definition_id"])
    assert fetched["name"] == "Test AR Definition"

    seeded = await dao.insert_rules_bulk(definition["definition_id"], list(DEFAULT_AR_RULE_CATALOG))
    assert len(seeded) == len(DEFAULT_AR_RULE_CATALOG)

    rules = await dao.list_rules(definition["definition_id"])
    assert len(rules) == len(DEFAULT_AR_RULE_CATALOG)
    # Ordered by (phase, priority) - the first row must be priority 0 within its phase.
    assert rules[0]["priority"] == 0

    rule = rules[0]
    updated = await dao.update_rule(rule["rule_id"], enabled=False, config=None)
    assert updated["enabled"] is False
    assert updated["config"] == rule["config"]  # config untouched when None is passed


async def test_gl_account_role_seeding_is_idempotent(conn):
    dao = ReconciliationDAO(conn)
    entity_id = await _any_entity_id(conn)

    first = await dao.seed_gl_account_roles(entity_id)
    second = await dao.seed_gl_account_roles(entity_id)

    assert {r["role_code"] for r in first} == set(GL_ROLE_CODES)
    # Same role_ids both times - re-seeding must not create duplicate rows
    # (gl_account_roles has a UNIQUE(entity_id, role_code) constraint; this
    # asserts the DAO's ON CONFLICT DO UPDATE path, not just the constraint).
    assert {r["role_id"] for r in first} == {r["role_id"] for r in second}

    listed = await dao.list_gl_account_roles(entity_id)
    assert {r["role_code"] for r in listed} == set(GL_ROLE_CODES)
