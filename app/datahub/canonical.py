"""Per-stream direct-to-canonical ingestion.

A parsed row is written straight into its real canonical table
(bank_statements / invoices / customers) - there's no intermediate staging
table. `apply_mapping`'s output only has the fields someone actually
configured a mapping for; this module fills in the handful of standard
derivations (home-currency amount defaults to the native amount, a fresh
invoice's balance_due defaults to its total), resolves the one real
foreign-key relationship (an invoice's `customer_code` -> `customers.customer_id`),
and rejects a row outright if it still can't satisfy a required column -
that rejection is caught by the caller (the worker) and recorded on the job
rather than raised, so one bad row doesn't fail the whole batch.
"""
from __future__ import annotations

import asyncpg


class RowRejected(Exception):
    """A single row couldn't be turned into a canonical record - not fatal to the batch."""


# Every column a mapping is allowed to target, per stream - used to catch
# mappings pointed at a field this stream doesn't recognize (silently
# dropping those would be worse than flagging them as an issue).
KNOWN_FIELDS = {
    "BANK": {
        "document_number", "line_number", "bank_reference", "transaction_date", "value_date",
        "fiscal_year", "fiscal_period", "narration", "payer_name", "payer_account_no", "payer_ifsc",
        "currency", "amount_minor", "amount_home_minor", "fx_rate", "dr_cr",
        "explicit_fee_minor", "is_bank_charge", "contra_reference",
    },
    "INVOICE": {
        "customer_code", "invoice_number", "issue_date", "due_date", "currency",
        "total_amount_minor", "total_home_minor", "balance_due_minor",
        "tds_rate_pct", "allowed_tds_minor", "status",
    },
    "CUSTOMER": {
        "customer_code", "company_name", "pan", "gstin", "vpa_handle",
        "payment_terms", "credit_limit_minor", "city", "state",
    },
}

# (table, primary key column) - used by Data Explorer to read back what an
# INGEST job for this stream produced.
STREAM_TABLES = {
    "BANK": ("bank_statements", "bank_txn_id"),
    "INVOICE": ("invoices", "invoice_id"),
    "CUSTOMER": ("customers", "customer_id"),
}

# Which single column Data Explorer's free-text search matches against, per stream.
SEARCH_COLUMNS = {
    "BANK": "bank_reference",
    "INVOICE": "invoice_number",
    "CUSTOMER": "company_name",
}


def unknown_field_issues(stream: str, canonical: dict) -> list[str]:
    known = KNOWN_FIELDS[stream]
    return [f"unknown canonical_field {f!r} for stream {stream} (ignored)" for f in canonical if f not in known]


def _require(canonical: dict, fields: tuple[str, ...]) -> None:
    missing = [f for f in fields if canonical.get(f) is None]
    if missing:
        raise RowRejected(f"missing required field(s): {', '.join(missing)}")


async def _insert(conn: asyncpg.Connection, table: str, pk_column: str, values: dict) -> None:
    columns = list(values.keys())
    placeholders = [f"${i}" for i in range(1, len(columns) + 1)]
    sql = f"INSERT INTO {table} ({pk_column}, {', '.join(columns)}) VALUES (gen_random_uuid(), {', '.join(placeholders)})"
    try:
        await conn.execute(sql, *values.values())
    except asyncpg.PostgresError as exc:
        # e.g. an unrecognized currency code, or a duplicate natural key -
        # a real constraint violation, not a bug in this row's own data shape
        raise RowRejected(str(exc)) from exc


async def insert_bank_row(conn: asyncpg.Connection, *, entity_id, source_job_id, canonical: dict, raw: dict, issues: list[str]) -> None:
    if canonical.get("amount_home_minor") is None:
        canonical["amount_home_minor"] = canonical.get("amount_minor")
    _require(canonical, ("transaction_date", "currency", "amount_minor", "amount_home_minor", "dr_cr"))
    await _insert(conn, "bank_statements", "bank_txn_id", {
        "entity_id": entity_id,
        "source_job_id": source_job_id,
        "document_number": canonical.get("document_number"),
        "line_number": canonical.get("line_number"),
        "bank_reference": canonical.get("bank_reference"),
        "transaction_date": canonical["transaction_date"],
        "value_date": canonical.get("value_date"),
        "fiscal_year": canonical.get("fiscal_year"),
        "fiscal_period": canonical.get("fiscal_period"),
        "narration": canonical.get("narration"),
        "payer_name": canonical.get("payer_name"),
        "payer_account_no": canonical.get("payer_account_no"),
        "payer_ifsc": canonical.get("payer_ifsc"),
        "currency": canonical["currency"],
        "amount_minor": canonical["amount_minor"],
        "amount_home_minor": canonical["amount_home_minor"],
        "fx_rate": canonical.get("fx_rate"),
        "dr_cr": canonical["dr_cr"],
        "explicit_fee_minor": canonical.get("explicit_fee_minor") or 0,
        "is_bank_charge": canonical.get("is_bank_charge") or False,
        "contra_reference": canonical.get("contra_reference"),
        "raw": raw,
        "valid": len(issues) == 0,
        "issues": issues or None,
    })


async def insert_customer_row(conn: asyncpg.Connection, *, entity_id, source_job_id, canonical: dict, raw: dict, issues: list[str]) -> None:
    _require(canonical, ("customer_code", "company_name"))
    await _insert(conn, "customers", "customer_id", {
        "entity_id": entity_id,
        "source_job_id": source_job_id,
        "customer_code": canonical["customer_code"],
        "company_name": canonical["company_name"],
        "pan": canonical.get("pan"),
        "gstin": canonical.get("gstin"),
        "vpa_handle": canonical.get("vpa_handle"),
        "payment_terms": canonical.get("payment_terms"),
        "credit_limit_minor": canonical.get("credit_limit_minor"),
        "city": canonical.get("city"),
        "state": canonical.get("state"),
        "raw": raw,
        "valid": len(issues) == 0,
        "issues": issues or None,
    })


async def insert_invoice_row(conn: asyncpg.Connection, *, entity_id, source_job_id, canonical: dict, raw: dict, issues: list[str]) -> None:
    customer_code = canonical.get("customer_code")
    if not customer_code:
        raise RowRejected("missing required field(s): customer_code")
    customer = await conn.fetchrow(
        "SELECT customer_id FROM customers WHERE entity_id = $1 AND customer_code = $2", entity_id, customer_code
    )
    if customer is None:
        raise RowRejected(f"no customer found with customer_code={customer_code!r} for this entity")

    if canonical.get("total_home_minor") is None:
        canonical["total_home_minor"] = canonical.get("total_amount_minor")
    if canonical.get("balance_due_minor") is None:
        canonical["balance_due_minor"] = canonical.get("total_amount_minor")
    _require(canonical, ("invoice_number", "issue_date", "due_date", "currency",
                          "total_amount_minor", "total_home_minor", "balance_due_minor"))

    await _insert(conn, "invoices", "invoice_id", {
        "entity_id": entity_id,
        "source_job_id": source_job_id,
        "customer_id": customer["customer_id"],
        "invoice_number": canonical["invoice_number"],
        "issue_date": canonical["issue_date"],
        "due_date": canonical["due_date"],
        "currency": canonical["currency"],
        "total_amount_minor": canonical["total_amount_minor"],
        "total_home_minor": canonical["total_home_minor"],
        "balance_due_minor": canonical["balance_due_minor"],
        "tds_rate_pct": canonical.get("tds_rate_pct"),
        "allowed_tds_minor": canonical.get("allowed_tds_minor") or 0,
        "status": canonical.get("status") or "OPEN",
        "raw": raw,
        "valid": len(issues) == 0,
        "issues": issues or None,
    })


STREAM_INSERTERS = {
    "BANK": insert_bank_row,
    "INVOICE": insert_invoice_row,
    "CUSTOMER": insert_customer_row,
}
