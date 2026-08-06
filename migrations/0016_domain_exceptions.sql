CREATE TABLE reconciliation_exceptions (
    exception_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES reconciliation_runs(run_id) ON DELETE CASCADE,
    exception_no TEXT,
    exception_type TEXT NOT NULL,
    bank_txn_id UUID REFERENCES bank_statements(bank_txn_id),
    invoice_id UUID REFERENCES invoices(invoice_id),
    customer_id UUID REFERENCES customers(customer_id),
    discrepancy_minor BIGINT,
    reason_code TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN',
    resolution_outcome TEXT,
    resolver_id UUID REFERENCES users(id),
    resolution_notes TEXT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_exc_run ON reconciliation_exceptions(run_id, status, exception_type);
CREATE INDEX idx_exc_customer ON reconciliation_exceptions(customer_id);
