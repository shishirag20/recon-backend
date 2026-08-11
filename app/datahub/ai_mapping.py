"""Integration point for AI-assisted field mapping - not implemented yet.

Once field_mappings became a global, stream-scoped synonym dictionary
(migration 0026) instead of a per-data-source config, the intent is: any raw
column header that doesn't match an existing synonym gets sent here, the
model guesses which canonical field it corresponds to, and the caller
(app/workers/ingestion_worker.py) persists that guess back into the
dictionary via DataHubDAO.insert_mapping_version - so the shared mapping
gets smarter over time instead of failing the same unknown column on every
future upload.

The actual model call is known-broken elsewhere in this project and is
explicitly out of scope here - this stub only exists so the worker has a
real call site to wire a working implementation into later without any
other code changing.
"""
from __future__ import annotations


def suggest_canonical_fields(
    stream: str, unmapped_columns: list[str], known_fields: set[str]
) -> list[dict]:
    """Would return a list of {"source_field", "canonical_field", "transform",
    "transform_param"} guesses, one per column it could confidently resolve.
    Columns it can't resolve are simply omitted, not guessed badly - the
    worker records those as `ingestion_jobs.unmapped_columns` instead.

    Stubbed to always return no suggestions; wire a real model call in here
    when that work is ready.
    """
    return []
