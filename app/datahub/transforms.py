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
)

_DEFAULT_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y")

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
        canonical_field = m["canonical_field"]
        raw_value = normalized_raw.get(normalize_header(source_field))
        try:
            value = apply_transform(raw_value, m["transform"], m["transform_param"])
            if canonical_field in _MONEY_CANONICAL_FIELDS and value is not None and not isinstance(value, int):
                raise ValueError(f"{canonical_field} requires TO_MINOR_UNITS, got transform {m['transform']!r}")
            canonical[canonical_field] = value
        except Exception as exc:  # noqa: BLE001 - a bad field is a data issue, not a worker crash
            issues.append(f"{source_field} -> {canonical_field}: {exc}")
    return canonical, issues
