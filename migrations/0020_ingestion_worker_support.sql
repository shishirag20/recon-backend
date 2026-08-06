-- Adds lease-based claiming support to ingestion_jobs so a pool of background
-- workers can compete for pending jobs via SELECT ... FOR UPDATE SKIP LOCKED,
-- with self-healing recovery if a worker dies mid-job (lease expiry) and
-- exponential-backoff retries up to max_attempts before dead-lettering to FAILED.

ALTER TABLE ingestion_jobs
    ALTER COLUMN status SET DEFAULT 'PENDING',
    ADD COLUMN locked_by TEXT,
    ADD COLUMN locked_at TIMESTAMPTZ,
    ADD COLUMN lease_expires_at TIMESTAMPTZ,
    ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 5,
    ADD COLUMN next_attempt_at TIMESTAMPTZ,
    ADD COLUMN last_error TEXT,
    ADD COLUMN file_uri TEXT;

CREATE INDEX idx_ingestion_jobs_claimable ON ingestion_jobs (status, next_attempt_at);
