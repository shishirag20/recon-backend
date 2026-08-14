"""Unit tests for app/reconciliation/rules/generic_functions.py - pure
logic, no DB. These functions aren't wired into any rule/dispatcher yet;
this just locks in their standalone behavior.
"""
from __future__ import annotations

from datetime import date

from app.reconciliation.rules.generic_functions import (
    calculate_dynamic_age,
    exact_value_matcher,
    fuzzy_string_matcher,
    regex_pattern_extractor,
    variance_tolerance_validator,
)


class TestRegexPatternExtractor:
    def test_known_pattern_shortcut(self):
        assert regex_pattern_extractor("UPI/nimbus@okhdfc/PAYMENT", "vpa") == ["nimbus@okhdfc"]

    def test_raw_regex_string(self):
        assert regex_pattern_extractor("REF KEST04 INVC 1046", r"\d{4,}") == ["1046"]

    def test_no_match_returns_empty_list(self):
        assert regex_pattern_extractor("nothing here", "gstin") == []

    def test_empty_text_returns_empty_list(self):
        assert regex_pattern_extractor("", "vpa") == []


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


class TestCalculateDynamicAge:
    def test_positive_age(self):
        assert calculate_dynamic_age(date(2026, 7, 1), as_of=date(2026, 8, 14)) == 44

    def test_zero_age(self):
        assert calculate_dynamic_age(date(2026, 8, 14), as_of=date(2026, 8, 14)) == 0

    def test_negative_age_for_future_date(self):
        assert calculate_dynamic_age(date(2026, 9, 1), as_of=date(2026, 8, 14)) == -18


class TestFuzzyStringMatcher:
    def test_similar_names_match(self):
        assert fuzzy_string_matcher("Acme Corp", "Acme Corp", min_similarity=0.8)

    def test_dissimilar_names_do_not_match(self):
        assert not fuzzy_string_matcher("Acme Corp", "Totally Different Ltd")

    def test_empty_input_never_matches(self):
        assert not fuzzy_string_matcher("", "Acme Corp")
        assert not fuzzy_string_matcher("Acme Corp", "")


class TestVarianceToleranceValidator:
    def test_within_absolute_tolerance(self):
        assert variance_tolerance_validator(998000, 1000000, tolerance=5000)

    def test_outside_absolute_tolerance(self):
        assert not variance_tolerance_validator(990000, 1000000, tolerance=5000)

    def test_within_percentage_tolerance(self):
        assert variance_tolerance_validator(998000, 1000000, tolerance=0.01, mode="percentage")

    def test_outside_percentage_tolerance(self):
        assert not variance_tolerance_validator(900000, 1000000, tolerance=0.01, mode="percentage")

    def test_percentage_mode_zero_reference_value(self):
        assert variance_tolerance_validator(0, 0, tolerance=0.01, mode="percentage")
        assert not variance_tolerance_validator(1, 0, tolerance=0.01, mode="percentage")
