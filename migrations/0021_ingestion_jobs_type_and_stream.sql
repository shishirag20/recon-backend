-- Extends ingestion_jobs to cover both stages of the data-hub pipeline on the
-- same lease-based queue: INGEST (parse an uploaded file into staging_records)
-- and PROMOTE (write a reviewed batch of staging_records into canonical
-- tables). The claim/heartbeat/complete/fail mechanics are already generic;
-- only the worker's dispatch needs to branch on job_type.
ALTER TABLE ingestion_jobs
    ADD COLUMN job_type TEXT NOT NULL DEFAULT 'INGEST',
    ADD COLUMN parent_job_id UUID REFERENCES ingestion_jobs(job_id),
    ADD COLUMN stream TEXT,
    ADD COLUMN mapping_version INTEGER;
