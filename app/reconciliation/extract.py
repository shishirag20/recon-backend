"""Pure regex/text extractors shared by identification (Phase 1a/1b) and
allocation (Phase 2) rules. No DB access, no side effects - unit-testable in
isolation, which is the whole point of splitting this out from the rule
modules that call it.
"""
from __future__ import annotations

import re

# UPI VPA, e.g. "nimbus@okhdfc" inside "UPI/nimbus@okhdfc/PAYMENT INV-2026-105".
_VPA_RE = re.compile(r"\b[\w.\-]+@[a-zA-Z]+\b")

# GSTIN: 2-digit state code + 10-char PAN + 1 entity digit + 'Z' + 1 checksum
# alphanumeric = 15 chars total, e.g. "27AASCS1234F1Z5".
_GSTIN_RE = re.compile(r"\b\d{2}[A-Za-z]{5}\d{4}[A-Za-z]\d[Zz][A-Za-z0-9]\b")

# PAN: 10-char alphanumeric, e.g. "AASCS1234F". Deliberately not anchored to
# GSTIN's stricter shape - PAN can appear standalone in narration.
_PAN_RE = re.compile(r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b")

# A 4+ digit numeric block, e.g. "1046" out of "...REF KEST04 INVC 1046" -
# for Phase 2's truncated-invoice-suffix rule (2.2).
_NUMERIC_BLOCK_RE = re.compile(r"\d{4,}")


def extract_vpa(text: str) -> str | None:
    """First UPI VPA found in `text`, or None."""
    match = _VPA_RE.search(text or "")
    return match.group(0) if match else None


def extract_gstin(text: str) -> str | None:
    match = _GSTIN_RE.search(text or "")
    return match.group(0).upper() if match else None


def extract_pan(text: str) -> str | None:
    """Best-effort - a GSTIN also contains a PAN-shaped substring, so callers
    that already found a GSTIN should prefer that match, not this one."""
    match = _PAN_RE.search(text or "")
    return match.group(0).upper() if match else None


def extract_numeric_blocks(text: str, min_length: int = 4) -> list[str]:
    """Every run of `min_length`+ digits, in order of appearance - used to
    find a truncated invoice-number suffix (Phase 2 rule 2.2) when the full
    invoice number isn't present verbatim."""
    return [m for m in _NUMERIC_BLOCK_RE.findall(text or "") if len(m) >= min_length]


def contains_substring(haystack: str, needle: str) -> bool:
    """Case-insensitive substring check - the shared primitive behind
    reference-code-in-narration (1.4a), invoice-number-in-narration (2.1),
    and the account-suffix checks. A thin wrapper mainly so callers don't
    each reimplement the `.upper()` normalization differently."""
    if not haystack or not needle:
        return False
    return needle.upper() in haystack.upper()


def account_suffix_matches(narration_or_account: str, known_account_no: str, suffix_length: int = 4) -> bool:
    """True if the last `suffix_length` digits of `known_account_no` appear
    as a trailing digit run somewhere in `narration_or_account` - Phase 1b
    rule 1.1b (masked/partial account-number suffix)."""
    if not known_account_no or len(known_account_no) < suffix_length:
        return False
    suffix = known_account_no[-suffix_length:]
    return any(block.endswith(suffix) for block in extract_numeric_blocks(narration_or_account or "", min_length=suffix_length))
