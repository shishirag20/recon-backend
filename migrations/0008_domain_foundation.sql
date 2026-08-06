CREATE TABLE currencies (
    code CHAR(3) PRIMARY KEY,
    name TEXT NOT NULL,
    minor_unit SMALLINT NOT NULL DEFAULT 2
);

CREATE TABLE entities (
    entity_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    company_code TEXT NOT NULL,
    name TEXT NOT NULL,
    site_code TEXT,
    home_currency CHAR(3) NOT NULL DEFAULT 'INR' REFERENCES currencies(code),
    accounting_standard TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, company_code)
);

CREATE TABLE fx_rates (
    fx_rate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_ccy CHAR(3) NOT NULL REFERENCES currencies(code),
    to_ccy CHAR(3) NOT NULL REFERENCES currencies(code),
    rate_date DATE NOT NULL,
    rate NUMERIC(18,8) NOT NULL,
    rate_type TEXT,
    UNIQUE (from_ccy, to_ccy, rate_date, rate_type)
);

CREATE TABLE gl_accounts (
    gl_account_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES entities(entity_id),
    account_code TEXT NOT NULL,
    account_name TEXT NOT NULL,
    account_type TEXT,
    normal_balance TEXT,
    l1_group TEXT,
    l2_group TEXT,
    l3_group TEXT,
    is_control BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (entity_id, account_code)
);
