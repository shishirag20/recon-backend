-- Four correctness fixes found in review:
-- 1. Duplicate-upload protection: a content hash per job (rejects re-uploading
--    the identical file) plus a per-row hash on bank_statements (rejects
--    byte-identical rows even from a different, partially-overlapping file -
--    document_number/line_number are always NULL on our CSV path, so the
--    existing unique constraint never engaged).
-- 2. (No schema change - transaction restructuring in the worker.)
-- 3. (No schema change - the fix reads entities.home_currency, which already exists.)
-- 4. Case-insensitive backstop on customer_code, on top of application-level
--    normalization, so a code path that forgets to normalize still can't
--    create a duplicate customer that only differs by case.

ALTER TABLE ingestion_jobs ADD COLUMN content_hash TEXT;
CREATE INDEX idx_ingestion_jobs_content_hash ON ingestion_jobs (source_id, content_hash);

ALTER TABLE bank_statements ADD COLUMN row_hash TEXT;
CREATE UNIQUE INDEX uniq_bank_statements_row_hash ON bank_statements (entity_id, row_hash) WHERE row_hash IS NOT NULL;

CREATE UNIQUE INDEX uniq_customers_code_ci ON customers (entity_id, upper(customer_code));
