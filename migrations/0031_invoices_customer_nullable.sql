-- Allow an invoice to be ingested before its customer master has caught up.
--
-- Previously insert_invoice_row() rejected any row whose customer_code
-- didn't resolve to a real customers row (RowRejected, landing in the job's
-- failed_rows, never inserted). That's correct when the code is genuinely
-- wrong, but too strict when it's just a sequencing problem - the customer
-- hasn't been synced yet. customer_id is now nullable so that case can be
-- ingested instead of dropped; app/datahub/canonical.py's insert_invoice_row
-- stashes the original unresolved customer_code into `issues` so the trail
-- isn't lost once the typed lookup fails.
--
-- Reconciliation-side handling (app/reconciliation/engine.py) is a separate,
-- deliberate change: a NULL-customer invoice is excluded from the normal
-- per-customer allocation working set (it isn't "this customer's invoice"
-- to any payment yet) and tracked instead in its own unresolved pool for a
-- narration-based invoice-number match to resolve later, which also
-- backfills this column. See docs/reconciliation.md for that design.
ALTER TABLE invoices
    ALTER COLUMN customer_id DROP NOT NULL;
