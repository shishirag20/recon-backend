CREATE TABLE invoices (
    invoice_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES entities(entity_id),
    customer_id UUID NOT NULL REFERENCES customers(customer_id),
    invoice_number TEXT NOT NULL,
    issue_date DATE NOT NULL,
    due_date DATE NOT NULL,
    currency CHAR(3) NOT NULL REFERENCES currencies(code),
    total_amount_minor BIGINT NOT NULL,
    total_home_minor BIGINT NOT NULL,
    balance_due_minor BIGINT NOT NULL,
    tds_rate_pct NUMERIC(5,2),
    allowed_tds_minor BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'OPEN',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entity_id, invoice_number)
);

CREATE INDEX idx_invoices_open ON invoices(customer_id, status, due_date) WHERE status <> 'PAID';

CREATE TABLE credit_debit_memos (
    memo_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(customer_id),
    invoice_id UUID REFERENCES invoices(invoice_id),
    memo_type TEXT NOT NULL,
    memo_date DATE NOT NULL,
    currency CHAR(3) NOT NULL REFERENCES currencies(code),
    amount_minor BIGINT NOT NULL,
    amount_home_minor BIGINT NOT NULL,
    is_open BOOLEAN NOT NULL DEFAULT TRUE
);
