"""Unit tests for the generic, config-driven matcher primitives
(app/reconciliation/rules/matchers.py) - pure logic, no DB needed except
for `trigram_similarity`, which is a real pg_trgm query.
"""
from __future__ import annotations

import pytest

from app.reconciliation.rules import RuleContext
from app.reconciliation.rules.matchers import (
    MATCHER_KINDS,
    MATCHER_REGISTRY,
    SOURCE_KINDS,
    extract_bank_value,
    find_matches,
)

def test_matcher_and_source_kinds_are_consistent():
    assert "trigram_similarity" in MATCHER_KINDS
    assert MATCHER_KINDS - {"trigram_similarity"} == set(MATCHER_REGISTRY)
    assert SOURCE_KINDS == {"customers", "customer_bank_accounts", "customer_reference_codes", "expected_remittances"}


class TestExtractBankValue:
    def test_direct_field(self):
        assert extract_bank_value({"narration": "PAYMENT REF 123"}, "narration") == "PAYMENT REF 123"

    def test_missing_direct_field(self):
        assert extract_bank_value({}, "narration") is None

    def test_extract_vpa(self):
        bank_txn = {"narration": "UPI/nimbus@okhdfc/PAYMENT INV-2026-105"}
        assert extract_bank_value(bank_txn, "extract:vpa") == "nimbus@okhdfc"

    def test_extract_gstin(self):
        bank_txn = {"narration": "RTGS FROM SOLACE GSTIN 27AASCS1234F1Z5"}
        assert extract_bank_value(bank_txn, "extract:gstin") == "27AASCS1234F1Z5"

    def test_extract_unknown_sentinel(self):
        assert extract_bank_value({"narration": "x"}, "extract:nonsense") is None


class TestMatcherPredicates:
    def test_exact_case_insensitive(self):
        assert MATCHER_REGISTRY["exact"]("UTR-ADV-7001", "utr-adv-7001", {})
        assert not MATCHER_REGISTRY["exact"]("UTR-ADV-7001", "UTR-ADV-7002", {})

    def test_substring(self):
        assert MATCHER_REGISTRY["substring"]("NEFT TRANSFER REF KEST04 INVC 1046", "KEST04", {})
        assert not MATCHER_REGISTRY["substring"]("NEFT TRANSFER", "KEST04", {})

    def test_numeric_suffix(self):
        assert MATCHER_REGISTRY["numeric_suffix"]("payer acct 998877665544", "112233445566", {"suffix_length": 4}) is False
        assert MATCHER_REGISTRY["numeric_suffix"]("payer acct 998877445566", "112233445566", {"suffix_length": 4})

    def test_token_overlap(self):
        assert MATCHER_REGISTRY["token_overlap"]("Halcyon Foods Pvt Ltd", "NEFT TRANSFER HALCYON SETTLEMENT", {})
        assert not MATCHER_REGISTRY["token_overlap"]("Halcyon Foods Pvt Ltd", "GENERIC TRANSFER", {})


class TestFindMatches:
    pytestmark = pytest.mark.asyncio

    def _ctx(self, conn, **overrides) -> RuleContext:
        base = dict(
            entity_id="e1", dao=None, conn=conn, customers=[], bank_accounts=[],
            reference_codes=[], expected_remittances=[],
        )
        base.update(overrides)
        return RuleContext(**base)

    async def test_missing_config_keys_returns_empty(self, conn):
        ctx = self._ctx(conn)
        assert await find_matches({"narration": "x"}, ctx, {"matcher": "exact"}) == []

    async def test_empty_bank_value_returns_empty(self, conn):
        ctx = self._ctx(conn, customers=[{"customer_id": "c1", "customer_code": "CUST-1"}])
        config = {"matcher": "substring", "bank_field": "narration", "source": "customers", "source_field": "customer_code"}
        assert await find_matches({}, ctx, config) == []

    async def test_exact_match_against_expected_remittances(self, conn):
        ctx = self._ctx(conn, expected_remittances=[{"customer_id": "c1", "utr_number": "UTR-ADV-7001"}])
        config = {"matcher": "exact", "bank_field": "bank_reference", "source": "expected_remittances", "source_field": "utr_number"}
        found = await find_matches({"bank_reference": "utr-adv-7001"}, ctx, config)
        assert [m["customer_id"] for m in found] == ["c1"]

    async def test_substring_match_against_customer_code(self, conn):
        ctx = self._ctx(conn, customers=[{"customer_id": "c1", "customer_code": "KEST04"}, {"customer_id": "c2", "customer_code": "OTHER"}])
        config = {"matcher": "substring", "bank_field": "narration", "source": "customers", "source_field": "customer_code"}
        found = await find_matches({"narration": "NEFT TRANSFER REF KEST04 INVC 1046"}, ctx, config)
        assert [m["customer_id"] for m in found] == ["c1"]

    async def test_multiple_matches_all_returned(self, conn):
        """find_matches collects every hit - identification.py's
        generic_field_match takes only the first (Phase 1a first-match-wins),
        pooling.py's generic_field_pool keeps all of them (Phase 1b)."""
        ctx = self._ctx(conn, bank_accounts=[
            {"customer_id": "c1", "bank_account_no": "112233445566"},
            {"customer_id": "c2", "bank_account_no": "998877445566"},
        ])
        config = {"matcher": "numeric_suffix", "bank_field": "narration", "source": "customer_bank_accounts", "source_field": "bank_account_no", "suffix_length": 4}
        found = await find_matches({"narration": "GENERIC TRANSFER 445566"}, ctx, config)
        assert {m["customer_id"] for m in found} == {"c1", "c2"}

    async def test_unknown_matcher_returns_empty(self, conn):
        ctx = self._ctx(conn, customers=[{"customer_id": "c1", "customer_code": "X"}])
        config = {"matcher": "does-not-exist", "bank_field": "narration", "source": "customers", "source_field": "customer_code"}
        assert await find_matches({"narration": "X"}, ctx, config) == []

    async def test_trigram_similarity_only_supports_customers_company_name(self, conn):
        ctx = self._ctx(conn)
        config = {"matcher": "trigram_similarity", "bank_field": "payer_name", "source": "customers", "source_field": "customer_code"}
        assert await find_matches({"payer_name": "Acme"}, ctx, config) == []
