-- 0034_customer_bank_account_field_mappings.sql
-- Seeds default field mappings for customer bank accounts & IFSC codes,
-- and backfills customer_bank_accounts from existing customers.raw payloads.

INSERT INTO field_mappings (mapping_id, source_field, canonical_field, transform, transform_param, is_active, stream)
VALUES
    (gen_random_uuid(), 'default_bank_account', 'bank_account_no', 'TRIM', NULL, true, 'CUSTOMER'),
    (gen_random_uuid(), 'bank_accounts', 'bank_account_no', 'TRIM', NULL, true, 'CUSTOMER'),
    (gen_random_uuid(), 'bank_account_no', 'bank_account_no', 'TRIM', NULL, true, 'CUSTOMER'),
    (gen_random_uuid(), 'bank_account_number', 'bank_account_no', 'TRIM', NULL, true, 'CUSTOMER'),
    (gen_random_uuid(), 'default_ifsc', 'ifsc_code', 'TRIM', NULL, true, 'CUSTOMER'),
    (gen_random_uuid(), 'ifsc_code', 'ifsc_code', 'TRIM', NULL, true, 'CUSTOMER'),
    (gen_random_uuid(), 'ifsc', 'ifsc_code', 'TRIM', NULL, true, 'CUSTOMER'),
    (gen_random_uuid(), 'expected_utr', 'expected_utr', 'TRIM', NULL, true, 'CUSTOMER')
ON CONFLICT DO NOTHING;

-- Backfill customer_bank_accounts for any existing customers that have bank account info in raw payload
INSERT INTO customer_bank_accounts (account_id, customer_id, bank_account_no, ifsc_code, is_primary, status)
SELECT
    gen_random_uuid(),
    c.customer_id,
    CASE 
        WHEN (c.raw->>'default_bank_account') ~* '^[0-9]+(\.[0-9]+)?e\+[0-9]+$' 
            THEN (c.raw->>'default_bank_account')::numeric::bigint::text
        WHEN (c.raw->>'bank_accounts') ~* '^[0-9]+(\.[0-9]+)?e\+[0-9]+$' 
            THEN (c.raw->>'bank_accounts')::numeric::bigint::text
        WHEN (c.raw->>'bank_account_no') ~* '^[0-9]+(\.[0-9]+)?e\+[0-9]+$' 
            THEN (c.raw->>'bank_account_no')::numeric::bigint::text
        ELSE COALESCE(
            NULLIF(TRIM(c.raw->>'default_bank_account'), ''),
            NULLIF(TRIM(c.raw->>'bank_accounts'), ''),
            NULLIF(TRIM(c.raw->>'bank_account_no'), '')
        )
    END AS bank_account_no,
    NULLIF(TRIM(UPPER(COALESCE(c.raw->>'default_ifsc', c.raw->>'ifsc_code', c.raw->>'ifsc'))), '') AS ifsc_code,
    true,
    'ACTIVE'
FROM customers c
WHERE COALESCE(
    NULLIF(TRIM(c.raw->>'default_bank_account'), ''),
    NULLIF(TRIM(c.raw->>'bank_accounts'), ''),
    NULLIF(TRIM(c.raw->>'bank_account_no'), '')
) IS NOT NULL
AND NOT EXISTS (
    SELECT 1 FROM customer_bank_accounts a WHERE a.customer_id = c.customer_id
);
