"""Per-stream direct-to-canonical ingestion.

A parsed row is written straight into its real canonical table
(bank_statements / invoices / customers) - there's no intermediate staging
table. `apply_mapping`'s output only has the fields someone actually
configured a mapping for; this module fills in the handful of standard
derivations (home-currency amount defaults to the native amount *only when
the row's currency actually is the entity's home currency*, a fresh
invoice's balance_due defaults to its total), resolves the customer foreign
key (checking `customers.customer_code` first, then `customer_reference_codes`
for source systems that use a different code namespace), and rejects a row
outright if it still can't satisfy a required column - that rejection is
caught by the caller (the worker) and recorded on the job rather than
raised, so one bad row doesn't fail the whole batch.
"""
from __future__ import annotations

import hashlib
import json

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
        "currency", "amount_minor", "fx_rate", "dr_cr",
        "explicit_fee_minor", "is_bank_charge", "contra_reference",
    },
    "INVOICE": {
        "customer_code", "invoice_number", "document_number", "issue_date", "due_date", "currency",
        "total_amount_minor", "balance_due_minor",
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

# Columns `PATCH .../records/{id}` is allowed to touch, per stream - real
# table columns, not mapping-target names (INVOICE's KNOWN_FIELDS has
# `customer_code`, which isn't an `invoices` column at all, it's a lookup
# key). Deliberately excludes the PK, entity_id, source_job_id, raw, issues,
# and (for invoices) customer_id - none of those should be client-editable;
# reassigning entity_id or customer_id via this endpoint would silently
# reparent a row to a different tenant/customer.
EDITABLE_FIELDS = {
    "BANK": {
        "document_number", "line_number", "bank_reference", "transaction_date", "value_date",
        "fiscal_year", "fiscal_period", "narration", "payer_name", "payer_account_no", "payer_ifsc",
        "currency", "amount_minor", "amount_home_minor", "fx_rate", "dr_cr",
        "explicit_fee_minor", "is_bank_charge", "contra_reference", "valid",
    },
    "INVOICE": {
        "invoice_number", "document_number", "issue_date", "due_date", "currency",
        "total_amount_minor", "total_home_minor", "balance_due_minor",
        "tds_rate_pct", "allowed_tds_minor", "status", "valid",
    },
    "CUSTOMER": {
        "customer_code", "company_name", "pan", "gstin", "vpa_handle",
        "payment_terms", "credit_limit_minor", "city", "state", "valid",
    },
}

# Which single column Data Explorer's free-text search matches against, per stream.
SEARCH_COLUMNS = {
    "BANK": "bank_reference",
    "INVOICE": "invoice_number",
    "CUSTOMER": "company_name",
}


def unknown_field_issues(stream: str, canonical: dict) -> list[str]:
    known = KNOWN_FIELDS[stream]
    return [
        f"unknown canonical_field {f!r} for stream {stream} (ignored)"
        for f in canonical
        if f and str(f).strip() not in ("", "-") and f not in known
    ]


def _require(canonical: dict, fields: tuple[str, ...]) -> None:
    missing = [f for f in fields if canonical.get(f) is None]
    if missing:
        raise RowRejected(f"missing required field(s): {', '.join(missing)}")


def _normalize_code(value: str) -> str:
    """Case/whitespace normalization only - 'CUST-001' vs 'cust-001' vs
    ' CUST-001 ' are the same code. Does NOT solve 'CUST-001' vs 'CUST-1'
    (a genuinely different string for the same real customer) - that's what
    customer_reference_codes is for, not a normalization rule."""
    return value.strip().upper()


def row_hash(raw: dict) -> str:
    """Deterministic fingerprint of a raw ingested row, used to reject
    byte-identical rows re-ingested via a duplicate or overlapping upload."""
    canonical_json = json.dumps(raw, sort_keys=True, default=str)
    return hashlib.sha256(canonical_json.encode()).hexdigest()


def _apply_home_currency_default(
    canonical: dict, *, native_field: str, home_field: str, home_currency: str
) -> None:
    """Fills `home_field` from `native_field` only when the row's currency
    genuinely is the entity's home currency - otherwise leaves it unset (so
    `_require` reports it plainly)."""
    if canonical.get(home_field) is not None:
        return
    currency = canonical.get("currency")
    effective_home = home_currency or "INR"
    if currency == effective_home or not currency:
        canonical[home_field] = canonical.get(native_field)
    elif currency is not None:
        raise RowRejected(
            f"currency {currency!r} differs from entity home currency {effective_home!r}; "
            f"{home_field} must be explicitly mapped (no fx_rates lookup wired up yet)"
        )


async def _insert(conn: asyncpg.Connection, table: str, pk_column: str, values: dict) -> None:
    columns = list(values.keys())
    placeholders = [f"${i}" for i in range(1, len(columns) + 1)]
    sql = f"INSERT INTO {table} ({pk_column}, {', '.join(columns)}) VALUES (gen_random_uuid(), {', '.join(placeholders)})"
    try:
        await conn.execute(sql, *values.values())
    except asyncpg.PostgresError as exc:
        raise RowRejected(str(exc)) from exc


def _safe_int(val, default=0):
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val, default=None):
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


async def insert_bank_row(
    conn: asyncpg.Connection, *, entity_id, source_job_id, canonical: dict, raw: dict | None, issues: list[str],
    home_currency: str, row_hash_value: str,
) -> None:
    effective_home = home_currency or "INR"
    if not canonical.get("currency"):
        canonical["currency"] = effective_home
    if not canonical.get("dr_cr"):
        amt = canonical.get("amount_minor")
        canonical["dr_cr"] = "DEBIT" if amt is not None and isinstance(amt, (int, float)) and amt < 0 else "CREDIT"

    _apply_home_currency_default(
        canonical, native_field="amount_minor", home_field="amount_home_minor", home_currency=effective_home
    )
    _require(canonical, ("transaction_date", "currency", "amount_minor", "amount_home_minor", "dr_cr"))
    await _insert(conn, "bank_statements", "bank_txn_id", {
        "entity_id": entity_id,
        "source_job_id": source_job_id,
        "document_number": canonical.get("document_number"),
        "line_number": _safe_int(canonical.get("line_number"), None),
        "bank_reference": canonical.get("bank_reference"),
        "transaction_date": canonical["transaction_date"],
        "value_date": canonical.get("value_date"),
        "fiscal_year": _safe_int(canonical.get("fiscal_year"), None),
        "fiscal_period": _safe_int(canonical.get("fiscal_period"), None),
        "narration": canonical.get("narration"),
        "payer_name": canonical.get("payer_name"),
        "payer_account_no": canonical.get("payer_account_no"),
        "payer_ifsc": canonical.get("payer_ifsc"),
        "currency": canonical["currency"],
        "amount_minor": _safe_int(canonical.get("amount_minor"), 0),
        "amount_home_minor": _safe_int(canonical.get("amount_home_minor"), 0),
        "fx_rate": _safe_float(canonical.get("fx_rate"), None),
        "dr_cr": canonical["dr_cr"],
        "explicit_fee_minor": _safe_int(canonical.get("explicit_fee_minor"), 0),
        "is_bank_charge": bool(canonical.get("is_bank_charge")),
        "contra_reference": canonical.get("contra_reference"),
        "raw": raw,
        "row_hash": row_hash_value,
        "valid": len(issues) == 0,
        "issues": issues or None,
    })


async def insert_customer_row(
    conn: asyncpg.Connection, *, entity_id, source_job_id, canonical: dict, raw: dict | None, issues: list[str],
    home_currency: str, row_hash_value: str,  # both unused here, accepted for a uniform STREAM_INSERTERS call signature
) -> None:
    _require(canonical, ("customer_code", "company_name"))
    await _insert(conn, "customers", "customer_id", {
        "entity_id": entity_id,
        "source_job_id": source_job_id,
        "customer_code": _normalize_code(canonical["customer_code"]),
        "company_name": canonical["company_name"],
        "pan": canonical.get("pan"),
        "gstin": canonical.get("gstin"),
        "vpa_handle": canonical.get("vpa_handle"),
        "payment_terms": canonical.get("payment_terms"),
        "credit_limit_minor": _safe_int(canonical.get("credit_limit_minor"), None),
        "city": canonical.get("city"),
        "state": canonical.get("state"),
        "raw": raw,
        "valid": len(issues) == 0,
        "issues": issues or None,
    })


async def _resolve_customer_id(conn: asyncpg.Connection, *, entity_id, customer_code: str):
    normalized = _normalize_code(customer_code)
    customer = await conn.fetchrow(
        "SELECT customer_id FROM customers WHERE entity_id = $1 AND upper(customer_code) = $2",
        entity_id, normalized,
    )
    if customer is not None:
        return customer["customer_id"]
    # Fall back to alternate codes (ERP business-partner codes, etc.) registered
    # for a customer under a different namespace than our own customer_code.
    customer = await conn.fetchrow(
        "SELECT c.customer_id FROM customers c "
        "JOIN customer_reference_codes r ON r.customer_id = c.customer_id "
        "WHERE c.entity_id = $1 AND r.is_active = true AND upper(r.code_value) = $2",
        entity_id, normalized,
    )
    return customer["customer_id"] if customer is not None else None


async def insert_invoice_row(
    conn: asyncpg.Connection, *, entity_id, source_job_id, canonical: dict, raw: dict | None, issues: list[str],
    home_currency: str, row_hash_value: str,  # unused here, accepted for a uniform STREAM_INSERTERS call signature
) -> None:
    effective_home = home_currency or "INR"
    if not canonical.get("currency"):
        canonical["currency"] = effective_home

    customer_code = canonical.get("customer_code")
    customer_id = await _resolve_customer_id(conn, entity_id=entity_id, customer_code=customer_code) if customer_code else None
    if customer_id is None:
        # Not a reject, in either case - customer_code missing/unmapped
        # (deliberately, e.g. mapped to "-") and customer_code present but
        # not yet in the customer master are the same outcome: the invoice
        # is still ingested, unlinked. Reconciliation resolves it later via a
        # narration-based invoice-number match (or a human), backfilling
        # customer_id at that point. See migration 0031.
        # Mutates the caller's list in place (append, not `issues = issues +
        # [...]`) - ingestion_worker.py's own error_count/row status check
        # reads the same list object after this call returns, and a rebind
        # here would be invisible to it, silently reporting a clean SUCCESS
        # for a row that actually landed without a customer link.
        issues.append(
            f"unresolved customer_code={customer_code!r} "
            "(checked customers.customer_code and customer_reference_codes) for this entity - "
            "invoice ingested without a customer link"
            if customer_code else
            "no customer_code supplied - invoice ingested without a customer link"
        )

    _apply_home_currency_default(
        canonical, native_field="total_amount_minor", home_field="total_home_minor", home_currency=effective_home
    )
    if canonical.get("balance_due_minor") is None:
        canonical["balance_due_minor"] = canonical.get("total_amount_minor")
    _require(canonical, ("invoice_number", "issue_date", "due_date", "currency",
                          "total_amount_minor", "total_home_minor", "balance_due_minor"))

    await _insert(conn, "invoices", "invoice_id", {
        "entity_id": entity_id,
        "source_job_id": source_job_id,
        "customer_id": customer_id,
        "invoice_number": canonical["invoice_number"],
        "document_number": canonical.get("document_number"),
        "issue_date": canonical["issue_date"],
        "due_date": canonical["due_date"],
        "currency": canonical["currency"],
        "total_amount_minor": _safe_int(canonical.get("total_amount_minor"), 0),
        "total_home_minor": _safe_int(canonical.get("total_home_minor"), 0),
        "balance_due_minor": _safe_int(canonical.get("balance_due_minor"), 0),
        "tds_rate_pct": _safe_float(canonical.get("tds_rate_pct"), None),
        "allowed_tds_minor": _safe_int(canonical.get("allowed_tds_minor"), 0),
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
