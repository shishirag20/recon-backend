# AR Reconciliation Engine — Implementation Plan Reference

This expands the approved plan ("Production-Ready AR Reconciliation Engine", `/Users/pro/.claude/plans/tidy-stirring-whisper.md`) into a field-by-field reference: every table touched, every new/changed column, every new module, and current build status. The plan itself stays the source of truth for *why*; this doc exists because the plan is terse about *what*, specifically.

## Contents

1. [Status by milestone](#1-status-by-milestone)
2. [What each milestone actually implements](#2-what-each-milestone-actually-implements)
3. [Database schema — new and changed](#3-database-schema--new-and-changed)
4. [Database schema — reused as-is (read/write reference)](#4-database-schema--reused-as-is)
5. [New application modules](#5-new-application-modules)
6. [The default AR rule catalog](#6-the-default-ar-rule-catalog)
7. [API surface](#7-api-surface)
8. [Known gaps found during M0/M1 build](#8-known-gaps-found-during-m0m1-build)

---

## 1. Status by milestone

| Milestone | Scope | Status |
|---|---|---|
| **M0** | Schema scaffold, `app/reconciliation/` skeleton, GL role seeding, pytest harness | ✅ Done |
| **M1** | Phase 1a (customer lock) + 1b (candidate pool), `payments`, Suspense routing, `reconciliation_worker` | ✅ Done, verified against golden test data |
| **M2** | Phase 2 scoped allocation (9 rules), `match_groups`/`invoice_allocations`, invoice balance/status updates | ✅ Done, verified against golden test data |
| **M3** | GL posting, SL-vs-GL control proof, standalone bank-charge + double-collision handling, exception resolve API | ⬜ Not started |
| **M4** | Sign-off, hash-chained audit trail, Rules Studio read/update API completeness, golden test fully green | ⬜ Not started |

---

## 2. What each milestone actually implements

The table above says *what scope* each milestone covers. This section says *what actually happens* — the mechanics, step by step, for a real bank inflow moving through the system.

### M0 — Schema & scaffold (done)

No matching logic at all. This milestone's job was purely to make M1-M4 possible without replumbing later: the three migrations add the pieces the schema was missing (a trigram index for fuzzy name matching, lease/retry columns on `reconciliation_runs` so it can be a work queue, and `gl_account_roles` so the engine never hardcodes a chart-of-accounts code). `app/reconciliation/` gets the full `router → service → dao` skeleton, but only the parts that don't require any matching intelligence are real: create/list a definition, list/update a rule, enqueue/get/retry a run. Creating an `AR` definition auto-seeds the 20-rule default catalog (§6) and the entity's 8 GL role accounts in the same call — deliberately, so a definition is never left half-configured. Nothing in M0 reads a bank statement or a customer.

### M1 — Customer identification (done)

**Goal**: for every inbound bank credit, figure out *who* paid — not yet *what invoice* they paid for.

1. `reconciliation_worker` polls `reconciliation_runs` for `QUEUED` rows and claims one (`SELECT ... FOR UPDATE SKIP LOCKED` + a 5-minute lease, identical mechanics to `ingestion_worker`).
2. `engine.run_phase_1` loads the entire Phase-1 "working set" once, into memory: every unreconciled credit inflow for the entity (`bank_statements` where `recon_status='PENDING' AND dr_cr='CREDIT' AND is_bank_charge=false`), plus the full customer master, every `customer_bank_accounts` row, every `customer_reference_codes` row, every `expected_remittances` row. This entity's data is small (tens of customers), so matching in Python against an in-memory set is simpler and faster than a query per rule per row.
3. It computes which `bank_reference` values appear more than once in this batch — the within-run half of duplicate detection (the cross-run half is a live query: "does this reference already belong to a row that's `recon_status='MATCHED'` from a *previous* run?").
4. For each candidate bank row, in order:
   - **Phase 1a (`CUSTOMER_LOCK`)** — try each enabled rule in ascending `priority`, stop at the first one that fires. `dup-utr-check` runs first: if the reference collides (this run or a prior one), the row gets rejected outright — a `DUPLICATE` exception, no `payments` row at all, `bank_statements.recon_status → EXCEPTION` — and none of the other 1a rules even run for it. Otherwise `utr-match` → `bank-account-match` → `vpa-match` → `reference-code-match` → `gstin-pan-match` → `fuzzy-name-match` try in turn; the first exact (or, for the last one, high-similarity) hit **locks** the row: one `payments` row is inserted with `customer_id` set and `locked_by_rule_id` recording which rule won.
   - **Phase 1b (`CANDIDATE_POOL`)** — only reached if every 1a rule missed. `masked-account-pool` then `token-pool` try in turn, first-match-wins on *whether a rule finds anything*, but the rule that fires returns *every* customer it matches, not just one — so it can genuinely return two candidates (e.g. two customers sharing the same last-4-digit account suffix). This always writes `candidate_pool` (a JSON array of customer IDs) and leaves `customer_id` `NULL`; Phase 1b never locks, by design.
   - **Suspense** — if neither phase found anything at all, a `payments` row is still created (so the money is tracked even though its owner isn't known) with both `customer_id` and `candidate_pool` `NULL`, plus a `SUSPENSE` exception.
   - Rows with `is_bank_charge=true` never enter this loop at all — they're excluded at the working-set query itself, reserved for M3's direct-to-GL posting path.
5. When the batch finishes, the worker writes `volume`/`matched_count`/`exception_count`/`matched_value_minor`/`exception_value_minor`/`unapplied_minor` back onto the run and marks it `COMPUTED`.

**State at the end of M1**: every inflow is locked, pooled, or flagged — but `unapplied_minor` equals the *full* received amount for every payment, because nothing has been matched to an invoice yet. That's M2's job.

### M2 — Scoped invoice allocation (not built)

**Goal**: for every locked (and eventually resolved-pool) payment, decide which specific invoice(s) it pays, and record that as real, decrementing balances.

1. Two guardrails run before any matching rule, narrowing the invoice universe: `period-cutoff-guard` drops invoices whose `issue_date` falls after the run's `period_end` (an invoice issued within the period is payable even if its due date falls later - e.g. Net-30 terms push `due_date` into the next month; filtering on `due_date` instead would wrongly exclude every not-yet-due invoice from a run covering cash received *within* the period), and `memo-netoff-guard` nets off any open `credit_debit_memos` against the customer's outstanding balance first (a credit memo genuinely reduces what's owed before amount-matching even starts).
2. For a **locked** payment, the scope is simply that customer's open invoices. For a **pooled** payment, Phase 2 tries the allocation rules against *each* candidate customer in turn: if exactly one candidate produces a clean match, that resolves the pool after the fact (it effectively becomes locked, retroactively); if two or more candidates each produce an equally plausible match, *that's* the moment a Double-Collision/Ambiguous exception actually gets raised — the pool itself isn't the exception, failing to disambiguate at allocation time is.
3. Within the scoped invoice set, rules try in priority order, first-match-wins per payment: `invoice-number-match` (the full invoice number appears verbatim in narration) → `truncated-suffix-match` (only the last 4+ digits appear) → `exact-balance-match` (the payment exactly equals one open invoice's `balance_due_minor` — if it ties between two invoices with the same balance, this deliberately does *not* guess; it raises an Ambiguous/Scoped exception instead, e.g. the golden test's Coral Living pair) → `tds-net-match` (payment equals balance minus that invoice's `allowed_tds_minor`) → `subset-sum-fifo` (search combinations of up to 10 open invoices, oldest due date first, for one that sums exactly to the payment — a single payment can settle several invoices at once) → `fee-tolerance-match` (the shortfall exactly equals the bank row's own `explicit_fee_minor` — the fee is decoupled from the invoice rather than counted as a partial payment) → `dust-writeoff` (a residual within a small materiality threshold, e.g. ₹5, gets written off instead of left open forever) → `overpay-on-account` (payment exceeds the invoice — the excess becomes unapplied/on-account credit, not an error) → `partial-pay` (the universal fallback: whatever no other rule claimed becomes a straightforward partial payment).
4. Every successful match writes one `match_groups` row (recording `match_type`, which rule, `confidence`) and one or more `invoice_allocations` rows (payment↔invoice, with the exact amount allocated — `subset-sum` is the case where one payment fans out across several invoices). Each allocation immediately decrements the target `invoices.balance_due_minor`; once it hits zero the invoice flips to `PAID`, otherwise it stays `OPEN` with a smaller balance. `payments.unapplied_minor` decreases by the same amount as each allocation lands.
5. Whatever clears no Phase-2 rule becomes a new exception type beyond Suspense: **Short-Pay** (a customer/invoice was identified but the amount falls meaningfully short with no fee/TDS explanation) and **No-Payment** (an open invoice that received zero allocation from anything in this run — a collections signal, not a bank-row problem).

**State at the end of M2**: `matched_count`/`matched_value_minor` on the run start meaning "fully allocated to an invoice," not just "customer identified." Invoice balances are live and decrementing. Every unresolved case has a specific, typed reason, not a generic miss.

### M3 — GL posting, control proof, exceptions dashboard (not built)

**Goal**: turn resolved matches (and the bank-charge/Suspense rows set aside earlier) into real double-entry bookkeeping, and prove the sub-ledger and GL agree.

1. `gl_posting.py` runs only after all identification/allocation is finished for the run — never speculatively mid-resolution. For every `invoice_allocations` row it builds a consolidated `gl_journal_entries` + `gl_journal_lines` pair: a normal settlement debits `CASH_CONTROL` and credits `AR_CONTROL` for the allocated amount; a fee/write-off posts to `BANK_CHARGES`/`WRITE_OFF` instead (or alongside); a TDS-net match posts the withheld portion to `TDS_RECEIVABLE`; an overpayment's excess posts to `ON_ACCOUNT_ADVANCE`; a still-open Suspense receipt posts its full amount to `SUSPENSE`. Every posting resolves its real `gl_account_id` through `gl_account_roles` — the module never hardcodes an `account_code`. The app enforces `sum(debits) = sum(credits)` per journal (no DB constraint does this).
2. Standalone bank charges (`is_bank_charge=true` rows Phase 1 excluded entirely) get their own direct path here: no customer was ever identified for them, so they skip `match_groups`/`invoice_allocations` completely and post straight from `bank_statements` to a fee journal entry.
3. `GET .../matches`, `GET .../exceptions`, and `PATCH /exceptions/{id}` land here — a human can now see every open exception (including M2's Double-Collision/Short-Pay/No-Payment and M1's Suspense/Duplicate) and resolve one: write it off, manually map it, mark it disputed. Each resolution is expected to feed M4's audit trail.
4. After posting, the **SL-vs-GL control proof** runs: sum the AR movement from `invoices`/`invoice_allocations` for the entity/period and compare it against `gl_control_balances.control_balance_minor` for the `AR_CONTROL` account. A mismatch beyond tolerance raises a `GL_VARIANCE`/"GL Control Mismatch" exception instead of silently accepting books that don't balance.

**State at the end of M3**: every rupee that moved through the run has a real double-entry trail, standalone fees are booked without pretending to be a customer match, and any sub-ledger/GL disagreement is a visible, typed exception.

### M4 — Sign-off, audit, worker completeness, Rules Studio (not built)

**Goal**: make a run's outcome tamper-evident and formally closeable, and make the rule catalog fully manageable without a code deploy.

1. `audit.py` appends one `immutable_audit_trail` row for every consequential action across the whole engine — a rule locking a customer, an exception being resolved, a journal entry posting, an approval. Each row's `row_hash = hash(this row's content + the previous row's row_hash)`, so altering or deleting any historical row breaks the chain and is detectable. The DB role backing this table is meant to have `INSERT` only, never `UPDATE`/`DELETE` (not yet enforced — depends on real auth/roles existing).
2. `POST /runs/{run_id}/sign-off` computes `run_hash` — a SHA-256 over the run's canonical final state (every match group, allocation, and adjustment) — stamps `reviewed_by`/`signed_at`, and flips `status → APPROVED`. The plan calls for `reviewed_by ≠ prepared_by` to be app-enforced (segregation of duties), which itself depends on real auth existing.
3. Once `APPROVED` (and later `CLOSED`), a run's matches/allocations are meant to be frozen — no further silent edits without a new, separately audited action.
4. The Rules Studio API becomes fully read/write for what M0 only scaffolded — rules tunable per definition without a redeploy — and this is the milestone where the full golden-dataset acceptance test (§8, and the two fixture bugs noted there) is expected to go green end-to-end, which the plan treats as the actual definition of "production-ready."

**State at the end of M4**: a run's outcome is provably unaltered after approval, and the system matches the plan's own bar for done.

---

## 3. Database schema — new and changed

### `gl_account_roles` — new table (migration `0029`)

Maps the engine's fixed vocabulary of semantic GL accounts to whatever real chart-of-accounts entry each entity actually uses, so `gl_posting.py` (M3) never hardcodes an `account_code`.

| Column | Type | Description |
|---|---|---|
| `role_id` | `uuid` PK | |
| `entity_id` | `uuid` NOT NULL, FK → `entities`, `ON DELETE CASCADE` | |
| `role_code` | `text` NOT NULL | One of the 8 fixed roles below. `UNIQUE(entity_id, role_code)` — one account per role per entity. |
| `gl_account_id` | `uuid` NOT NULL, FK → `gl_accounts` | |

**The 8 role codes** (`app/reconciliation/constants.py::GL_ROLE_CODES`), each seeded with a baseline `gl_accounts` row when a new AR definition is created (`ReconciliationDAO.seed_gl_account_roles`, idempotent):

| Role code | Default account | Purpose |
|---|---|---|
| `AR_CONTROL` | 1200 · Accounts Receivable Control | Credited when a payment settles an invoice |
| `CASH_CONTROL` | 1100 · Cash / Bank Clearing | Debited on receipt |
| `BANK_CHARGES` | 5100 · Bank Charges | Standalone bank fees + minor tolerance variance |
| `TDS_RECEIVABLE` | 1250 · TDS Receivable | Tax withheld by the customer at source |
| `WRITE_OFF` | 5200 · Write-Off Expense | Dust/small-balance write-offs |
| `ON_ACCOUNT_ADVANCE` | 2400 · Customer Advances (On-Account) | Overpayment / unapplied cash |
| `SUSPENSE` | 2900 · Suspense | Unidentified receipts pending investigation |
| `FX_GAIN_LOSS` | 5300 · FX Gain / Loss | Reserved — multi-currency is a follow-up, not in this plan's scope |

### `reconciliation_runs` — new columns (migration `0028`)

Turns the run row into a lease-based work queue, identical in shape to `ingestion_jobs` (migration `0020`).

| Column | Type | Description |
|---|---|---|
| `locked_by` | `text` | Worker ID currently holding the lease (`WORKER_ID` format: `hostname-pid-random8`) |
| `locked_at` | `timestamptz` | When the lease was acquired |
| `lease_expires_at` | `timestamptz` | A `RUNNING` run whose lease has expired is reclaimable by any worker |
| `attempt_count` | `int` NOT NULL DEFAULT 0 | Incremented on every claim |
| `max_attempts` | `int` NOT NULL DEFAULT 3 | Attempt ≥ this → `FAILED` instead of retried |
| `next_attempt_at` | `timestamptz` | Exponential-backoff target after a failure |
| `last_error` | `text` | Exception message from the most recent failed attempt |

Plus index `idx_recon_runs_claimable (status, next_attempt_at)` for the worker's claim query.

### `reconciliation_exceptions` — new columns (migration `0029`)

| Column | Type | Description |
|---|---|---|
| `detail` | `jsonb` | Candidate lists for Double-Collision/Ambiguous exceptions, so the UI can present the options a rule refused to guess between |
| `match_group_id` | `uuid`, FK → `match_groups` (nullable) | Links an exception back to the match group that produced it, when there is one |

### `customers` — trigram index (migration `0027`)

`CREATE EXTENSION pg_trgm` + `idx_customers_name_trgm` (GIN, `gin_trgm_ops` on `company_name`) — backs `similarity()` queries for Rule 1.6a (fuzzy name match) and 1.2b (token pool). No column changes, index-only.

---

## 4. Database schema — reused as-is

Pre-existing (migrations `0012`–`0018`), unmodified by this plan. Documented here field-by-field since the plan only named the tables, not their shape — this is what the engine actually reads from and writes to.

### `reconciliation_definitions` — one rule catalog per entity/recon type

| Column | Type | Description |
|---|---|---|
| `definition_id` | `uuid` PK | |
| `entity_id` | `uuid` NOT NULL, FK → `entities` | |
| `name` | `text` NOT NULL | |
| `recon_type` | `text` NOT NULL | `AR` \| `AP` \| `BANK` — only `AR` has a seeded catalog/engine |
| `cadence` | `text` | Free text (e.g. `MONTHLY`), not enforced |
| `owner_user_id` | `uuid`, FK → `users` | `NULL` until real auth exists |

### `reconciliation_rules` — the Rules Studio catalog

| Column | Type | Description |
|---|---|---|
| `rule_id` | `uuid` PK | |
| `definition_id` | `uuid` NOT NULL, FK → `reconciliation_definitions`, `ON DELETE CASCADE` | |
| `phase` | `text` NOT NULL | `INTAKE_VALIDATION` \| `CUSTOMER_LOCK` \| `CANDIDATE_POOL` \| `ALLOCATION` \| `SHORT_PAY` \| `UNAPPLIED` \| `GL_CHECK` |
| `kind` | `text` NOT NULL | Open vocabulary (not enum-closed) — dispatches to a callable in `rules/{identification,pooling,allocation}.py` |
| `name` | `text` NOT NULL | Human-readable label |
| `priority` | `int` NOT NULL | Evaluation order within a phase, ascending. `UNIQUE(definition_id, phase, priority)` |
| `enabled` | `bool` NOT NULL DEFAULT true | Disabled rules are skipped by the engine |
| `confidence` | `smallint` | Doubles as the match threshold for fuzzy kinds (e.g. `85` = 85% similarity) |
| `config` | `jsonb` NOT NULL DEFAULT `{}` | Rule-specific tunables — shape differs per `kind`, see §5 |

### `reconciliation_runs` — one execution of a definition

| Column | Type | Description |
|---|---|---|
| `run_id` | `uuid` PK | |
| `definition_id` | `uuid` NOT NULL, FK | |
| `run_no` | `text` NOT NULL UNIQUE | `RUN-{YYYYMMDD}-{6 hex}`, generated client-side (collision-safe, no counting query) |
| `period_start` / `period_end` | `date` | Authoritative cutoff for the Phase 2 period-cutoff guardrail — a stored value, not `today()` at compute time |
| `status` | `text` NOT NULL DEFAULT `DRAFT` | `DRAFT → QUEUED → RUNNING → COMPUTED → APPROVED → CLOSED`, or `FAILED` |
| `volume` | `int` | Bank inflows processed this run |
| `matched_count` | `int` | As of M2: fully-allocated-to-invoice count (was Phase-1a-lock count under M1 alone) |
| `exception_count` | `int` | Duplicates + Suspense (M1) plus Short-Pay/Ambiguous/Double-Collision/Unresolved-pool/No-Payment (M2) |
| `matched_value_minor` | `bigint` | Sum of locked payments' amounts |
| `exception_value_minor` | `bigint` | Sum of duplicate/suspense payments' amounts |
| `unapplied_minor` | `bigint` | Sum of all payments' `unapplied_minor` — everything, until Phase 2 allocates some of it |
| `prepared_by` / `reviewed_by` | `uuid`, FK → `users` | App enforces `reviewed_by ≠ prepared_by` (segregation of duties) — not wired yet, pending auth |
| `signed_at` | `timestamptz` | Set at M4 sign-off |
| `run_hash` | `text` | SHA-256 over matched pairs + adjustments, computed at sign-off (M4) |
| `started_at` | `timestamptz` NOT NULL DEFAULT now() | |

*Note: `matched_count`/`exception_count` have no third bucket for "pooled" (Phase 1b, not yet resolved) — the engine tracks it internally (`counts["pooled"]`) but there's no column for it. Worth a schema addition if pooled-but-unresolved needs its own top-line visibility later.*

### `payments` — identified cash (Phase 1 output)

| Column | Type | Description |
|---|---|---|
| `payment_id` | `uuid` PK | |
| `bank_txn_id` | `uuid` NOT NULL UNIQUE, FK → `bank_statements` | One payment row per bank line ever processed by Phase 1 |
| `customer_id` | `uuid`, FK → `customers` (nullable) | Set only when Phase 1a locks; `NULL` for pooled or Suspense |
| `total_received_minor` | `bigint` NOT NULL | The bank line's full amount |
| `unapplied_minor` | `bigint` NOT NULL | Starts equal to `total_received_minor`; Phase 2 (M2) decrements it as allocations land |
| `locked_by_rule_id` | `uuid`, FK → `reconciliation_rules` (nullable) | Which Phase 1a rule locked the customer, if any |
| `candidate_pool` | `jsonb` (nullable) | Array of candidate `customer_id`s from Phase 1b — possibly more than one (see Double-Collision) |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

### `match_groups` — one matched set (M2)

| Column | Type | Description |
|---|---|---|
| `match_group_id` | `uuid` PK | |
| `run_id` | `uuid` NOT NULL, FK → `reconciliation_runs`, `ON DELETE CASCADE` | |
| `match_type` | `text` NOT NULL | `EXACT` \| `TOLERANCE` \| `PARTIAL` \| `SUBSET_SUM` \| `MANY_TO_ONE` \| `ONE_TO_MANY` \| `MANUAL` |
| `rule_id` | `uuid`, FK → `reconciliation_rules` (nullable) | `NULL` = manually created, not by the engine |
| `confidence` | `smallint` | |
| `status` | `text` NOT NULL DEFAULT `AUTO_MATCHED` | `AUTO_MATCHED` \| `SUGGESTED` \| `CONFIRMED` \| `REJECTED` |
| `reason` | `text` | Free-text explanation |
| `created_by` | `uuid`, FK → `users` (nullable) | `NULL` = engine |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

### `invoice_allocations` — the money-carrying junction (M2)

| Column | Type | Description |
|---|---|---|
| `allocation_id` | `uuid` PK | |
| `match_group_id` | `uuid` NOT NULL, FK → `match_groups`, `ON DELETE CASCADE` | |
| `invoice_id` | `uuid` NOT NULL, FK → `invoices` | |
| `payment_id` | `uuid` NOT NULL, FK → `payments` | |
| `bank_txn_id` | `uuid`, FK → `bank_statements` (nullable) | |
| `allocated_minor` | `bigint` NOT NULL, `CHECK (> 0)` | How much of the payment went to this invoice |
| `gl_journal_id` | `uuid`, FK → `gl_journal_entries` (nullable) | Set once the JE posts (M3) |
| `allocated_at` | `timestamptz` NOT NULL DEFAULT now() | |

`UNIQUE(match_group_id, invoice_id, payment_id)` — the three-way FK enforcement is the whole reason this is a relational DB and not a document store per the original design comparison.

### `reconciliation_exceptions` — full shape (base + `0029` additions from §2)

| Column | Type | Description |
|---|---|---|
| `exception_id` | `uuid` PK | |
| `run_id` | `uuid` NOT NULL, FK → `reconciliation_runs`, `ON DELETE CASCADE` | |
| `exception_no` | `text` | Display code, e.g. `EXC-001` (not auto-generated yet) |
| `exception_type` | `text` NOT NULL | `SHORT_PAY` \| `OVERPAYMENT` \| `UNAPPLIED_CASH` \| `TIMING_DIFFERENCE` \| `GL_VARIANCE` \| `DUPLICATE` \| `MULTIPLE_INVOICE_MATCH` \| `DOUBLE_COLLISION` \| `SUSPENSE` \| `BANK_CHARGE` \| `GATEWAY_VARIANCE` \| `NO_PAYMENT` |
| `bank_txn_id` / `invoice_id` / `customer_id` | `uuid`, FKs (nullable) | Whichever are relevant to this exception type |
| `discrepancy_minor` | `bigint` | |
| `reason_code` | `text` | e.g. `SP-01 Freight deduction` |
| `status` | `text` NOT NULL DEFAULT `OPEN` | `OPEN` \| `INVESTIGATING` \| `RESOLVED` \| `AUTO_RESOLVED` \| `DEFERRED` \| `WRITTEN_OFF` \| `ADJUSTED` \| `CARRIED_FORWARD` |
| `resolution_outcome` | `text` | `WRITEOFF` \| `KEEPOPEN` \| `DISPUTE` \| `JOURNAL` \| `ON_ACCOUNT` |
| `resolver_id` | `uuid`, FK → `users` | |
| `resolution_notes` | `text` | |
| `resolved_at` | `timestamptz` | |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |
| `detail` | `jsonb` | *(0029)* candidate lists |
| `match_group_id` | `uuid`, FK | *(0029)* |

### `gl_journal_entries` / `gl_journal_lines` (M3)

**`gl_journal_entries`** (header): `journal_id` PK, `entity_id` FK NOT NULL, `run_id` FK (nullable), `posting_date` NOT NULL, `source_type` NOT NULL (`CASH_RECEIPT` \| `FEE_ADJUSTMENT` \| `WRITE_OFF`), `memo`, `posted_by` FK → `users`, `created_at`.

**`gl_journal_lines`**: `line_id` PK, `journal_id` FK NOT NULL `ON DELETE CASCADE`, `line_number` NOT NULL (`UNIQUE(journal_id, line_number)`), `gl_account_id` FK NOT NULL, `dr_cr` NOT NULL, `currency` NOT NULL, `amount_minor` NOT NULL, `amount_home_minor` NOT NULL, `business_partner_id` FK → `customers` (nullable). App enforces `sum(debits) = sum(credits)` per journal — no DB constraint for it.

### `gl_control_balances` — SL-vs-GL control proof input (M3)

| Column | Type | Description |
|---|---|---|
| `balance_id` | `uuid` PK | |
| `gl_account_id` | `uuid` NOT NULL, FK → `gl_accounts` | |
| `period_date` | `date` NOT NULL | `UNIQUE(gl_account_id, period_date)` |
| `control_balance_minor` | `bigint` NOT NULL | The GL's own stated balance — compared against summed `invoices.balance_due_minor` to detect `GL_VARIANCE` |

No ingestion pipeline writes this table (see §7) — seeded directly for now.

### `immutable_audit_trail` — hash-chained append-only log (M4)

| Column | Type | Description |
|---|---|---|
| `audit_id` | `bigserial` PK | |
| `at` | `timestamptz` NOT NULL DEFAULT now() | |
| `run_id` | `uuid`, FK (nullable) | |
| `entry_type` | `text` NOT NULL | `SYSTEM` \| `MANUAL` \| `API` |
| `category` | `text` NOT NULL | e.g. `CASH_APP`, `GL_POSTING`, `RULE_ENGINE`, `SIGN_OFF` |
| `action` | `text` NOT NULL | e.g. `FORCE_MATCH`, `WRITE_OFF`, `APPROVED` |
| `user_id` | `uuid`, FK → `users` (nullable) | |
| `target_ref` | `text` | |
| `impact_minor` | `bigint` | |
| `entity_ref` | `text` | |
| `old_state` / `new_state` | `jsonb` | Field-level before/after |
| `prev_hash` / `row_hash` | `text` | `row_hash = hash(this row + prev_hash)` — tamper-evident chain |

DB role should have `INSERT` only, no `UPDATE`/`DELETE` grant — not yet enforced (auth is stubbed).

### Supporting reference tables the engine reads (unchanged, pre-existing)

| Table | Used by | Key columns |
|---|---|---|
| `customer_bank_accounts` | Rule 1.2a (exact), 1.1b (suffix pool) | `customer_id`, `bank_account_no`, `ifsc_code`, `is_primary`, `status` |
| `customer_reference_codes` | Rule 1.4a (fallback after `customers.customer_code`) | `customer_id`, `code_value`, `code_type`, `match_priority`, `is_active` |
| `expected_remittances` | Rule 1.1a | `customer_id`, `utr_number`, `declared_amount_minor` NOT NULL, `currency`, `declared_date`, `reconciled` |
| `credit_debit_memos` | Phase 2.0b memo net-off guardrail (M2) | `customer_id`, `invoice_id`, `memo_type`, `memo_date`, `amount_minor`, `is_open` |

No ingestion pipeline writes `customer_bank_accounts` or `expected_remittances` either — same gap as `gl_control_balances`, see §7.

---

## 5. New application modules

`app/reconciliation/` mirrors `app/datahub/`'s layering (`router → service → dao`, `schema`/`constants` shared).

| File | Status | What it does |
|---|---|---|
| `constants.py` | ✅ Done | Phase/exception/match/GL-role vocabularies, `GL_ROLE_DEFAULTS`, the 20-rule `DEFAULT_AR_RULE_CATALOG` |
| `dao.py` | ✅ M0+M1+M2 scope done | Definitions/rules CRUD, GL-role seeding, run queue CRUD, Phase-1 working-set loaders, `payments`/`reconciliation_exceptions` writes, Phase-2 working-set loaders (open invoices/memos), `match_groups`/`invoice_allocations` writes, balance-mutating updates. `gl_journal_*` writes land in M3 |
| `schema.py` | ✅ M0+M1 scope done | Pydantic models for definitions/rules/runs. No models yet for matches/exceptions/sign-off (M2-M4) |
| `service.py` | ✅ M0+M1 scope done | `create_definition` (seeds catalog + GL roles), rule/run CRUD |
| `router.py` | ✅ M0+M1 scope done | 9 endpoints, see §6. Carries an explicit milestone-map docstring |
| `extract.py` | ✅ Done | Pure regex: VPA, GSTIN, PAN, 4+ digit numeric blocks, account-suffix matching |
| `fuzzy.py` | ✅ Done | `best_fuzzy_match` (pg_trgm SQL), `fuzzy_ratio` (pure-Python fallback), `significant_tokens`/`token_overlap_match` |
| `rules/__init__.py` | ✅ Done | Shared `RuleContext` (the loaded working set) and `IdentificationResult` types |
| `rules/identification.py` | ✅ Done | `dup_utr_check` + the 6 Phase 1a rules, `IDENTIFICATION_RULES` registry |
| `rules/pooling.py` | ✅ Done | The 2 Phase 1b rules, `POOLING_RULES` registry |
| `rules/allocation.py` | ✅ Done | Rules 2.1–2.9 (the two guardrail kinds, 2.0a/2.0b, are context-prep, not registry callables - see `GUARDRAIL_KINDS`) |
| `engine.py` | ✅ Phase 1+2 done | `run()` calls `run_phase_1` then `run_phase_2` in one transaction; GL posting (M3) will extend `run()` further |
| `gl_posting.py` | ⬜ M3 | Consolidated journal entries + SL-vs-GL control proof |
| `audit.py` | ⬜ M4 | Hash-chained `immutable_audit_trail` writes, `run_hash` computation |
| `app/workers/reconciliation_worker.py` | ✅ Done | Lease-based queue worker, structurally identical to `ingestion_worker.py`. Calls `engine.run()` (Phase 1+2); GL posting (M3) extends `engine.run()`, not this file |

---

## 6. The default AR rule catalog

Seeded onto every new `AR` definition (`DEFAULT_AR_RULE_CATALOG`, 20 rows). ✅ = engine implementation exists (M1); everything in ALLOCATION phase is data-only until M2.

### CUSTOMER_LOCK (Phase 1a) — first match wins, locks a single customer

| Priority | Kind | Confidence | Config | Status |
|---|---|---|---|---|
| 0 | `dup-utr-check` | 100 | `{}` | ✅ |
| 10 | `utr-match` | 100 | `{source: expected_remittances, match_field: utr_number}` | ✅ |
| 20 | `bank-account-match` | 100 | `{source: customer_bank_accounts, match_fields: [bank_account_no, ifsc_code]}` | ✅ |
| 30 | `vpa-match` | 100 | `{source: customers, match_field: vpa_handle, extract: vpa}` | ✅ |
| 40 | `reference-code-match` | 100 | `{source: customer_reference_codes, extract: narration_substring}` | ✅ |
| 50 | `gstin-pan-match` | 100 | `{source: customers, extract: [gstin, pan]}` | ✅ |
| 60 | `fuzzy-name-match` | 85 | `{source: customers, match_field: company_name, min_similarity: 0.85}` | ✅ |

### CANDIDATE_POOL (Phase 1b) — only if 1a found nothing; never locks, only pools

| Priority | Kind | Confidence | Config | Status |
|---|---|---|---|---|
| 10 | `masked-account-pool` | 60 | `{source: customer_bank_accounts, match_field: bank_account_no, mode: suffix}` | ✅ |
| 20 | `token-pool` | 55 | `{source: customers, match_field: company_name, mode: token_substring}` | ✅ |

### ALLOCATION (Phase 2) — scoped to the locked customer or candidate pool

| Priority | Kind | Confidence | Config | Status |
|---|---|---|---|---|
| 0 | `period-cutoff-guard` | — | `{date_field: issue_date, compare: lte_period_end}` | ✅ Context-prep (baked into `load_open_invoices`'s query), not a registry rule |
| 5 | `memo-netoff-guard` | — | `{source: credit_debit_memos, filter: memo_date_lte_period_end}` | ✅ Best-effort - only invoice-linked memos are netted; customer-level memos with no `invoice_id` aren't (no golden-data case exercises this) |
| 10 | `invoice-number-match` | 100 | `{match_field: invoice_number, location: narration}` | ✅ |
| 20 | `truncated-suffix-match` | 90 | `{match_field: invoice_number, mode: suffix, min_length: 4}` | ✅ |
| 30 | `exact-balance-match` | 100 | `{amount: {mode: exact, field: balance_due_minor}, tie_break: ambiguous_exception}` | ✅ Ties → `MULTIPLE_INVOICE_MATCH` exception |
| 40 | `tds-net-match` | 95 | `{amount: {mode: net_of_tds, field: allowed_tds_minor}}` | ✅ Computes TDS from `tds_rate_pct × total_amount_minor` at match time (see §8 - no ingestion path derives `allowed_tds_minor` yet) |
| 50 | `subset-sum-fifo` | 90 | `{amount: {mode: subset_sum}, order_by: due_date, max_invoices: 10}` | ✅ Only searches combinations of 2+ invoices - a single-invoice exact sum is `exact-balance-match`'s job at earlier priority |
| 60 | `fee-tolerance-match` | 80 | `{amount: {mode: tolerance, value_minor: 500}, decouple_field: explicit_fee_minor}` | ✅ Prefers an exact match against the bank row's own `explicit_fee_minor`; falls back to the generic tolerance only if no exact-fee candidate exists |
| 70 | `dust-writeoff` | 100 | `{amount: {mode: tolerance, value_minor: 500}, gl_role: WRITE_OFF}` | ✅ Threshold corrected from an initial 100 (₹1) to 500 (₹5) - the golden dataset's BANK-014/INV-118 case needs ₹5 headroom |
| 80 | `overpay-on-account` | 100 | `{gl_role: ON_ACCOUNT_ADVANCE}` | ✅ Targets whichever open invoice has the *smallest* excess (closest match), not an arbitrary one |
| 90 | `partial-pay` | 60 | `{mode: partial, allow_short_pay: true}` | ✅ Applies to the customer's oldest open invoice (by due date) when nothing else identifies a target |

---

## 7. API surface

All under `/api/v1`, tag `Reconciliation`.

| Method | Path | Status | Notes |
|---|---|---|---|
| `POST` | `/reconciliations` | ✅ | Creates definition; seeds rule catalog + GL roles for `AR` |
| `GET` | `/reconciliations` | ✅ | Filter: `entity_id` |
| `GET` | `/reconciliations/{id}` | ✅ | |
| `GET` | `/reconciliations/{id}/rules` | ✅ | Ordered `(phase, priority)` |
| `PATCH` | `/reconciliations/{id}/rules/{rule_id}` | ✅ | `config` is a full replacement, not a merge |
| `POST` | `/reconciliations/{id}/runs` | ✅ | Enqueues only (`202`, `status=QUEUED`) |
| `GET` | `/reconciliations/{id}/runs` | ✅ | |
| `GET` | `/runs/{run_id}` | ✅ | Counters are `NULL` until a worker completes the run |
| `POST` | `/runs/{run_id}/retry` | ✅ | Only from `FAILED` |
| `GET` | `/runs/{run_id}/matches` | ⬜ M3 | |
| `GET` | `/runs/{run_id}/exceptions` | ⬜ M3 | |
| `PATCH` | `/exceptions/{exception_id}` | ⬜ M3 | Resolve/manual-map/write-off |
| `POST` | `/runs/{run_id}/sign-off` | ⬜ M4 | Compute `run_hash`, `status → APPROVED` |

Reserved permission slugs (auth module still stubbed, same as `app/datahub`): `recon.run.prepare`, `recon.run.approve`, `recon.exception.resolve`.

---

## 8. Known gaps found during M0/M1/M2 build

Carried forward honestly, not silently dropped — same convention as `docs/data-hub.md` §9.

- **No ingestion pipeline for `customer_bank_accounts`, `expected_remittances`, `customer_reference_codes`, `credit_debit_memos`, or `gl_control_balances`.** The Data Hub only ingests into `bank_statements`/`invoices`/`customers`. These tables were seeded directly via SQL for golden-data verification — a real gap if this needs to be a repeatable, non-engineer-operated flow. Extending Data Hub to cover them is out of this plan's file list and would be a separate scoped decision.
- **No ingestion path derives `invoices.allowed_tds_minor` or `tds_rate_pct` from a source file either** (e.g. `SL.csv`'s `tds_allowed_pct` column has no `field_mappings` synonym today). `tds-net-match` (M2) works around this by computing the effective TDS amount itself from `tds_rate_pct` at match time - but `tds_rate_pct` still has to get into the DB somehow first, which today means direct seeding, not a real upload.
- **`field_mappings` has no synonym-precedence mechanism.** When a source row has *both* a generic synonym (e.g. `customer_id`) and a more specific one (`customer_code`) populated with different values, "first non-None value wins" picks whichever the DB happens to return first — not necessarily the more authoritative one. Found via the golden test's Kestrel/Coral Living rows. A real fix needs a rank/priority column on `field_mappings`; not built here.
- **`reconciliation_runs` has no counter column for "pooled" (Phase 1b/2, unresolved).** `matched_count`/`exception_count` don't have a third bucket — unresolved pools are tracked in the worker's log line (`pooled_count`) but not persisted at the run level.
- **The memo net-off guardrail (2.0b) is best-effort.** Only `credit_debit_memos` rows already linked to a specific `invoice_id` are netted off that invoice's balance before matching; a customer-level memo with no `invoice_id` is left alone. No golden-data case exercises this yet, so it's unverified beyond reading correctly.
- **Fixed along the way in the ingestion layer, worth knowing about**: `apply_mapping`'s clobbering bug (a non-matching synonym could overwrite a value a matching one already found — now "first non-None wins"), the missing `PARSE_BOOL` transform (booleans previously stayed strings and asyncpg rejected them), and corrupted `CONST` `transform_param`s on the live `BANK` mapping (`currency`/`dr_cr` had silently lost their values at some point) — all fixed in `app/datahub/transforms.py` and the live mapping data.
- **Fixed along the way in the reconciliation engine itself, worth knowing about**: (1) the `period-cutoff-guard` originally filtered on `due_date`, which would have excluded almost every invoice in the golden dataset from a same-month run (due dates routinely fall a month past issue dates under Net-30 terms) - changed to filter on `issue_date`. (2) Several places passed a raw `uuid.UUID` where a `str` was needed (JSON-encoding an exception's `detail`, comparing an invoice ID from one source against another) - the same bug class caught in M0/M1, now fixed by normalizing every `invoice_id`/`customer_id` to `str` once, at the point invoices are loaded in `engine.py`, rather than converting ad hoc at each use site. (3) A closed invoice was decremented to a zero balance but never removed from the in-memory working set, so a later payment in the same run could still match against it via `invoice-number-match`/`truncated-suffix-match` (neither checks balance > 0) and attempt a zero-amount allocation, which `invoice_allocations`' `CHECK(allocated_minor > 0)` correctly rejected - fixed by removing an invoice from the working set the moment its balance reaches zero.
- **The golden test fixture (`truebalance/rule-test-data/`) has two real inconsistencies**: the README describes a `BANK-015`/`BANK-016` duplicate-reference case that doesn't exist in `Bank_Statement.csv` (only 18 rows, `BANK-015`/`016` are simply absent); and `BANK-001`/`BANK-019` unintentionally collide on the same `bank_reference`, which correctly triggers `dup-utr-check` but means `BANK-001` doesn't demonstrate "Expected UTR match" as the README describes (covered explicitly by `test_acme_flagged_duplicate_not_locked` and `test_acme_invoice_101_unpaid_due_to_duplicate_fixture_bug` in `tests/reconciliation/test_golden_m2.py`, rather than hidden). Neither is an engine bug — both are worth fixing at the fixture level before this is called a clean acceptance test.
- **`tests/reconciliation/test_golden_m2.py` is the golden acceptance test, run entirely via direct SQL seeding inside a rolled-back transaction** - not through `app/datahub` ingestion or the HTTP API. An earlier verification pass (M1) uploaded the same dataset through the real Data Hub API against the shared dev database, which left visible "Golden *" data source cards and a test entity in the UI that had to be found and manually cleaned up afterward. The pytest version gives the same verification confidence with zero persisted footprint - prefer it for future milestone verification too.
