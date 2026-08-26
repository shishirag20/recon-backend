-- Semantic GL-account resolution + richer exception context for the AR engine.
--
-- 1) gl_account_roles: the engine posts journal entries against *semantic*
--    accounts ("book this fee to Bank Charges", "credit the AR control
--    account") but gl_accounts only carries chart-of-accounts codes that vary
--    per entity. This table maps a fixed set of role codes the engine knows
--    about to whichever gl_account an entity actually uses for that role, so
--    gl_posting.py never hardcodes an account_code.
--
--    Role codes (validated in app code, TEXT here per the no-enum convention):
--      AR_CONTROL         - Accounts Receivable control account (credited on settle)
--      CASH_CONTROL       - Bank/cash clearing account (debited on receipt)
--      BANK_CHARGES       - Bank fees / minor-variance / write-off expense sink
--      TDS_RECEIVABLE     - Tax deducted at source withheld by the customer
--      WRITE_OFF          - Small-balance ("dust") write-off expense
--      ON_ACCOUNT_ADVANCE - Unapplied cash / customer advance liability
--      SUSPENSE           - Unidentified receipts parked pending investigation
--      FX_GAIN_LOSS       - Realized FX difference (reserved; multi-currency follow-up)
CREATE TABLE gl_account_roles (
    role_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    role_code     TEXT NOT NULL,
    gl_account_id UUID NOT NULL REFERENCES gl_accounts(gl_account_id),
    UNIQUE (entity_id, role_code)
);

-- 2) Exception context: a Double-Collision / Ambiguous-Match exception must
--    show the human every candidate it refused to choose between, and it helps
--    to link the exception back to the match group that produced it.
ALTER TABLE reconciliation_exceptions
    ADD COLUMN detail         JSONB,
    ADD COLUMN match_group_id UUID REFERENCES match_groups(match_group_id);
