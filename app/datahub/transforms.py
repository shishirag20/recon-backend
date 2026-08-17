"""Applies a data source's field_mappings to a raw ingested row.

Each mapping row is one (source_field -> canonical_field) pair plus a single
named transform. There's no chaining — a canonical field that genuinely needs
two operations (e.g. "flip sign, then convert to minor units") is handled by
TO_MINOR_UNITS' own `negate` transform_param rather than composing transforms,
since field_mappings intentionally has one transform column, not a pipeline.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

TRANSFORMS = (
    "NONE",
    "TRIM",
    "UPPER",
    "LOWER",
    "CONST",
    "TO_MINOR_UNITS",
    "NEGATE",
    "PARSE_DATE",
    "REGEX",
    "PARSE_BOOL",
    "TO_DECIMAL",
)

_DEFAULT_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y")

# A boolean-typed canonical column (is_bank_charge is the only one today)
# needs a real Python bool, not the string a source file spells it as -
# asyncpg raises rather than coercing a str for a boolean column.
_TRUE_VALUES = {"true", "1", "yes", "y"}
_FALSE_VALUES = {"false", "0", "no", "n"}

# staging_records' typed money columns are BIGINT minor units; any mapping
# targeting these two fields must produce an int, not a string.
_MONEY_CANONICAL_FIELDS = {"amount_minor", "amount_home_minor"}


def _clean_decimal(raw_value: str) -> Decimal:
    cleaned = raw_value.replace(",", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"not a decimal: {raw_value!r}") from exc


def apply_transform(raw_value, transform: str, transform_param: str | None):
    if transform not in TRANSFORMS:
        raise ValueError(f"unknown transform {transform!r}")

    if transform == "CONST":
        return transform_param

    if raw_value is None or (isinstance(raw_value, str) and raw_value.strip() == ""):
        return None

    if transform == "NONE":
        return raw_value
    if transform == "TRIM":
        return raw_value.strip()
    if transform == "UPPER":
        return raw_value.strip().upper()
    if transform == "LOWER":
        return raw_value.strip().lower()

    if transform == "TO_MINOR_UNITS":
        value = _clean_decimal(raw_value)
        multiplier = Decimal("100")
        if transform_param == "negate":
            multiplier = Decimal("-100")
        elif transform_param:
            multiplier = Decimal(transform_param)
        return int((value * multiplier).to_integral_value())

    if transform == "NEGATE":
        return str(-_clean_decimal(raw_value))

    if transform == "PARSE_DATE":
        formats = [f.strip() for f in transform_param.split(",")] if transform_param else list(_DEFAULT_DATE_FORMATS)
        text = raw_value.strip()
        for fmt in formats:
            try:
                return datetime.strptime(text, fmt).date()  # a real `date`, not a string - asyncpg needs the native type
            except ValueError:
                continue
        raise ValueError(f"could not parse date {raw_value!r} with formats {formats}")

    if transform == "REGEX":
        if not transform_param:
            raise ValueError("REGEX transform requires transform_param")
        match = re.search(transform_param, raw_value)
        if not match:
            raise ValueError(f"pattern {transform_param!r} did not match {raw_value!r}")
        return match.group(1)

    if transform == "PARSE_BOOL":
        text = raw_value.strip().lower()
        if text in _TRUE_VALUES:
            return True
        if text in _FALSE_VALUES:
            return False
        raise ValueError(f"not a recognized boolean: {raw_value!r}")

    if transform == "TO_DECIMAL":
        # A numeric-typed canonical column that isn't money (e.g.
        # invoices.tds_rate_pct, a percentage) - needs a real Decimal at
        # face value, no *100 scaling (that's TO_MINOR_UNITS' job) and no
        # raw string (asyncpg doesn't coerce a str for a numeric column,
        # same reasoning as PARSE_BOOL above).
        return _clean_decimal(raw_value)

    raise AssertionError("unreachable")  # every TRANSFORMS value is handled above


def normalize_header(name: str) -> str:
    """Case/whitespace-insensitive key for matching a mapping's
    `source_field` against a raw file's actual column header. Needed now
    that one mapping is shared globally per stream (see migration 0026)
    rather than configured per data source - independently-formatted files
    for "the same" column (e.g. "Amount" vs "amount " vs "AMOUNT") must all
    resolve to the same synonym row instead of requiring a byte-exact match."""
    return name.strip().lower()


def apply_mapping(raw_row: dict, mappings: list) -> tuple[dict, list[str]]:
    """Returns (canonical_fields, issues) for one raw source row.

    `mappings` is a list of objects/records exposing `source_field`,
    `canonical_field`, `transform`, `transform_param` (asyncpg Records or
    plain dicts both work).
    """
    canonical: dict = {}
    issues: list[str] = []
    normalized_raw = {normalize_header(k): v for k, v in raw_row.items()}
    for m in mappings:
        source_field = m["source_field"]
        canonical_field = m.get("canonical_field")
        if not canonical_field or str(canonical_field).strip() in ("", "-"):
            continue
        normalized_source = normalize_header(source_field)
        if m["transform"] != "CONST" and normalized_source not in normalized_raw:
            # This synonym's column isn't in this file at all - not the same
            # as "present but blank". Skip rather than let a genuine miss
            # overwrite a value a different synonym already found for this
            # canonical_field - a real risk now that one canonical field can
            # have many source_field synonyms (migration 0026). CONST is
            # exempt: it never reads the raw row at all, so it must still
            # fire even when its (unused) source_field placeholder is absent.
            continue
        raw_value = normalized_raw.get(normalized_source)
        try:
            value = apply_transform(raw_value, m["transform"], m["transform_param"])
            if canonical_field in _MONEY_CANONICAL_FIELDS and value is not None and not isinstance(value, int):
                raise ValueError(f"{canonical_field} requires TO_MINOR_UNITS, got transform {m['transform']!r}")
            # First non-None value for this canonical_field wins. A later
            # synonym that's blank *for this row* (column exists, cell is
            # empty - e.g. Customers.csv's own `customer_code` column is
            # blank for most rows, with `customer_id` as the real fallback)
            # must not clobber a value an earlier synonym already found.
            if canonical.get(canonical_field) is None:
                canonical[canonical_field] = value
        except Exception as exc:  # noqa: BLE001 - a bad field is a data issue, not a worker crash
            issues.append(f"{source_field} -> {canonical_field}: {exc}")
    return canonical, issues
