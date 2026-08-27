-- Tracks which Phase 1b (CANDIDATE_POOL) rule - or, when no pooling rule
-- fired, the NARRATION_CHECK cross-check's single-candidate fallback -
-- produced a payment's candidate_pool. Symmetric with locked_by_rule_id
-- (Phase 1a); the two are mutually exclusive on a given payment, same as
-- candidate_pool/locked_by_rule_id already are.
ALTER TABLE payments ADD COLUMN pooled_by_rule_id UUID REFERENCES reconciliation_rules(rule_id);
