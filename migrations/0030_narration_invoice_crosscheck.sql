-- Support for the "Invoice Number in Narration" cross-check: a Phase 1
-- (CUSTOMER_LOCK) catalog row that doesn't compete for priority like the
-- other six identification rules - it independently resolves which customer
-- owns whatever invoice number the narration references (searched across the
-- whole entity, not just the customer Phase 1a locks) and reconciles that
-- against Phase 1a's own answer. Recorded here only when it actually found a
-- narration-referenced invoice AND it agreed with the lock - a disagreement
-- raises a CUSTOMER_INVOICE_MISMATCH exception instead and never reaches a
-- committed payment, so this column stays NULL for that case (and for the
-- common case where the narration doesn't reference any invoice at all).
ALTER TABLE payments
    ADD COLUMN narration_crosscheck_rule_id UUID REFERENCES reconciliation_rules(rule_id);
