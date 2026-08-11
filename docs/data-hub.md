# Data Hub — Implementation Reference

This document covers the full data-hub module as it currently stands: schema, ingestion flow, worker mechanics, API surface, the integrity/security fixes applied, and known gaps. It reflects the code as of migration `0026`.

## Contents

1. [Architecture & design decisions](#1-architecture--design-decisions)
2. [Database schema](#2-database-schema)
3. [The ingestion flow, end to end](#3-the-ingestion-flow-end-to-end)
4. [Field mapping & the transform engine](#4-field-mapping--the-transform-engine)
5. [Per-stream canonical insert logic](#5-per-stream-canonical-insert-logic)
6. [The background worker](#6-the-background-worker)
7. [API reference](#7-api-reference)
8. [Data integrity & security fixes](#8-data-integrity--security-fixes)
9. [Known limitations / open gaps](#9-known-limitations--open-gaps)
10. [File structure](#10-file-structure)

---

## 1. Architecture & design decisions

The data hub ingests external files (bank statements, sub-ledger/invoice exports, customer masters) and writes them **directly into canonical business tables** — `bank_statements`, `invoices`, `customers`. There is no intermediate staging table (an earlier design used one; it was removed — see below) and no separate "promote" step. A successful upload's rows are live, real data the moment the job finishes.

Key decisions, in the order they were made:

- **No ORM.** All database access is raw SQL via `asyncpg`. No SQLAlchemy, no Alembic. Migrations are hand-written, numbered `.sql` files applied by a small custom runner (`app/db/migrate.py`).
- **No native Postgres enums.** Every status/category/type column is plain `TEXT`, validated only in application code. Rationale: enum types need `ALTER TYPE` for every new value; a fast-moving business domain adds categories often.
- **`stream` is a property of the data source, not the upload.** Originally, the client chose `stream` (`BANK`/`INVOICE`/`CUSTOMER`) on every upload call, independent of which data source was selected. This let a file get ingested against the wrong canonical table if the frontend's stream-guessing logic (based on fuzzy-matching a source's display name) got it wrong — which it did, in production use, twice. Fixed by moving `stream` onto `data_sources` (set once at creation, immutable) and deriving it server-side from `source_id` on every upload. The client cannot request a stream anymore.
- **Direct-to-canonical, not staging-then-promote.** An earlier design landed every ingested row in a generic `staging_records` table (columns: `txn_date`, `reference`, `counterparty`, `amount_minor`, ...) and required a separate `PROMOTE` job to copy reviewed rows into the real tables. This was replaced because: (a) the generic staging shape couldn't represent any stream's full real column set (no `document_number`, `due_date`, `tds_rate_pct`, etc. — so "promotion" would have had to re-derive most fields anyway), and (b) `bank_statements`/`invoices`/`customers` already have `raw JSONB` for exactly the same "catch whatever wasn't mapped" purpose staging existed for. `staging_records` was dropped in migration `0022`; canonical tables gained `valid`/`issues`/`raw`/`source_job_id` columns to take over its job. There is no human review gate between ingestion and a row being live — see [§9](#9-known-limitations--open-gaps).
- **Lease-based worker, not a message broker.** Ingestion is asynchronous: upload returns `202` immediately, a background worker polls for work. No Redis/RabbitMQ/Celery — the job queue *is* the `ingestion_jobs` table, claimed via `SELECT ... FOR UPDATE SKIP LOCKED`. This means enqueueing a job and recording its metadata are the same atomic write, multiple worker replicas can run with zero coordination, and a crashed worker's job self-heals via lease expiry rather than needing manual intervention.
- **Transform engine is generic; per-stream logic is not.** `transforms.py` holds 9 pure, stream-agnostic functions (`PARSE_DATE`, `TO_MINOR_UNITS`, etc.) shared by real ingestion and the `/preview` endpoint — so preview can never lie about what a real upload will do. `canonical.py` holds the parts that *are* stream-specific: required-field validation, defaults (home-currency amount, invoice balance), and the one real foreign-key resolution (customer lookup).
- **`field_mappings` is global per stream, not per data source (migration `0026`).** Originally a mapping belonged to one `data_source`, which belongs to one `entity` — so every org/entity connecting "a bank feed" had to reconfigure an identical mapping from scratch. Since real header text still varies across independently-formatted files even for the same stream, `apply_mapping` now matches `source_field` against the raw file's headers case/whitespace-insensitively (`transforms.normalize_header`) rather than requiring a byte-exact match — the shared dictionary is meant to accumulate synonyms (multiple `source_field` rows mapping to the same `canonical_field`), not assume one canonical spelling. `app/datahub/ai_mapping.py` is a stubbed integration point: any raw column that matches no synonym is meant to go through an AI guess that gets persisted back as a new synonym, but the actual model call isn't implemented — it always returns no suggestions, and unresolved columns are recorded on the job instead (`ingestion_jobs.unmapped_columns`).

---

## 2. Database schema

### `data_sources` — a registered feed

| Column | Type | Notes |
|---|---|---|
| `source_id` | `uuid` PK | |
| `entity_id` | `uuid` NOT NULL, FK → `entities` | |
| `name` | `text` NOT NULL | e.g. "HDFC CMRG-1240" |
| `kind` | `text` NOT NULL | `BANK_FEED` \| `GATEWAY` \| `ERP` \| `MANUAL_UPLOAD` — descriptive only, not used for dispatch |
| `stream` | `text` NOT NULL | `BANK` \| `INVOICE` \| `CUSTOMER` \| `LEDGER` \| `GATEWAY` — **the** dispatch key; fixed at creation, no update path exists |
| `status` | `text` NOT NULL DEFAULT `'CONNECTED'` | `CONNECTED` \| `PENDING` \| `ERROR` — not currently enforced anywhere (uploads against an `ERROR` source still succeed) |

Migrations: `0011` (create), `0023`/`0024` (add `stream`, backfill, `NOT NULL`).

### `field_mappings` — how to translate a stream's columns (global, not per source)

| Column | Type | Notes |
|---|---|---|
| `mapping_id` | `uuid` PK | |
| `stream` | `text` NOT NULL | `BANK` \| `INVOICE` \| `CUSTOMER` \| `LEDGER` \| `GATEWAY` — the scoping key. **Not** `source_id` as of `0026`: one mapping set is shared by every data source/entity/org ingesting that stream, instead of each source configuring its own copy |
| `version` | `int` NOT NULL | Versioned; `POST .../field-mappings/{stream}/versions` submits the **whole** set, not a diff |
| `source_field` | `text` NOT NULL | Column name as it appears in the raw file. Multiple rows may target the same `canonical_field` with different `source_field` values — that's how the shared dictionary represents synonyms across differently-formatted sources |
| `canonical_field` | `text` NOT NULL | Target column on the stream's real table (see [§5](#5-per-stream-canonical-insert-logic)'s `KNOWN_FIELDS`) |
| `transform` | `text` NOT NULL DEFAULT `'NONE'` | One of the 9 transforms, [§4](#4-field-mapping--the-transform-engine) |
| `transform_param` | `text` NULL | Meaning depends on `transform` |
| `is_active` | `bool` NOT NULL | Only one version per **stream** is active; saving a new version flips the old one off in the same transaction |

Migrations: `0011` (create, originally `source_id`-scoped), `0026` (re-scoped to `stream`; `source_id` dropped, backfilled from each row's data source before dropping).

### `ingestion_jobs` — the work queue

| Column | Type | Notes |
|---|---|---|
| `job_id` | `uuid` PK | |
| `source_id` | `uuid` FK → `data_sources` | |
| `file_name`, `format`, `file_uri` | `text` | `format`: only `CSV` is actually parsed; `file_uri` points into the shared `/data/uploads` volume |
| `stream` | `text` | Copied from the source at upload time |
| `status` | `text` DEFAULT `'PENDING'` | `PENDING → RUNNING → SUCCESS/PARTIAL`, or `PENDING` (retry) `→ FAILED` |
| `row_count`, `error_count` | `int` | |
| `failed_rows` | `jsonb` | Rows that couldn't be inserted at all (`{raw, issues}` each) — **unbounded**, see [§9](#9-known-limitations--open-gaps) |
| `attempt_count`, `max_attempts` | `int` | Retry bookkeeping, `max_attempts` defaults to 5 |
| `locked_by`, `locked_at`, `lease_expires_at` | | Lease-claim state, cleared on completion |
| `next_attempt_at` | `timestamptz` | Exponential backoff target for the next retry |
| `last_error` | `text` | |
| `mapping_version` | `int` | Which `field_mappings.version` (for this job's `stream`) was active when this job ran — audit trail |
| `unmapped_columns` | `text[]` | Raw file headers that matched no synonym in the active stream mapping and that the AI-suggestion stub ([§4](#4-field-mapping--the-transform-engine)) couldn't resolve either. `NULL`/empty in the common case |
| `content_hash` | `text` | SHA-256 of the uploaded file, checked against prior jobs for the same source to reject exact re-uploads |
| `job_type`, `parent_job_id` | `text`, `uuid` | **Vestigial** — leftover from the removed staging/promote design (`PROMOTE` job type). Always `'INGEST'`/`NULL` now; no code sets them otherwise. Not dropped, just unused. |
| `started_by` | `uuid` FK → `users` | Always `NULL` today — auth isn't wired in |

Indexes: `idx_ingestion_jobs_claimable (status, next_attempt_at)` (the worker's claim query), `idx_ingestion_jobs_content_hash (source_id, content_hash)`.

Migrations: `0011` (create), `0020` (lease columns), `0021` (`job_type`/`parent_job_id`/`stream`/`mapping_version` — job_type/parent_job_id later orphaned by `0022`), `0022` (`failed_rows`), `0025` (`content_hash`), `0026` (`unmapped_columns`).

### Canonical tables — `bank_statements`, `invoices`, `customers`

These are the platform's real domain tables (designed independently of ingestion, much earlier). Ingestion added the following columns to each, uniformly:

| Column | Type | Purpose |
|---|---|---|
| `raw` | `jsonb` | The original source row, verbatim — catches anything the mapping didn't cover |
| `valid` | `bool` NOT NULL DEFAULT `true` | `false` if any mapped field had a transform issue (row still inserted) |
| `issues` | `text[]` | One entry per problem, human-readable |
| `source_job_id` | `uuid` FK → `ingestion_jobs` | Which job produced this row |

Stream-specific integrity columns added by the fixes in [§8](#8-data-integrity--security-fixes):

- **`bank_statements.row_hash`** (`text`) + `UNIQUE (entity_id, row_hash) WHERE row_hash IS NOT NULL` — SHA-256 of the row's `raw` content; rejects a byte-identical row even from a different upload.
- **`customers`**: `UNIQUE (entity_id, upper(customer_code))` — case-insensitive backstop on top of application-level normalization.

Pre-existing natural-key protection (not added for ingestion, but relevant to it): `invoices` has `UNIQUE (entity_id, invoice_number)`; `customers` has `UNIQUE (entity_id, customer_code)`. **`bank_statements` has no natural-key protection beyond `row_hash`** — its other unique constraint, `(entity_id, document_number, line_number)`, is never populated by the CSV ingestion path (those columns come from GL-style exports only), so it never engages for ingested rows.

Migrations: `0019` (`raw` added to `customers`/`invoices`, `bank_statements` already had it), `0022` (`valid`/`issues`/`source_job_id` added to all three), `0025` (`row_hash`, case-insensitive customer index).

### Supporting tables used by ingestion

- **`entities`** — `home_currency` is read by the FX fix ([§8](#8-data-integrity--security-fixes)) to decide whether the native amount can default into the home-currency field.
- **`currencies`** — `bank_statements.currency`/`invoices.currency` FK here; an unrecognized code hard-fails the insert (caught generically as a row rejection, not a worker crash).
- **`customer_reference_codes`** (`customer_id`, `code_type`, `code_value`, `is_active`) — pre-existing table, originally unused by ingestion. Now consulted as a fallback when an invoice's `customer_code` doesn't match `customers.customer_code` directly, letting a customer be found under an alternate code (ERP ID, business-partner code, etc.) from a different source system's namespace.

---

## 3. The ingestion flow, end to end

```
1. POST /data-sources          (once) — register a feed: entity, name, kind, stream
2. POST /field-mappings/{stream}/versions  (once per stream, shared by every source using it — not once per source)
3. POST /ingestion-jobs         (per file) — upload; returns 202 immediately, status=PENDING
4. Worker polls (~3s cadence), claims the job, processes every row, writes status=SUCCESS/PARTIAL/FAILED
5. GET /ingestion-jobs/{id}      — poll for the outcome
6. GET /ingestion-jobs/{id}/records  or  GET /records?stream=&entity_id=  — see what landed
```

Concretely, for one uploaded row: the worker reads it via `csv.DictReader`, calls `apply_mapping(raw_row, mappings)` (the transform engine, [§4](#4-field-mapping--the-transform-engine)) to get a `canonical` dict, checks it against `KNOWN_FIELDS[stream]` for unrecognized targets, then calls the stream's insert function (`insert_bank_row`/`insert_invoice_row`/`insert_customer_row`, [§5](#5-per-stream-canonical-insert-logic)), which applies stream-specific defaults/FK resolution, validates required fields, and either inserts the row or raises `RowRejected` (caught per-row, recorded in `failed_rows`, doesn't fail the batch).

Nothing pushes work to the worker — see [§6](#6-the-background-worker) for exactly how it discovers jobs.

---

## 4. Field mapping & the transform engine

`app/datahub/transforms.py`. `apply_transform(raw_value, transform, transform_param)` — pure, stateless. `apply_mapping(raw_row, mappings)` runs every configured mapping row through it and returns `(canonical_dict, issues_list)`; a single field's failure becomes an issue string, never an exception.

| Transform | Behavior |
|---|---|
| `NONE` | passthrough |
| `TRIM` / `UPPER` / `LOWER` | string cleanup |
| `CONST` | ignore the raw value, always return `transform_param` (defaults a field the source doesn't supply) |
| `TO_MINOR_UNITS` | parse a decimal (strips commas), multiply by 100 → `int`. `transform_param='negate'` also flips sign; otherwise `transform_param` overrides the multiplier |
| `NEGATE` | numeric sign flip, returns a string |
| `PARSE_DATE` | tries formats from `transform_param` (comma-separated) or a default list, returns a real `datetime.date` (not a string — asyncpg needs the native type) |
| `REGEX` | `transform_param` is a pattern with one capture group; returns `group(1)` |

Deliberately **no chaining** — one transform per mapping row. A field needing two operations (e.g. sign-flip *and* unit conversion) uses `TO_MINOR_UNITS`'s own `negate` param rather than composing two rows.

`POST /field-mappings/{stream}/preview` calls the exact same `apply_mapping` function real ingestion uses — there is one code path for "what does this mapping do," not two that could drift apart.

**Header matching is case/whitespace-insensitive** (`transforms.normalize_header`, added in `0026`): a mapping row's `source_field` and the raw file's actual column header only need to match after `.strip().lower()`, not byte-for-byte. This matters now that one mapping is shared across independently-formatted files — "Amount", "amount", " Amount " from different sources' exports all resolve to the same synonym row instead of needing a separate mapping row (or failing) per casing variant.

**AI fallback (stubbed, not implemented)**: before processing a job's rows, the worker (`process_ingestion_job`) diffs the file's headers against the active mapping's `source_field`s (normalized); anything left over is passed to `app/datahub/ai_mapping.suggest_canonical_fields(stream, unmapped_columns, known_fields)`. If it returns guesses, they're merged into the stream's mapping via `dao.insert_mapping_version` (bumping the version, immediately usable for the rest of that same job) — the intent is a synonym dictionary that grows on its own as new file formats show up. The function is currently a hardcoded no-op (`return []`); real AI integration is out of scope here. Whatever's still unresolved after that call is written to `ingestion_jobs.unmapped_columns`.

---

## 5. Per-stream canonical insert logic

`app/datahub/canonical.py` — the stream-specific half of ingestion.

### `KNOWN_FIELDS` — valid mapping targets per stream

```python
KNOWN_FIELDS = {
    "BANK": {document_number, line_number, bank_reference, transaction_date, value_date,
             fiscal_year, fiscal_period, narration, payer_name, payer_account_no, payer_ifsc,
             currency, amount_minor, amount_home_minor, fx_rate, dr_cr,
             explicit_fee_minor, is_bank_charge, contra_reference},
    "INVOICE": {customer_code, invoice_number, issue_date, due_date, currency,
                total_amount_minor, total_home_minor, balance_due_minor,
                tds_rate_pct, allowed_tds_minor, status},
    "CUSTOMER": {customer_code, company_name, pan, gstin, vpa_handle,
                 payment_terms, credit_limit_minor, city, state},
}
```

A `canonical_field` outside this set for its stream isn't silently dropped — `unknown_field_issues()` flags it as an issue on the row (this used to be a silent-drop bug; fixed).

### `EDITABLE_FIELDS` — allowlist for `PATCH .../records/{id}`

A second, narrower set per stream — **real table columns**, not mapping-target names (`INVOICE`'s `KNOWN_FIELDS` includes `customer_code`, which isn't an `invoices` column at all, it's a lookup key — `EDITABLE_FIELDS["INVOICE"]` correctly omits it). Explicitly excludes primary keys, `entity_id`, `source_job_id`, `customer_id`, `raw`, `issues` — see [§8](#8-data-integrity--security-fixes) for why this exists.

### Standard derivations (not user-configurable, applied automatically)

- **Home-currency amount**: `amount_home_minor`/`total_home_minor` default to the native amount *only if* `currency == entity.home_currency` (checked via `_apply_home_currency_default`, which takes `home_currency` as a parameter fetched by the worker from `entities`). If they differ and no explicit mapping supplies the home amount, the row is rejected with a specific reason rather than silently defaulting.
- **Invoice `balance_due_minor`**: defaults to `total_amount_minor` (nothing paid yet).
- **Invoice `status`**: defaults to `'OPEN'`.

### Customer resolution (`insert_invoice_row`)

1. Normalize the mapped `customer_code` (`_normalize_code`: strip + uppercase).
2. Look up `customers.customer_code` (case-insensitively).
3. If not found, fall back to `customer_reference_codes.code_value` (case-insensitively, `is_active = true`).
4. If still not found, reject the row.

### Row/duplicate hashing

`row_hash(raw)` — `sha256(json.dumps(raw, sort_keys=True, default=str))`. Computed and stored on every `bank_statements` insert; the DB's unique index does the actual rejection (caught as a generic `asyncpg.PostgresError` → `RowRejected` by `_insert`'s `except` clause — no special-case code needed for this).

### `STREAM_INSERTERS` / `STREAM_TABLES` / `SEARCH_COLUMNS`

The three per-stream dictionaries everything else in the module dispatches through:

```python
STREAM_INSERTERS = {"BANK": insert_bank_row, "INVOICE": insert_invoice_row, "CUSTOMER": insert_customer_row}
STREAM_TABLES    = {"BANK": ("bank_statements", "bank_txn_id"), "INVOICE": ("invoices", "invoice_id"), "CUSTOMER": ("customers", "customer_id")}
SEARCH_COLUMNS   = {"BANK": "bank_reference", "INVOICE": "invoice_number", "CUSTOMER": "company_name"}
```

**`LEDGER` and `GATEWAY` are valid `stream` values with no entry in any of these three dicts.** A job with `stream=LEDGER` fails immediately in the worker (`STREAM_INSERTERS.get(...)` returns `None`) — see [§9](#9-known-limitations--open-gaps) for the retry-storm consequence.

---

## 6. The background worker

`app/workers/ingestion_worker.py`. Run standalone: `python -m app.workers.ingestion_worker` (a separate `worker` service in `docker-compose.yml`, same image as `app`, different `command:`).

### Discovery is polling, not push

```python
while True:
    job = await claim_job(pool)
    if job is None:
        await asyncio.sleep(3)
        continue
    await run_one_job(pool, job)
```

Nothing notifies the worker. It re-asks the database every 3 seconds when idle. Multiple worker replicas can run this identical loop against the same table with zero coordination between them — see the claim query below.

### The claim — one atomic statement

```sql
WITH claimed AS (
    SELECT job_id FROM ingestion_jobs
    WHERE (status = 'PENDING' AND (next_attempt_at IS NULL OR next_attempt_at <= now()))
       OR (status = 'RUNNING' AND lease_expires_at < now())
    ORDER BY started_at LIMIT 1 FOR UPDATE SKIP LOCKED
)
UPDATE ingestion_jobs SET status='RUNNING', locked_by=$1, lease_expires_at=now()+$2, attempt_count=attempt_count+1
FROM claimed WHERE ingestion_jobs.job_id = claimed.job_id
RETURNING ...;
```

`FOR UPDATE SKIP LOCKED` means a second worker racing for the same row just skips it and finds a different one — no blocking, no external lock manager. The `OR (status='RUNNING' AND lease_expires_at < now())` clause is what makes a **crashed worker's job self-healing**: another worker will pick it back up once its 5-minute lease expires, no manual intervention.

### Heartbeat (lease renewal)

A background task renews `lease_expires_at` roughly every 100 seconds while a job is processing. If a renewal ever finds the row no longer locked by this worker (another worker reclaimed it as abandoned), a stop flag is set and the eventual result is discarded — a "zombie" worker can't overwrite a fresher claim.

### Processing — transaction structure (crash-safety fix)

```python
async with conn.transaction():                       # outer: the whole batch
    for raw_row in rows:
        try:
            async with conn.transaction():            # nested → Postgres SAVEPOINT
                await insert_fn(conn, ..., home_currency=home_currency)
        except RowRejected as exc:
            failed_rows.append({"raw": raw_row, "issues": [...]})
```

The nested transaction isolates one row's failure (rolls back to its savepoint, loop continues) — this is what produces `failed_rows` without aborting the batch. The **outer** transaction is what makes a mid-file worker crash safe: if the process dies before reaching `COMMIT`, Postgres rolls back everything, so a retry starting from row 1 is correct instead of duplicating whatever had already been inserted.

### Completion / failure

- Success or partial: one `UPDATE` sets `status`, `row_count`, `error_count`, `failed_rows`, clears the lock.
- Any exception (bad format, unsupported stream, missing file, whatever): caught in `run_one_job`, backoff computed as `30s × 2^attempt_count`, `status` goes back to `PENDING` if attempts remain or `FAILED` at `attempt_count >= max_attempts` (5).

---

## 7. API reference

All routes mounted under `/api/v1` (see `app/main.py` — CORS middleware and this prefix were added after the initial build). No auth/permission checks are wired in anywhere in this module yet (see [§9](#9-known-limitations--open-gaps)).

### Data sources

| Method | Path | Notes |
|---|---|---|
| `POST` | `/data-sources` | Body requires `entity_id`, `name`, `kind`, `stream` |
| `GET` | `/data-sources` | Filter: `entity_id`, `kind` |
| `GET` | `/data-sources/{id}` | |
| `PATCH` | `/data-sources/{id}` | `name`/`status` only — `stream` has no update path (immutable by design) |

### Field mappings

Scoped by `stream`, not `source_id` (as of `0026`) — one shared mapping per stream, used by every data source/entity/org ingesting it.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/field-mappings/{stream}` | Active version only |
| `POST` | `/field-mappings/{stream}/versions` | Full replacement, bumps version, deactivates the old one — affects every source using this stream |
| `POST` | `/field-mappings/{stream}/preview` | Dry-run against sample rows; omit `mappings` to preview the active version, or pass a draft to test unsaved changes |

### Ingestion jobs

| Method | Path | Notes |
|---|---|---|
| `POST` | `/ingestion-jobs` | Multipart: `source_id`, `format`, `file`. **No `stream` field** — derived server-side. Returns `202`. `409` if `content_hash` matches a prior job for the same source. |
| `GET` | `/ingestion-jobs` | Filter: `source_id`, `status` |
| `GET` | `/ingestion-jobs/{id}` | Includes `failed_rows` |
| `POST` | `/ingestion-jobs/{id}/retry` | Only valid from `FAILED` (409 otherwise) |

### Records (Data Explorer)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/ingestion-jobs/{id}/records` | One upload's rows. Filter: `valid`, `search`, `limit`, `offset` |
| `GET` | `/records?stream=&entity_id=` | Every row of a stream's canonical table across **all** jobs/sources for an entity — not scoped to one upload |
| `GET` | `/ingestion-jobs/{id}/records/{record_id}` | |
| `PATCH` | `/ingestion-jobs/{id}/records/{record_id}` | Body is an untyped `dict` (columns differ by stream) — validated against `EDITABLE_FIELDS`, see [§8](#8-data-integrity--security-fixes) |

Both `GET .../records` endpoints return **untyped dicts**, not a typed Pydantic model — the three canonical tables have genuinely different shapes, and forcing them through one schema would misrepresent one or lose fields from another. This means these specific endpoints don't get the OpenAPI field-level documentation the rest of the API has.

---

## 8. Data integrity & security fixes

Applied together in one pass (migration `0025` + code changes), after a structured review surfaced them:

### SQL injection (critical, fixed)

`dao.update_record` built its `SET` clause from `fields.items()` — the **keys** of the client's raw PATCH-body dict were interpolated directly into SQL text with no validation. A payload like `{"amount_minor); DROP TABLE bank_statements; --": 1}` would have reached the database as literal SQL.

**Full audit result** (every string-formatted SQL statement in the codebase, checked): this was the *only* vulnerable spot. Four other similar-looking f-string SQL builders (`canonical.py`'s `_insert`, `dao.py`'s `_list_records`/`get_record`) all interpolate identifiers sourced from fixed, server-side dictionaries (`STREAM_TABLES`, `SEARCH_COLUMNS`) — never client input.

**Fix, two layers**:
1. `canonical.EDITABLE_FIELDS` — service-layer allowlist checked before any DAO call; unknown keys → `400`.
2. `dao.py`'s `_SAFE_IDENTIFIER` regex (`^[a-zA-Z_][a-zA-Z0-9_]*$`) — independent backstop inside `update_record` itself, so the function stays safe even if some future caller forgets the allowlist.

### Duplicate-upload protection

Two layers, catching different scenarios:
- **Job-level** (`ingestion_jobs.content_hash`): rejects re-uploading the byte-identical file against the same source, before parsing (`409`).
- **Row-level** (`bank_statements.row_hash`, unique index): rejects a byte-identical row even from a *different* file with partial overlap (e.g. two monthly statement exports sharing a few days).

`invoices`/`customers` already had natural-key uniqueness (`invoice_number`, `customer_code`) that happened to provide this protection; `bank_statements` did not, because its only prior unique constraint depends on columns (`document_number`, `line_number`) the CSV path never populates.

### Crash-safe transactions

See [§6](#6-the-background-worker) — outer transaction wraps the batch, nested transaction (savepoint) isolates each row. A worker crash mid-file now rolls back entirely instead of leaving a partial, duplicatable state.

### Silent FX corruption

See [§5](#5-per-stream-canonical-insert-logic)'s "standard derivations" — the home-currency amount default now checks `currency == home_currency` before applying; a mismatch with no explicit mapping is a loud row rejection, not a silently wrong number. No `fx_rates` lookup is performed (the table exists in the schema, unused) — that's a follow-up, not part of this fix.

### `customer_code` normalization

See [§5](#5-per-stream-canonical-insert-logic)'s "Customer resolution" — case/whitespace normalized at write and lookup time, backed by a case-insensitive unique index, plus fallback to `customer_reference_codes` for genuinely different code namespaces (e.g. an ERP's own business-partner code).

---

## 9. Known limitations / open gaps

Carried forward honestly rather than silently dropped:

- **No auth/permission enforcement anywhere in this module.** Every endpoint trusts client-supplied `entity_id`/`source_id`/`job_id` with no ownership check. `app/auth/` exists but is stubbed.
- **No enforced boundary between "ingested" and "trustworthy" data.** `valid`/`issues` are just columns — nothing (no view, no RLS) stops a future reconciliation-matching query from including `valid=false` rows. The convention exists only in this document, not in the schema.
- **No restatement/supersession concept.** A legitimately corrected re-issued bank statement has no way to say "this replaces that batch" — only pure-additive insert, protected from *accidental* duplication but not designed for *intentional* replacement.
- **`LEDGER`/`GATEWAY` streams have zero canonical support.** A job against either fails immediately and retries to exhaustion (several minutes of exponential backoff) before dead-lettering — the failure is deterministic from attempt 1, so this burns time for no benefit. Not fixed to fail-fast yet.
- **`failed_rows` is unbounded JSONB.** A large file with a broadly-misconfigured mapping (real sample files in this domain have run to 47K+ rows) could balloon a single job row and its `GET` response with no pagination.
- **No per-row caching within a single job.** `insert_invoice_row` does a fresh customer lookup query for every row, even when many rows share the same customer.
- **No worker healthcheck.** `docker-compose.yml` has one for `db`, not for `app`/`worker` — a silently-dead worker (this has happened) isn't detected automatically.
- **No mapping-save-time validation.** A mapping missing a required field for its stream isn't caught at `POST /field-mappings/{stream}/versions` — only discovered row-by-row during a real upload.
- **AI mapping suggestion is a stub.** `app/datahub/ai_mapping.suggest_canonical_fields` always returns `[]` — no model is actually called. Unmapped columns just accumulate in `ingestion_jobs.unmapped_columns` with no automated resolution path yet.
- **A conflicting mapping across sources of the same stream isn't reconciled.** Since `0026`, if two data sources of the same stream genuinely need a different `canonical_field` for the same literal `source_field` text, there's no way to express that (the mapping is global, not per-source) — the last version saved wins for everyone. Not an issue today (checked: exactly one data source per stream in the current dataset), but a real constraint of the global-mapping design worth knowing before scaling to many genuinely divergent sources per stream.
- **Only CSV is parsed.** `XLSX`/`MT940`/`OFX` are accepted as valid `format` values and will fail every job that uses them.
- **No automated tests.** Every fix and feature in this document was verified manually via `curl`/`psql` against a running stack, not via a test suite.
- **`ingestion_jobs.job_type`/`parent_job_id`** are vestigial (see [§2](#2-database-schema)) — harmless, but a maintenance trap for anyone who assumes they're live.

---

## 10. File structure

```
app/
  datahub/
    router.py       # FastAPI endpoints (§7)
    service.py       # business logic: validation, orchestration, HTTP errors
    dao.py            # raw SQL (asyncpg), no ORM
    canonical.py      # per-stream insert logic, KNOWN_FIELDS/EDITABLE_FIELDS, defaults, FK resolution (§5)
    transforms.py     # the 9-transform mapping engine (§4)
    schema.py         # Pydantic request/response models
    constants.py      # enums-as-constants, error messages
  workers/
    ingestion_worker.py   # the lease-based poll loop (§6)
  db/
    pool.py           # asyncpg pool + FastAPI dependency + jsonb codec
    migrate.py         # migration runner (`python -m app.db.migrate`)
migrations/
  0011_domain_data_hub.sql             # data_sources, ingestion_jobs, field_mappings, staging_records (later dropped)
  0019_domain_raw_passthrough.sql       # raw JSONB on customers/invoices
  0020_ingestion_worker_support.sql     # lease columns
  0021_ingestion_jobs_type_and_stream.sql
  0022_direct_to_canonical_ingestion.sql # drops staging_records; valid/issues/source_job_id on canonical tables
  0023, 0024_data_source_stream*.sql     # stream moved onto data_sources
  0025_ingestion_integrity_fixes.sql     # content_hash, row_hash, case-insensitive customer index
```
