"""Unit tests for app/reconciliation/rules/generic_functions.py - pure
logic, no DB. These functions aren't wired into any rule/dispatcher yet;
this just locks in their standalone behavior.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.reconciliation.rules.generic_functions import (
    aggregate_sum,
    dynamic_age_calc,
    exact_value_matcher,
    execute_settlement,
    fifo_waterfall_allocator,
    filter_dataset,
    fuzzy_string_matcher,
    net_open_credits,
    pattern_extractor,
    relational_comparator,
    tolerance_validator,
)


class TestPatternExtractor:
    def test_known_pattern_shortcut(self):
        assert pattern_extractor("UPI/nimbus@okhdfc/PAYMENT", "vpa") == ["nimbus@okhdfc"]

    def test_raw_regex_string(self):
        assert pattern_extractor("REF KEST04 INVC 1046", r"\d{4,}") == ["1046"]

    def test_no_match_returns_empty_list(self):
        assert pattern_extractor("nothing here", "gstin") == []

    def test_empty_text_returns_empty_list(self):
        assert pattern_extractor("", "vpa") == []


class TestExactValueMatcher:
    def test_string_case_insensitive_by_default(self):
        assert exact_value_matcher("UTR-ADV-7001", "utr-adv-7001")

    def test_string_case_sensitive(self):
        assert not exact_value_matcher("UTR-ADV-7001", "utr-adv-7001", case_sensitive=True)

    def test_numeric_equality_across_types(self):
        assert exact_value_matcher(100, "100.0")
        assert exact_value_matcher(100, 100.0)

    def test_numeric_mismatch(self):
        assert not exact_value_matcher(100, 101)

    def test_none_never_matches(self):
        assert not exact_value_matcher(None, "x")
        assert not exact_value_matcher("x", None)


class TestDynamicAgeCalc:
    def test_positive_age(self):
        assert dynamic_age_calc(date(2026, 7, 1), as_of=date(2026, 8, 14)) == 44

    def test_zero_age(self):
        assert dynamic_age_calc(date(2026, 8, 14), as_of=date(2026, 8, 14)) == 0

    def test_negative_age_for_future_date(self):
        assert dynamic_age_calc(date(2026, 9, 1), as_of=date(2026, 8, 14)) == -18


class TestFuzzyStringMatcher:
    def test_similar_names_match(self):
        assert fuzzy_string_matcher("Acme Corp", "Acme Corporation", min_similarity=0.6)

    def test_dissimilar_names_do_not_match(self):
        assert not fuzzy_string_matcher("Acme Corp", "Totally Different Ltd")

    def test_empty_input_never_matches(self):
        assert not fuzzy_string_matcher("", "Acme Corp")
        assert not fuzzy_string_matcher("Acme Corp", "")


class TestToleranceValidator:
    def test_within_absolute_tolerance(self):
        assert tolerance_validator(998000, 1000000, tolerance=5000)

    def test_outside_absolute_tolerance(self):
        assert not tolerance_validator(990000, 1000000, tolerance=5000)

    def test_within_percentage_tolerance(self):
        assert tolerance_validator(998000, 1000000, tolerance=0.01, mode="percentage")

    def test_outside_percentage_tolerance(self):
        assert not tolerance_validator(900000, 1000000, tolerance=0.01, mode="percentage")

    def test_percentage_mode_zero_reference_value(self):
        assert tolerance_validator(0, 0, tolerance=0.01, mode="percentage")
        assert not tolerance_validator(1, 0, tolerance=0.01, mode="percentage")


class TestFilterDataset:
    RECORDS = [
        {"invoice_id": "1", "status": "OPEN", "balance_due_minor": 1000},
        {"invoice_id": "2", "status": "PAID", "balance_due_minor": 0},
        {"invoice_id": "3", "status": "PARTIALLY_SETTLED", "balance_due_minor": 500},
    ]

    def test_in_operator(self):
        result = filter_dataset(self.RECORDS, field="status", operator="in", value=["OPEN", "PARTIALLY_SETTLED"])
        assert {r["invoice_id"] for r in result} == {"1", "3"}

    def test_eq_operator(self):
        result = filter_dataset(self.RECORDS, field="status", operator="==", value="PAID")
        assert [r["invoice_id"] for r in result] == ["2"]

    def test_gt_operator(self):
        result = filter_dataset(self.RECORDS, field="balance_due_minor", operator=">", value=0)
        assert {r["invoice_id"] for r in result} == {"1", "3"}

    def test_unrecognized_operator_returns_empty(self):
        assert filter_dataset(self.RECORDS, field="status", operator="~=", value="OPEN") == []

    def test_missing_field_excluded(self):
        records = [{"a": 1}, {"b": 2}]
        assert filter_dataset(records, field="a", operator="==", value=1) == [{"a": 1}]


class TestNetOpenCredits:
    def test_credit_note_reduces_balance(self):
        memos = [{"memo_type": "CREDIT", "amount_minor": 10000}]
        assert net_open_credits(100000, memos) == Decimal(90000)

    def test_debit_note_increases_balance(self):
        memos = [{"memo_type": "DEBIT", "amount_minor": 5000}]
        assert net_open_credits(100000, memos) == Decimal(105000)

    def test_floored_at_zero(self):
        memos = [{"memo_type": "CREDIT", "amount_minor": 500000}]
        assert net_open_credits(100000, memos) == Decimal(0)

    def test_no_memos_unchanged(self):
        assert net_open_credits(100000, []) == Decimal(100000)


class TestAggregateSum:
    def test_sums_field_across_records(self):
        records = [{"balance_due_minor": 1000}, {"balance_due_minor": 2000}, {"balance_due_minor": 500}]
        assert aggregate_sum(records, "balance_due_minor") == Decimal(3500)

    def test_missing_field_contributes_zero(self):
        records = [{"balance_due_minor": 1000}, {"other": 1}]
        assert aggregate_sum(records, "balance_due_minor") == Decimal(1000)

    def test_empty_dataset(self):
        assert aggregate_sum([], "balance_due_minor") == Decimal(0)


class TestRelationalComparator:
    def test_numeric_operators(self):
        assert relational_comparator(75000, "<", 90000)
        assert relational_comparator(90000, "==", 90000)
        assert not relational_comparator(90000, ">", 90000)

    def test_membership_operators(self):
        assert relational_comparator("OPEN", "in", ["OPEN", "PARTIALLY_SETTLED"])
        assert not relational_comparator("PAID", "in", ["OPEN", "PARTIALLY_SETTLED"])

    def test_unrecognized_operator_returns_false(self):
        assert not relational_comparator(1, "~=", 1)


class TestFifoWaterfallAllocator:
    INVOICES = [
        {"invoice_id": "old-1", "balance_due_minor": 40000},
        {"invoice_id": "old-2", "balance_due_minor": 50000},
        {"invoice_id": "old-3", "balance_due_minor": 30000},
    ]

    def test_exact_fill_across_two_invoices(self):
        result = fifo_waterfall_allocator(90000, self.INVOICES)
        assert result == [
            {"invoice_id": "old-1", "allocated_minor": Decimal(40000)},
            {"invoice_id": "old-2", "allocated_minor": Decimal(50000)},
        ]

    def test_partial_fill_on_last_touched_invoice(self):
        result = fifo_waterfall_allocator(75000, self.INVOICES)
        assert result == [
            {"invoice_id": "old-1", "allocated_minor": Decimal(40000)},
            {"invoice_id": "old-2", "allocated_minor": Decimal(35000)},
        ]

    def test_zero_cash_allocates_nothing(self):
        assert fifo_waterfall_allocator(0, self.INVOICES) == []

    def test_zero_balance_invoice_skipped(self):
        invoices = [{"invoice_id": "paid", "balance_due_minor": 0}, {"invoice_id": "open", "balance_due_minor": 1000}]
        result = fifo_waterfall_allocator(1000, invoices)
        assert result == [{"invoice_id": "open", "allocated_minor": Decimal(1000)}]


class TestExecuteSettlement:
    def test_fully_settled_exact(self):
        result = execute_settlement(90000, 90000)
        assert result["status"] == "FULLY_SETTLED"
        assert result["excess_minor"] == Decimal(0)
        assert result["shortfall_minor"] == Decimal(0)

    def test_fully_settled_with_excess(self):
        result = execute_settlement(95000, 90000)
        assert result["status"] == "FULLY_SETTLED"
        assert result["excess_minor"] == Decimal(5000)

    def test_partially_settled(self):
        result = execute_settlement(70000, 90000)
        assert result["status"] == "PARTIALLY_SETTLED"
        assert result["shortfall_minor"] == Decimal(20000)

    def test_unsettled_zero_cash(self):
        result = execute_settlement(0, 90000)
        assert result["status"] == "UNSETTLED"
        assert result["shortfall_minor"] == Decimal(90000)
