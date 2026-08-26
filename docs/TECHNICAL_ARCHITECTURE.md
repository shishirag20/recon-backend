# Technical Architecture — Decisions, Rationale, Trade-offs

This is the single consolidated reference for *why the system is built the way it is*: every
notable architectural decision, the alternative(s) it was chosen over, and its concrete
advantages and disadvantages. It does not replace the deeper existing docs — it sits above them
and points down into them for detail:

| Doc | Depth |
|---|---|
| `docs/database-schema.md` | Every table, every column, every index — field-by-field |
| `docs/data-hub.md` | Full ingestion pipeline mechanics, transform engine, worker internals, API surface |
| `docs/reconciliation.md` | Milestone-by-milestone build log, the full rule catalog, API surface |
| `docs/reconciliation-invoice-flow-example.md` | Two real payments traced end-to-end, every SQL statement, every row written |
| `docs/Ingestion-plan.md` | A *proposed, not yet built* change to ingestion (see §4.6) |

**One correction this doc makes**: `docs/reconciliation.md` §2's description of Phase 2 pool
resolution ("if exactly one candidate produces a clean match, that resolves the pool") describes
an earlier version of the engine. It was deliberately replaced (§5.3 below) — a pooled payment
today *never* auto-resolves, no matter how clean the match. That doc is stale on this one point;
this doc and the current `app/reconciliation/engine.py` are the source of truth for it.

Everything below is grounded in the actual code and migrations, not reconstructed from memory —
file/line references are given so any claim here can be checked against the source it describes.

---

## 1. System at a glance

```
                      ┌─────────────┐
   HTTP clients ──────▶   app       │  FastAPI, all read/write API surface
                      └──────┬──────┘
                             │ same Postgres DB, no message broker
              ┌──────────────┼──────────────┐
              ▼                              ▼
     ┌─────────────────┐           ┌─────────────────────────┐
     │ worker           │           │ reconciliation-worker    │
     │ (ingestion_worker)│           │ (reconciliation_worker)  │
     │ polls ingestion_  │           │ polls reconciliation_    │
     │ jobs, parses CSVs,│           │ runs, executes the        │
     │ writes canonical   │           │ matching engine            │
     │ rows                │           │                            │
     └─────────────────┘           └─────────────────────────┘
```

Three services, one Postgres database, one Docker image (`build: .`) reused for all three —
`app`/`worker`/`reconciliation-worker` differ only in their `command:` in `docker-compose.yml`.
There is no message broker, no cache layer, no separate search index. The database is the only
piece of shared infrastructure, and it plays three roles at once: system of record, work queue,
and (via `pg_trgm`) the fuzzy-matching engine.

`app/` mirrors the same internal layering everywhere: `router.py` (FastAPI, HTTP concerns only)
→ `service.py` (business rules, orchestration, raises typed `HTTPException`s) → `dao.py` (raw
`asyncpg` SQL, no business logic) → Postgres. `schema.py` (Pydantic models) and `constants.py`
(string vocabularies) are shared across all three layers of a module.

**Advantage**: one mental model for every module (`app/datahub/`, `app/reconciliation/`,
`app/auth/`) — a developer who understands one understands the shape of all of them. Business
logic lives in exactly one place (`service.py`), never duplicated between a route handler and a
DAO method.

**Disadvantage**: `dao.py` files are large (reconciliation's is 500+ lines) because nothing
is split by sub-resource — there's no `payments_dao.py` vs `invoices_dao.py`. This is a
deliberate trade (see §2.1) but it does mean the file grows without bound as the module grows.

---

## 2. Cross-cutting decisions

These aren't scoped to one module — they're conventions applied everywhere, established early
and never violated since (each violation would itself have been worth flagging).

### 2.1 No ORM — raw SQL via `asyncpg`, hand-written migrations

**Decision**: every query in the codebase is hand-written SQL text with `$1`/`$2` bind
parameters, executed through `asyncpg`. No SQLAlchemy, no Alembic. Migrations are numbered
`.sql` files in `migrations/`, applied in order by a ~50-line custom runner
(`app/db/migrate.py`) that tracks what's already run in a `schema_migrations` table.

**Why**: this is a reconciliation engine — the actual logic (subset-sum search, tolerance
matching, fuzzy name similarity via `pg_trgm`) is expressed more naturally as SQL/`asyncpg`
queries and Python over loaded rows than as ORM-mapped objects with lazy-loaded relationships.
An ORM's unit-of-work/session model also fights the "load the whole working set into memory
once per run" pattern the engine relies on (§5.1) — that pattern deliberately avoids
N+1 query patterns an ORM would make easy to write by accident.

**Advantages**:
- Every query's actual execution plan is visible and `EXPLAIN`-able directly — no ORM-generated
  SQL to reverse-engineer when something is slow.
- No ORM version-migration risk, no N+1 query surprises from lazy relationship loading.
- Migrations are just SQL — reviewable in a PR exactly like any other file, no auto-generated
  migration scripts that need a second pass to check they're doing what's intended.

**Disadvantages**:
- No compile-time/type-check safety on a query's shape — a typo in a column name is a runtime
  error, not a build-time one.
- No auto-generated schema introspection — `docs/database-schema.md` has to be hand-maintained
  (which is why it explicitly says "generated by reading migrations 0001-0026" rather than
  claiming to be live-generated).
- More boilerplate per query than an ORM's `Model.objects.filter(...)` equivalent.

### 2.2 No native Postgres enums — plain `TEXT`, validated in application code

**Decision**: every status/type/kind column (`payments` has none, but `bank_statements.dr_cr`,
`reconciliation_exceptions.exception_type`, `match_groups.match_type`, `ingestion_jobs.status`,
...) is `TEXT`, not a Postgres `ENUM` type. The allowed values live in
`app/*/constants.py` as Python sets/tuples, checked at the service layer.

**Why** (`docs/data-hub.md` §1): a Postgres `ENUM` needs `ALTER TYPE ... ADD VALUE` for every
new value, which historically had transactional restrictions and is a schema migration either
way. A reconciliation-engine domain adds new exception types, rule kinds, and resolution
outcomes as the rule catalog grows (this session added `MANUAL_MATCH` to
`EXCEPTION_RESOLUTION_OUTCOMES` with a one-line constant change, no migration).

**Advantages**: adding a new status value is a code change, not a schema migration — much
faster iteration, no `ALTER TYPE` lock consideration on a large table.

**Disadvantages**: the database itself cannot reject an invalid value — a bug or a direct SQL
`UPDATE` can write `exception_type = 'SUSPENSEE'` and Postgres will accept it silently. The
only enforcement is the application layer remembering to validate before every write. This is
a real, accepted risk, not an oversight — the schema doc calls it out at the top of
`docs/database-schema.md` rather than pretending it's actively enforced elsewhere.

### 2.3 UUID primary keys everywhere, via `pgcrypto`'s `gen_random_uuid()`

**Decision**: every table's PK defaults to `gen_random_uuid()`. The one exception is
`immutable_audit_trail.audit_id`, a `BIGSERIAL` — deliberately, because that table's hash chain
(§3.9) needs a total, gap-free insert order, which a UUID PK doesn't give you.

**Advantages**: IDs can be generated client-side or by any of the three services without a
round-trip or coordination; no auto-increment contention; IDs don't leak row-count/business
volume information the way a sequential ID would.

**Disadvantages**: UUIDs are 16 bytes vs 4/8 for an int, so indexes are larger; UUIDv4 (the
`pgcrypto` default) is not sequential, so B-tree inserts aren't append-only — on very large
tables this causes more index-page fragmentation than a sequential key would. Not a problem at
this system's current data volume; would be a real one at very high transaction volume, where
UUIDv7 (time-ordered) would be the fix without changing the "generated, not app-assigned"
property.

### 2.4 Money as integer minor units, never floats

**Decision**: every monetary column is `bigint`, suffixed `_minor` (paise for INR, cents for
USD), e.g. `invoices.total_amount_minor`. `currencies.minor_unit` records how many decimal
places that implies per currency. There is no `numeric`/`float` money column anywhere in the
schema.

**Why**: floating-point can't represent most decimal fractions exactly (`0.1 + 0.2 != 0.3` in
IEEE 754) — accumulating floating-point rounding error across thousands of allocations would
eventually produce a control-account variance that isn't real, undermining the entire GL
control-proof design (§5.5). Postgres `numeric` avoids that specific problem but is slower and
was already caught causing a real bug in this codebase: `sum_open_ar_balance`'s `SUM(...)`
returned a `numeric`, which `asyncpg` decodes as Python `Decimal` — that broke `json.dumps()`
when embedded in an exception's `detail` JSONB until the query was changed to cast `::bigint`
(`docs/reconciliation.md` §8). Integer minor units side-step the whole class of problem.

**Advantages**: exact arithmetic, no rounding-error accumulation, fast integer comparisons for
tolerance checks (`bank-fee`, `write-off` rules compare `bigint <= bigint`, no epsilon needed).

**Disadvantages**: every value needs `/ 100` (or the currency's actual `minor_unit`) at display
time — a missed conversion either direction is a 100x display bug. This class of bug did occur
this session (an amount-field display bug in the Matched tab was explicitly reported and fixed)
— not a flaw unique to this convention, but a real cost of it that a `numeric`/decimal column
wouldn't have.

### 2.5 The database itself as the work queue — no message broker

**Decision**: `ingestion_jobs` and `reconciliation_runs` are both, simultaneously, business
records *and* the queue a worker polls. There is no Redis, RabbitMQ, Celery, or SQS anywhere in
the system. A worker claims work with:

```sql
WITH claimed AS (
    SELECT job_id FROM ingestion_jobs
    WHERE (status = 'PENDING' AND (next_attempt_at IS NULL OR next_attempt_at <= now()))
       OR (status = 'RUNNING' AND lease_expires_at < now())
    ORDER BY started_at LIMIT 1 FOR UPDATE SKIP LOCKED
)
UPDATE ingestion_jobs SET status='RUNNING', locked_by=$1, lease_expires_at=now()+$2, attempt_count=attempt_count+1
FROM claimed WHERE ingestion_jobs.job_id = claimed.job_id RETURNING ...;
```

(`app/workers/ingestion_worker.py`; `reconciliation_worker.py` is structurally identical
against `reconciliation_runs`, added in migration `0028` specifically to give that table the
same lease/retry shape.)

`FOR UPDATE SKIP LOCKED` means a second worker racing for the same row just moves on to a
different one — no external lock manager, no blocking. `OR (status='RUNNING' AND
lease_expires_at < now())` is what makes a **crashed worker's job self-healing**: any other
worker reclaims it once the 5-minute lease expires, with no manual intervention and no
dead-worker detection needed. A background heartbeat renews the lease roughly every 100 seconds
while a job is actually being processed; if a renewal ever finds the row no longer owned by this
worker (reclaimed as abandoned), the worker sets a stop flag and discards its own result rather
than racing to overwrite a fresher claim.

**Why not a broker**: the enqueue and the metadata write are the *same* row and the *same*
transaction — no dual-write problem (write to Postgres, then separately push to a queue, with a
crash between the two leaving them inconsistent). Multiple worker replicas need zero
coordination infrastructure beyond Postgres, which is already a hard dependency. Discovery is
polling (`asyncio.sleep(3)` when idle), not push — nothing notifies a worker; it re-asks the
database on a fixed cadence.

**Advantages**: one less piece of infrastructure to run, monitor, and reason about failure
modes for; the "enqueue" write is transactionally consistent with the business write that
triggered it by construction, not by discipline; a worker crash is invisible to the *system*
(another replica or the same one after restart just picks the lease back up) — no queue message
can be "lost" the way an unacked broker message theoretically can be misconfigured to be.

**Disadvantages**: polling means up to ~3 seconds of added latency between "job created" and "a
worker notices it," where a push-based broker would be near-instant — acceptable for a
reconciliation engine (runs are minutes-to-hours-scale operations already) but would be a real
cost in a low-latency system. Postgres itself becomes more central to *runtime* throughput, not
just storage — every idle worker replica is a `SELECT` every 3 seconds; at a much larger worker
fleet than this system runs, that polling load itself becomes a scaling concern a real broker
wouldn't have. No priority queue, no visibility into queue depth without a direct query, no
built-in dead-letter UI (dead-lettering exists — `attempt_count >= max_attempts` → `FAILED` —
but there's no broker dashboard to see it, only a DB query).

### 2.6 One Docker image, three different `command:`s

**Decision**: `app`, `worker`, and `reconciliation-worker` in `docker-compose.yml` all build
from the same `Dockerfile` (`build: .`) and differ only in their startup command
(`uvicorn ...` vs `python -m app.workers.ingestion_worker` vs `python -m
app.workers.reconciliation_worker`).

**Advantages**: one image to build, version, and keep dependency-consistent across all three
processes — no risk of the API server and a worker silently running different code versions of
shared modules (`app/datahub/canonical.py`, `app/reconciliation/dao.py`, etc., which both a
worker and the API import).

**Disadvantages**: the image carries FastAPI/uvicorn's dependencies even inside a worker
container that never serves HTTP, and vice versa — the workers' dependency footprint is larger
than a purpose-built worker image would need. Operationally, this shape also created a real,
repeated deployment trap during this project's development: because all three are separate
*images* built from the one Dockerfile, rebuilding only `app` (`docker compose build app`) after
a code change leaves `worker`/`reconciliation-worker` running stale code until they're
explicitly rebuilt too — `docker compose build app worker reconciliation-worker` together is
required every time, and this was missed more than once, causing live-tested behavior to lag
behind the actual code for a period.

### 2.7 `router → service → dao` layering, one module per domain

**Decision**: `app/datahub/` and `app/reconciliation/` (and the stubbed `app/auth/`) each carry
the same four-file shape — `router.py`, `service.py`, `dao.py`, `schema.py` — plus a
`constants.py` for shared vocabularies. A route handler never touches `asyncpg` directly; a DAO
method never raises an `HTTPException`.

**Advantages**: business rules (e.g. "an exception can only move from `OPEN`/`INVESTIGATING` to
a terminal status," "a definition creation auto-seeds its rule catalog and GL roles in the same
call so it's never left half-configured") live in exactly one place per concern, testable
independent of HTTP or SQL specifics. `dao.py` is where every raw SQL string lives, which is
also what makes the SQL-injection audit (§4.7) tractable — there's exactly one place to check.

**Disadvantages**: extra indirection for a genuinely trivial operation (a pure passthrough
"get by ID" still crosses all three layers) — more files to open to trace one request compared
to a framework that lets a route handler query directly.

---

## 3. Database schema decisions

Full field-by-field reference: `docs/database-schema.md`. This section covers *why the shape is
what it is*, not what every column is.

### 3.1 Multi-tenant hierarchy: `organizations → entities → everything`

Every business table (`customers`, `invoices`, `bank_statements`, `reconciliation_definitions`,
`gl_accounts`, ...) hangs off `entity_id`, and every `entities` row belongs to exactly one
`organizations` row. An `entity` represents one legal company/site (e.g. one subsidiary), not
one tenant/customer-of-the-platform — an `organization` is the tenant boundary; an
`organization` can have multiple `entities` (multiple subsidiaries reconciling independently
but administered together).

**Advantage**: reconciliation logic never has to ask "which entities should this query span" —
every working-set query (§5.1) is naturally scoped by a single `entity_id`, matching how
reconciliation is actually performed (one legal entity's books at a time). Multi-entity
organizations get shared user/role/permission management for free.

**Disadvantage**: there's no cross-entity reconciliation or consolidated reporting primitive in
the schema today — a genuinely intercompany reconciliation (the `INTERCOMPANY` API stub exists
in the frontend's route config but has no backend implementation) would need real design work,
not just a wider `WHERE` clause.

### 3.2 `raw JSONB` passthrough on every ingested table

`customers`, `invoices`, `bank_statements` all carry a `raw jsonb` column (migration `0019`
initially, refined in the ingestion integrity pass). It stores **only the source row's fields
the active mapping didn't capture** — not the whole row. A file whose every column is mapped
stores `NULL` here, not `{}`.

**Why**: a new source column showing up in tomorrow's file (a bank adds a new field to their
statement export) shouldn't *require* a migration before that data can be captured somewhere —
it lands in `raw` until someone deliberately maps it to a real column. This is also explicitly
why the earlier `staging_records` design (§3.3) was removed: the canonical tables already do
`raw`'s job, so a separate staging table's only reason to exist (catch what wasn't mapped)
became redundant.

**Advantages**: zero data loss on an unmapped column, no blocking migration in the ingestion
critical path; a field mapping can be extended later and reprocessed without the underlying
data having been silently dropped at ingest time.

**Disadvantages**: `raw` is opaque to SQL queries and reporting — data sitting in `raw` isn't
reportable/matchable until someone maps it to a real column. There's no automated signal
surfacing "this JSONB key appears in 40% of rows and might deserve a real column" — that
discovery is manual today.

### 3.3 Direct-to-canonical ingestion, not staging-then-promote (a reversed decision)

**The original design** (migration `0011`): every ingested row landed in a generic
`staging_records` table (`stream`, `txn_date`, `reference`, `counterparty`, `amount_minor`,
`raw jsonb`, `valid`, `issues`) and a separate `PROMOTE` job copied *reviewed* rows into
`bank_statements`/`invoices`/`customers`.

**Why it was reversed** (migration `0022`, per its own migration comment, echoed in
`docs/database-schema.md` §14 and `docs/data-hub.md` §1): the generic staging shape could never
represent any one stream's *real* columns — no `document_number`, `due_date`, `tds_rate_pct`, no
stream-specific FK resolution. "Promotion" would have had to re-derive most fields from `raw`
anyway, making the staging step pure overhead with no real review value, since the canonical
tables already had (or gained) their own `valid`/`issues`/`raw`/`source_job_id` columns to do
exactly what staging existed for.

**Advantages of the current (direct) design**: one insert path, not two; no "promotion" step to
forget to run or to drift out of sync with; a row is either accepted (with `valid=true` or
`false`, but present) or rejected (`failed_rows`) the moment the file is processed — no
in-between "reviewed but not promoted" limbo state to manage.

**Disadvantages** (stated plainly in `docs/data-hub.md` §9): a successful upload's rows are
**live, real data the moment the job finishes** — there is no human review gate between
ingestion and a row affecting reconciliation. A staging design, whatever its other costs, does
give you that gate for free. This system trades it away for simplicity and accepts the
consequence: a badly-mapped file's bad rows are either caught by validation (rejected into
`failed_rows`) or they're live immediately, with no "are you sure" step in between.

### 3.4 `field_mappings`: per-source → global-per-stream (a second reversed decision)

**Original**: a `field_mappings` row belonged to one `data_source` (via `source_id`), so every
org/entity connecting "a bank feed" reconfigured an identical mapping from scratch.

**Current** (migration `0026`): `field_mappings` is scoped by `stream` (`BANK`/`INVOICE`/
`CUSTOMER`/...), not `source_id` — one shared mapping set per stream, used by every data source
across every entity and organization ingesting that stream. Since real header text still varies
across independently-formatted files even for the same logical stream, matching became
case/whitespace-insensitive (`transforms.normalize_header`) rather than requiring a byte-exact
match, and the mapping is designed to accumulate *synonyms* — multiple `source_field` rows
targeting the same `canonical_field`.

**Advantages**: a new bank feed with a slightly different CSV header layout doesn't need its own
mapping configured from scratch — most headers are already covered by an existing synonym
across some other source's file, and new synonyms just get added, not a whole new mapping tree.

**Disadvantages** (explicitly called out in `docs/data-hub.md` §9): a mapping is now
*genuinely shared, global state* — if two data sources of the same stream need the same literal
`source_field` text mapped to two different `canonical_field`s, there is no way to express that;
the mapping is global, not per-source, so the last version saved wins for everyone. Not an
issue at current scale (checked: exactly one data source per stream in the live dataset) but a
real structural limit before scaling to many genuinely divergent sources of one stream. A
second, related gap found and documented but not yet fixed: no synonym-precedence mechanism — if
a source row has *both* a generic synonym and a more specific one populated with different
values, "first non-`None` value wins" picks whichever the DB happens to return first, not
necessarily the more authoritative one (`docs/reconciliation.md` §8).

### 3.5 Duplicate-upload protection: two independent layers

- **Job-level** (`ingestion_jobs.content_hash`, migration `0025`): a SHA-256 of the *entire
  uploaded file*, checked against prior jobs for the same source — rejects re-uploading the
  byte-identical file (`409`), before parsing even starts.
- **Row-level** (`bank_statements.row_hash`, `UNIQUE (entity_id, row_hash) WHERE row_hash IS NOT
  NULL`, migration `0025`): a SHA-256 of one row's *full original content*, computed **before**
  it's trimmed down to `raw`'s leftover-only shape (§3.2) — this ordering matters: if it hashed
  the already-trimmed `raw` column instead, every row of a fully-mapped file would trim to the
  same `{}`/`NULL` and hash identically, falsely flagging every row after the first as a
  duplicate.

**Why both, not one**: they catch different real scenarios. Content-hash catches "the exact same
file was uploaded twice" (a doubled click, a retry). Row-hash catches a harder case — two
*different* files sharing some overlapping rows (e.g. two monthly statement exports where one
re-exports the last week of the prior period too). `invoices`/`customers` already had this
protection for free via their natural-key uniqueness (`invoice_number`, `customer_code`);
`bank_statements`' only pre-existing unique constraint (`entity_id, document_number,
line_number`) is never populated by the real CSV ingestion path (those columns come from a
GL-style export format this system doesn't use), so it never actually engaged — `row_hash` was
added specifically to close that gap.

**Disadvantage worth naming**: this design has **no restatement/supersession concept**
(`docs/data-hub.md` §9). A legitimately *corrected*, re-issued bank statement — same rows,
fixed values — has no way to say "this batch replaces that one." The protection here is
pure-additive and only distinguishes accidental duplication from genuinely new data; it isn't
designed for intentional replacement, which would need a different mechanism entirely.

### 3.6 `gl_account_roles`: an indirection layer between the engine and the real chart of accounts

**Decision** (migration `0029`): the reconciliation engine never writes a literal
`account_code` when posting a journal entry. It writes to one of 8 fixed *semantic roles*
(`AR_CONTROL`, `CASH_CONTROL`, `BANK_CHARGES`, `TDS_RECEIVABLE`, `WRITE_OFF`,
`ON_ACCOUNT_ADVANCE`, `SUSPENSE`, `FX_GAIN_LOSS`), and `gl_account_roles` maps each role to
whatever real `gl_accounts` row a given entity actually uses for it. Every new `AR` definition
gets a baseline mapping auto-seeded (idempotently) the moment it's created.

**Why**: every entity's real chart of accounts differs (different account codes, different
numbering conventions) but the reconciliation engine's *logic* — "debit cash, credit AR" — is
universal. Hardcoding `"1200"` into `gl_posting.py` would break the moment a second entity used
a different chart of accounts.

**Advantages**: `gl_posting.py` is entity-agnostic code; onboarding a new entity with its own
chart of accounts requires no code change, only seeding its `gl_account_roles` rows. The engine
raises loudly (refuses to post) if an entity is missing a required role, rather than posting to
a wrong or default account.

**Disadvantages**: an extra indirection to trace when debugging "why did this post to that
account" — you follow role → `gl_account_roles` → `gl_accounts`, not a direct reference. The 8
roles are a fixed, closed vocabulary hardcoded in `constants.py`; adding a 9th semantic
distinction the engine needs to make (e.g. splitting `WRITE_OFF` by reason) is a code change
across both the constant and the posting logic, not just new data.

### 3.7 `payments.candidate_pool`: "unidentified, but narrowed" as a first-class stored state

**Decision**: `payments` has both `customer_id` (nullable — set only when Phase 1a *locks* an
identity) and `candidate_pool` (a JSONB array of candidate customer IDs, set only when Phase 1b
narrows but can't lock). These are mutually exclusive in practice: a row is locked, pooled, or
neither (pure Suspense) — never both.

**Why**: this is the schema's way of representing three genuinely different confidence levels
about "who paid this" as distinct, queryable states, rather than collapsing "weak signal" and
"no signal" into the same bucket. A locked payment and a pooled payment are allowed to reach very
different downstream treatment (§5.3) specifically because the schema keeps them distinguishable.

**Advantage**: the confidence distinction survives past the moment it was computed — a UI or a
later process can tell "we have a hunch" from "we have nothing" from "we're sure," instead of
that information existing only transiently inside the matching code's control flow.

**Disadvantage**: `candidate_pool`'s shape (a bare JSONB array of UUIDs) has evolved
informally as new consumers needed more context — the Suspense resolution UI now also wants a
*suggested* single candidate and suggested invoice IDs, which live in the exception's `detail`
JSONB (§3.8) instead of on `payments` itself, splitting "the pool" and "the best guess from the
pool" across two different tables' JSONB columns rather than one coherent structure.

### 3.8 `reconciliation_exceptions.detail` (JSONB) + `match_group_id` link

**Decision** (migration `0029`): exceptions carry a free-form `detail jsonb` column (candidate
lists for Double-Collision/Ambiguous, shortfall/tolerance figures for Short-Pay, the
sub-ledger/GL variance breakdown for `GL_VARIANCE`) and an optional FK back to the
`match_groups` row that produced them, when one exists (Short-Pay is the one exception type that
*does* have a real match behind it — see the table in §5.6).

**Why JSONB and not typed columns**: each `exception_type` needs a genuinely different detail
shape — a Double-Collision's detail is a list of competing customers; a GL-variance's is three
numbers; a Suspense's (added this session, for the resolution UI) is a suggested customer ID
plus suggested invoice IDs plus the rule that produced the suggestion. Modeling each shape as its
own nullable-column-per-type would mean a wide, mostly-`NULL` row and a new migration for every
new exception type's detail shape.

**Advantages**: adding a new exception type with a novel detail shape is a code-only change,
consistent with the no-enum philosophy (§2.2); the UI can present "here's exactly what the
engine saw and why it couldn't decide," not a generic "ambiguous" message.

**Disadvantages**: same trade as any JSONB "detail bag" — no schema enforcement that a given
`exception_type`'s `detail` actually has the shape the frontend expects; a typo in a key name on
the write side silently produces `undefined` on the read side rather than a query-time or
compile-time error. Consuming code has to know, out of band, which `exception_type` implies
which `detail` shape.

### 3.9 Hash-chained, append-only audit trail

**Decision** (migration `0017`, M4 scope — writer not yet implemented): `immutable_audit_trail`
has `prev_hash`/`row_hash` columns where `row_hash = hash(this row's content + the previous
row's row_hash)`. This is the one table using a `BIGSERIAL` PK instead of a UUID (§2.3) —
deliberately, because the hash chain needs a total, gap-free insert order to verify against, and
a UUID PK gives you neither ordering nor gap-freeness.

**Why**: a reconciliation audit trail's core value proposition is that it's provably unaltered —
"trust me, nobody edited this row" isn't good enough for an accounting control. A broken hash
chain (any row's stored `row_hash` no longer matches a fresh recomputation from its content +
its predecessor's hash) is detectable proof of tampering or deletion, not just a claim of
integrity.

**Advantages**: tamper-evidence without needing a separate blockchain/external-ledger
dependency — it's one Postgres table with a self-referential integrity property.

**Disadvantages**: the guarantee only holds if the database role that writes this table is
genuinely `INSERT`-only (no `UPDATE`/`DELETE` grant) — that's stated as the intent in
`docs/database-schema.md` §12 but explicitly **not yet enforced**, since it depends on real
auth/role infrastructure that doesn't exist yet (`app/auth/` is stubbed). Until that grant is
actually restricted at the database level, the tamper-evidence property is aspirational, not
real — anyone with write access to the table could rewrite the whole chain forward from a
tampered row and it would verify fine.

### 3.10 Reporting views are plain `CREATE VIEW`, not materialized

`v_report_matched` and `v_report_runs` (migration `0018`) are live views — every query
re-executes the underlying joins, there's no refresh step and no staleness window.

**Advantage**: always-current, zero cache-invalidation logic to get wrong.

**Disadvantage**: every report query pays the full join cost (`invoice_allocations` →
`match_groups` → `reconciliation_runs` → `invoices` → `customers`, plus a `bank_statements`
left-join and a correlated-subquery `users` name resolution) at read time — fine at current
data volume, would need to become materialized (with an explicit refresh/staleness trade) if
report query volume or table size grew significantly.

---

## 4. Data ingestion pipeline

Full mechanics: `docs/data-hub.md`. This section is the decision layer above it.

### 4.1 The flow, and what "done" means

```
POST /data-sources                         (once)   register a feed: entity, name, kind, stream
POST /field-mappings/{stream}/versions      (once per stream, not per source)
POST /ingestion-jobs                        (per file) upload; 202 immediately, status=PENDING
   worker polls (~3s), claims, parses every row, writes canonical rows directly
GET /ingestion-jobs/{id}                    poll for the outcome
GET /records?stream=&entity_id=             see what actually landed
```

A file is parsed with `csv.DictReader`, each row passed through `apply_mapping()` (the
transform engine, §4.2) to become a canonical dict, checked against the stream's `KNOWN_FIELDS`,
then inserted via the stream's dedicated insert function
(`insert_bank_row`/`insert_invoice_row`/`insert_customer_row`), which applies stream-specific
defaults, FK resolution (customer lookup), and required-field validation. A row that fails any
of this is caught as `RowRejected`, recorded in `ingestion_jobs.failed_rows`, and the batch
continues — this is today's actual behavior, and it's the subject of §4.6 below.

### 4.2 The transform engine: 9 generic transforms, explicitly no chaining

`app/datahub/transforms.py` — pure, stateless functions (`NONE`, `TRIM`, `UPPER`, `LOWER`,
`CONST`, `TO_MINOR_UNITS`, `NEGATE`, `PARSE_DATE`, `REGEX`), each applied to exactly one mapping
row. A field needing two operations (e.g. sign-flip *and* unit conversion) uses
`TO_MINOR_UNITS`'s own `negate` parameter, rather than composing two mapping rows.

**Why generic + stream-specific split**: `transforms.py` knows nothing about "invoice" or
"bank statement" — it's pure value transformation. `canonical.py` holds everything that *is*
stream-specific (required fields, defaults, the one real FK resolution). This split is also what
makes `POST /field-mappings/{stream}/preview` trustworthy: it calls the exact same
`apply_mapping()` function real ingestion uses, so "what will this mapping do" and "what did
this mapping actually do" can never drift apart into two slightly-different code paths.

**Why no chaining**: a chain of transforms multiplies the states a single field's value can pass
through, and multiplies the ways a chain can partially fail (which step failed? does the next
step run on the failed step's raw or defaulted output?). One transform per row keeps a mapping
row's behavior fully specified by three columns (`transform`, `transform_param`, and the
`source_field` it reads) — no ordering-dependent behavior between rows to reason about.

**Disadvantage**: a field genuinely needing two independent transforms neither of which has an
overloaded parameter for the other (unlike the `negate`-inside-`TO_MINOR_UNITS` case) has no
expressible mapping today — it would need a new dedicated transform kind rather than composing
existing ones.

### 4.3 Header matching is fuzzy; mapping *targets* are AI-stubbed, not yet real

**Decision**: a mapping row's `source_field` matches a file's actual column header after
`.strip().lower()` (`normalize_header`), not byte-for-byte — necessary once one mapping is
shared across independently-formatted files (§3.4). Separately, `app/datahub/ai_mapping.py` is
a *stubbed* integration point: any raw column matching no synonym is meant to be passed to an AI
suggestion step that, if confident, gets persisted back as a new synonym automatically. The
function currently always returns `[]` — no model is actually called yet. Unresolved columns
just accumulate in `ingestion_jobs.unmapped_columns`.

**Why stub it rather than skip the integration point entirely**: the rest of the pipeline (the
"if it returns guesses, merge them into the mapping, immediately usable for the rest of that
same job" logic in the worker) is already built and correct — swapping the stub for a real model
call is a contained, later change, not a redesign.

**Disadvantage of shipping the stub as-is**: every genuinely new column in every new file
requires a human to notice it in `unmapped_columns` and map it manually — there's no current
automation reducing that toil, despite the architecture being ready for it.

### 4.4 Crash safety: nested transactions (savepoints), not per-row commits

```python
async with conn.transaction():                       # outer: the whole batch
    for raw_row in rows:
        try:
            async with conn.transaction():            # nested -> Postgres SAVEPOINT
                await insert_fn(conn, ...)
        except RowRejected as exc:
            failed_rows.append({"raw": raw_row, "issues": [...]})
```

**Why nested, not flat**: the *inner* transaction isolates one row's failure — a rejected row
rolls back to its own savepoint and the loop continues, which is what produces `failed_rows`
without aborting the whole file. The *outer* transaction is what makes a mid-file worker
**crash** safe: if the process dies before the final `COMMIT`, Postgres rolls back everything
inserted so far, so a retry from row 1 is correct rather than duplicating already-inserted rows.

**Advantage**: this genuinely solves two different problems (partial-row-failure tolerance,
whole-batch crash safety) with one mechanism, no extra bookkeeping table needed to track "what
did I already insert before I died."

**Disadvantage**: it's this exact mechanism that makes today's ingestion "partial success by
row, atomic by crash" rather than "atomic by row count" — see §4.6, where a proposal exists to
change this trade deliberately.

### 4.5 Current behavior: partial success is allowed (`PARTIAL` status)

Today, a 10-row file where 5 rows fail validation inserts the 5 good rows and marks the job
`PARTIAL` — `row_count=10`, `error_count=5`, `failed_rows` holding the 5 rejects. The 5 good
rows are live, real data.

**Advantage**: the 5 good rows are usable immediately — no need to wait for a perfect file
before any of its data is available for reconciliation.

**Disadvantage**: re-uploading a corrected file after fixing the 5 bad rows risks re-inserting
the 5 already-good ones as duplicates, unless the duplicate-protection layers (§3.5) happen to
catch them (they generally do, via `row_hash`/natural keys — but that's a safety net catching a
consequence of this design, not something the design itself prevents). The user is left
reasoning about a genuinely mixed-state file: "which 5 of my 10 rows are already in, and which 5
do I still need to fix."

### 4.6 Proposed alternative (not yet implemented): all-or-nothing two-pass ingestion

`docs/Ingestion-plan.md` describes — as a plan, not yet built (confirmed: no `Pass 1`/`Pass 2`/
`valid_records` logic exists in the current `ingestion_worker.py`) — restructuring §4.4/§4.5's
row-by-row insert into two explicit passes: **Pass 1** validates every row in memory (Pydantic
schemas mirroring the DB constraints) with zero database writes; if *any* row fails, the whole
job is aborted with `status=FAILED` and the database is left completely untouched. Only if every
row is clean does **Pass 2** insert all of them inside one bulk transaction.

**Advantages this would bring over the current design**:
- A file is genuinely all-or-nothing from the caller's point of view — no mixed-state "5 of 10
  rows already landed" to reason about on a re-upload.
- Validation errors for the *whole* file are visible in one shot (Pass 1 doesn't stop at the
  first failure), so a user fixes every problem before re-uploading once, instead of
  discovering errors in successive partial-failure rounds.

**Disadvantages/costs of adopting it**:
- A file that's 99% good and 1% bad inserts *nothing* until the 1% is fixed — the opposite
  trade from §4.5's "get the good rows in now" advantage. For a large recurring feed with a
  small number of chronically-malformed rows, this could mean a bank feed never lands any data
  until every row is perfect, which may or may not be the desired behavior depending on how the
  business wants to treat a "mostly good" file.
- Requires holding the entire file's parsed+validated row set in memory before any DB write —
  a real constraint for very large files (the existing docs note real sample files in this
  domain run to 47K+ rows) that the current per-row-streaming design doesn't have.
- Introduces a second validation surface (Pydantic schemas in `canonical.py`) that has to be
  kept in sync with the actual DB constraints (`NOT NULL`, `CHECK`, FK) it's meant to
  pre-validate against — a drift between the two would either reject a row Postgres would have
  accepted, or (worse) accept one Postgres would then reject mid-Pass-2, defeating the "abort
  before any write" guarantee.

This is a genuine, currently-open design decision — not yet made either way in code. The
trade-off is fundamentally business-facing (does the user want partial-success-now or
all-or-nothing-correctness), not a pure engineering call.

### 4.7 Security & integrity fixes applied to this pipeline

Found via a structured review pass (migration `0025` + code changes), documented in
`docs/data-hub.md` §8, worth carrying into this doc as decisions in their own right:

- **SQL injection (critical)**: `dao.update_record` built its `SET` clause by interpolating the
  client's raw PATCH-body **keys** directly into SQL text. Fixed with two independent layers —
  a service-layer allowlist (`canonical.EDITABLE_FIELDS`, real table columns only, explicitly
  excluding PKs/`entity_id`/`source_job_id`/`customer_id`/`raw`/`issues`) and a DAO-layer regex
  backstop (`^[a-zA-Z_][a-zA-Z0-9_]*$`) inside `update_record` itself, so the function stays
  safe even if a future caller forgets the allowlist. A full audit of every other
  string-formatted SQL statement in the codebase confirmed this was the *only* vulnerable spot —
  every other f-string SQL builder interpolates identifiers from fixed, server-side dictionaries
  (`STREAM_TABLES`, `SEARCH_COLUMNS`), never client input.
- **Silent FX corruption**: the home-currency amount default (`amount_home_minor` defaulting to
  the native amount) now explicitly checks `currency == entity.home_currency` before applying —
  a mismatch with no explicit mapping is now a loud row rejection instead of a silently wrong
  number. No live FX-rate lookup happens even now (`fx_rates` exists, unused) — flagged as a
  follow-up, not fixed here.
- **`customer_code` normalization**: case/whitespace normalized at both write and lookup time,
  backed by a case-insensitive unique index (`uniq_customers_code_ci`) as a backstop against a
  code path that forgets to normalize.

### 4.8 Known gaps (carried forward honestly, not silently dropped)

- No auth/permission enforcement anywhere in this module — every endpoint trusts
  client-supplied `entity_id`/`source_id`/`job_id` with no ownership check.
- No enforced boundary between "ingested" and "trustworthy" — `valid`/`issues` are just columns;
  nothing stops a query from including `valid=false` rows.
- `LEDGER`/`GATEWAY` are valid `stream` values with zero canonical-table support — a job against
  either fails deterministically from attempt 1 but still burns several minutes of exponential
  backoff before dead-lettering, rather than failing fast.
- `failed_rows` is unbounded JSONB — a large, broadly-misconfigured-mapping file could balloon
  one job row with no pagination on the `GET` response.
- No mapping-save-time validation — a mapping missing a required field for its stream is only
  discovered row-by-row during a real upload, not at the moment the mapping is saved.
- Only `CSV` is actually parsed; `XLSX`/`MT940`/`OFX` are accepted as valid `format` values and
  will deterministically fail every job that uses them.

---

## 5. Reconciliation rule engine

Full milestone history and API surface: `docs/reconciliation.md`. Full worked example, every SQL
statement, every row written: `docs/reconciliation-invoice-flow-example.md`. This section is the
decision layer, corrected against the current code (see the note at the top of this document).

### 5.1 The phase model, and why the whole working set loads once per run

A run executes as a strict phase pipeline, all inside one DB transaction:

```
Phase 0  INTAKE_VALIDATION   dup-utr — reject duplicate bank_reference outright
Phase 1a CUSTOMER_LOCK       first rule to fire wins outright, locks one customer
Phase 1b CANDIDATE_POOL      only if 1a found nothing; narrows to a short list, never locks
         (Suspense)          if 1a and 1b both found nothing at all
Phase 2  ALLOCATION          which invoice(s) a locked/resolved payment settles
         (Short-Pay/No-Payment/Unapplied-Cash) — typed leftovers Phase 2 couldn't place
M3       GL posting          turns every outcome above into a real double-entry journal
         SL-vs-GL control proof — compares summed sub-ledger to the GL's stated balance
```

Before Phase 1 evaluates a single row, `engine.run_phase_1` loads the **entire** working set for
the entity into memory once: every unreconciled credit inflow, the full customer master, every
bank account, reference code, and expected remittance row. Phase 2 does the same for open
invoices and open memos. Every rule then runs as pure Python over these already-loaded lists —
no further `SELECT` against `customers`/`invoices`/`bank_statements` happens per row.

**Why**: this entity's data is small (tens to low hundreds of customers/invoices per run) —
matching in Python against an in-memory set is both simpler to write correctly and faster than a
query-per-rule-per-row pattern, which would be `O(rows × rules)` round-trips to the database.

**Advantage**: rule logic is trivially testable in isolation (pass in a small in-memory
`RuleContext`, no test database needed for the matching logic itself) and the whole run is one
transaction, so a mid-run failure rolls back cleanly with no partial matching state ever visible.

**Disadvantage**: this doesn't scale past "entity's data fits comfortably in worker memory."
A genuinely large entity (tens of thousands of open invoices) would need this redesigned into a
query-driven or batched approach — not a problem today, but a real ceiling on the current
design, not an oversight it's unaware of.

### 5.2 The rule catalog — every rule and its actual matching logic

20 rules, seeded onto every new `AR` definition (`DEFAULT_AR_RULE_CATALOG`,
`app/reconciliation/constants.py`), reconciled name-for-name against the frontend prototype's
canonical rule labels. `✅` = implemented; all listed here are implemented.

**Phase 0 — `INTAKE_VALIDATION`** (runs once per bank row, before identification starts)

| Priority | Rule | Logic |
|---|---|---|
| 0 | **Duplicate UTR Check** (`dup-utr`) | Checks the in-memory set of `bank_reference` values repeated within this same run's batch, then a live query for whether this reference already belongs to a row `recon_status='MATCHED'` from a *prior* run. A hit rejects the row outright as `DUPLICATE` — Phase 1a/1b never run for it. |

**Phase 1a — `CUSTOMER_LOCK`** (first rule to fire wins; locks exactly one customer)

| Priority | Rule | Logic |
|---|---|---|
| 10 | **Pre-Advised UTR Match** (`expected-utr`) | Exact match of the bank row's reference against `expected_remittances.utr_number` — a customer told you in advance "I'm sending this exact UTR." Highest-trust signal available. |
| 20 | **Payer Account & IFSC Match** (`account-ifsc`) | Bank row's `payer_account_no` + `payer_ifsc` looked up against `customer_bank_accounts`. |
| 30 | **UPI Handle Match** (`upi`) | Extracts a VPA (`name@bank`) from the narration via regex, compares against `customers.vpa_handle`. |
| 40 | **Customer Code in Narration Match** (`customer-code`) | Substring check: does any customer's `customer_code` literally appear in the narration text? |
| 50 | **Tax ID & PAN Match** (`gstin-pan`) | Extracts GSTIN/PAN patterns from the narration, compares against `customers.gstin`/`customers.pan`. |
| 60 | **Company Name Match** (`fuzzy-name`) | `pg_trgm` `similarity()` of the narration against `customers.company_name`, threshold 85%. Lowest-trust rule in this phase — still locks (unlike Phase 1b) because it clears the 85% bar. |

**Phase 1b — `CANDIDATE_POOL`** (only reached if every 1a rule missed; never locks — see §5.3)

| Priority | Rule | Logic |
|---|---|---|
| 10 | **Masked Account Suffix Match** (`account-suffix`) | Matches only the last 4+ digits of a bank account number — can genuinely return 2+ candidates if two customers share that suffix. |
| 20 | **Token-Based Narration Match** (`narration-tokens`) | Splits the narration into significant tokens, checks for substring overlap against each customer's `company_name` tokens — a much weaker signal than Phase 1a's exact matches. |

**Phase 2 — `ALLOCATION`** (scoped to the locked customer, or tried per-candidate for a pool —
see §5.3; two guardrails run first)

| Priority | Rule | Logic |
|---|---|---|
| 0 | **Period-cutoff guard** (`period-cutoff-guard`) | Drops invoices whose `issue_date` falls after the run's `period_end`. Deliberately filters on `issue_date`, not `due_date` — Net-30 terms routinely push `due_date` a month past `issue_date`, so filtering on `due_date` would wrongly exclude every not-yet-due invoice from a run covering cash received *within* the period. (This was found and fixed during the build — the original version filtered on `due_date` and would have excluded almost every invoice in the golden test dataset.) |
| 5 | **Memo net-off guard** (`memo-netoff-guard`) | Nets any open, invoice-linked `credit_debit_memos` against that invoice's balance before amount-matching starts. Best-effort: only memos already linked to a specific `invoice_id` are netted; a customer-level memo with no `invoice_id` is left alone. |
| 10 | **Exact Invoice Number Match** (`exact-invoice-num`) | The full invoice number appears verbatim in the narration. Result is `EXACT` if cash exactly covers the balance, `PARTIAL` if it falls short — the *same* rule produces both outcomes depending on amount, not two separate rules. |
| 20 | **Truncated Invoice Number Match** (`invoice-suffix`) | Same as above but only the last 4+ digits need to appear. |
| 30 | **Exact Amount Match** (`exact-amount`) | Payment exactly equals one open invoice's `balance_due_minor`. If it ties between two invoices with the same balance, this deliberately does **not** guess — it raises `MULTIPLE_INVOICE_MATCH` instead. |
| 40 | **TDS-Adjusted Amount Match** (`tds-match`) | Payment equals `balance_due_minor - allowed_tds_minor` — the customer legitimately withheld tax at source; a payment short by exactly that much is treated as fully settled, not a short-pay. |
| 50 | **Combined Invoice Match** (`subset-sum`) | Searches combinations of up to 10 open invoices (oldest due date first) for one that sums exactly to the payment. Only searches 2+-invoice combinations — a single-invoice exact sum is `exact-amount`'s job at a higher priority. |
| 60 | **Bank Fee Variance Match** (`bank-fee`) | The shortfall exactly equals the bank row's own `explicit_fee_minor` when set (preferred), else falls back to a generic ±₹5 tolerance. The fee amount is posted to a `BANK_CHARGES` gap line (§5.5), decoupled from the invoice rather than counted as a genuine partial payment. |
| 70 | **Small Balance Write-Off** (`write-off`) | A residual within ±₹5 tolerance gets written off to `WRITE_OFF` rather than left open indefinitely. **Shares its exact tolerance and adjacent priority with `bank-fee`, and evaluates second** — for a bank row with no `explicit_fee_minor` set, `bank-fee`'s generic fallback tolerance always wins the same residual before `write-off` ever gets a chance to fire. Documented, not yet fixed — a rule-catalog tuning question (should `write-off`'s default threshold be tightened, or should the two swap priority?), not a code bug. |
| 80 | **Overpayment to On-Account Credit** (`overpayment`) | Payment exceeds the invoice; the excess becomes unapplied/on-account credit, not an error. Targets whichever open invoice has the *smallest* excess (closest match), not an arbitrary one. |
| 90 | **Partial Payment Allocation** (`partial-payment`) | The universal fallback — applies to the customer's oldest open invoice (by due date) when nothing more specific claims the payment. |

Two phases exist in the schema/UI (`SHORT_PAY`, `UNAPPLIED`, `GL_CHECK`) as reserved phase
names with a single conceptual `threshold` rule each in the frontend prototype's rule catalog,
but have **no seeded, tunable catalog rows today** — their tolerances are hardcoded directly in
`engine.py`/`gl_posting.py` rather than configurable per definition. Closing that gap (real
tunable rows, wired into the currently-hardcoded checks) is scoped but not built.

### 5.3 Pass A / Pass B: why a pooled payment can never silently commit

**The decision this session made** (superseding an earlier version of the engine that let a
single-candidate pool match resolve automatically): Phase 2's allocation logic is split into two
passes over the payments a run needs to allocate.

- **Pass A** — every *pooled* payment (Phase 1b — no independently confirmed identity, only a
  weak hint) is resolved **entirely within Pass A**, and never as a real, committed match. Every
  allocation rule is tried against *every* candidate in the pool. Zero candidates producing a
  match → `SUSPENSE`. Exactly one candidate producing a clean match → **still `SUSPENSE`**, not
  a commit — the suggestion (which customer, which invoice(s), which rule) is recorded in the
  exception's `detail` so a human reviewing it doesn't have to re-derive it, but nothing is
  written to `match_groups`/`invoice_allocations` and no invoice balance moves. Two or more
  candidates each producing a clean match → `DOUBLE_COLLISION`.
- **Pass B** — only a payment that Phase 1a **locked** outright (an independently confirmed
  identity, not a guess) ever reaches Pass B, where a real match can actually commit
  (`match_groups`/`invoice_allocations` written, balances decremented, `bank_statements.
  recon_status` flipped).

**Why this is the right design, not overcaution**: a Phase 1b signal (account-suffix overlap,
narration-token overlap) is fundamentally a *hint about identity*, not a confirmation of it — an
exact-amount coincidence downstream doesn't retroactively make the identity guess trustworthy;
it just means two independently weak signals happened to agree, which is exactly the situation
that most looks safe to auto-commit and is most likely to be wrong in a way nobody double-checks.
This mirrors the frontend prototype's own reference design exactly (its `exactAmountRule` probe
logic): even a single-candidate exact-amount hit only ever sets a *suggestion*, never commits.

**Advantage**: false-positive customer misidentification — money applied to the wrong
customer's invoice, silently — cannot happen through the pool path, structurally, not by
convention. The cost of being wrong here (a misapplied payment closing the wrong invoice,
requiring a manual reversal to undo) is asymmetric enough to justify the friction.

**Disadvantage**: every pooled payment, even ones that turn out to have had an unambiguous
answer, becomes a human review item — there's no "auto-confirm if confidence is very high"
fast path. At a high pooled-payment volume, this is a real ongoing manual-review cost, not a
one-time design decision with no operational consequence. (This session also added a UI —
the Suspense resolution panel — precisely to make that manual review fast: it shows the
suggested candidate and its suggested invoices pre-selected, with a one-click confirm, plus a
fallback to browse and pick any open invoice across any customer manually if the suggestion is
wrong.)

### 5.4 Rule-outer / payment-inner ordering (a starvation fix)

**The problem this replaced**: an earlier version of Pass B iterated payment-outer,
rule-inner — for each payment (in whatever order they were loaded), try every rule until one
fires. This has a real starvation failure mode: if an earlier, lower-priority payment happens to
be processed first and its low-priority catch-all rule (e.g. `overpayment`, priority 80) claims
an invoice that a *later* payment's higher-priority rule (e.g. `bank-fee`, priority 60) actually
needed, the later payment loses out on the correct match purely because of iteration order — not
because the earlier rule was actually a better fit.

**The fix**: Pass B iterates **rule-outer, payment-inner**, grouped by resolved customer — for
each customer, for each rule in ascending priority, try that one rule against *every one* of
that customer's still-unmatched payments before moving to the next rule. This guarantees every
payment gets first crack at the highest-priority rule that could possibly match it, across the
whole batch, before any lower-priority rule gets a turn at any payment.

**Advantage**: match quality no longer depends on transaction-date ordering or load order — the
outcome is now a genuine function of rule priority and invoice availability, matching what
"priority order" is actually supposed to mean.

**Disadvantage**: this is more expensive than the naive version — for a customer with N pending
payments, each of the ~9 allocation rules is evaluated against every payment still remaining
after the previous rule's pass, so the worst case touches `rules × payments` rule evaluations
per customer rather than `payments × (rules until first match)`. At this system's data volumes
this is immaterial; it's a real trade if either number grew by orders of magnitude.

### 5.5 GL posting: one journal per bank transaction, four shapes

`gl_posting.post_run()` runs as the **last step inside the same transaction** as Phase 1/2 —
never speculatively mid-resolution, never as a separate worker pass. It posts **one journal
entry per `bank_txn`**, not per allocation (a subset-sum match against 3 invoices still produces
one journal, not three) — built from the in-memory outcomes Phase 1/2 already computed, handed
forward directly rather than re-queried (there's no `run_id` column on `payments`/
`invoice_allocations` to re-derive this by later; passing the outcome forward is the same
pattern Phase 1→Phase 2 already uses).

Four shapes cover every outcome a payment can reach:

1. **Normal settlement** — `Dr CASH_CONTROL` / `Cr AR_CONTROL` for the cash applied.
2. **A gap** (TDS withheld, a decoupled bank fee, or a small write-off) — `AR_CONTROL` credited
   for the invoice's *full* closed amount, `CASH_CONTROL` debited only for cash actually
   received, and the difference debits whichever role the firing rule maps to
   (`tds-match → TDS_RECEIVABLE`, `bank-fee → BANK_CHARGES`, `write-off → WRITE_OFF`).
3. **Unapplied/leftover cash** (overpayment excess, or a payment that never resolved) —
   `Dr CASH_CONTROL`, credited to `ON_ACCOUNT_ADVANCE` if a customer is known, `SUSPENSE`
   otherwise.
4. **Standalone bank charge** (`is_bank_charge=true`, never entered Phase 1/2 at all) —
   `Dr BANK_CHARGES` / `Cr CASH_CONTROL`, posted straight from `bank_statements`.

Every line resolves its real account through `gl_account_roles` (§3.6) and the module raises
before posting anything if a required role is missing, rather than writing a partial/unbalanced
journal. Each debit/credit pair is emitted together from the same source amount — never
computed independently and reconciled after the fact — so `sum(DEBIT) == sum(CREDIT)` holds by
construction, though there is deliberately **no database `CHECK` constraint** enforcing that; it
relies entirely on the posting code's own discipline.

**Advantage of "by construction, not by constraint"**: the invariant is enforced at the single
point where journal lines are actually generated, which is simpler than a cross-row `CHECK`
Postgres can't easily express anyway (a `CHECK` can't sum across rows of a table without a
trigger).

**Disadvantage**: a future code path that inserts into `gl_journal_lines` directly (bypassing
`gl_posting.py`) has no database-level safety net stopping it from writing an unbalanced entry.

### 5.6 Exception taxonomy and what "resolved" means

12 `exception_type` values exist in the schema's vocabulary; 8 are live (produced by real code
today), 4 are reserved with no producing rule yet (`OVERPAYMENT`, `BANK_CHARGE`,
`TIMING_DIFFERENCE`, `GATEWAY_VARIANCE` — the first two are deliberately *not* exceptions: an
overpayment is handled silently via on-account credit, a bank charge posts straight through with
no exception row, because neither is actually a problem needing review).

| Outcome | `payments` row | `match_groups`/`invoice_allocations` | Exception raised |
|---|---|---|---|
| Matched (exact or with a gap) | ✅ | ✅ | — |
| Short-Pay | ✅ | ✅ (for the partial amount) | `SHORT_PAY` |
| Overpayment | ✅ | ✅ | — (silent, on-account) |
| Multiple-Invoice-Match | ✅ | — | `MULTIPLE_INVOICE_MATCH` |
| Double-Collision | ✅ | — | `DOUBLE_COLLISION` |
| Suspense | ✅ | — | `SUSPENSE` |
| Duplicate | — | — | `DUPLICATE` |

`SHORT_PAY` is the **only** exception type that ever coexists with a real `match_groups`/
`invoice_allocations` row — every other exception type means the payment never actually touched
an invoice. This is a deliberate distinction the schema and the UI both need to respect: a
Short-Pay is "real money landed, but not enough" (still shows in a matched view *and* needs
review), while the rest are "nothing committed at all" (exception-only). Getting this wrong in
the frontend (a Short-Pay silently appearing in a "fully matched" list because a match row
technically exists) was a real bug found and fixed during this project — the frontend's Matched
tab now explicitly excludes Short-Pay's `match_group_id`s from its displayed "matched" set.

`resolution_outcome` on a resolved exception is one of `WRITEOFF | KEEPOPEN | DISPUTE | JOURNAL
| ON_ACCOUNT | MANUAL_MATCH` — the last one added this session specifically for the two new
manual-resolution flows below, to distinguish "a human matched this by hand" from an
engine-driven outcome.

### 5.7 Human-in-the-loop resolution UX (No-Payment, Suspense)

Two exception types this session gave dedicated resolution UIs, replacing a generic
reason/JSON-detail dump — both deliberately modeled on the reference prototype's own resolution
panels rather than invented fresh:

- **No-Payment-Received**: an open invoice nothing in this run touched. The panel offers the
  entity's currently-unapplied payments as candidates to manually match against it — since the
  root cause is usually "the payment exists somewhere in the system but didn't get identified
  or allocated to this specific invoice," not "the money doesn't exist."
- **Suspense**: a payment with no confirmed (or only pool-suggested — §5.3) identity. The panel
  shows, in order of trust: the engine's own suggestion (if Pass A produced one), the
  candidate-pool's other members (if the pool had more than one), and — added specifically in
  response to "why can't I match it to *any* open invoice" — a searchable, unscoped browser
  across every open invoice for every customer in the entity, for when the suggestion is wrong
  or the pool was empty. Picking an invoice from a different customer than currently selected
  starts a fresh selection, since a payment can only ever lock to one customer.

**Why build dedicated resolution flows rather than a generic "edit this JSON" form**: the
underlying design principle is the same one driving §5.3 — the engine deliberately declines to
guess in ambiguous cases, which only works as a *system* design if the human step that follows
is fast and well-scoped, not a raw data-entry chore. A generic edit-any-field form would have
made the "we didn't guess" discipline feel like a dead end instead of a deliberate handoff.

**Advantage**: resolution actions are typed and constrained (pick from real open invoices/
payments, not free-text), so a resolution can't accidentally reference a nonexistent invoice or
double-apply an already-fully-allocated payment — the same allocation rules and balance checks
the engine itself uses are reused for a manual match, not a separate, looser path.

**Disadvantage**: these panels are bespoke per exception type — adding a resolution UI for
`DOUBLE_COLLISION` or `MULTIPLE_INVOICE_MATCH` (not yet built) means writing another dedicated
panel, not configuring a generic one. There is no shared "resolution panel" abstraction across
exception types today, which will mean either accepted duplication or a refactor once a third or
fourth type gets the same treatment.

---

## 6. Workers & orchestration

### 6.1 `ingestion_worker.py`

Polls `ingestion_jobs` (§2.5), 3-second idle cadence, 5-minute lease, exponential backoff
(`30s × 2^attempt_count`) up to `max_attempts=5` before dead-lettering to `FAILED`.

### 6.2 `reconciliation_worker.py`

Structurally identical against `reconciliation_runs` (migration `0028` gave that table the same
lease/retry columns specifically to reuse this exact mechanism). Calls `engine.run()` — Phase 1,
Phase 2, GL posting, all one transaction — and writes the run's summary counters
(`volume`/`matched_count`/`exception_count`/`matched_value_minor`/`exception_value_minor`/
`unapplied_minor`) back once the whole run completes.

### 6.3 Why identical mechanics, not two different worker designs

Both workers are deliberately the same shape (claim query, heartbeat, nested-transaction
row/batch isolation where applicable, backoff/dead-letter policy) — a developer who understands
one worker's failure modes understands the other's for free, and a fix to one class of bug
(e.g. the lease-heartbeat zombie-write guard) is a pattern to apply to the other, not a
from-scratch design problem. The cost is the one already named in §2.6: this uniformity is
purchased by both workers shipping in the same Docker image as the API server, carrying
dependencies neither worker itself needs.

---

## 7. Known gaps and deferred decisions (consolidated)

Pulled together from across the module-specific docs, so the honest "not done yet" list isn't
scattered:

| Gap | Where | Status |
|---|---|---|
| No auth/permission enforcement anywhere | ingestion + reconciliation | `app/auth/` stubbed; every endpoint trusts client-supplied IDs |
| Audit trail's `INSERT`-only DB grant | `immutable_audit_trail` | Intent documented, not enforced — depends on auth |
| `reviewed_by ≠ prepared_by` segregation of duties | Sign-off (M4) | Not built — depends on auth |
| No restatement/supersession concept | ingestion | Pure-additive duplicate protection only |
| `field_mappings` has no synonym-precedence | ingestion | "First non-`None` wins," not necessarily most-authoritative |
| No ingestion path for `customer_bank_accounts`, `expected_remittances`, `customer_reference_codes`, `credit_debit_memos`, `gl_control_balances` | ingestion | Seeded directly via SQL today, not a repeatable non-engineer flow |
| No ingestion path derives `allowed_tds_minor`/`tds_rate_pct` | ingestion | `tds-match` computes it at match time, but the source values are seeded, not uploaded |
| `bank-fee` and `write-off` share tolerance/adjacent priority | rule engine | `bank-fee` always wins the shared case; a tuning question, not a bug |
| No counter column for "pooled, unresolved" at the run level | rule engine | Tracked in-memory/in logs only, not persisted on `reconciliation_runs` |
| Memo net-off guardrail is best-effort | rule engine | Only invoice-linked memos are netted; customer-level memos aren't |
| No resolution UI yet for Double-Collision / Multiple-Invoice-Match | rule engine UX | Suspense and No-Payment got dedicated panels this session; these two didn't |
| `LEDGER`/`GATEWAY` streams accepted but unsupported | ingestion | Fails deterministically but burns full backoff time first |
| `uniq_reconciled_ref` is a *global*, not entity-scoped, unique index | schema | Causes live-vs-test-fixture collisions during test runs; known, not yet fixed |
| Two-pass all-or-nothing ingestion | ingestion | Proposed (`docs/Ingestion-plan.md`), not implemented — see §4.6 for the trade-off analysis |

---

## 8. Appendix: milestone status

| Milestone | Scope | Status |
|---|---|---|
| M0 | Schema scaffold, `app/reconciliation/` skeleton, GL role seeding | ✅ Done |
| M1 | Phase 1a/1b identification, `payments`, Suspense routing | ✅ Done |
| M2 | Phase 2 scoped allocation (11 rules incl. guardrails), balances | ✅ Done, redesigned this session (§5.3, §5.4) |
| M3 | GL posting, SL-vs-GL control proof, exception resolve API | ✅ Done |
| M4 | Sign-off, hash-chained audit trail writer, full Rules Studio API | ⬜ Not started |

Detailed milestone-by-milestone build log: `docs/reconciliation.md` §1-2.
