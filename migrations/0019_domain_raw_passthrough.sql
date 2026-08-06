-- Extends the raw-JSONB-passthrough pattern (already used by staging_records,
-- bank_statements, gateway_settlements) to the other tables that are populated
-- directly from ingestion, so a new source column never requires a migration
-- before it can be captured somewhere on the promoted row.
ALTER TABLE customers ADD COLUMN raw JSONB;
ALTER TABLE invoices ADD COLUMN raw JSONB;
ALTER TABLE credit_debit_memos ADD COLUMN raw JSONB;
ALTER TABLE expected_remittances ADD COLUMN raw JSONB;
