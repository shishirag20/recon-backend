"""Shared types for the rule modules (identification.py Phase 1a,
pooling.py Phase 1b, allocation.py Phase 2 - M2).

Each rule is a small async callable `(bank_txn: dict, ctx: RuleContext,
config: dict) -> ...` registered by `kind` in its module's `*_RULES` dict and
evaluated in `(phase, priority)` order (first-match-wins within a phase) -
see app/reconciliation/engine.py for the loop that drives this.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import asyncpg

from app.reconciliation.dao import ReconciliationDAO


@dataclass
class RuleContext:
    """The full Phase-1 working set for one run, loaded once and shared
    across every bank_txn - this entity's customer master is small enough
    (tens to low hundreds of rows) that loading it all upfront and matching
    in Python is simpler and faster than a query per rule per row."""
    entity_id: str
    dao: ReconciliationDAO
    conn: asyncpg.Connection
    customers: list[dict]
    bank_accounts: list[dict]
    reference_codes: list[dict]
    expected_remittances: list[dict]
    duplicate_refs_in_run: set[str] = field(default_factory=set)
    # Every open invoice for this entity, across every customer - not scoped
    # to whichever customer a rule is currently evaluating, unlike everything
    # else on this context. Only used by the "Invoice Number in Narration"
    # cross-check, which needs to search entity-wide by design (a narration
    # can reference a real invoice belonging to a customer other than the one
    # actually being identified - that's the mismatch it exists to catch).
    all_open_invoices: list[dict] = field(default_factory=list)


@dataclass
class IdentificationResult:
    """Result of one Phase 0 (INTAKE_VALIDATION) or Phase 1a (CUSTOMER_LOCK)
    rule. `reject=True` (dup-utr only, Phase 0) means stop evaluating this
    bank_txn entirely - it's a duplicate, not a miss - rather than falling
    through to the next rule."""
    customer_id: str | None = None
    reject: bool = False
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.customer_id is not None or self.reject


@dataclass
class AllocationContext:
    """The Phase-2 working set, scoped to one run. `invoices_by_customer` is
    mutated in place as allocations land within the run (an invoice's
    in-memory `balance_due_minor` is decremented, or the invoice removed
    once fully closed) - later payments in the same run see the updated
    state, matching the real DB writes the engine makes alongside it. Keyed
    by `str(customer_id)`."""
    entity_id: str
    dao: ReconciliationDAO
    conn: asyncpg.Connection
    invoices_by_customer: dict[str, list[dict]]
    memos_by_customer: dict[str, list[dict]] = field(default_factory=dict)


@dataclass
class InvoiceAllocation:
    """One (invoice, amount) pair a rule wants to write. `cash_minor` is the
    real money recorded on `invoice_allocations.allocated_minor`.
    `close_full=True` means the invoice's balance should go to zero
    regardless of `cash_minor` being less than its original balance - the
    gap is being explicitly absorbed (a bank fee, a dust write-off, TDS
    withheld at source), not left owing. The gap itself isn't posted
    anywhere yet in M2 - that's gl_posting.py's job in M3; this just decides
    the invoice is *done*."""
    invoice_id: str
    cash_minor: int
    close_full: bool = False


@dataclass
class AllocationOutcome:
    """Result of one Phase 2 rule for one (payment, candidate customer)
    pair. `ambiguous=True` means the rule found more than one equally valid
    invoice for *this same customer* (e.g. two identical-balance invoices)
    and deliberately didn't pick one - `ambiguous_invoice_ids` carries what
    it refused to choose between, for the exception's `detail`."""
    allocations: list[InvoiceAllocation] = field(default_factory=list)
    match_type: str = "EXACT"
    reason: str = ""
    ambiguous: bool = False
    ambiguous_invoice_ids: list[str] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return bool(self.allocations) or self.ambiguous


def get_threshold_minor(rules: list[dict], phase: str) -> int:
    """Reads the single enabled `kind='threshold'` rule's tolerance for a
    SHORT_PAY/UNAPPLIED/GL_CHECK phase (each phase has exactly one - there's
    no priority cascade to evaluate, unlike the other phases). Returns 0 -
    the original, strictest behavior - if the row is missing or disabled,
    so deleting/disabling it never silently loosens a check, only tightens
    it back to unconditional."""
    for rule in rules:
        if rule["phase"] == phase and rule["enabled"] and rule["kind"] == "threshold":
            return rule["config"].get("amount", {}).get("value_minor", 0)
    return 0
