CREATE TABLE match_groups (
    match_group_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES reconciliation_runs(run_id) ON DELETE CASCADE,
    match_type TEXT NOT NULL,
    rule_id UUID REFERENCES reconciliation_rules(rule_id),
    confidence SMALLINT,
    status TEXT NOT NULL DEFAULT 'AUTO_MATCHED',
    reason TEXT,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE invoice_allocations (
    allocation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    match_group_id UUID NOT NULL REFERENCES match_groups(match_group_id) ON DELETE CASCADE,
    invoice_id UUID NOT NULL REFERENCES invoices(invoice_id),
    payment_id UUID NOT NULL REFERENCES payments(payment_id),
    bank_txn_id UUID REFERENCES bank_statements(bank_txn_id),
    allocated_minor BIGINT NOT NULL CHECK (allocated_minor > 0),
    gl_journal_id UUID,  -- FK added in 0015_domain_gl_posting.sql once gl_journal_entries exists
    allocated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (match_group_id, invoice_id, payment_id)
);

CREATE INDEX idx_alloc_invoice ON invoice_allocations(invoice_id);
CREATE INDEX idx_alloc_payment ON invoice_allocations(payment_id);
