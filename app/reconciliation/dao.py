"""Data Access Object for the reconciliation module.

Raw SQL only (asyncpg), no ORM - same convention as app/datahub/dao.py.
Every function takes an `asyncpg.Connection`/`Pool`; every fetch result is
converted to a plain dict at this boundary (`_row`/`_rows`) so nothing above
this module touches the driver-specific asyncpg.Record type.

M0 scope: definitions, rules, GL account-role seeding, run queue CRUD.
M1 adds: loading a run's Phase-1a/1b working set (candidate bank inflows,
customer master, bank accounts, reference codes, expected remittances) and
writing `payments`/`reconciliation_exceptions`. M2 adds: loading a run's
Phase-2 working set (open invoices, open memos) and writing
`match_groups`/`invoice_allocations`, plus the balance-mutating updates
(`apply_invoice_allocation`, `apply_payment_allocation`) and retroactively
resolving a Phase-1b pool (`lock_payment_customer`). The queue claim/
heartbeat/complete/fail SQL lives in app/workers/reconciliation_worker.py
itself, not here - same split as app/workers/ingestion_worker.py's
`_CLAIM_SQL` etc., which aren't in DataHubDAO either. `gl_journal_entries`
writes land in M3 - see the milestone map in router.py.
"""

from __future__ import annotations

import uuid
from datetime import date

import asyncpg

from app.reconciliation.constants import GL_ROLE_DEFAULTS


def _row(record: asyncpg.Record | None) -> dict | None:
    return dict(record) if record is not None else None


def _rows(records: list[asyncpg.Record]) -> list[dict]:
    return [dict(r) for r in records]


class ReconciliationDAO:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    # -- entities (read-only sanity check, same pattern as DataHubDAO) ----------
    async def entity_exists(self, entity_id: str) -> bool:
        row = await self.conn.fetchrow(
            "SELECT 1 FROM entities WHERE entity_id = $1", entity_id
        )
        return row is not None

    # -- reconciliation_definitions ----------------------------------------------
    async def insert_definition(
        self,
        *,
        entity_id: str,
        name: str,
        recon_type: str,
        cadence: str | None,
        owner_user_id: str | None,
    ) -> dict:
        row = await self.conn.fetchrow(
            "INSERT INTO reconciliation_definitions (definition_id, entity_id, name, recon_type, cadence, owner_user_id) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5) "
            "RETURNING definition_id, entity_id, name, recon_type, cadence, owner_user_id",
            entity_id,
            name,
            recon_type,
            cadence,
            owner_user_id,
        )
        return _row(row)

    async def get_definition(self, definition_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT definition_id, entity_id, name, recon_type, cadence, owner_user_id "
            "FROM reconciliation_definitions WHERE definition_id = $1",
            definition_id,
        )
        return _row(row)

    async def list_definitions(self, *, entity_id: str | None) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT definition_id, entity_id, name, recon_type, cadence, owner_user_id "
            "FROM reconciliation_definitions WHERE ($1::uuid IS NULL OR entity_id = $1) ORDER BY name",
            entity_id,
        )
        return _rows(rows)

    # -- reconciliation_rules ------------------------------------------------------
    async def insert_rules_bulk(
        self, definition_id: str, rules: list[tuple]
    ) -> list[dict]:
        """`rules` is a list of (phase, kind, name, priority, confidence, config)
        tuples - see constants.DEFAULT_AR_RULE_CATALOG for the shape."""
        out = []
        for phase, kind, name, priority, confidence, config in rules:
            row = await self.conn.fetchrow(
                "INSERT INTO reconciliation_rules "
                "(rule_id, definition_id, phase, kind, name, priority, enabled, confidence, config) "
                "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, true, $6, $7::jsonb) "
                "RETURNING rule_id, definition_id, phase, kind, name, priority, enabled, confidence, config",
                definition_id,
                phase,
                kind,
                name,
                priority,
                confidence,
                config,
            )
            out.append(_row(row))
        return out

    async def insert_rule(
        self, definition_id: str, *, phase: str, kind: str, name: str, priority: int,
        confidence: int | None, config: dict,
    ) -> dict:
        """Adds one rule to an existing definition's catalog, post-creation
        - unlike insert_rules_bulk, which only ever runs once, at
        create_definition time. `(definition_id, phase, priority)` is
        UNIQUE at the DB level; a collision raises
        asyncpg.exceptions.UniqueViolationError, which the caller (service.py)
        turns into a 409."""
        row = await self.conn.fetchrow(
            "INSERT INTO reconciliation_rules "
            "(rule_id, definition_id, phase, kind, name, priority, enabled, confidence, config) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, true, $6, $7::jsonb) "
            "RETURNING rule_id, definition_id, phase, kind, name, priority, enabled, confidence, config",
            definition_id, phase, kind, name, priority, confidence, config,
        )
        return _row(row)

    async def list_rules(self, definition_id: str) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT rule_id, definition_id, phase, kind, name, priority, enabled, confidence, config "
            "FROM reconciliation_rules WHERE definition_id = $1 ORDER BY phase, priority",
            definition_id,
        )
        return _rows(rows)

    async def get_rule(self, rule_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT rule_id, definition_id, phase, kind, name, priority, enabled, confidence, config "
            "FROM reconciliation_rules WHERE rule_id = $1",
            rule_id,
        )
        return _row(row)

    async def update_rule(
        self, rule_id: str, *, enabled: bool | None, config: dict | None
    ) -> dict | None:
        row = await self.conn.fetchrow(
            "UPDATE reconciliation_rules SET enabled = COALESCE($2, enabled), config = COALESCE($3::jsonb, config) "
            "WHERE rule_id = $1 "
            "RETURNING rule_id, definition_id, phase, kind, name, priority, enabled, confidence, config",
            rule_id,
            enabled,
            config,
        )
        return _row(row)

    # -- gl_account_roles ----------------------------------------------------------
    async def seed_gl_account_roles(self, entity_id: str) -> list[dict]:
        """Idempotent: creates the baseline chart-of-accounts entry for every
        role in GL_ROLE_DEFAULTS (if the entity doesn't already have that
        account_code) and links it via gl_account_roles (if that role isn't
        already mapped for this entity). Safe to call repeatedly - e.g. once
        per definition creation - without duplicating rows."""
        out = []
        async with self.conn.transaction():
            for role_code, (
                account_code,
                account_name,
                account_type,
                normal_balance,
            ) in GL_ROLE_DEFAULTS.items():
                gl_account = await self.conn.fetchrow(
                    "INSERT INTO gl_accounts (gl_account_id, entity_id, account_code, account_name, account_type, normal_balance) "
                    "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5) "
                    "ON CONFLICT (entity_id, account_code) DO UPDATE SET account_code = EXCLUDED.account_code "
                    "RETURNING gl_account_id",
                    entity_id,
                    account_code,
                    account_name,
                    account_type,
                    normal_balance,
                )
                row = await self.conn.fetchrow(
                    "INSERT INTO gl_account_roles (role_id, entity_id, role_code, gl_account_id) "
                    "VALUES (gen_random_uuid(), $1, $2, $3) "
                    "ON CONFLICT (entity_id, role_code) DO UPDATE SET gl_account_id = EXCLUDED.gl_account_id "
                    "RETURNING role_id, entity_id, role_code, gl_account_id",
                    entity_id,
                    role_code,
                    gl_account["gl_account_id"],
                )
                out.append(_row(row))
        return out

    async def list_gl_account_roles(self, entity_id: str) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT r.role_id, r.entity_id, r.role_code, r.gl_account_id, a.account_code, a.account_name "
            "FROM gl_account_roles r JOIN gl_accounts a ON a.gl_account_id = r.gl_account_id "
            "WHERE r.entity_id = $1 ORDER BY r.role_code",
            entity_id,
        )
        return _rows(rows)

    async def get_gl_account_roles_map(self, entity_id: str) -> dict[str, str]:
        """`{role_code: gl_account_id}` for this entity - gl_posting.py never
        hardcodes an account_code, it always resolves through this map."""
        rows = await self.list_gl_account_roles(entity_id)
        return {r["role_code"]: str(r["gl_account_id"]) for r in rows}

    # -- reconciliation_runs (queue) ------------------------------------------------
    _RUN_COLUMNS = (
        "run_id, definition_id, run_no, period_start, period_end, status, volume, matched_count, "
        "exception_count, matched_value_minor, exception_value_minor, unapplied_minor, prepared_by, "
        "reviewed_by, signed_at, run_hash, attempt_count, max_attempts, last_error, started_at"
    )

    async def insert_run(
        self,
        *,
        definition_id: str,
        run_no: str,
        period_start: date | None,
        period_end: date | None,
    ) -> dict:
        row = await self.conn.fetchrow(
            f"INSERT INTO reconciliation_runs (run_id, definition_id, run_no, period_start, period_end, status) "
            f"VALUES (gen_random_uuid(), $1, $2, $3, $4, 'QUEUED') "
            f"RETURNING {self._RUN_COLUMNS}",
            definition_id,
            run_no,
            period_start,
            period_end,
        )
        return _row(row)

    # DEV-ONLY: wipes every run/match/exception/GL posting for a definition
    # and resets invoices/bank_statements back to their pre-reconciliation
    # state, so the same source data can be re-run from scratch while
    # iterating on rules/mappings. Not meant to survive past dev - grep
    # "DEV-ONLY" to find every piece of this if/when it's time to remove it.
    async def reset_definition(self, definition_id: str, entity_id: str) -> None:
        async with self.conn.transaction():
            await self.conn.execute(
                "DELETE FROM reconciliation_exceptions WHERE run_id IN "
                "(SELECT run_id FROM reconciliation_runs WHERE definition_id = $1)",
                definition_id,
            )
            # match_groups -> invoice_allocations cascades (ON DELETE CASCADE);
            # gl_journal_entries -> gl_journal_lines cascades too. Order here
            # only matters for invoice_allocations.gl_journal_id, a plain FK
            # (no cascade) - deleting match_groups first removes the
            # referencing rows before gl_journal_entries is touched.
            await self.conn.execute(
                "DELETE FROM match_groups WHERE run_id IN "
                "(SELECT run_id FROM reconciliation_runs WHERE definition_id = $1)",
                definition_id,
            )
            await self.conn.execute(
                "DELETE FROM gl_journal_entries WHERE run_id IN "
                "(SELECT run_id FROM reconciliation_runs WHERE definition_id = $1)",
                definition_id,
            )
            await self.conn.execute(
                "DELETE FROM payments WHERE bank_txn_id IN "
                "(SELECT bank_txn_id FROM bank_statements WHERE entity_id = $1)",
                entity_id,
            )
            await self.conn.execute(
                "DELETE FROM reconciliation_runs WHERE definition_id = $1", definition_id
            )
            await self.conn.execute(
                "UPDATE invoices SET balance_due_minor = total_amount_minor, status = 'OPEN' "
                "WHERE entity_id = $1",
                entity_id,
            )
            await self.conn.execute(
                "UPDATE bank_statements SET recon_status = 'PENDING', gl_posted = false "
                "WHERE entity_id = $1",
                entity_id,
            )

    async def get_run(self, run_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            f"SELECT {self._RUN_COLUMNS} FROM reconciliation_runs WHERE run_id = $1",
            run_id,
        )
        return _row(row)

    async def list_runs(
        self, *, definition_id: str | None, status: str | None
    ) -> list[dict]:
        rows = await self.conn.fetch(
            f"SELECT {self._RUN_COLUMNS} FROM reconciliation_runs "
            f"WHERE ($1::uuid IS NULL OR definition_id = $1) AND ($2::text IS NULL OR status = $2) "
            f"ORDER BY started_at DESC",
            definition_id,
            status,
        )
        return _rows(rows)

    async def retry_run(self, run_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            f"UPDATE reconciliation_runs SET status = 'QUEUED', attempt_count = 0, next_attempt_at = NULL, last_error = NULL "
            f"WHERE run_id = $1 AND status = 'FAILED' "
            f"RETURNING {self._RUN_COLUMNS}",
            run_id,
        )
        return _row(row)

    # -- run execution context (M1: Phase 1a/1b working set) -----------------------
    async def get_run_context(self, run_id: str) -> dict | None:
        """Joins through to the owning entity/definition - the engine needs
        `entity_id` to scope every other query and `period_end` for the
        Phase-2 period-cutoff guardrail (unused by Phase 1, loaded anyway
        since it's the same row)."""
        row = await self.conn.fetchrow(
            "SELECT r.run_id, r.definition_id, r.period_start, r.period_end, d.entity_id "
            "FROM reconciliation_runs r JOIN reconciliation_definitions d ON d.definition_id = r.definition_id "
            "WHERE r.run_id = $1",
            run_id,
        )
        return _row(row)

    async def list_candidate_bank_inflows(self, entity_id: str) -> list[dict]:
        """Unreconciled credit inflows for this entity - the rows Phase 1
        attempts to identify a paying customer for. Excludes `is_bank_charge`
        rows entirely; the engine routes those straight to GL posting (M3),
        never through customer identification. Includes `explicit_fee_minor`
        even though Phase 1 doesn't use it - Phase 2's fee-tolerance-match
        rule needs it and reuses this same row dict rather than re-querying."""
        rows = await self.conn.fetch(
            "SELECT bank_txn_id, transaction_date, bank_reference, narration, payer_name, "
            "payer_account_no, payer_ifsc, amount_minor, amount_home_minor, currency, explicit_fee_minor "
            "FROM bank_statements "
            "WHERE entity_id = $1 AND recon_status = 'PENDING' AND dr_cr = 'CREDIT' AND is_bank_charge = false "
            "ORDER BY transaction_date, bank_txn_id",
            entity_id,
        )
        return _rows(rows)

    async def load_customer_master(self, entity_id: str) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT customer_id, company_name, customer_code, pan, gstin, vpa_handle "
            "FROM customers WHERE entity_id = $1",
            entity_id,
        )
        return _rows(rows)

    async def load_customer_bank_accounts(self, entity_id: str) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT a.customer_id, a.bank_account_no, a.ifsc_code "
            "FROM customer_bank_accounts a JOIN customers c ON c.customer_id = a.customer_id "
            "WHERE c.entity_id = $1 AND a.status = 'ACTIVE'",
            entity_id,
        )
        return _rows(rows)

    async def load_customer_reference_codes(self, entity_id: str) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT r.customer_id, r.code_value, r.code_type "
            "FROM customer_reference_codes r JOIN customers c ON c.customer_id = r.customer_id "
            "WHERE c.entity_id = $1 AND r.is_active = true",
            entity_id,
        )
        return _rows(rows)

    async def load_expected_remittances(self, entity_id: str) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT e.customer_id, e.utr_number "
            "FROM expected_remittances e JOIN customers c ON c.customer_id = e.customer_id "
            "WHERE c.entity_id = $1 AND e.utr_number IS NOT NULL",
            entity_id,
        )
        return _rows(rows)

    async def bank_reference_already_matched(
        self, entity_id: str, bank_reference: str, exclude_bank_txn_id: str
    ) -> bool:
        """True if `bank_reference` belongs to another already-MATCHED row
        for this entity - the cross-run half of the duplicate-reference
        check. The within-run half (two PENDING rows in the same batch
        sharing a reference) is checked in Python by the engine, since the
        whole candidate set is already loaded in memory."""
        row = await self.conn.fetchrow(
            "SELECT 1 FROM bank_statements "
            "WHERE entity_id = $1 AND bank_reference = $2 AND recon_status = 'MATCHED' AND bank_txn_id != $3",
            entity_id,
            bank_reference,
            exclude_bank_txn_id,
        )
        return row is not None

    async def insert_payment(
        self,
        *,
        bank_txn_id: str,
        customer_id: str | None,
        total_received_minor: int,
        locked_by_rule_id: str | None,
        candidate_pool: list[str] | None,
        narration_crosscheck_rule_id: str | None = None,
    ) -> dict:
        """`unapplied_minor` starts equal to the full received amount -
        nothing's been allocated to an invoice yet; Phase 2 (M2) decrements
        it as allocations land. Exactly one of `locked_by_rule_id` (Phase 1a
        lock) or `candidate_pool` (Phase 1b pool) should be set; both NULL
        means Phase 1 found nothing at all (Suspense). `narration_crosscheck_rule_id`
        is set only when the "Invoice Number in Narration" cross-check found a
        narration-referenced invoice AND it agreed with `customer_id` - a
        disagreement raises CUSTOMER_INVOICE_MISMATCH instead and never calls
        this with a locked customer_id at all."""
        row = await self.conn.fetchrow(
            "INSERT INTO payments (payment_id, bank_txn_id, customer_id, total_received_minor, unapplied_minor, "
            "locked_by_rule_id, candidate_pool, narration_crosscheck_rule_id) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $3, $4, $5::jsonb, $6) "
            "RETURNING payment_id, bank_txn_id, customer_id, total_received_minor, unapplied_minor, "
            "locked_by_rule_id, candidate_pool, narration_crosscheck_rule_id",
            bank_txn_id,
            customer_id,
            total_received_minor,
            locked_by_rule_id,
            candidate_pool,
            narration_crosscheck_rule_id,
        )
        return _row(row)

    async def insert_exception(
        self,
        *,
        run_id: str,
        exception_type: str,
        bank_txn_id: str | None,
        customer_id: str | None,
        reason_code: str | None,
        detail: dict | None,
        match_group_id: str | None = None,
        invoice_id: str | None = None,
        discrepancy_minor: int | None = None,
    ) -> dict:
        row = await self.conn.fetchrow(
            "INSERT INTO reconciliation_exceptions "
            "(exception_id, run_id, exception_type, bank_txn_id, customer_id, invoice_id, discrepancy_minor, reason_code, status, detail, match_group_id) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, 'OPEN', $8::jsonb, $9) "
            "RETURNING exception_id, run_id, exception_type, bank_txn_id, customer_id, invoice_id, discrepancy_minor, reason_code, status, detail, match_group_id",
            run_id,
            exception_type,
            bank_txn_id,
            customer_id,
            invoice_id,
            discrepancy_minor,
            reason_code,
            detail,
            match_group_id,
        )
        return _row(row)

    async def mark_bank_statement_status(
        self, bank_txn_id: str, recon_status: str
    ) -> None:
        await self.conn.execute(
            "UPDATE bank_statements SET recon_status = $2 WHERE bank_txn_id = $1",
            bank_txn_id,
            recon_status,
        )

    # -- Phase 2 working set (open invoices, open memos) ----------------------------
    async def load_open_invoices(self, entity_id: str, period_end) -> list[dict]:
        """Every not-yet-PAID invoice for this entity issued at or before
        `period_end` - the 2.0a period-cutoff guardrail is baked into this
        query (it applies identically ahead of every allocation rule, so
        there's no reason to re-check it per rule). Filters on `issue_date`,
        not `due_date`: an invoice issued within the period is legitimately
        payable even if its due date falls later (e.g. Net-30 terms push
        `due_date` into the next month) - filtering on `due_date` would wrongly
        exclude every not-yet-due invoice from a run that's supposed to cover
        cash received *within* the period. Includes `tds_rate_pct` so
        tds-net-match can compute the effective TDS amount itself
        (`total_amount_minor * tds_rate_pct / 100`) rather than depending on
        a pre-populated `allowed_tds_minor` - the ingestion mapping has no
        way to derive that product today (see docs/reconciliation.md §8)."""
        rows = await self.conn.fetch(
            "SELECT invoice_id, customer_id, invoice_number, document_number, issue_date, due_date, "
            "total_amount_minor, balance_due_minor, allowed_tds_minor, tds_rate_pct, status "
            "FROM invoices WHERE entity_id = $1 AND status != 'PAID' "
            "AND ($2::date IS NULL OR issue_date <= $2) "
            "ORDER BY customer_id, due_date, invoice_id",
            entity_id,
            period_end,
        )
        return _rows(rows)

    async def load_open_memos(self, entity_id: str) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT m.memo_id, m.customer_id, m.invoice_id, m.memo_type, m.memo_date, m.amount_minor "
            "FROM credit_debit_memos m JOIN customers c ON c.customer_id = m.customer_id "
            "WHERE c.entity_id = $1 AND m.is_open = true",
            entity_id,
        )
        return _rows(rows)

    # -- match_groups / invoice_allocations (Phase 2 output) -------------------------
    async def insert_match_group(
        self,
        *,
        run_id: str,
        match_type: str,
        rule_id: str | None,
        confidence: int | None,
        status: str,
        reason: str,
    ) -> dict:
        row = await self.conn.fetchrow(
            "INSERT INTO match_groups (match_group_id, run_id, match_type, rule_id, confidence, status, reason) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6) "
            "RETURNING match_group_id, run_id, match_type, rule_id, confidence, status, reason",
            run_id,
            match_type,
            rule_id,
            confidence,
            status,
            reason,
        )
        return _row(row)

    async def insert_invoice_allocation(
        self,
        *,
        match_group_id: str,
        invoice_id: str,
        payment_id: str,
        bank_txn_id: str,
        allocated_minor: int,
    ) -> dict:
        row = await self.conn.fetchrow(
            "INSERT INTO invoice_allocations (allocation_id, match_group_id, invoice_id, payment_id, bank_txn_id, allocated_minor) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5) "
            "RETURNING allocation_id, match_group_id, invoice_id, payment_id, bank_txn_id, allocated_minor",
            match_group_id,
            invoice_id,
            payment_id,
            bank_txn_id,
            allocated_minor,
        )
        return _row(row)

    async def apply_invoice_allocation(
        self, invoice_id: str, amount_minor: int
    ) -> dict:
        """Decrements balance_due_minor and flips status: PAID once it
        reaches zero, PARTIALLY_SETTLED otherwise. `amount_minor` must
        already be capped at the invoice's remaining balance by the caller -
        this does not clamp."""
        row = await self.conn.fetchrow(
            "UPDATE invoices SET balance_due_minor = balance_due_minor - $2, "
            "status = CASE WHEN balance_due_minor - $2 <= 0 THEN 'PAID' ELSE 'PARTIALLY_SETTLED' END, "
            "updated_at = now() "
            "WHERE invoice_id = $1 "
            "RETURNING invoice_id, balance_due_minor, status",
            invoice_id,
            amount_minor,
        )
        return _row(row)

    async def link_invoice_customer(self, invoice_id: str, customer_id: str) -> dict:
        """Backfills customer_id on an invoice ingested without one
        (migration 0031), once a narration-based invoice-number match ties it
        to a real payment's already-identified customer. Only ever called
        against a row that's currently NULL there - see
        allocation.py's _promote_unresolved_invoice."""
        row = await self.conn.fetchrow(
            "UPDATE invoices SET customer_id = $2, updated_at = now() "
            "WHERE invoice_id = $1 "
            "RETURNING invoice_id, customer_id",
            invoice_id,
            customer_id,
        )
        return _row(row)

    async def apply_payment_allocation(
        self, payment_id: str, amount_minor: int
    ) -> dict:
        row = await self.conn.fetchrow(
            "UPDATE payments SET unapplied_minor = unapplied_minor - $2 "
            "WHERE payment_id = $1 "
            "RETURNING payment_id, unapplied_minor, customer_id",
            payment_id,
            amount_minor,
        )
        return _row(row)

    async def get_invoice(self, invoice_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT invoice_id, entity_id, customer_id, invoice_number, balance_due_minor, total_amount_minor, status "
            "FROM invoices WHERE invoice_id = $1",
            invoice_id,
        )
        return _row(row)

    async def get_payment(self, payment_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT payment_id, bank_txn_id, customer_id, total_received_minor, unapplied_minor FROM payments WHERE payment_id = $1",
            payment_id,
        )
        return _row(row)

    async def get_payment_by_bank_txn(self, bank_txn_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT payment_id, bank_txn_id, customer_id, total_received_minor, unapplied_minor FROM payments WHERE bank_txn_id = $1",
            bank_txn_id,
        )
        return _row(row)

    async def get_customer(self, customer_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT customer_id, entity_id, company_name FROM customers WHERE customer_id = $1",
            customer_id,
        )
        return _row(row)

    async def list_open_invoices_for_customer(self, customer_id: str) -> list[dict]:
        """The Suspense resolution panel's invoice picker, once a candidate
        customer is chosen (from the exception's own suggestion, its
        candidate pool, or picked manually) - every real balance still owed,
        for the reviewer to pick which one(s) this payment actually
        settles."""
        rows = await self.conn.fetch(
            "SELECT i.invoice_id, i.invoice_number, i.balance_due_minor, i.due_date, "
            "c.customer_id, c.company_name AS customer_name "
            "FROM invoices i JOIN customers c ON c.customer_id = i.customer_id "
            "WHERE i.customer_id = $1 AND i.balance_due_minor > 0 "
            "ORDER BY i.due_date",
            customer_id,
        )
        return _rows(rows)

    async def list_open_invoices_for_entity(self, entity_id: str, search: str | None, limit: int) -> list[dict]:
        """The Suspense resolution panel's "match to a different invoice"
        fallback - every open invoice across every customer, not scoped to
        one candidate (unlike list_open_invoices_for_customer above), so a
        reviewer can override a wrong/missing suggestion entirely. `search`
        matches invoice_number or company_name (mirrors the Data Explorer's
        own natural-key search - app/datahub/service.py's list_records)."""
        rows = await self.conn.fetch(
            "SELECT i.invoice_id, i.invoice_number, i.balance_due_minor, i.due_date, "
            "c.customer_id, c.company_name AS customer_name "
            "FROM invoices i JOIN customers c ON c.customer_id = i.customer_id "
            "WHERE i.entity_id = $1 AND i.balance_due_minor > 0 "
            "AND ($2::text IS NULL OR i.invoice_number ILIKE '%' || $2 || '%' OR c.company_name ILIKE '%' || $2 || '%') "
            "ORDER BY i.due_date LIMIT $3",
            entity_id, search, limit,
        )
        return _rows(rows)

    async def list_open_payments_for_entity(self, entity_id: str) -> list[dict]:
        """Payments with real leftover cash (unapplied_minor > 0) for this
        entity - the candidate pool a reviewer can manually match against an
        invoice from the No-Payment-Received resolution panel. Not scoped to
        one run - `payments` carries no run_id (see engine.py's
        payment_ledger_records docstring) - so a payment an earlier run left
        unapplied is still fair game today, same as the prototype's
        `availablePayments` (which isn't run-scoped either)."""
        rows = await self.conn.fetch(
            "SELECT p.payment_id, p.bank_txn_id, p.customer_id, p.total_received_minor, p.unapplied_minor, p.created_at, "
            "bs.bank_reference, COALESCE(c.company_name, bs.payer_name) AS customer_name "
            "FROM payments p "
            "JOIN bank_statements bs ON bs.bank_txn_id = p.bank_txn_id "
            "LEFT JOIN customers c ON c.customer_id = p.customer_id "
            "WHERE bs.entity_id = $1 AND p.unapplied_minor > 0 "
            "ORDER BY p.created_at",
            entity_id,
        )
        return _rows(rows)

    async def auto_resolve_suspense_for_payment(self, payment_id: str, note: str | None) -> None:
        """Cross-resolves a payment's own open Suspense exception once it
        gets manually matched from the invoice side - matches the
        prototype's "Automatically resolved via matched No Payment Received"
        behavior, so a reviewer doesn't have to separately dismiss both
        exceptions for what was really one decision."""
        await self.conn.execute(
            "UPDATE reconciliation_exceptions SET status = 'AUTO_RESOLVED', "
            "resolution_outcome = 'MANUAL_MATCH', "
            "resolution_notes = COALESCE(resolution_notes, $2), resolved_at = now() "
            "WHERE bank_txn_id = (SELECT bank_txn_id FROM payments WHERE payment_id = $1) "
            "AND exception_type = 'SUSPENSE' AND status = 'OPEN'",
            payment_id, note or "Automatically resolved via matched No Payment Received",
        )

    async def lock_payment_customer(
        self, payment_id: str, customer_id: str, rule_id: str | None
    ) -> None:
        """Retroactively resolves a Phase-1b pool once Phase 2 disambiguates
        it to exactly one candidate - the payment becomes indistinguishable
        from one Phase 1a locked directly."""
        await self.conn.execute(
            "UPDATE payments SET customer_id = $2, candidate_pool = NULL, locked_by_rule_id = $3 WHERE payment_id = $1",
            payment_id,
            customer_id,
            rule_id,
        )

    # -- M3: gl_journal_entries/gl_journal_lines/gl_control_balances -----------------
    async def insert_journal(
        self, *, entity_id: str, run_id: str | None, posting_date, source_type: str, memo: str | None,
        lines: list[dict],
    ) -> str:
        """Inserts one gl_journal_entries header plus every line in `lines`
        (each `{gl_account_id, dr_cr, currency, amount_minor, amount_home_minor, business_partner_id}`)
        in one transaction - a journal is never left with a header and no
        lines, or vice versa. Caller is responsible for `lines` actually
        balancing (sum(DEBIT) == sum(CREDIT)) - this does not check that."""
        async with self.conn.transaction():
            journal = await self.conn.fetchrow(
                "INSERT INTO gl_journal_entries (journal_id, entity_id, run_id, posting_date, source_type, memo) "
                "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5) RETURNING journal_id",
                entity_id, run_id, posting_date, source_type, memo,
            )
            journal_id = journal["journal_id"]
            for i, line in enumerate(lines, start=1):
                await self.conn.execute(
                    "INSERT INTO gl_journal_lines "
                    "(line_id, journal_id, line_number, gl_account_id, dr_cr, currency, amount_minor, amount_home_minor, business_partner_id) "
                    "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $6, $7)",
                    journal_id, i, line["gl_account_id"], line["dr_cr"], line["currency"],
                    line["amount_minor"], line.get("business_partner_id"),
                )
        return str(journal_id)

    async def link_allocation_journal(self, invoice_id: str, payment_id: str, journal_id: str) -> None:
        """Back-fills invoice_allocations.gl_journal_id once the JE that
        covers it has posted - lets a later query trace an allocation to its
        journal entry without re-deriving it."""
        await self.conn.execute(
            "UPDATE invoice_allocations SET gl_journal_id = $3 WHERE invoice_id = $1 AND payment_id = $2",
            invoice_id, payment_id, journal_id,
        )

    async def list_pending_bank_charges(self, entity_id: str) -> list[dict]:
        """`is_bank_charge=true` rows Phase 1 excluded entirely - M1/M2 never
        touch these; they get a direct-to-GL posting path instead of
        customer identification."""
        rows = await self.conn.fetch(
            "SELECT bank_txn_id, transaction_date, narration, amount_minor, currency "
            "FROM bank_statements WHERE entity_id = $1 AND is_bank_charge = true AND recon_status = 'PENDING'",
            entity_id,
        )
        return _rows(rows)

    async def sum_open_ar_balance(self, entity_id: str) -> int:
        """Current sub-ledger AR position - every not-yet-PAID invoice's
        balance, for the SL-vs-GL control proof. Not run-scoped: this is a
        point-in-time snapshot of the whole sub-ledger, not just what this
        run touched."""
        row = await self.conn.fetchrow(
            "SELECT COALESCE(SUM(balance_due_minor), 0)::bigint AS total FROM invoices WHERE entity_id = $1 AND status != 'PAID'",
            entity_id,
        )
        return row["total"]

    async def get_gl_control_balance(self, gl_account_id: str, period_date) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT balance_id, gl_account_id, period_date, control_balance_minor "
            "FROM gl_control_balances WHERE gl_account_id = $1 AND period_date = $2",
            gl_account_id, period_date,
        )
        return _row(row)

    # -- M3: match/exception review ---------------------------------------------------
    async def list_match_groups_for_run(self, run_id: str) -> list[dict]:
        """`m.rule_id` is the Phase-2 (ALLOCATION) rule that committed this
        match group. `locked_by_rule_id` is the separate Phase-1a
        (CUSTOMER_LOCK) rule that identified the payment's customer in the
        first place - pulled from `payments` via the group's first
        allocation, since every allocation in one match group shares the
        same payment. `narration_crosscheck_rule_id` is the "Invoice Number
        in Narration" cross-check, set only when it found a narration
        reference and confirmed the lock - a third, independent check, not
        just the two that "win" something. All three are surfaced together
        so the UI can show the full "how did this get matched" story, not
        just the pieces that produced the final match."""
        rows = await self.conn.fetch(
            "SELECT m.match_group_id, m.run_id, m.match_type, m.rule_id, m.confidence, m.status, m.reason, m.created_at, "
            "(SELECT p.locked_by_rule_id FROM payments p "
            "  JOIN invoice_allocations a2 ON a2.payment_id = p.payment_id "
            "  WHERE a2.match_group_id = m.match_group_id LIMIT 1) AS locked_by_rule_id, "
            "(SELECT p.narration_crosscheck_rule_id FROM payments p "
            "  JOIN invoice_allocations a2 ON a2.payment_id = p.payment_id "
            "  WHERE a2.match_group_id = m.match_group_id LIMIT 1) AS narration_crosscheck_rule_id, "
            "COALESCE(json_agg(json_build_object("
            "  'allocation_id', a.allocation_id, 'invoice_id', a.invoice_id, 'invoice_number', inv.invoice_number, "
            "  'invoice_amount_minor', inv.total_amount_minor, "
            "  'payment_id', a.payment_id, 'payment_amount_minor', p.total_received_minor, "
            "  'bank_txn_id', a.bank_txn_id, 'bank_reference', bs.bank_reference, "
            "  'allocated_minor', a.allocated_minor"
            ") ORDER BY a.allocated_at) FILTER (WHERE a.allocation_id IS NOT NULL), '[]') AS allocations "
            "FROM match_groups m "
            "LEFT JOIN invoice_allocations a ON a.match_group_id = m.match_group_id "
            "LEFT JOIN invoices inv ON inv.invoice_id = a.invoice_id "
            "LEFT JOIN payments p ON p.payment_id = a.payment_id "
            "LEFT JOIN bank_statements bs ON bs.bank_txn_id = a.bank_txn_id "
            "WHERE m.run_id = $1 GROUP BY m.match_group_id ORDER BY m.created_at",
            run_id,
        )
        return _rows(rows)

    async def list_exceptions_for_run(self, run_id: str, status: str | None) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT e.exception_id, e.run_id, e.exception_no, e.exception_type, e.bank_txn_id, e.invoice_id, e.customer_id, "
            "COALESCE("
            "  e.discrepancy_minor, "
            "  (e.detail->>'shortfall_minor')::bigint, "
            "  (e.detail->>'variance_minor')::bigint, "
            "  (e.detail->>'amount_minor')::bigint, "
            "  inv.balance_due_minor, "
            "  inv.total_amount_minor, "
            "  bs.amount_minor"
            ") AS discrepancy_minor, "
            "e.reason_code, e.status, e.resolution_outcome, e.resolver_id, e.resolution_notes, "
            "e.resolved_at, e.created_at, e.detail, e.match_group_id, "
            "COALESCE(c.company_name, bs.payer_name) AS customer_name, "
            "c.customer_code, "
            "inv.invoice_number, "
            "bs.bank_reference, "
            "bs.payer_name, "
            "bs.narration, "
            "COALESCE("
            "  e.discrepancy_minor, "
            "  (e.detail->>'shortfall_minor')::bigint, "
            "  (e.detail->>'variance_minor')::bigint, "
            "  (e.detail->>'amount_minor')::bigint, "
            "  inv.balance_due_minor, "
            "  inv.total_amount_minor, "
            "  bs.amount_minor"
            ") AS amount_minor "
            "FROM reconciliation_exceptions e "
            "LEFT JOIN customers c ON c.customer_id = e.customer_id "
            "LEFT JOIN invoices inv ON inv.invoice_id = e.invoice_id "
            "LEFT JOIN bank_statements bs ON bs.bank_txn_id = e.bank_txn_id "
            "WHERE e.run_id = $1 AND ($2::text IS NULL OR e.status = $2) "
            "ORDER BY e.created_at",
            run_id, status,
        )
        return _rows(rows)

    async def get_exception(self, exception_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT e.exception_id, e.run_id, e.exception_no, e.exception_type, e.bank_txn_id, e.invoice_id, e.customer_id, "
            "COALESCE("
            "  e.discrepancy_minor, "
            "  (e.detail->>'shortfall_minor')::bigint, "
            "  (e.detail->>'variance_minor')::bigint, "
            "  (e.detail->>'amount_minor')::bigint, "
            "  inv.balance_due_minor, "
            "  inv.total_amount_minor, "
            "  bs.amount_minor"
            ") AS discrepancy_minor, "
            "e.reason_code, e.status, e.resolution_outcome, e.resolver_id, e.resolution_notes, "
            "e.resolved_at, e.created_at, e.detail, e.match_group_id, "
            "COALESCE(c.company_name, bs.payer_name) AS customer_name, "
            "c.customer_code, "
            "inv.invoice_number, "
            "bs.bank_reference, "
            "bs.payer_name, "
            "bs.narration, "
            "COALESCE("
            "  e.discrepancy_minor, "
            "  (e.detail->>'shortfall_minor')::bigint, "
            "  (e.detail->>'variance_minor')::bigint, "
            "  (e.detail->>'amount_minor')::bigint, "
            "  inv.balance_due_minor, "
            "  inv.total_amount_minor, "
            "  bs.amount_minor"
            ") AS amount_minor "
            "FROM reconciliation_exceptions e "
            "LEFT JOIN customers c ON c.customer_id = e.customer_id "
            "LEFT JOIN invoices inv ON inv.invoice_id = e.invoice_id "
            "LEFT JOIN bank_statements bs ON bs.bank_txn_id = e.bank_txn_id "
            "WHERE e.exception_id = $1",
            exception_id,
        )
        return _row(row)

    async def update_exception(
        self, exception_id: str, *, status: str | None, resolution_outcome: str | None,
        resolution_notes: str | None, resolver_id: str | None, match_group_id: str | None = None,
    ) -> dict | None:
        """`resolved_at` is stamped automatically the moment `status` moves
        away from `OPEN`/`INVESTIGATING` - the caller doesn't set it directly.
        `match_group_id` links the exception to the match that resolved it
        (e.g. a manual No-Payment-Received match) - optional, COALESCE'd like
        every other field here."""
        row = await self.conn.fetchrow(
            "UPDATE reconciliation_exceptions SET "
            "status = COALESCE($2, status), "
            "resolution_outcome = COALESCE($3, resolution_outcome), "
            "resolution_notes = COALESCE($4, resolution_notes), "
            "resolver_id = COALESCE($5, resolver_id), "
            "match_group_id = COALESCE($6, match_group_id), "
            "resolved_at = CASE WHEN $2 IS NOT NULL AND $2 NOT IN ('OPEN', 'INVESTIGATING') THEN now() ELSE resolved_at END "
            "WHERE exception_id = $1 "
            "RETURNING exception_id, run_id, exception_no, exception_type, bank_txn_id, invoice_id, customer_id, "
            "discrepancy_minor, reason_code, status, resolution_outcome, resolver_id, resolution_notes, "
            "resolved_at, created_at, detail, match_group_id",
            exception_id, status, resolution_outcome, resolution_notes, resolver_id, match_group_id,
        )
        return _row(row)


def new_id() -> str:
    return str(uuid.uuid4())


def new_run_no() -> str:
    """Human-readable, collision-resistant (no counting query needed under
    concurrent inserts) - reconciliation_runs.run_no is UNIQUE."""
    return f"RUN-{date.today():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
