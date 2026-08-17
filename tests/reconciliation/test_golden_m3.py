"""Golden-data acceptance test for M3 (GL posting + control proof +
exception-resolution API), built on the exact same fixture dataset as
test_golden_m2.py (`golden`, imported below) - this file adds the GL layer
on top of M1/M2's already-verified identification/allocation outcomes.

Deliberately does NOT go through the HTTP API - `ReconciliationService`
methods are called directly, same rationale as test_golden_m2.py's module
docstring (zero persisted footprint, no visible "Golden *" rows in the UI).
"""
from __future__ import annotations

from collections import Counter
from datetime import date

import pytest

from app.reconciliation import engine
from app.reconciliation.constants import DEFAULT_AR_RULE_CATALOG, GL_ROLE_AR_CONTROL
from app.reconciliation.dao import ReconciliationDAO
from app.reconciliation.service import ReconciliationService
from tests.reconciliation.test_golden_m2 import golden  # noqa: F401 - reused fixture

pytestmark = pytest.mark.asyncio

# The M2 golden run leaves exactly this much AR open across every non-PAID
# invoice (INV-101, 107, 108, 112, 113, 114, 115, 117, 120, 121) - see
# docs/reconciliation.md. INV-112/113 (Halcyon/Meridian) are included here
# now too: their payments only ever reach a candidate pool (never an
# independently confirmed Phase 1a lock), so per
# test_halcyon_and_meridian_pool_raises_suspense_not_auto_matched they
# correctly stay open pending human confirmation instead of auto-settling.
# Seeding a GL control balance of 5,000,000 (Rs 50,000) deliberately
# understates it by 2,950,000 (Rs 29,500), so the control proof must fire.
_EXPECTED_SL_BALANCE_MINOR = 7_950_000
_SEEDED_GL_CONTROL_BALANCE_MINOR = 5_000_000
_EXPECTED_VARIANCE_MINOR = _EXPECTED_SL_BALANCE_MINOR - _SEEDED_GL_CONTROL_BALANCE_MINOR


async def _run_with_control_balance(conn, entity_id: str, *, ar_control_balance_minor: int) -> str:
    dao = ReconciliationDAO(conn)
    definition = await dao.insert_definition(entity_id=entity_id, name="Golden AR (pytest, M3)", recon_type="AR", cadence=None, owner_user_id=None)
    await dao.insert_rules_bulk(definition["definition_id"], list(DEFAULT_AR_RULE_CATALOG))
    await dao.seed_gl_account_roles(entity_id)
    roles = await dao.get_gl_account_roles_map(entity_id)
    await conn.execute(
        "INSERT INTO gl_control_balances (balance_id, gl_account_id, period_date, control_balance_minor) "
        "VALUES (gen_random_uuid(), $1, $2, $3)",
        roles[GL_ROLE_AR_CONTROL], date(2026, 7, 31), ar_control_balance_minor,
    )
    run = await dao.insert_run(
        definition_id=definition["definition_id"], run_no="RUN-PYTEST-GOLDEN-M3",
        period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
    )
    run_context = await dao.get_run_context(run["run_id"])
    await engine.run(conn, dao, run["run_id"], run_context)
    return run["run_id"]


async def _journal_lines_by_role(conn, journal_id, entity_id: str) -> Counter:
    rows = await conn.fetch(
        "SELECT l.dr_cr, l.amount_minor, r.role_code FROM gl_journal_lines l "
        "JOIN gl_account_roles r ON r.gl_account_id = l.gl_account_id AND r.entity_id = $2 "
        "WHERE l.journal_id = $1",
        journal_id, entity_id,
    )
    out = Counter()
    for r in rows:
        out[(r["role_code"], r["dr_cr"])] += r["amount_minor"]
    return out


async def _journal_for_invoice(conn, invoice_id: str):
    row = await conn.fetchrow("SELECT gl_journal_id FROM invoice_allocations WHERE invoice_id = $1", invoice_id)
    assert row is not None and row["gl_journal_id"] is not None, f"invoice {invoice_id} has no linked journal"
    return row["gl_journal_id"]


class TestGLPostingScenarios:
    async def test_every_journal_this_run_posted_is_balanced(self, conn, golden):
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        rows = await conn.fetch(
            "SELECT l.journal_id, l.dr_cr, l.amount_minor FROM gl_journal_lines l "
            "JOIN gl_journal_entries j ON j.journal_id = l.journal_id WHERE j.run_id = $1",
            run_id,
        )
        assert rows, "expected at least one journal line to have been posted"
        by_journal: dict = {}
        for r in rows:
            sums = by_journal.setdefault(r["journal_id"], Counter())
            sums[r["dr_cr"]] += r["amount_minor"]
        for journal_id, sums in by_journal.items():
            assert sums["DEBIT"] == sums["CREDIT"], f"journal {journal_id} is unbalanced: {dict(sums)}"

    async def test_tds_net_match_102_gap_posts_to_tds_receivable(self, conn, golden):
        await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        journal_id = await _journal_for_invoice(conn, golden["invoices"]["102"])
        lines = await _journal_lines_by_role(conn, journal_id, golden["entity_id"])
        assert lines[("CASH_CONTROL", "DEBIT")] == 1_350_000
        assert lines[("AR_CONTROL", "CREDIT")] == 1_500_000
        assert lines[("TDS_RECEIVABLE", "DEBIT")] == 150_000

    async def test_fee_tolerance_104_gap_posts_to_bank_charges(self, conn, golden):
        await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        journal_id = await _journal_for_invoice(conn, golden["invoices"]["104"])
        lines = await _journal_lines_by_role(conn, journal_id, golden["entity_id"])
        assert lines[("CASH_CONTROL", "DEBIT")] == 598_000
        assert lines[("AR_CONTROL", "CREDIT")] == 600_000
        assert lines[("BANK_CHARGES", "DEBIT")] == 2_000

    async def test_small_residual_118_gap_posts_to_bank_charges_not_write_off(self, conn, golden):
        """INV-118's 200-minor-unit residual is within both bank-fee's
        and write-off's tolerance (both default 500, no differentiator here
        since BANK-014 has no explicit_fee_minor) - bank-fee wins on
        priority (60 vs write-off's 70), so the gap posts to BANK_CHARGES,
        not WRITE_OFF. Documented, not treated as a bug: see docs/reconciliation.md."""
        await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        journal_id = await _journal_for_invoice(conn, golden["invoices"]["118"])
        lines = await _journal_lines_by_role(conn, journal_id, golden["entity_id"])
        assert lines[("CASH_CONTROL", "DEBIT")] == 299_800
        assert lines[("AR_CONTROL", "CREDIT")] == 300_000
        assert lines[("BANK_CHARGES", "DEBIT")] == 200

    async def test_overpay_103_posts_cash_ar_and_on_account_advance(self, conn, golden):
        await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        journal_id = await _journal_for_invoice(conn, golden["invoices"]["103"])
        lines = await _journal_lines_by_role(conn, journal_id, golden["entity_id"])
        # 950,000 received: 900,000 settles the invoice, 50,000 leftover -
        # both are cash-in, so CASH_CONTROL carries both as separate DEBIT lines.
        assert lines[("CASH_CONTROL", "DEBIT")] == 950_000
        assert lines[("AR_CONTROL", "CREDIT")] == 900_000
        assert lines[("ON_ACCOUNT_ADVANCE", "CREDIT")] == 50_000

    async def test_standalone_bank_charge_017_posts_directly_no_customer_involved(self, conn, golden):
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        bank_row = await conn.fetchrow("SELECT recon_status FROM bank_statements WHERE bank_txn_id = $1", golden["bank"]["017"])
        assert bank_row["recon_status"] == "BANK_CHARGE"
        journal = await conn.fetchrow(
            "SELECT journal_id FROM gl_journal_entries WHERE run_id = $1 AND source_type = 'FEE_ADJUSTMENT'", run_id
        )
        assert journal is not None
        lines = await _journal_lines_by_role(conn, journal["journal_id"], golden["entity_id"])
        assert lines[("BANK_CHARGES", "DEBIT")] == 50_000
        assert lines[("CASH_CONTROL", "CREDIT")] == 50_000

    async def test_suspense_018_posts_cash_and_suspense_only(self, conn, golden):
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        journal = await conn.fetchrow(
            "SELECT journal_id FROM gl_journal_entries WHERE run_id = $1 AND memo LIKE 'Suspense receipt%'", run_id
        )
        assert journal is not None
        lines = await _journal_lines_by_role(conn, journal["journal_id"], golden["entity_id"])
        assert lines[("CASH_CONTROL", "DEBIT")] == 99_900
        assert lines[("SUSPENSE", "CREDIT")] == 99_900


class TestGLControlProof:
    async def test_gl_variance_fires_with_expected_mismatch(self, conn, golden):
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        rows = await conn.fetch(
            "SELECT detail FROM reconciliation_exceptions WHERE run_id = $1 AND exception_type = 'GL_VARIANCE'", run_id
        )
        assert len(rows) == 1
        detail = rows[0]["detail"]
        assert detail["sub_ledger_balance_minor"] == _EXPECTED_SL_BALANCE_MINOR
        assert detail["gl_control_balance_minor"] == _SEEDED_GL_CONTROL_BALANCE_MINOR
        assert detail["variance_minor"] == _EXPECTED_VARIANCE_MINOR

    async def test_no_control_balance_seeded_means_no_variance_exception(self, conn, golden):
        """Without a gl_control_balances row for this exact period_date,
        there's nothing to compare against - skipped, not a mismatch."""
        dao = ReconciliationDAO(conn)
        definition = await dao.insert_definition(entity_id=golden["entity_id"], name="Golden AR (pytest, M3, no control)", recon_type="AR", cadence=None, owner_user_id=None)
        await dao.insert_rules_bulk(definition["definition_id"], list(DEFAULT_AR_RULE_CATALOG))
        await dao.seed_gl_account_roles(golden["entity_id"])
        run = await dao.insert_run(definition_id=definition["definition_id"], run_no="RUN-PYTEST-GOLDEN-M3-NOCTRL", period_start=date(2026, 7, 1), period_end=date(2026, 7, 31))
        run_context = await dao.get_run_context(run["run_id"])
        await engine.run(conn, dao, run["run_id"], run_context)
        rows = await conn.fetch(
            "SELECT 1 FROM reconciliation_exceptions WHERE run_id = $1 AND exception_type = 'GL_VARIANCE'", run["run_id"]
        )
        assert rows == []


class TestExceptionResolutionAPI:
    async def test_list_matches_returns_nested_allocations(self, conn, golden):
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        service = ReconciliationService(ReconciliationDAO(conn))
        matches = await service.list_matches(run_id)
        assert matches, "expected at least one match group"
        subset_sum_group = next(m for m in matches if len(m["allocations"]) >= 2)
        assert all(a["allocated_minor"] > 0 for a in subset_sum_group["allocations"])

    async def test_list_exceptions_filters_by_status(self, conn, golden):
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        service = ReconciliationService(ReconciliationDAO(conn))
        all_exceptions = await service.list_exceptions(run_id, status_=None)
        open_exceptions = await service.list_exceptions(run_id, status_="OPEN")
        assert len(open_exceptions) == len(all_exceptions)  # nothing resolved yet
        assert all(e["status"] == "OPEN" for e in open_exceptions)

    async def test_update_exception_resolves_and_stamps_resolved_at(self, conn, golden):
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        service = ReconciliationService(ReconciliationDAO(conn))
        exceptions = await service.list_exceptions(run_id, status_="OPEN")
        target = next(e for e in exceptions if e["exception_type"] == "NO_PAYMENT")
        updated = await service.update_exception(
            str(target["exception_id"]), status_="WRITTEN_OFF", resolution_outcome="WRITEOFF", resolution_notes="pytest resolution"
        )
        assert updated["status"] == "WRITTEN_OFF"
        assert updated["resolution_outcome"] == "WRITEOFF"
        assert updated["resolution_notes"] == "pytest resolution"
        assert updated["resolved_at"] is not None

    async def test_update_exception_rejects_invalid_status(self, conn, golden):
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        service = ReconciliationService(ReconciliationDAO(conn))
        exceptions = await service.list_exceptions(run_id, status_="OPEN")
        with pytest.raises(Exception):
            await service.update_exception(str(exceptions[0]["exception_id"]), status_="NOT_A_REAL_STATUS", resolution_outcome=None, resolution_notes=None)
