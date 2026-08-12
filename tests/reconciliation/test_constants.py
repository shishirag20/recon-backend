"""Pure unit tests for the default AR rule catalog's shape - no DB needed.

Catches data-authoring mistakes in constants.DEFAULT_AR_RULE_CATALOG (a typo'd
phase, a duplicate priority that would silently make first-match-wins
ambiguous, an out-of-range confidence) before they ever reach a real
definition via ReconciliationService.create_definition.
"""
from __future__ import annotations

from collections import defaultdict

from app.reconciliation.constants import DEFAULT_AR_RULE_CATALOG, RECON_PHASES


def test_every_rule_uses_a_known_phase():
    for phase, kind, name, priority, confidence, config in DEFAULT_AR_RULE_CATALOG:
        assert phase in RECON_PHASES, f"{kind!r} uses unknown phase {phase!r}"


def test_priorities_unique_within_each_phase():
    by_phase: dict[str, list[int]] = defaultdict(list)
    for phase, kind, name, priority, confidence, config in DEFAULT_AR_RULE_CATALOG:
        by_phase[phase].append(priority)
    for phase, priorities in by_phase.items():
        assert len(priorities) == len(set(priorities)), (
            f"duplicate priority in phase {phase!r} - first-match-wins ordering would be ambiguous"
        )


def test_confidence_in_range_when_set():
    for phase, kind, name, priority, confidence, config in DEFAULT_AR_RULE_CATALOG:
        if confidence is not None:
            assert 0 <= confidence <= 100, f"{kind!r} confidence {confidence!r} out of 0-100 range"


def test_kinds_are_unique():
    kinds = [kind for _, kind, *_ in DEFAULT_AR_RULE_CATALOG]
    assert len(kinds) == len(set(kinds)), "duplicate rule kind in the default catalog"


def test_every_rule_has_a_non_empty_config():
    for phase, kind, name, priority, confidence, config in DEFAULT_AR_RULE_CATALOG:
        assert isinstance(config, dict) and config, f"{kind!r} has an empty/missing config"
