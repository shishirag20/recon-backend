CREATE TABLE data_sources (
    source_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES entities(entity_id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CONNECTED'
);

CREATE TABLE ingestion_jobs (
    job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID REFERENCES data_sources(source_id),
    file_name TEXT,
    format TEXT,
    trigger_type TEXT NOT NULL DEFAULT 'MANUAL',
    row_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    started_by UUID REFERENCES users(id),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE field_mappings (
    mapping_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES data_sources(source_id),
    version INTEGER NOT NULL DEFAULT 1,
    source_field TEXT NOT NULL,
    canonical_field TEXT NOT NULL,
    transform TEXT NOT NULL DEFAULT 'NONE',
    transform_param TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE staging_records (
    staging_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES ingestion_jobs(job_id) ON DELETE CASCADE,
    stream TEXT NOT NULL,
    txn_date DATE,
    reference TEXT,
    counterparty TEXT,
    amount_minor BIGINT,
    amount_home_minor BIGINT,
    currency CHAR(3),
    dr_cr TEXT,
    raw JSONB NOT NULL,
    valid BOOLEAN NOT NULL DEFAULT TRUE,
    issues TEXT[]
);

CREATE INDEX idx_staging_job ON staging_records(job_id, stream);
CREATE INDEX idx_staging_raw ON staging_records USING GIN (raw);
