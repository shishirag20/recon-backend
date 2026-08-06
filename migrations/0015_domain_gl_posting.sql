CREATE TABLE gl_journal_entries (
    journal_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES entities(entity_id),
    run_id UUID REFERENCES reconciliation_runs(run_id),
    posting_date DATE NOT NULL,
    source_type TEXT NOT NULL,
    memo TEXT,
    posted_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE gl_journal_lines (
    line_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    journal_id UUID NOT NULL REFERENCES gl_journal_entries(journal_id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL,
    gl_account_id UUID NOT NULL REFERENCES gl_accounts(gl_account_id),
    dr_cr TEXT NOT NULL,
    currency CHAR(3) NOT NULL REFERENCES currencies(code),
    amount_minor BIGINT NOT NULL,
    amount_home_minor BIGINT NOT NULL,
    business_partner_id UUID REFERENCES customers(customer_id),
    UNIQUE (journal_id, line_number)
);

CREATE TABLE gl_control_balances (
    balance_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gl_account_id UUID NOT NULL REFERENCES gl_accounts(gl_account_id),
    period_date DATE NOT NULL,
    control_balance_minor BIGINT NOT NULL,
    UNIQUE (gl_account_id, period_date)
);

ALTER TABLE invoice_allocations
    ADD CONSTRAINT fk_alloc_journal FOREIGN KEY (gl_journal_id) REFERENCES gl_journal_entries(journal_id);
