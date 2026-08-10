-- Run after backfilling existing rows (see 0023's comment).
ALTER TABLE data_sources ALTER COLUMN stream SET NOT NULL;
