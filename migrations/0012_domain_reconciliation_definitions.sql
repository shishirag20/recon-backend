CREATE TABLE reconciliation_definitions (
    definition_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES entities(entity_id),
    name TEXT NOT NULL,
    recon_type TEXT NOT NULL,
    cadence TEXT,
    owner_user_id UUID REFERENCES users(id)
);

CREATE TABLE reconciliation_rules (
    rule_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    definition_id UUID NOT NULL REFERENCES reconciliation_definitions(definition_id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    priority INTEGER NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    confidence SMALLINT,
    config JSONB NOT NULL DEFAULT '{}',
    UNIQUE (definition_id, phase, priority)
);

CREATE TABLE reconciliation_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    definition_id UUID NOT NULL REFERENCES reconciliation_definitions(definition_id),
    run_no TEXT NOT NULL UNIQUE,
    period_start DATE,
    period_end DATE,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    volume INTEGER,
    matched_count INTEGER,
    exception_count INTEGER,
    matched_value_minor BIGINT,
    exception_value_minor BIGINT,
    unapplied_minor BIGINT,
    prepared_by UUID REFERENCES users(id),
    reviewed_by UUID REFERENCES users(id),
    signed_at TIMESTAMPTZ,
    run_hash TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_runs_status ON reconciliation_runs(status, period_end);
