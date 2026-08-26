"""Golden-data acceptance test for M3 (GL posting + control proof +
exception-resolution API), built on the exact same fixture dataset as
test_golden_m2.py (`golden`, imported below) - this file adds the GL layer
on top of M1/M2's already-verified identification/allocation outcomes.

Deliberately does NOT go through the HTTP API - `ReconciliationService`
methods are called directly, same rationale as test_golden_m2.py's module
docstring (zero persisted footprint, no visible "Golden *" rows in the UI).
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date

import pytest

from app.reconciliation import engine
from app.reconciliation.constants import DEFAULT_AR_RULE_CATALOG, GL_ROLE_AR_CONTROL
from app.reconciliation.dao import ReconciliationDAO
from app.reconciliation.service import ReconciliationService
from tests.reconciliation.test_golden_m2 import golden  # noqa: F401 - reused fixture
from tests.reconciliation.test_golden_m2 import _exceptions_for, _invoice, _payment_for

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
        period_start=date(2026, 6, 1), period_end=date(2026, 7, 31),
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
        for j_id, sums in by_journal.items():
            assert sums["DEBIT"] == sums["CREDIT"], f"journal {j_id} is unbalanced: {dict(sums)}"

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

    async def test_small_residual_118_gap_posts_to_write_off_not_bank_charges(self, conn, golden):
        """INV-118's 200-minor-unit residual has no explicit_fee_minor on
        BANK-014 - bank-fee no longer has a generic tolerance fallback (it
        only ever matches a residual against the bank row's own declared
        fee), so an unexplained gap like this one is correctly left for
        write-off instead. Previously bank-fee's now-removed fallback beat
        write-off on priority regardless of whether the gap was ever
        actually explained as a fee - see the allocation.py fix."""
        await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        journal_id = await _journal_for_invoice(conn, golden["invoices"]["118"])
        lines = await _journal_lines_by_role(conn, journal_id, golden["entity_id"])
        assert lines[("CASH_CONTROL", "DEBIT")] == 299_800
        assert lines[("AR_CONTROL", "CREDIT")] == 300_000
        assert lines[("WRITE_OFF", "DEBIT")] == 200

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
            "SELECT journal_id FROM gl_journal_entries WHERE run_id = $1 AND (memo LIKE 'Suspense receipt%' OR memo LIKE $2)",
            run_id,
            f"%{golden['bank']['018']}%",
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
        if isinstance(detail, str):
            detail = json.loads(detail)
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

    async def test_resolve_no_payment_matches_payment_and_cross_resolves_suspense(self, conn, golden):
        """INV-112 (Halcyon) never gets touched by the engine - BANK-011 only
        ever reaches a candidate pool (weak narration-token hint), so per
        test_halcyon_and_meridian_pool_raises_suspense_not_auto_matched it's
        Suspense on the bank side and NO_PAYMENT on the invoice side, with
        BANK-011's payment sitting fully unapplied (600000 minor). Manually
        matching that payment from the No-Payment-Received panel should
        close the invoice AND auto-resolve the sibling Suspense exception -
        one reviewer decision, two exceptions cleared."""
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        service = ReconciliationService(ReconciliationDAO(conn))

        no_payment_exc = next(
            e for e in await service.list_exceptions(run_id, status_="OPEN")
            if e["exception_type"] == "NO_PAYMENT" and str(e["invoice_id"]) == golden["invoices"]["112"]
        )
        halcyon_payment = await _payment_for(conn, golden["bank"]["011"])
        assert halcyon_payment["unapplied_minor"] == 600_000

        open_payments = await service.list_open_payments(run_id)
        assert any(p["payment_id"] == halcyon_payment["payment_id"] for p in open_payments)

        resolved = await service.resolve_no_payment(
            str(no_payment_exc["exception_id"]), payment_ids=[str(halcyon_payment["payment_id"])], note="pytest manual match",
        )
        assert resolved["status"] == "RESOLVED"
        assert resolved["resolution_outcome"] == "MANUAL_MATCH"
        assert resolved["match_group_id"] is not None

        inv112 = await _invoice(conn, golden["invoices"]["112"])
        assert inv112["status"] == "PAID" and inv112["balance_due_minor"] == 0

        payment_after = await _payment_for(conn, golden["bank"]["011"])
        assert payment_after["unapplied_minor"] == 0

        halcyon_suspense = next(
            e for e in await _exceptions_for(conn, golden["bank"]["011"]) if e["exception_type"] == "SUSPENSE"
        )
        assert halcyon_suspense["status"] == "AUTO_RESOLVED"
        assert halcyon_suspense["resolution_outcome"] == "MANUAL_MATCH"

    async def test_resolve_no_payment_rejects_wrong_exception_type(self, conn, golden):
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        service = ReconciliationService(ReconciliationDAO(conn))
        exceptions = await service.list_exceptions(run_id, status_="OPEN")
        not_no_payment = next(e for e in exceptions if e["exception_type"] != "NO_PAYMENT")
        with pytest.raises(Exception):
            await service.resolve_no_payment(str(not_no_payment["exception_id"]), payment_ids=["00000000-0000-0000-0000-000000000000"], note=None)

    async def test_resolve_suspense_matches_suggested_customer_and_invoice(self, conn, golden):
        """Halcyon's BANK-011 Suspense exception carries a suggestion
        (suggested_customer_id=Halcyon, suggested_invoice_ids=[INV-112]) -
        confirming exactly that suggestion should lock the payment, close
        the invoice, and resolve the exception as a real MANUAL_MATCH."""
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        service = ReconciliationService(ReconciliationDAO(conn))

        suspense_exc = next(
            e for e in await service.list_exceptions(run_id, status_="OPEN")
            if e["exception_type"] == "SUSPENSE" and str(e["bank_txn_id"]) == golden["bank"]["011"]
        )
        assert suspense_exc["detail"]["suggested_customer_id"] == golden["customers"]["halcyon"]

        invoices = await service.list_open_invoices_for_customer(golden["customers"]["halcyon"])
        assert any(str(i["invoice_id"]) == golden["invoices"]["112"] for i in invoices)

        resolved = await service.resolve_suspense(
            str(suspense_exc["exception_id"]), customer_id=golden["customers"]["halcyon"],
            invoice_ids=[golden["invoices"]["112"]], note="pytest confirmed suggestion",
        )
        assert resolved["status"] == "RESOLVED"
        assert resolved["resolution_outcome"] == "MANUAL_MATCH"
        assert resolved["match_group_id"] is not None

        inv112 = await _invoice(conn, golden["invoices"]["112"])
        assert inv112["status"] == "PAID" and inv112["balance_due_minor"] == 0
        payment = await _payment_for(conn, golden["bank"]["011"])
        assert str(payment["customer_id"]) == golden["customers"]["halcyon"]
        assert payment["unapplied_minor"] == 0

    async def test_resolve_suspense_on_account_leaves_payment_unapplied(self, conn, golden):
        """Confirming a customer with no invoice_ids (the "candidate pool,
        no exact match - apply as unapplied cash" case) should lock the
        payment to that customer but leave its cash untouched."""
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        service = ReconciliationService(ReconciliationDAO(conn))

        suspense_exc = next(
            e for e in await service.list_exceptions(run_id, status_="OPEN")
            if e["exception_type"] == "SUSPENSE" and str(e["bank_txn_id"]) == golden["bank"]["012"]
        )
        resolved = await service.resolve_suspense(
            str(suspense_exc["exception_id"]), customer_id=golden["customers"]["meridian"], invoice_ids=[], note=None,
        )
        assert resolved["status"] == "RESOLVED"
        assert resolved["resolution_outcome"] == "ON_ACCOUNT"
        assert resolved["match_group_id"] is None

        payment = await _payment_for(conn, golden["bank"]["012"])
        assert str(payment["customer_id"]) == golden["customers"]["meridian"]
        assert payment["unapplied_minor"] == 1_100_000  # untouched

    async def test_list_open_invoices_searches_across_customers(self, conn, golden):
        """The Suspense panel's "match to a different invoice" fallback -
        unscoped by customer, unlike list_open_invoices_for_customer."""
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        service = ReconciliationService(ReconciliationDAO(conn))

        all_open = await service.list_open_invoices(run_id, search=None)
        assert any(str(i["invoice_id"]) == golden["invoices"]["112"] for i in all_open)
        assert any(str(i["invoice_id"]) == golden["invoices"]["108"] for i in all_open)

        by_number = await service.list_open_invoices(run_id, search="INV-2026-112")
        assert len(by_number) == 1 and str(by_number[0]["invoice_id"]) == golden["invoices"]["112"]

        by_customer_name = await service.list_open_invoices(run_id, search="Halcyon")
        assert any(str(i["invoice_id"]) == golden["invoices"]["112"] for i in by_customer_name)
        assert not any(str(i["invoice_id"]) == golden["invoices"]["108"] for i in by_customer_name)

    async def test_resolve_suspense_rejects_wrong_exception_type(self, conn, golden):
        run_id = await _run_with_control_balance(conn, golden["entity_id"], ar_control_balance_minor=_SEEDED_GL_CONTROL_BALANCE_MINOR)
        service = ReconciliationService(ReconciliationDAO(conn))
        exceptions = await service.list_exceptions(run_id, status_="OPEN")
        not_suspense = next(e for e in exceptions if e["exception_type"] != "SUSPENSE")
        with pytest.raises(Exception):
            await service.resolve_suspense(str(not_suspense["exception_id"]), customer_id=golden["customers"]["halcyon"], invoice_ids=[], note=None)
