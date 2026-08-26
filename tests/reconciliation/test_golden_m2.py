"""Golden-data acceptance test for M1 (identification) + M2 (allocation),
mirroring truebalance/rule-test-data/{Customers,SL,Bank_Statement}.csv
exactly - see docs/reconciliation.md §8 for the two known fixture bugs
(missing BANK-015/016, and BANK-001/BANK-019's unintentional reference
collision) that this test accounts for rather than hides.

Deliberately does NOT go through app/datahub ingestion or the HTTP API - it
seeds entities/customers/invoices/bank_statements directly via SQL inside
the `conn` fixture's rolled-back transaction. Earlier verification of this
same dataset was done by uploading through the real Data Hub API against the
shared dev database, which left visible "Golden *" data source cards and
entities in the UI that had to be manually cleaned up afterward - this test
gets the same verification confidence with zero persisted footprint.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.reconciliation import engine
from app.reconciliation.constants import DEFAULT_AR_RULE_CATALOG
from app.reconciliation.dao import ReconciliationDAO

pytestmark = pytest.mark.asyncio


async def _seed_entity(conn) -> str:
    row = await conn.fetchrow(
        "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'Test Org', 'test-org-' || substr(md5(random()::text), 1, 8)) "
        "RETURNING id"
    )
    org_id = row["id"]
    row = await conn.fetchrow(
        "INSERT INTO entities (entity_id, organization_id, company_code, name, home_currency) "
        "VALUES (gen_random_uuid(), $1, 'T01', 'Golden Test (pytest)', 'INR') RETURNING entity_id",
        org_id,
    )
    return str(row["entity_id"])


async def _seed_customer(conn, entity_id: str, *, code: str | None, name: str, pan=None, gstin=None, vpa=None) -> str:
    row = await conn.fetchrow(
        "INSERT INTO customers (customer_id, entity_id, customer_code, company_name, pan, gstin, vpa_handle) "
        "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6) RETURNING customer_id",
        entity_id, code, name, pan, gstin, vpa,
    )
    return str(row["customer_id"])


async def _seed_bank_account(conn, customer_id: str, account_no: str, ifsc: str) -> None:
    await conn.execute(
        "INSERT INTO customer_bank_accounts (account_id, customer_id, bank_account_no, ifsc_code, is_primary) "
        "VALUES (gen_random_uuid(), $1, $2, $3, true)",
        customer_id, account_no, ifsc,
    )


async def _seed_expected_remittance(conn, customer_id: str, utr: str, amount_minor: int) -> None:
    await conn.execute(
        "INSERT INTO expected_remittances (remittance_id, customer_id, utr_number, declared_amount_minor, currency, declared_date) "
        "VALUES (gen_random_uuid(), $1, $2, $3, 'INR', '2026-07-01')",
        customer_id, utr, amount_minor,
    )


async def _seed_invoice(conn, entity_id: str, customer_id: str, *, number: str, issue: str, due: str, total_minor: int, tds_rate_pct=None) -> str:
    row = await conn.fetchrow(
        "INSERT INTO invoices (invoice_id, entity_id, customer_id, invoice_number, issue_date, due_date, currency, "
        "total_amount_minor, total_home_minor, balance_due_minor, tds_rate_pct) "
        "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, 'INR', $6, $6, $6, $7) RETURNING invoice_id",
        entity_id, customer_id, number, date.fromisoformat(issue), date.fromisoformat(due), total_minor, tds_rate_pct,
    )
    return str(row["invoice_id"])


async def _seed_bank_txn(conn, entity_id: str, *, ref: str, payer: str, narration: str, amount_minor: int,
                          account_no=None, ifsc=None, explicit_fee_minor=0, is_bank_charge=False, txn_date="2026-07-01") -> str:
    row = await conn.fetchrow(
        "INSERT INTO bank_statements (bank_txn_id, entity_id, bank_reference, transaction_date, payer_name, "
        "payer_account_no, payer_ifsc, narration, currency, amount_minor, amount_home_minor, dr_cr, "
        "explicit_fee_minor, is_bank_charge, recon_status) "
        "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, 'INR', $8, $8, 'CREDIT', $9, $10, 'PENDING') "
        "RETURNING bank_txn_id",
        entity_id, ref, date.fromisoformat(txn_date), payer, account_no, ifsc, narration,
        amount_minor, explicit_fee_minor, is_bank_charge,
    )
    return str(row["bank_txn_id"])


@pytest.fixture
async def golden(conn):
    """Seeds the full golden dataset and returns {"entity_id", "customers": {code: customer_id}}."""
    entity_id = await _seed_entity(conn)
    cust = {}
    cust["acme"] = await _seed_customer(conn, entity_id, code="CUST-001", name="Acme Industries Pvt Ltd")
    cust["bright"] = await _seed_customer(conn, entity_id, code="CUST-002", name="Bright Textiles Pvt Ltd")
    cust["nimbus"] = await _seed_customer(conn, entity_id, code="CUST-003", name="Nimbus Traders", vpa="nimbus@okhdfc")
    cust["kestrel"] = await _seed_customer(conn, entity_id, code="KEST04", name="Kestrel Freight Co")
    cust["solace"] = await _seed_customer(conn, entity_id, code="CUST-005", name="Solace Pharma Ltd", gstin="27AASCS1234F1Z5")
    cust["vantage"] = await _seed_customer(conn, entity_id, code="CUST-006", name="Vantage Retail Solutions")
    cust["halcyon"] = await _seed_customer(conn, entity_id, code="CUST-007", name="Halcyon Foods")
    cust["meridian"] = await _seed_customer(conn, entity_id, code="CUST-008", name="Meridian Freight Lines")
    cust["silverline_t"] = await _seed_customer(conn, entity_id, code="CUST-009", name="Silverline Traders")
    cust["silverline_e"] = await _seed_customer(conn, entity_id, code="CUST-010", name="Silverline Exports")
    cust["coral"] = await _seed_customer(conn, entity_id, code="CORL12", name="Coral Living Pvt Ltd")

    await _seed_bank_account(conn, cust["bright"], "112233445566", "HDFC0001234")
    await _seed_bank_account(conn, cust["meridian"], "998877665544", "KKBK0005544")
    await _seed_bank_account(conn, cust["silverline_t"], "550011227788", "ICIC0007788")
    await _seed_bank_account(conn, cust["silverline_e"], "990022337788", "AXIS0009988")
    await _seed_expected_remittance(conn, cust["acme"], "UTR-ADV-7001", 1000000)

    inv = {}
    inv["101"] = await _seed_invoice(conn, entity_id, cust["acme"], number="INV-2026-101", issue="2026-07-01", due="2026-07-31", total_minor=1000000)
    inv["102"] = await _seed_invoice(conn, entity_id, cust["bright"], number="INV-2026-102", issue="2026-07-02", due="2026-08-01", total_minor=1500000, tds_rate_pct=10)
    inv["103"] = await _seed_invoice(conn, entity_id, cust["bright"], number="INV-2026-103", issue="2026-07-05", due="2026-08-04", total_minor=900000)
    inv["104"] = await _seed_invoice(conn, entity_id, cust["bright"], number="INV-2026-104", issue="2026-07-06", due="2026-08-05", total_minor=600000)
    inv["118"] = await _seed_invoice(conn, entity_id, cust["bright"], number="INV-2026-118", issue="2026-07-08", due="2026-08-07", total_minor=300000)
    inv["105"] = await _seed_invoice(conn, entity_id, cust["nimbus"], number="INV-2026-105", issue="2026-07-03", due="2026-08-02", total_minor=800000)
    inv["117"] = await _seed_invoice(conn, entity_id, cust["nimbus"], number="INV-2026-117", issue="2026-07-16", due="2026-08-15", total_minor=400000)
    inv["106"] = await _seed_invoice(conn, entity_id, cust["kestrel"], number="INV-2026-1046", issue="2026-07-04", due="2026-08-03", total_minor=1200000)
    inv["107"] = await _seed_invoice(conn, entity_id, cust["kestrel"], number="INV-2026-107", issue="2026-06-20", due="2026-07-20", total_minor=700000)
    inv["108"] = await _seed_invoice(conn, entity_id, cust["kestrel"], number="INV-2026-108", issue="2026-06-10", due="2026-07-10", total_minor=400000)
    inv["109"] = await _seed_invoice(conn, entity_id, cust["solace"], number="INV-2026-109", issue="2026-07-07", due="2026-08-06", total_minor=500000)
    inv["110"] = await _seed_invoice(conn, entity_id, cust["solace"], number="INV-2026-110", issue="2026-07-08", due="2026-08-07", total_minor=700000)
    inv["111"] = await _seed_invoice(conn, entity_id, cust["vantage"], number="INV-2026-111", issue="2026-07-09", due="2026-08-08", total_minor=900000)
    inv["112"] = await _seed_invoice(conn, entity_id, cust["halcyon"], number="INV-2026-112", issue="2026-07-10", due="2026-08-09", total_minor=600000)
    inv["113"] = await _seed_invoice(conn, entity_id, cust["meridian"], number="INV-2026-113", issue="2026-07-11", due="2026-08-10", total_minor=1100000)
    inv["114"] = await _seed_invoice(conn, entity_id, cust["silverline_t"], number="INV-2026-114", issue="2026-07-12", due="2026-08-11", total_minor=1800000)
    inv["115"] = await _seed_invoice(conn, entity_id, cust["silverline_e"], number="INV-2026-115", issue="2026-07-13", due="2026-08-12", total_minor=1800000)
    inv["120"] = await _seed_invoice(conn, entity_id, cust["coral"], number="INV-2026-120", issue="2026-07-14", due="2026-08-13", total_minor=450000)
    inv["121"] = await _seed_invoice(conn, entity_id, cust["coral"], number="INV-2026-121", issue="2026-07-14", due="2026-08-10", total_minor=450000)

    bank = {}
    bank["001"] = await _seed_bank_txn(conn, entity_id, ref="UTR-ADV-7001", payer="Acme Industries Pvt Ltd", narration="NEFT PAYMENT ACME", amount_minor=1000000, txn_date="2026-07-02")
    bank["002"] = await _seed_bank_txn(conn, entity_id, ref="NEFT-BT-002", payer="Bright Textiles Pvt Ltd", narration="NEFT SETTLEMENT", amount_minor=1350000, account_no="112233445566", ifsc="HDFC0001234", txn_date="2026-07-03")
    bank["003"] = await _seed_bank_txn(conn, entity_id, ref="NEFT-BT-003", payer="Bright Textiles Pvt Ltd", narration="OVERPAYMENT SETTLEMENT", amount_minor=950000, account_no="112233445566", ifsc="HDFC0001234", txn_date="2026-07-06")
    bank["004"] = await _seed_bank_txn(conn, entity_id, ref="NEFT-BT-004", payer="Bright Textiles Pvt Ltd", narration="FEE ADJUSTED PAYMENT", amount_minor=598000, account_no="112233445566", ifsc="HDFC0001234", explicit_fee_minor=2000, txn_date="2026-07-07")
    bank["014"] = await _seed_bank_txn(conn, entity_id, ref="NEFT-BT-014", payer="Bright Textiles Pvt Ltd", narration="WRITE OFF TEST PAYMENT", amount_minor=299800, account_no="112233445566", ifsc="HDFC0001234", txn_date="2026-07-08")
    bank["005"] = await _seed_bank_txn(conn, entity_id, ref="UTR-NIM-005", payer="Nimbus Traders", narration="UPI/nimbus@okhdfc/PAYMENT INV-2026-105", amount_minor=800000, txn_date="2026-07-04")
    bank["006"] = await _seed_bank_txn(conn, entity_id, ref="UTR-NIM-006", payer="Nimbus Traders", narration="UPI/nimbus@okhdfc/PARTIAL SETTLEMENT", amount_minor=250000, txn_date="2026-07-17")
    bank["007"] = await _seed_bank_txn(conn, entity_id, ref="NEFT-KF-007", payer="Kestrel Freight Co", narration="NEFT TRANSFER REF KEST04 INVC 1046", amount_minor=1200000, txn_date="2026-07-05")
    bank["008"] = await _seed_bank_txn(conn, entity_id, ref="NEFT-KF-008", payer="Kestrel Freight Co", narration="NEFT TRANSFER REF KEST04 SHORT PAY INV-2026-107", amount_minor=500000, txn_date="2026-06-21")
    bank["009"] = await _seed_bank_txn(conn, entity_id, ref="UTR-SOL-009", payer="Solace Pharma Ltd", narration="RTGS FROM SOLACE GSTIN 27AASCS1234F1Z5", amount_minor=1200000, txn_date="2026-07-09")
    bank["010"] = await _seed_bank_txn(conn, entity_id, ref="UTR-VAN-010", payer="Vantage Retail Solution", narration="NEFT PAYMENT VANTAGE", amount_minor=900000, txn_date="2026-07-10")
    bank["011"] = await _seed_bank_txn(conn, entity_id, ref="UTR-HAL-011", payer="XYZ Remitter Co", narration="NEFT TRANSFER HALCYON SETTLEMENT", amount_minor=600000, txn_date="2026-07-11")
    bank["012"] = await _seed_bank_txn(conn, entity_id, ref="UTR-MER-012", payer="Random Remitter Ltd", narration="GENERIC TRANSFER", amount_minor=1100000, account_no="334455665544", txn_date="2026-07-13")
    bank["013"] = await _seed_bank_txn(conn, entity_id, ref="UTR-SIL-013", payer="Silverline Remit Co", narration="GENERIC SETTLEMENT PAYMENT", amount_minor=1800000, account_no="112233447788", txn_date="2026-07-14")
    bank["020"] = await _seed_bank_txn(conn, entity_id, ref="UTR-COR-020", payer="Coral Living Pvt Ltd", narration="NEFT TRANSFER REF CORL12 PAYMENT", amount_minor=450000, txn_date="2026-07-15")
    bank["017"] = await _seed_bank_txn(conn, entity_id, ref="FEE-BANK-017", payer="Bank", narration="MONTHLY ACCOUNT MAINTENANCE FEE", amount_minor=50000, is_bank_charge=True, txn_date="2026-07-20")
    bank["018"] = await _seed_bank_txn(conn, entity_id, ref="UTR-UNK-018", payer="Unknown Remitter XYZ", narration="MISC TRANSFER UNRECOGNIZED", amount_minor=99900, txn_date="2026-07-21")
    bank["019"] = await _seed_bank_txn(conn, entity_id, ref="UTR-ADV-7001", payer="Acme Industries Pvt Ltd", narration="NEFT PAYMENT ACME", amount_minor=1000000, txn_date="2026-07-02")

    # "Invoice Number in Narration" cross-check cases - not part of the
    # original truebalance fixture, added for this rule specifically.
    # 021: locks to Nimbus via UPI (same signal as bank["005"]), but the
    # narration references INV-2026-102 - a real invoice, just Bright
    # Textiles' rather than Nimbus's. Should raise CUSTOMER_INVOICE_MISMATCH,
    # never lock to either customer.
    bank["021"] = await _seed_bank_txn(conn, entity_id, ref="UTR-NIM-021", payer="Nimbus Traders", narration="UPI/nimbus@okhdfc/PAYMENT INV-2026-102", amount_minor=500000, txn_date="2026-07-16")
    # 022: no Phase 1a rule fires (payer name doesn't fuzzy-match anyone, no
    # account info, no customer code in narration) and no pooling rule fires
    # either - but the narration references Solace's real INV-2026-109.
    # Should seed candidate_pool=[solace] rather than falling straight to a
    # suggestion-less Suspense.
    bank["022"] = await _seed_bank_txn(conn, entity_id, ref="UTR-XYZ-022", payer="Totally Unrelated Payer Co", narration="SETTLEMENT REF INV-2026-109", amount_minor=999999, txn_date="2026-07-18")

    return {"entity_id": entity_id, "customers": cust, "invoices": inv, "bank": bank}


async def _run_full_reconciliation(conn, entity_id: str) -> str:
    dao = ReconciliationDAO(conn)
    definition = await dao.insert_definition(entity_id=entity_id, name="Golden AR (pytest)", recon_type="AR", cadence=None, owner_user_id=None)
    await dao.insert_rules_bulk(definition["definition_id"], list(DEFAULT_AR_RULE_CATALOG))
    await dao.seed_gl_account_roles(entity_id)
    run = await dao.insert_run(definition_id=definition["definition_id"], run_no="RUN-PYTEST-GOLDEN", period_start=None, period_end=None)
    run_context = await dao.get_run_context(run["run_id"])
    await engine.run(conn, dao, run["run_id"], run_context)
    return run["run_id"]


async def _payment_for(conn, bank_txn_id: str) -> dict | None:
    row = await conn.fetchrow("SELECT * FROM payments WHERE bank_txn_id = $1", bank_txn_id)
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("candidate_pool"), str):
        try:
            d["candidate_pool"] = json.loads(d["candidate_pool"])
        except Exception:
            pass
    return d


import json


async def _exceptions_for(conn, bank_txn_id: str) -> list[dict]:
    rows = await conn.fetch("SELECT * FROM reconciliation_exceptions WHERE bank_txn_id = $1", bank_txn_id)
    res = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("detail"), str):
            try:
                d["detail"] = json.loads(d["detail"])
            except Exception:
                pass
        res.append(d)
    return res


async def _invoice(conn, invoice_id: str) -> dict:
    row = await conn.fetchrow("SELECT * FROM invoices WHERE invoice_id = $1", invoice_id)
    return dict(row)


class TestPhase1Identification:
    async def test_bright_textiles_locks_via_bank_account_all_four_rows(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        for key in ("002", "003", "004", "014"):
            payment = await _payment_for(conn, golden["bank"][key])
            assert str(payment["customer_id"]) == golden["customers"]["bright"], f"BANK-{key} should lock to Bright Textiles"

    async def test_nimbus_locks_via_vpa_both_rows(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        for key in ("005", "006"):
            payment = await _payment_for(conn, golden["bank"][key])
            assert str(payment["customer_id"]) == golden["customers"]["nimbus"]

    async def test_kestrel_locks_via_reference_code_both_rows(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        for key in ("007", "008"):
            payment = await _payment_for(conn, golden["bank"][key])
            assert str(payment["customer_id"]) == golden["customers"]["kestrel"]

    async def test_solace_locks_via_gstin(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        payment = await _payment_for(conn, golden["bank"]["009"])
        assert str(payment["customer_id"]) == golden["customers"]["solace"]

    async def test_vantage_locks_via_fuzzy_name(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        payment = await _payment_for(conn, golden["bank"]["010"])
        assert str(payment["customer_id"]) == golden["customers"]["vantage"]

    async def test_coral_locks_via_reference_code(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        payment = await _payment_for(conn, golden["bank"]["020"])
        assert str(payment["customer_id"]) == golden["customers"]["coral"]

    async def test_acme_flagged_duplicate_not_locked(self, conn, golden):
        """Known fixture issue (docs/reconciliation.md §8): BANK-001 and
        BANK-019 share a bank_reference, so dup-utr correctly rejects
        both rather than locking BANK-001 to Acme as the README describes."""
        await _run_full_reconciliation(conn, golden["entity_id"])
        for key in ("001", "019"):
            exceptions = await _exceptions_for(conn, golden["bank"][key])
            assert any(e["exception_type"] == "DUPLICATE" for e in exceptions)
            assert await _payment_for(conn, golden["bank"][key]) is None

    async def test_halcyon_and_meridian_pool_raises_suspense_not_auto_matched(self, conn, golden):
        """Matches the prototype exactly (index copy.html's exactAmountRule
        probe): a candidate pool that resolves to exactly one customer with a
        clean exact-amount hit is still only a *suggestion* - it must raise
        Suspense for a human to confirm, never auto-commit a match_group or
        lock the payment's customer_id, no matter how clean the hit looks."""
        await _run_full_reconciliation(conn, golden["entity_id"])

        halcyon_payment = await _payment_for(conn, golden["bank"]["011"])
        assert halcyon_payment["customer_id"] is None, "a pool resolution must never lock the payment's customer"
        halcyon_exceptions = await _exceptions_for(conn, golden["bank"]["011"])
        assert any(e["exception_type"] == "SUSPENSE" for e in halcyon_exceptions)
        halcyon_suspense = next(e for e in halcyon_exceptions if e["exception_type"] == "SUSPENSE")
        assert halcyon_suspense["detail"]["suggested_customer_id"] == golden["customers"]["halcyon"]
        assert halcyon_suspense["detail"]["suggested_invoice_ids"] == [golden["invoices"]["112"]]

        meridian_payment = await _payment_for(conn, golden["bank"]["012"])
        assert meridian_payment["customer_id"] is None
        meridian_exceptions = await _exceptions_for(conn, golden["bank"]["012"])
        assert any(e["exception_type"] == "SUSPENSE" for e in meridian_exceptions)
        meridian_suspense = next(e for e in meridian_exceptions if e["exception_type"] == "SUSPENSE")
        assert meridian_suspense["detail"]["suggested_customer_id"] == golden["customers"]["meridian"]

        inv112 = await _invoice(conn, golden["invoices"]["112"])
        inv113 = await _invoice(conn, golden["invoices"]["113"])
        assert inv112["status"] == "OPEN" and inv113["status"] == "OPEN", "the suggested invoices must stay untouched until confirmed"

    async def test_bank_018_suspense(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        payment = await _payment_for(conn, golden["bank"]["018"])
        assert payment is not None and payment["customer_id"] is None
        exceptions = await _exceptions_for(conn, golden["bank"]["018"])
        assert any(e["exception_type"] == "SUSPENSE" for e in exceptions)

    async def test_bank_017_is_bank_charge_excluded_entirely(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        assert await _payment_for(conn, golden["bank"]["017"]) is None
        assert await _exceptions_for(conn, golden["bank"]["017"]) == []


class TestPhase2Allocation:
    async def test_invoice_number_match_105(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        inv = await _invoice(conn, golden["invoices"]["105"])
        assert inv["status"] == "PAID" and inv["balance_due_minor"] == 0

    async def test_truncated_suffix_match_106(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        inv = await _invoice(conn, golden["invoices"]["106"])
        assert inv["status"] == "PAID" and inv["balance_due_minor"] == 0

    async def test_exact_balance_matches(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        # INV-101 is deliberately excluded here: Acme's own payment (BANK-001)
        # never locks at all (see test_acme_flagged_duplicate_not_locked), so
        # it's covered separately by test_acme_invoice_101_unpaid_due_to_duplicate_fixture_bug.
        # INV-112/113 (Halcyon/Meridian) are also excluded - BANK-011/012 only
        # ever reach a candidate *pool*, never a Phase 1a lock, so per
        # test_halcyon_and_meridian_pool_raises_suspense_not_auto_matched
        # they now correctly stay OPEN pending human confirmation instead of
        # auto-settling here.
        for key in ("111",):
            inv = await _invoice(conn, golden["invoices"][key])
            assert inv["status"] == "PAID" and inv["balance_due_minor"] == 0, f"INV-{key} should be exactly settled"

    async def test_acme_invoice_101_unpaid_due_to_duplicate_fixture_bug(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        inv = await _invoice(conn, golden["invoices"]["101"])
        assert inv["status"] == "OPEN" and inv["balance_due_minor"] == 1000000

    async def test_tds_net_match_102(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        inv = await _invoice(conn, golden["invoices"]["102"])
        assert inv["status"] == "PAID" and inv["balance_due_minor"] == 0

    async def test_subset_sum_109_110(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        inv109 = await _invoice(conn, golden["invoices"]["109"])
        inv110 = await _invoice(conn, golden["invoices"]["110"])
        assert inv109["status"] == "PAID" and inv110["status"] == "PAID"

    async def test_fee_tolerance_104(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        inv = await _invoice(conn, golden["invoices"]["104"])
        assert inv["status"] == "PAID" and inv["balance_due_minor"] == 0

    async def test_dust_writeoff_118(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        inv = await _invoice(conn, golden["invoices"]["118"])
        assert inv["status"] == "PAID" and inv["balance_due_minor"] == 0

    async def test_overpay_on_account_103(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        inv = await _invoice(conn, golden["invoices"]["103"])
        assert inv["status"] == "PAID" and inv["balance_due_minor"] == 0
        payment = await _payment_for(conn, golden["bank"]["003"])
        assert payment["unapplied_minor"] == 50000, "500 rupees excess should sit as on-account credit"

    async def test_universal_partial_pay_117(self, conn, golden):
        await _run_full_reconciliation(conn, golden["entity_id"])
        inv = await _invoice(conn, golden["invoices"]["117"])
        assert inv["status"] == "PARTIALLY_SETTLED" and inv["balance_due_minor"] == 150000

    async def test_short_pay_exceptions_107_and_117(self, conn, golden):
        """INV-107 is identified via exact-invoice-num (BANK-008's
        narration) but the payment (5000) falls 2000 short of its 7000
        balance; INV-117 is the universal-fallback partial payment. Both
        leave their invoice open with a remaining balance, so both raise
        SHORT_PAY (bank_txn_id-scoped, unlike NO_PAYMENT below)."""
        await _run_full_reconciliation(conn, golden["entity_id"])
        exc_107 = await _exceptions_for(conn, golden["bank"]["008"])
        assert any(e["exception_type"] == "SHORT_PAY" for e in exc_107)
        exc_117 = await _exceptions_for(conn, golden["bank"]["006"])
        assert any(e["exception_type"] == "SHORT_PAY" for e in exc_117)

    async def test_no_payment_108(self, conn, golden):
        """INV-108 (Kestrel) has no bank row referencing it at all, so it
        should stay OPEN and pick up a NO_PAYMENT exception from the
        end-of-run sweep - NO_PAYMENT has no bank_txn_id (nothing paid it),
        so it's looked up by invoice_id instead."""
        await _run_full_reconciliation(conn, golden["entity_id"])
        inv = await _invoice(conn, golden["invoices"]["108"])
        assert inv["status"] == "OPEN" and inv["balance_due_minor"] == 400000
        rows = await conn.fetch(
            "SELECT * FROM reconciliation_exceptions WHERE invoice_id = $1", golden["invoices"]["108"]
        )
        assert any(dict(r)["exception_type"] == "NO_PAYMENT" for r in rows)

    async def test_scoped_ambiguous_exception_coral(self, conn, golden):
        """BANK-020 pays exactly 4500, and Coral Living has two open
        invoices (INV-120, INV-121) that both have a 4500 balance - must not
        guess which one, per exact-amount's tie_break config."""
        await _run_full_reconciliation(conn, golden["entity_id"])
        exceptions = await _exceptions_for(conn, golden["bank"]["020"])
        assert any(e["exception_type"] == "MULTIPLE_INVOICE_MATCH" for e in exceptions)
        inv120 = await _invoice(conn, golden["invoices"]["120"])
        inv121 = await _invoice(conn, golden["invoices"]["121"])
        assert inv120["status"] == "OPEN" and inv121["status"] == "OPEN", "neither invoice should be touched"

    async def test_double_collision_silverline(self, conn, golden):
        """BANK-013 pools to both Silverline entities, and each has an open
        18000 invoice - both would produce a clean exact-balance match, so
        neither should be committed."""
        await _run_full_reconciliation(conn, golden["entity_id"])
        exceptions = await _exceptions_for(conn, golden["bank"]["013"])
        assert any(e["exception_type"] == "DOUBLE_COLLISION" for e in exceptions)
        payment = await _payment_for(conn, golden["bank"]["013"])
        assert payment["customer_id"] is None
        inv114 = await _invoice(conn, golden["invoices"]["114"])
        inv115 = await _invoice(conn, golden["invoices"]["115"])
        assert inv114["status"] == "OPEN" and inv115["status"] == "OPEN"


class TestPhase2AllocationOrdering:
    """Regression coverage for the rule-outer/payment-inner restructuring of
    run_phase_2: a low-priority catch-all rule (overpayment, priority 8) on
    an *earlier* payment must not be allowed to consume an invoice that a
    *later* payment's higher-priority, more specific rule (bank-fee,
    priority 6) needs - simply because it happened to be evaluated first.

    Deliberately a separate, minimal fixture rather than reusing `golden`:
    the shared fixture's Bright Textiles rows (BANK-002/003/004/014) don't
    actually exercise this bug, because BANK-002 correctly matches INV-102
    via the (higher-priority) TDS rule and so never touches the invoice
    BANK-004's fee-match needs - the starvation this test targets requires
    the *first* payment to have no exact/fee/TDS match of its own, only an
    overpayment-fallback one that happens to land on the same invoice a
    later payment needs precisely.
    """

    async def test_bank_fee_match_not_starved_by_earlier_overpayment(self, conn):
        entity_id = await _seed_entity(conn)
        customer_id = await _seed_customer(conn, entity_id, code="CUST-901", name="Ordering Test Co")

        inv_a = await _seed_invoice(conn, entity_id, customer_id, number="INV-A", issue="2026-07-01", due="2026-07-31", total_minor=600000)
        inv_b = await _seed_invoice(conn, entity_id, customer_id, number="INV-B", issue="2026-07-01", due="2026-07-31", total_minor=300000)

        # Processed first (transaction_date order): no exact/fee match against
        # either invoice - only overpayment's "closest fully-payable invoice"
        # fallback applies, and INV-A (excess 3500) is closer than INV-B
        # (excess 6500), so the old payment-outer design would let this one
        # grab INV-A immediately.
        payment1 = await _seed_bank_txn(
            conn, entity_id, ref="ORD-001", payer="Ordering Test Co", narration="GENERIC SETTLEMENT",
            amount_minor=950000, txn_date="2026-07-03",
        )
        # Processed second: designed to match INV-A *exactly* via the
        # bank-fee rule (balance 600000 - amount 598000 == fee 2000) - a
        # higher-priority rule than overpayment, so it should win INV-A
        # regardless of processing order.
        payment2 = await _seed_bank_txn(
            conn, entity_id, ref="ORD-002", payer="Ordering Test Co", narration="FEE ADJUSTED PAYMENT",
            amount_minor=598000, explicit_fee_minor=2000, txn_date="2026-07-06",
        )

        dao = ReconciliationDAO(conn)
        definition = await dao.insert_definition(entity_id=entity_id, name="Ordering Test (pytest)", recon_type="AR", cadence=None, owner_user_id=None)
        await dao.insert_rules_bulk(definition["definition_id"], list(DEFAULT_AR_RULE_CATALOG))
        await dao.seed_gl_account_roles(entity_id)
        run = await dao.insert_run(definition_id=definition["definition_id"], run_no="RUN-PYTEST-ORDERING", period_start=date(2026, 7, 1), period_end=date(2026, 7, 31))
        run_context = await dao.get_run_context(run["run_id"])
        await engine.run(conn, dao, run["run_id"], run_context)

        inv_a_after = await _invoice(conn, inv_a)
        inv_b_after = await _invoice(conn, inv_b)
        assert inv_a_after["status"] == "PAID" and inv_a_after["balance_due_minor"] == 0, "INV-A should be closed by payment2's exact bank-fee match, not payment1's overpayment fallback"
        assert inv_b_after["status"] == "PAID" and inv_b_after["balance_due_minor"] == 0, "INV-B should be closed by payment1's overpayment fallback, once INV-A is correctly out of the running"

        pay1 = await _payment_for(conn, payment1)
        pay2 = await _payment_for(conn, payment2)
        assert pay1["unapplied_minor"] == 650000, "payment1 should settle INV-B (300000) with 650000 excess on-account, not INV-A"
        assert pay2["unapplied_minor"] == 0, "payment2 should cleanly close INV-A via its fee match, no leftover"

        alloc2 = await conn.fetchrow("SELECT invoice_id FROM invoice_allocations WHERE payment_id = $1", pay2["payment_id"])
        assert str(alloc2["invoice_id"]) == inv_a, "payment2 must be the one that settled INV-A"


class TestNarrationInvoiceCrosscheck:
    """The "Invoice Number in Narration" cross-check (CUSTOMER_LOCK, kind
    invoice-number-in-narration): independently resolves which customer owns
    whatever invoice number the narration references (searched across every
    customer, not just whoever Phase 1a locks) and reconciles that against
    Phase 1a's own result."""

    async def test_agreement_is_recorded_not_just_silently_passed(self, conn, golden):
        """bank['005'] locks to Nimbus via UPI, and its narration references
        INV-2026-105 - Nimbus's own invoice. No conflict, so it should still
        lock and allocate exactly as before - but the cross-check having
        agreed should be recorded on the payment, not silently dropped."""
        await _run_full_reconciliation(conn, golden["entity_id"])
        payment = await _payment_for(conn, golden["bank"]["005"])
        assert str(payment["customer_id"]) == golden["customers"]["nimbus"]
        assert payment["narration_crosscheck_rule_id"] is not None, \
            "narration referenced the locked customer's own invoice - the cross-check should have confirmed it"
        rule = await conn.fetchrow(
            "SELECT kind FROM reconciliation_rules WHERE rule_id = $1", payment["narration_crosscheck_rule_id"]
        )
        assert rule["kind"] == "invoice-number-in-narration"
        # And the match itself is untouched - still a real, clean allocation.
        assert await _exceptions_for(conn, golden["bank"]["005"]) == []

    async def test_conflicting_narration_invoice_raises_exception_not_a_wrong_match(self, conn, golden):
        """bank['021'] locks to Nimbus via UPI - the exact same strong signal
        as bank['005'] - but its narration references INV-2026-102, which is
        Bright Textiles' invoice, not Nimbus's. Neither customer should get
        a committed match; a human has to resolve the disagreement."""
        await _run_full_reconciliation(conn, golden["entity_id"])
        payment = await _payment_for(conn, golden["bank"]["021"])
        assert payment is not None, "money is still tracked even though nobody locked"
        assert payment["customer_id"] is None
        assert payment["candidate_pool"] is None

        exceptions = await _exceptions_for(conn, golden["bank"]["021"])
        assert len(exceptions) == 1
        exc = exceptions[0]
        assert exc["exception_type"] == "CUSTOMER_INVOICE_MISMATCH"
        assert exc["detail"]["locked_customer_id"] == golden["customers"]["nimbus"]
        assert exc["detail"]["locked_via_rule"] == "upi"
        assert exc["detail"]["narration_customer_id"] == golden["customers"]["bright"]
        assert exc["detail"]["narration_invoice_id"] == golden["invoices"]["102"]
        assert exc["detail"]["narration_invoice_number"] == "INV-2026-102"

        # This payment itself never allocated to anything - not INV-2026-102
        # (Bright's, referenced in narration) and not any Nimbus invoice.
        alloc = await conn.fetchrow(
            "SELECT 1 FROM invoice_allocations WHERE payment_id = $1", payment["payment_id"]
        )
        assert alloc is None

    async def test_unresolved_lock_falls_back_to_narration_suggestion(self, conn, golden):
        """bank['022']: no Phase 1a rule fires (payer name matches nobody, no
        account info, no customer code in narration) and no Phase 1b pooling
        rule fires either - but the narration references Solace's real
        INV-2026-109. Should seed candidate_pool with Solace rather than
        falling straight to a suggestion-less Suspense."""
        await _run_full_reconciliation(conn, golden["entity_id"])
        payment = await _payment_for(conn, golden["bank"]["022"])
        assert payment["customer_id"] is None, "a narration hint alone must never auto-lock"
        assert payment["candidate_pool"] == [golden["customers"]["solace"]]

        # Pass A (Phase 2) still never auto-commits a pool, however clean -
        # this always lands in Suspense/Double-Collision for a human, same as
        # every other pooled payment.
        exceptions = await _exceptions_for(conn, golden["bank"]["022"])
        assert len(exceptions) == 1
        assert exceptions[0]["exception_type"] in ("SUSPENSE", "DOUBLE_COLLISION")
