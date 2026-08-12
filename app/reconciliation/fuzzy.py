"""Name-similarity and token-overlap helpers for Phase 1a rule 1.6a (fuzzy
company-name match) and Phase 1b rule 1.2b (token-based narration match).

The real match (`best_fuzzy_match`) is backed by Postgres `pg_trgm` -
`similarity()` needs the trigram index from migration 0027 to stay fast as
the customer master grows, so it's a SQL query, not a Python computation.
`fuzzy_ratio` is a pure-Python fallback (stdlib `difflib`) used only where a
DB connection isn't available - unit tests for the threshold logic itself,
not a code path the engine takes.
"""
from __future__ import annotations

from difflib import SequenceMatcher

import asyncpg

# Words too common in a company name to be a meaningful match signal on their
# own (legal suffixes, generic business words) - filtered out before token
# overlap is checked, so "Pvt Ltd" matching some unrelated "XYZ Pvt Ltd" in
# narration doesn't produce a false positive.
_STOPWORDS = {
    "the", "and", "co", "company", "corp", "corporation", "inc", "incorporated",
    "ltd", "limited", "pvt", "private", "llp", "llc", "group", "industries",
    "enterprises", "solutions", "services", "trading", "traders",
}


def fuzzy_ratio(a: str, b: str) -> float:
    """Pure-Python similarity in [0, 1] - SequenceMatcher ratio, not trigram
    similarity, so this is a fallback/unit-test tool only. The real engine
    path always uses `best_fuzzy_match`'s SQL `similarity()` (pg_trgm), which
    scores differently - don't expect the two to agree on a borderline case."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio()


async def best_fuzzy_match(
    conn: asyncpg.Connection, *, entity_id: str, probe_name: str, min_similarity: float = 0.85
) -> dict | None:
    """The single best `customers` row by trigram similarity to `probe_name`
    for this entity, or None if nothing clears `min_similarity`. Requires the
    `idx_customers_name_trgm` GIN index (migration 0027) to stay fast."""
    if not probe_name:
        return None
    row = await conn.fetchrow(
        "SELECT customer_id, company_name, similarity(company_name, $2) AS score "
        "FROM customers WHERE entity_id = $1 AND similarity(company_name, $2) >= $3 "
        "ORDER BY score DESC LIMIT 1",
        entity_id, probe_name, min_similarity,
    )
    return dict(row) if row is not None else None


def significant_tokens(company_name: str) -> list[str]:
    """Uppercased tokens from `company_name` with stopwords/short noise
    dropped - "Halcyon Foods" -> ["HALCYON"] ("Foods" isn't a stopword by
    itself but common generic business words are; a 2-letter leftover token
    is dropped as too weak a signal on its own)."""
    tokens = []
    for raw in (company_name or "").split():
        word = "".join(c for c in raw if c.isalnum())
        if len(word) < 3 or word.lower() in _STOPWORDS:
            continue
        tokens.append(word.upper())
    return tokens


def token_overlap_match(company_name: str, narration: str) -> str | None:
    """The first significant token from `company_name` that appears in
    `narration` (case-insensitive substring), or None. Phase 1b rule 1.2b -
    deliberately weaker/cheaper than fuzzy_ratio/best_fuzzy_match: a single
    matching token is enough to land in the candidate pool, not to lock."""
    upper_narration = (narration or "").upper()
    for token in significant_tokens(company_name):
        if token in upper_narration:
            return token
    return None
