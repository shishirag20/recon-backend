-- field_mappings moves from per-data-source to per-stream ownership: one
-- mapping set (BANK/INVOICE/CUSTOMER/LEDGER/GATEWAY) shared by every data
-- source/entity/org that ingests that stream, instead of every source
-- configuring its own copy of an identical mapping. Safe as a straight
-- backfill here - checked the live data first, and there is exactly one
-- data_source per stream today, so this is a lossless 1:1 move, not a merge.

ALTER TABLE field_mappings ADD COLUMN stream TEXT;

UPDATE field_mappings fm
SET stream = ds.stream
FROM data_sources ds
WHERE ds.source_id = fm.source_id;

ALTER TABLE field_mappings ALTER COLUMN stream SET NOT NULL;

ALTER TABLE field_mappings DROP COLUMN source_id;

-- Per-job visibility into raw columns that matched no synonym in the active
-- stream mapping (and that the AI-suggestion stub couldn't resolve either -
-- see app/datahub/ai_mapping.py). Nothing auto-resolves these yet; this is
-- just so they're not silently invisible.
ALTER TABLE ingestion_jobs ADD COLUMN unmapped_columns TEXT[];
