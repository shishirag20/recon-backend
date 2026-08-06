CREATE TABLE customers (
    customer_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES entities(entity_id),
    customer_code TEXT NOT NULL,
    company_name TEXT NOT NULL,
    pan TEXT,
    gstin TEXT,
    vpa_handle TEXT,
    payment_terms TEXT,
    credit_limit_minor BIGINT,
    city TEXT,
    state TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entity_id, customer_code)
);

CREATE INDEX idx_customers_gstin ON customers(gstin);
CREATE INDEX idx_customers_name ON customers USING GIN (to_tsvector('simple', company_name));

CREATE TABLE customer_bank_accounts (
    account_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
    bank_account_no TEXT NOT NULL,
    ifsc_code TEXT,
    alias TEXT,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
);

CREATE INDEX idx_cust_bank_acct ON customer_bank_accounts(bank_account_no, ifsc_code);

CREATE TABLE customer_reference_codes (
    reference_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
    code_type TEXT NOT NULL,
    code_value TEXT NOT NULL,
    match_priority SMALLINT NOT NULL DEFAULT 5,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX idx_cust_ref_code ON customer_reference_codes(code_value) WHERE is_active;

CREATE TABLE expected_remittances (
    remittance_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(customer_id),
    utr_number TEXT,
    declared_amount_minor BIGINT NOT NULL,
    currency CHAR(3) NOT NULL REFERENCES currencies(code),
    declared_date DATE,
    reconciled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
