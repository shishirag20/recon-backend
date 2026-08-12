-- Fuzzy / token customer-name matching for the AR reconciliation engine.
--
-- Phase 1a Rule 1.6a (fuzzy company-name match > threshold) and Phase 1b
-- Rule 1.2b (token-based narration match) need trigram similarity over
-- customers.company_name. pg_trgm ships with Postgres and is confirmed
-- available in the target image; enable it and add a GIN trigram index so
-- `similarity(company_name, :probe)` / `company_name %% :probe` stay fast as
-- the customer master grows (the existing idx_customers_name is a full-text
-- GIN index, which does not accelerate trigram similarity).
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_customers_name_trgm
    ON customers USING gin (company_name gin_trgm_ops);
