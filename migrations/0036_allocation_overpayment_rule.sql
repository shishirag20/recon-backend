-- 0036_allocation_overpayment_rule.sql
-- Backfills the standalone ALLOCATION `overpayment` rule (priority 6) onto
-- every existing AR definition's catalog. New definitions get it via
-- constants.DEFAULT_AR_RULE_CATALOG automatically.
--
-- Why: overpayment used to be folded into exact-amount's fallback, which ran
-- at a higher priority than subset-sum - "closest overpaid invoice" consumed
-- payments that summed exactly across 2+ invoices before the combined-match
-- rule ever saw them (subset-sum could never fire). It's a late-priority
-- standalone rule again so the settlement order is:
--   exact single invoice -> exact 2+ split (subset-sum) -> closest overpayment.
-- Priority must land between subset-sum (5) and partial-payment (9); 6 is
-- free in the standard catalog, with 7/8 as fallbacks if a custom rule
-- already occupies 6 (UNIQUE (definition_id, phase, priority)).

INSERT INTO reconciliation_rules
    (rule_id, definition_id, phase, kind, name, priority, enabled, confidence, config)
SELECT
    gen_random_uuid(),
    d.definition_id,
    'ALLOCATION',
    'overpayment',
    'Overpayment → On Account',
    COALESCE(
        (SELECT 6 WHERE NOT EXISTS (
            SELECT 1 FROM reconciliation_rules r
            WHERE r.definition_id = d.definition_id
              AND r.phase = 'ALLOCATION' AND r.priority = 6)),
        (SELECT 7 WHERE NOT EXISTS (
            SELECT 1 FROM reconciliation_rules r
            WHERE r.definition_id = d.definition_id
              AND r.phase = 'ALLOCATION' AND r.priority = 7)),
        8
    ),
    TRUE,
    100,
    '{"excess": "on_account", "selection": "closest", "description": "Runs only after exact single-invoice and combined (subset-sum) matching both failed: fully settles the one open invoice the payment comes closest to covering, leaving the excess as on-account credit for the customer."}'::jsonb
FROM reconciliation_definitions d
WHERE d.recon_type = 'AR'
  AND NOT EXISTS (
      SELECT 1 FROM reconciliation_rules r
      WHERE r.definition_id = d.definition_id AND r.kind = 'overpayment'
  )
  AND EXISTS (
      -- Only definitions that actually carry the AR allocation catalog.
      SELECT 1 FROM reconciliation_rules r
      WHERE r.definition_id = d.definition_id AND r.kind = 'exact-amount'
  );
