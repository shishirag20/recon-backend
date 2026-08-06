CREATE TABLE bank_statements (
    bank_txn_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES entities(entity_id),
    gl_account_id UUID REFERENCES gl_accounts(gl_account_id),
    source_job_id UUID REFERENCES ingestion_jobs(job_id),
    document_number TEXT,
    line_number INTEGER,
    bank_reference TEXT,
    transaction_date DATE NOT NULL,
    value_date DATE,
    fiscal_year INTEGER,
    fiscal_period SMALLINT,
    narration TEXT,
    payer_name TEXT,
    payer_account_no TEXT,
    payer_ifsc TEXT,
    currency CHAR(3) NOT NULL REFERENCES currencies(code),
    amount_minor BIGINT NOT NULL,
    amount_home_minor BIGINT NOT NULL,
    fx_rate NUMERIC(18,8),
    dr_cr TEXT NOT NULL,
    explicit_fee_minor BIGINT NOT NULL DEFAULT 0,
    is_bank_charge BOOLEAN NOT NULL DEFAULT FALSE,
    contra_reference TEXT,
    recon_status TEXT NOT NULL DEFAULT 'PENDING',
    gl_posted BOOLEAN NOT NULL DEFAULT FALSE,
    raw JSONB,
    UNIQUE (entity_id, document_number, line_number)
);

CREATE INDEX idx_bank_ref ON bank_statements(bank_reference);
CREATE INDEX idx_bank_status ON bank_statements(recon_status, transaction_date);
CREATE UNIQUE INDEX uniq_reconciled_ref ON bank_statements(bank_reference)
    WHERE recon_status = 'MATCHED' AND bank_reference IS NOT NULL;

CREATE TABLE payments (
    payment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bank_txn_id UUID NOT NULL UNIQUE REFERENCES bank_statements(bank_txn_id),
    customer_id UUID REFERENCES customers(customer_id),
    total_received_minor BIGINT NOT NULL,
    unapplied_minor BIGINT NOT NULL,
    locked_by_rule_id UUID REFERENCES reconciliation_rules(rule_id),
    candidate_pool JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE gateway_settlements (
    settlement_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES entities(entity_id),
    source_job_id UUID REFERENCES ingestion_jobs(job_id),
    gateway TEXT NOT NULL,
    gateway_transaction_id TEXT NOT NULL,
    customer_id UUID REFERENCES customers(customer_id),
    bank_txn_id UUID REFERENCES bank_statements(bank_txn_id),
    currency CHAR(3) NOT NULL REFERENCES currencies(code),
    gross_amount_minor BIGINT NOT NULL,
    fee_minor BIGINT NOT NULL DEFAULT 0,
    gst_on_fee_minor BIGINT NOT NULL DEFAULT 0,
    net_settled_minor BIGINT NOT NULL,
    settlement_date DATE NOT NULL,
    matched BOOLEAN NOT NULL DEFAULT FALSE,
    raw JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (gateway, gateway_transaction_id)
);
