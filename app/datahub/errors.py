"""Turns a job's raw `failed_rows` blob into a diagnosis an analyst can act on.

A rejected row arrives as `{"raw": {...}, "issues": [...]}` (see
ingestion_worker.process_ingestion_job). The worker appends the RowRejected
cause *last*, after whatever mapping/transform issues the row had already
accumulated - so `issues[-1]` is why the row was actually rejected and the
earlier entries are contributing problems, not independent failures. Grouping
on the last issue is therefore grouping on the real cause; grouping on all of
them would double-count a single bad row across several buckets.

The raw causes are unusable as group keys on their own because they embed the
offending value ("unparseable date '13/45/2026'"), which makes every row its
own group. `classify` maps each cause onto a stable (code, reason, field)
triple that keeps the discriminating part - which *field* broke - and drops
the per-row literal.
"""

from __future__ import annotations

import re
from typing import Any

# Grouping caps. A 50k-row file where every row fails must not turn one API
# response into a 50k-element payload; that is the bug this endpoint exists to
# avoid, so it must not reintroduce it.
DEFAULT_SAMPLE_LIMIT = 3
MAX_GROUPS = 50


class _Rule:
    __slots__ = ("pattern", "code", "build")

    def __init__(self, pattern: str, code: str, build):
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.code = code
        self.build = build


def _plural(items: list[str]) -> str:
    return ", ".join(items)


# Ordered most-specific first; the first match wins.
_RULES: list[_Rule] = [
    # Our own RowRejected messages -------------------------------------------
    _Rule(
        r"^missing required field\(s\): (?P<fields>.+)$",
        "MISSING_REQUIRED_FIELD",
        lambda m: (
            f"Missing required field: {_plural(sorted(f.strip() for f in m['fields'].split(',')))}",
            ", ".join(sorted(f.strip() for f in m["fields"].split(","))),
        ),
    ),
    _Rule(
        r"^currency '(?P<got>[^']*)' differs from entity home currency '(?P<home>[^']*)'",
        "CURRENCY_MISMATCH",
        lambda m: (
            f"Currency {m['got']} does not match the entity's home currency {m['home']}, "
            f"and no home-amount field is mapped",
            "currency",
        ),
    ),
    # Postgres constraint violations surfaced through RowRejected(str(exc)) ---
    _Rule(
        r'duplicate key value violates unique constraint "(?P<con>[^"]*row_hash[^"]*)"',
        "DUPLICATE_ROW",
        lambda m: ("Identical row already ingested from an earlier upload", None),
    ),
    _Rule(
        r'duplicate key value violates unique constraint "(?P<con>[^"]+)"',
        "DUPLICATE_KEY",
        lambda m: (f"Duplicate value violates {m['con']}", None),
    ),
    _Rule(
        r'null value in column "(?P<col>[^"]+)".*?violates not-null constraint',
        "MISSING_REQUIRED_FIELD",
        lambda m: (f"Missing required field: {m['col']}", m["col"]),
    ),
    _Rule(
        r'value too long for type character varying\((?P<n>\d+)\)',
        "VALUE_TOO_LONG",
        lambda m: (f"Value exceeds the {m['n']}-character column limit", None),
    ),
    _Rule(
        r'invalid input syntax for type (?P<t>\w+)',
        "INVALID_VALUE",
        lambda m: (f"Value is not a valid {m['t']}", None),
    ),
    _Rule(
        r'violates foreign key constraint "(?P<con>[^"]+)"',
        "UNKNOWN_REFERENCE",
        lambda m: (f"References a record that does not exist ({m['con']})", None),
    ),
    _Rule(
        r"numeric field overflow",
        "NUMERIC_OVERFLOW",
        lambda m: ("Number is too large for its column", None),
    ),
    # Mapping/transform failures: "Txn Date -> txn_date: unparseable date '13/45/2026'"
    _Rule(
        r"^(?P<src>[^>]+?) -> (?P<canon>[^:]+): (?P<detail>.+)$",
        "TRANSFORM_FAILED",
        lambda m: (
            f"Could not convert column '{m['src'].strip()}' into {m['canon'].strip()} "
            f"({_strip_literals(m['detail']).strip()})",
            m["canon"].strip(),
        ),
    ),
    _Rule(
        r"^unknown canonical_field '(?P<f>[^']+)' for stream",
        "UNKNOWN_FIELD",
        lambda m: (f"Mapped to '{m['f']}', which is not a field on this stream", m["f"]),
    ),
]

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_NUMBERS = re.compile(r"\d+")


def _strip_literals(text: str) -> str:
    """Removes the per-row value from a message so two rows that broke the
    same way land in the same bucket."""
    return _NUMBERS.sub("N", _QUOTED.sub("value", text))


def classify(cause: str) -> tuple[str, str, str | None]:
    """Maps one rejection cause onto (code, human reason, field or None)."""
    text = (cause or "").strip()
    if not text:
        return ("OTHER", "Row rejected without a recorded reason", None)
    for rule in _RULES:
        match = rule.pattern.search(text)
        if match:
            reason, field = rule.build(match)
            return (rule.code, reason, field)
    # Unrecognized: keep the message but strip literals so it still groups.
    return ("OTHER", _strip_literals(text), None)


def group_failed_rows(
    failed_rows: list[dict[str, Any]] | None,
    *,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> list[dict[str, Any]]:
    """Groups a job's rejected rows by why they were rejected.

    Returns groups sorted by count descending, each carrying at most
    `sample_limit` example rows. `contributing_issues` holds the non-cause
    issues seen on rows in this group - a bad date that *also* left a required
    field empty shows up there, so the analyst sees the whole story without
    the group count being inflated.
    """
    if not failed_rows:
        return []

    buckets: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for entry in failed_rows:
        if not isinstance(entry, dict):
            continue
        issues = entry.get("issues") or []
        if not isinstance(issues, list):
            issues = [str(issues)]
        cause = str(issues[-1]) if issues else ""
        contributing = [str(i) for i in issues[:-1]]

        code, reason, field = classify(cause)
        key = (code, reason, field)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "code": code,
                "reason": reason,
                "field": field,
                "count": 0,
                "samples": [],
                "contributing_issues": [],
            }
            buckets[key] = bucket

        bucket["count"] += 1
        if len(bucket["samples"]) < sample_limit:
            bucket["samples"].append(
                {
                    "row_number": entry.get("row_number"),
                    "raw": entry.get("raw") or {},
                    "issues": [str(i) for i in issues],
                }
            )
        for issue in contributing:
            normalized = _strip_literals(issue)
            if normalized not in bucket["contributing_issues"]:
                bucket["contributing_issues"].append(normalized)

    groups = sorted(
        buckets.values(), key=lambda g: (-g["count"], g["reason"])
    )
    return groups[:MAX_GROUPS]
