CREATE TABLE immutable_audit_trail (
    audit_id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id UUID REFERENCES reconciliation_runs(run_id),
    entry_type TEXT NOT NULL,
    category TEXT NOT NULL,
    action TEXT NOT NULL,
    user_id UUID REFERENCES users(id),
    target_ref TEXT,
    impact_minor BIGINT,
    entity_ref TEXT,
    old_state JSONB,
    new_state JSONB,
    prev_hash TEXT,
    row_hash TEXT
);

CREATE INDEX idx_audit_run ON immutable_audit_trail(run_id, at DESC);
CREATE INDEX idx_audit_user ON immutable_audit_trail(user_id, at DESC);

CREATE TABLE documents (
    document_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_name TEXT NOT NULL,
    byte_size BIGINT,
    mime_type TEXT,
    category TEXT,
    storage_uri TEXT,
    linked_type TEXT,
    linked_id UUID,
    uploaded_by UUID REFERENCES users(id),
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
