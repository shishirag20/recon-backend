"""SHORT_PAY/UNAPPLIED/GL_CHECK are no longer inert placeholder phases -
engine.py/gl_posting.py now actually read the single `threshold` rule seeded
onto each phase (constants.py's DEFAULT_AR_RULE_CATALOG) via
`rules.get_threshold_minor`, and gate their respective exception on it.

Default thresholds (Rs 1.00 / Rs 0.00 / Rs 0.00) already have zero effect on
the M1-M3 golden dataset (test_golden_m2.py/test_golden_m3.py stay green
unchanged) - this file specifically exercises a *nonzero* tolerance actually
suppressing an exception, which nothing else covers. Same zero-footprint
pytest + rolled-back-transaction convention as the golden tests.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.reconciliation import engine
from app.reconciliation.constants import DEFAULT_AR_RULE_CATALOG, PHASE_GL_CHECK, PHASE_SHORT_PAY, PHASE_UNAPPLIED
from app.reconciliation.dao import ReconciliationDAO
from tests.reconciliation.test_golden_m2 import (  # noqa: F401 - reused fixtures/helpers
    _run_full_reconciliation,
    _seed_bank_account,
    _seed_bank_txn,
    _seed_entity,
    _seed_customer,
    _seed_invoice,
    golden,
)

pytestmark = pytest.mark.asyncio


async def _set_threshold(conn, definition_id: str, phase: str, value_minor: int) -> None:
    row = await conn.fetchrow(
        "SELECT rule_id FROM reconciliation_rules WHERE definition_id = $1 AND phase = $2 AND kind = 'threshold'",
        definition_id, phase,
    )
    assert row is not None, f"no threshold rule seeded for phase {phase!r}"
    await conn.execute(
        "UPDATE reconciliation_rules SET config = $2::jsonb WHERE rule_id = $1",
        row["rule_id"], {"amount": {"mode": "abs", "value_minor": value_minor}},
    )


class TestShortPayThreshold:
    async def test_smaller_shortfall_suppressed_between_the_two_tolerances(self, conn, golden):
        """INV-117's shortfall (BANK-006: 4,00,000 - 2,50,000 = 1,50,000
        minor units, Rs 1,500) is smaller than INV-107's (BANK-008:
        7,00,000 - 5,00,000 = 2,00,000 minor units, Rs 2,000). With the
        tolerance raised to 1,70,000 (between the two), only the smaller
        one should stop raising SHORT_PAY."""
        dao = ReconciliationDAO(conn)
        definition = await dao.insert_definition(entity_id=golden["entity_id"], name="Golden AR (pytest, threshold)", recon_type="AR", cadence=None, owner_user_id=None)
        await dao.insert_rules_bulk(definition["definition_id"], list(DEFAULT_AR_RULE_CATALOG))
        await dao.seed_gl_account_roles(golden["entity_id"])
        await _set_threshold(conn, definition["definition_id"], PHASE_SHORT_PAY, 170_000)
        run = await dao.insert_run(definition_id=definition["definition_id"], run_no="RUN-PYTEST-THRESH-SP", period_start=date(2026, 7, 1), period_end=date(2026, 7, 31))
        run_context = await dao.get_run_context(run["run_id"])
        await engine.run(conn, dao, run["run_id"], run_context)

        exc_117 = await conn.fetch("SELECT * FROM reconciliation_exceptions WHERE bank_txn_id = $1", golden["bank"]["006"])
        assert not any(dict(e)["exception_type"] == "SHORT_PAY" for e in exc_117), "1,50,000 shortfall should be suppressed by a 1,70,000 tolerance"

        exc_107 = await conn.fetch("SELECT * FROM reconciliation_exceptions WHERE bank_txn_id = $1", golden["bank"]["008"])
        assert any(dict(e)["exception_type"] == "SHORT_PAY" for e in exc_107), "2,00,000 shortfall is above the 1,70,000 tolerance - should still raise"

    async def test_default_tolerance_still_flags_both(self, conn, golden):
        """Regression guard: the default Rs 1.00 tolerance must not
        accidentally suppress either golden Short-Pay case (both shortfalls
        are orders of magnitude above it)."""
        run_id = await _run_full_reconciliation(conn, golden["entity_id"])
        for key in ("008", "006"):
            exceptions = await conn.fetch("SELECT * FROM reconciliation_exceptions WHERE bank_txn_id = $1", golden["bank"][key])
            assert any(dict(e)["exception_type"] == "SHORT_PAY" for e in exceptions)


class TestUnappliedCashThreshold:
    async def _seed_locked_customer_with_no_invoices(self, conn):
        entity_id = await _seed_entity(conn)
        customer_id = await _seed_customer(conn, entity_id, code="CUST-900", name="Threshold Test Co")
        await _seed_bank_account(conn, customer_id, "776655443322", "UTIB0009900")
        bank_txn_id = await _seed_bank_txn(
            conn, entity_id, ref="UTR-THRESH-900", payer="Threshold Test Co", narration="NEFT PAYMENT",
            amount_minor=500, account_no="776655443322", ifsc="UTIB0009900", txn_date="2026-07-01",
        )
        return entity_id, customer_id, bank_txn_id

    async def test_small_unapplied_amount_flagged_at_default_zero_tolerance(self, conn):
        entity_id, customer_id, bank_txn_id = await self._seed_locked_customer_with_no_invoices(conn)
        await _run_full_reconciliation(conn, entity_id)
        exceptions = await conn.fetch("SELECT * FROM reconciliation_exceptions WHERE bank_txn_id = $1", bank_txn_id)
        assert any(dict(e)["exception_type"] == "UNAPPLIED_CASH" for e in exceptions), "default Rs 0.00 tolerance should flag any nonzero unapplied amount"

    async def test_small_unapplied_amount_suppressed_above_tolerance(self, conn):
        entity_id, customer_id, bank_txn_id = await self._seed_locked_customer_with_no_invoices(conn)
        dao = ReconciliationDAO(conn)
        definition = await dao.insert_definition(entity_id=entity_id, name="Threshold test (unapplied)", recon_type="AR", cadence=None, owner_user_id=None)
        await dao.insert_rules_bulk(definition["definition_id"], list(DEFAULT_AR_RULE_CATALOG))
        await dao.seed_gl_account_roles(entity_id)
        await _set_threshold(conn, definition["definition_id"], PHASE_UNAPPLIED, 1000)
        run = await dao.insert_run(definition_id=definition["definition_id"], run_no="RUN-PYTEST-THRESH-UNAP", period_start=date(2026, 7, 1), period_end=date(2026, 7, 31))
        run_context = await dao.get_run_context(run["run_id"])
        await engine.run(conn, dao, run["run_id"], run_context)

        exceptions = await conn.fetch("SELECT * FROM reconciliation_exceptions WHERE bank_txn_id = $1", bank_txn_id)
        assert not any(dict(e)["exception_type"] == "UNAPPLIED_CASH" for e in exceptions), "500 minor units should be suppressed by a 1,000 tolerance"
        payment = await conn.fetchrow("SELECT unapplied_minor FROM payments WHERE bank_txn_id = $1", bank_txn_id)
        assert payment["unapplied_minor"] == 500, "the money itself is still tracked, only the exception is suppressed"


class TestGlCheckThreshold:
    async def _seed_open_invoice(self, conn, *, balance_minor: int, control_balance_minor: int):
        entity_id = await _seed_entity(conn)
        customer_id = await _seed_customer(conn, entity_id, code="CUST-901", name="GL Threshold Test Co")
        invoice_id = await _seed_invoice(conn, entity_id, customer_id, number="INV-GLTEST-1", issue="2026-07-01", due="2026-07-31", total_minor=balance_minor)
        dao = ReconciliationDAO(conn)
        await dao.seed_gl_account_roles(entity_id)
        roles = await dao.get_gl_account_roles_map(entity_id)
        await conn.execute(
            "INSERT INTO gl_control_balances (balance_id, gl_account_id, period_date, control_balance_minor) VALUES (gen_random_uuid(), $1, $2, $3)",
            roles["AR_CONTROL"], date(2026, 7, 31), control_balance_minor,
        )
        return entity_id, invoice_id, dao

    async def test_small_variance_flagged_at_default_zero_tolerance(self, conn):
        # sl_balance (open AR) = 100,000; gl control = 99,950 -> variance = 50
        entity_id, invoice_id, dao = await self._seed_open_invoice(conn, balance_minor=100_000, control_balance_minor=99_950)
        definition = await dao.insert_definition(entity_id=entity_id, name="GL threshold test (default)", recon_type="AR", cadence=None, owner_user_id=None)
        await dao.insert_rules_bulk(definition["definition_id"], list(DEFAULT_AR_RULE_CATALOG))
        run = await dao.insert_run(definition_id=definition["definition_id"], run_no="RUN-PYTEST-GL-DEFAULT", period_start=date(2026, 7, 1), period_end=date(2026, 7, 31))
        run_context = await dao.get_run_context(run["run_id"])
        await engine.run(conn, dao, run["run_id"], run_context)

        rows = await conn.fetch("SELECT * FROM reconciliation_exceptions WHERE run_id = $1 AND exception_type = 'GL_VARIANCE'", run["run_id"])
        assert len(rows) == 1
        assert rows[0]["detail"]["variance_minor"] == 50

    async def test_small_variance_suppressed_above_tolerance(self, conn):
        entity_id, invoice_id, dao = await self._seed_open_invoice(conn, balance_minor=100_000, control_balance_minor=99_950)
        definition = await dao.insert_definition(entity_id=entity_id, name="GL threshold test (tolerant)", recon_type="AR", cadence=None, owner_user_id=None)
        await dao.insert_rules_bulk(definition["definition_id"], list(DEFAULT_AR_RULE_CATALOG))
        await _set_threshold(conn, definition["definition_id"], PHASE_GL_CHECK, 100)
        run = await dao.insert_run(definition_id=definition["definition_id"], run_no="RUN-PYTEST-GL-TOLERANT", period_start=date(2026, 7, 1), period_end=date(2026, 7, 31))
        run_context = await dao.get_run_context(run["run_id"])
        await engine.run(conn, dao, run["run_id"], run_context)

        rows = await conn.fetch("SELECT * FROM reconciliation_exceptions WHERE run_id = $1 AND exception_type = 'GL_VARIANCE'", run["run_id"])
        assert rows == [], "a 50 minor-unit variance should be suppressed by a 100 minor-unit tolerance"
