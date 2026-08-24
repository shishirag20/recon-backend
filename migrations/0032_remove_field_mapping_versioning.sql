-- Remove the version/history concept from field_mappings entirely.
--
-- The version-and-is_active-swap design (migration 0011, refined in 0026)
-- turned out to have a real, observed problem under concurrent writers:
-- POST /field-mappings/{stream}/versions computed next_version = MAX(version)+1
-- and deactivated the old set as two separate steps, not one atomic
-- operation - two concurrent saves could interleave into a corrupted state
-- where rows from both submissions ended up simultaneously is_active=true
-- under the same version number. app/datahub/service.py's create_mapping_version
-- had also drifted into silently merging a submission with whatever was
-- currently active rather than truly replacing it (contradicting its own
-- endpoint's documented contract), which independently made it impossible
-- to ever remove a bad mapping row through the API.
--
-- field_mappings is now just the current mapping, full stop: no version
-- history, no audit trail of past mapping sets. A save (DAO save_mapping)
-- is a single DELETE + INSERT in one transaction - either fully applies or
-- fully rolls back, with no possible partial/merged intermediate state for
-- a concurrent reader or writer to observe.
ALTER TABLE field_mappings DROP COLUMN version;

-- ingestion_jobs.mapping_version existed purely as an audit pointer into
-- that version history ("which version was active when this job ran") -
-- meaningless once there's no history to point into.
ALTER TABLE ingestion_jobs DROP COLUMN mapping_version;
