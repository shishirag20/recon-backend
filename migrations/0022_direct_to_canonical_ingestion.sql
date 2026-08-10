-- Replaces the staging_records landing zone with direct-to-canonical
-- ingestion: a parsed row is written straight into bank_statements/invoices/
-- customers with its own valid/issues flags, instead of a generic
-- intermediate table that could never hold each stream's real shape anyway.
--
-- Rows that can't satisfy a canonical table's required (NOT NULL) columns
-- can't be inserted at all - those are recorded in ingestion_jobs.failed_rows
-- (diagnostic: visible with their raw content and the reason, but fixing one
-- means correcting the source and re-uploading, not an in-app row edit).

ALTER TABLE bank_statements
    ADD COLUMN valid BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN issues TEXT[];

ALTER TABLE invoices
    ADD COLUMN source_job_id UUID REFERENCES ingestion_jobs(job_id),
    ADD COLUMN valid BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN issues TEXT[];

ALTER TABLE customers
    ADD COLUMN source_job_id UUID REFERENCES ingestion_jobs(job_id),
    ADD COLUMN valid BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN issues TEXT[];

ALTER TABLE ingestion_jobs
    ADD COLUMN failed_rows JSONB;

DROP TABLE IF EXISTS staging_records;
