-- 0035_drop_bank_statements_gl_account_id.sql
-- bank_statements.gl_account_id was never populated by ingestion
-- (app/datahub/canonical.py::insert_bank_row) and never read by GL posting
-- (app/reconciliation/gl_posting.py posts every cash receipt for an entity
-- to the single per-entity CASH_CONTROL role account, not a per-row account).
ALTER TABLE bank_statements DROP COLUMN IF EXISTS gl_account_id;
