-- Make reconciliation_runs a lease-based work queue, mirroring the
-- ingestion_jobs pattern (migration 0020) so the reconciliation worker can
-- claim a queued run with `SELECT ... FOR UPDATE SKIP LOCKED`, heartbeat a
-- lease while it executes, and have a crashed worker's run self-heal once the
-- lease expires - the same self-coordinating, no-external-broker design the
-- ingestion worker already uses.
--
-- Run status lifecycle:
--   DRAFT -> QUEUED -> RUNNING -> COMPUTED -> APPROVED -> CLOSED
--   (RUNNING -> FAILED after max_attempts; QUEUED again on retry)
ALTER TABLE reconciliation_runs
    ADD COLUMN locked_by        TEXT,
    ADD COLUMN locked_at        TIMESTAMPTZ,
    ADD COLUMN lease_expires_at TIMESTAMPTZ,
    ADD COLUMN attempt_count    INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN max_attempts     INTEGER NOT NULL DEFAULT 3,
    ADD COLUMN next_attempt_at  TIMESTAMPTZ,
    ADD COLUMN last_error       TEXT;

-- The worker's claim query filters on (status, next_attempt_at); mirrors
-- idx_ingestion_jobs_claimable.
CREATE INDEX idx_recon_runs_claimable ON reconciliation_runs(status, next_attempt_at);
