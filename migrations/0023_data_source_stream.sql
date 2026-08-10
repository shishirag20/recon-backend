-- `stream` moves from "guessed by the frontend from a display name, resent on
-- every upload" to a real, immutable property of the data source itself. A
-- source only ever produces one kind of canonical data, so there's no
-- legitimate reason a client should be able to say otherwise per-upload.
-- Nullable for now - existing rows get backfilled by hand (their correct
-- value isn't derivable by any rule that belongs in a migration), then
-- 0024 enforces NOT NULL once that's done.
ALTER TABLE data_sources ADD COLUMN stream TEXT;
