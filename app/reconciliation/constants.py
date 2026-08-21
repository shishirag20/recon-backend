"""Static configuration for the reconciliation module: phase/kind vocabularies,
GL role codes, run/match/exception status enums (all TEXT in the DB, validated
here only - see app/datahub/constants.py for the same no-native-enum
convention), and the default AR rule catalog seeded onto a new definition.

Rule `kind` is deliberately an open vocabulary (free TEXT in the DB, not a
closed enum here) - see migrations/0008_domain_foundation.sql's rationale on
reconciliation_rules. DEFAULT_AR_RULE_KINDS below is just the set this
module's own rule implementations (app/reconciliation/rules/*) recognize
today; a future rule kind ships without a schema change.
"""
from __future__ import annotations

# -- Router / errors --------------------------------------------------------
ROUTER_TAGS = ["Reconciliation"]


class ReconciliationErrors:
    ENTITY_NOT_FOUND = "Entity not found"
    DEFINITION_NOT_FOUND = "Reconciliation definition not found"
    RULE_NOT_FOUND = "Reconciliation rule not found"
    RUN_NOT_FOUND = "Reconciliation run not found"
    INVALID_RECON_TYPE = "Invalid recon_type"
    RUN_NOT_RETRYABLE = "Only a FAILED run can be retried"
    EXCEPTION_NOT_FOUND = "Reconciliation exception not found"
    INVALID_EXCEPTION_STATUS = "Invalid exception status"
    INVALID_RESOLUTION_OUTCOME = "Invalid resolution_outcome"
    INVALID_PHASE = "Invalid phase"
    INVALID_RULE_KIND = "kind is not a registered rule for this phase"
    INVALID_FIELD_MATCH_CONFIG = (
        "kind='field-match' requires config.matcher (one of MATCHER_KINDS), config.bank_field, "
        "config.source (one of SOURCE_KINDS), and config.source_field"
    )
    DUPLICATE_PRIORITY = "A rule already exists at this phase+priority"
    NOT_A_NO_PAYMENT_EXCEPTION = "Only a NO_PAYMENT exception (with an invoice_id) can be resolved this way"
    NO_PAYMENT_IDS_SELECTED = "payment_ids must include at least one payment"
    INVOICE_NOT_OPEN = "This invoice has no remaining balance to apply a payment against"
    PAYMENT_NOT_FOUND_OR_NOT_OPEN = "One or more payment_ids were not found or have no unapplied balance left"
    NOT_A_SUSPENSE_EXCEPTION = "Only a SUSPENSE exception (with a bank_txn_id) can be resolved this way"
    SUSPENSE_PAYMENT_NOT_FOUND = "No payment found for this exception's bank transaction"
    CUSTOMER_NOT_FOUND = "Customer not found"
    INVOICE_NOT_FOUND_FOR_CUSTOMER = "One or more invoice_ids were not found, not open, or don't belong to customer_id"


# -- reconciliation_definitions ----------------------------------------------
RECON_TYPES = ("AR", "AP", "BANK")  # only AR has an engine implementation today

# -- reconciliation_rules.phase ----------------------------------------------
# Matches the phase vocabulary the original domain-schema plan fixed for this
# column - see migrations/0012_domain_reconciliation_definitions.sql.
PHASE_INTAKE_VALIDATION = "INTAKE_VALIDATION"
PHASE_CUSTOMER_LOCK = "CUSTOMER_LOCK"        # Phase 1a in the proposal doc
PHASE_CANDIDATE_POOL = "CANDIDATE_POOL"      # Phase 1b
# Runs after both 1a and 1b have had their chance for a row, reconciled
# against whichever (if either) actually identified a customer - not one of
# the six CUSTOMER_LOCK rules, so it gets its own phase rather than
# competing in that first-match-wins loop. See engine.py::run_phase_1.
PHASE_NARRATION_CHECK = "NARRATION_CHECK"
PHASE_ALLOCATION = "ALLOCATION"              # Phase 2
PHASE_SHORT_PAY = "SHORT_PAY"
PHASE_UNAPPLIED = "UNAPPLIED"
PHASE_GL_CHECK = "GL_CHECK"

RECON_PHASES = (
    PHASE_INTAKE_VALIDATION, PHASE_CUSTOMER_LOCK, PHASE_CANDIDATE_POOL,
    PHASE_NARRATION_CHECK, PHASE_ALLOCATION, PHASE_SHORT_PAY, PHASE_UNAPPLIED, PHASE_GL_CHECK,
)

# -- reconciliation_runs.status lifecycle ------------------------------------
RUN_STATUS_DRAFT = "DRAFT"
RUN_STATUS_QUEUED = "QUEUED"
RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_COMPUTED = "COMPUTED"
RUN_STATUS_APPROVED = "APPROVED"
RUN_STATUS_CLOSED = "CLOSED"
RUN_STATUS_FAILED = "FAILED"

RUN_STATUSES = (
    RUN_STATUS_DRAFT, RUN_STATUS_QUEUED, RUN_STATUS_RUNNING, RUN_STATUS_COMPUTED,
    RUN_STATUS_APPROVED, RUN_STATUS_CLOSED, RUN_STATUS_FAILED,
)

# -- match_groups -------------------------------------------------------------
MATCH_TYPES = ("EXACT", "TOLERANCE", "PARTIAL", "SUBSET_SUM", "MANY_TO_ONE", "ONE_TO_MANY", "MANUAL")
MATCH_STATUSES = ("AUTO_MATCHED", "SUGGESTED", "CONFIRMED", "REJECTED")

# -- reconciliation_exceptions -------------------------------------------------
EXCEPTION_TYPES = (
    "SHORT_PAY", "OVERPAYMENT", "UNAPPLIED_CASH", "TIMING_DIFFERENCE", "GL_VARIANCE",
    "DUPLICATE", "MULTIPLE_INVOICE_MATCH", "DOUBLE_COLLISION", "SUSPENSE", "BANK_CHARGE",
    "GATEWAY_VARIANCE", "NO_PAYMENT", "CUSTOMER_INVOICE_MISMATCH",
)
EXCEPTION_STATUSES = (
    "OPEN", "INVESTIGATING", "RESOLVED", "AUTO_RESOLVED", "DEFERRED",
    "WRITTEN_OFF", "ADJUSTED", "CARRIED_FORWARD",
)
EXCEPTION_RESOLUTION_OUTCOMES = ("WRITEOFF", "KEEPOPEN", "DISPUTE", "JOURNAL", "ON_ACCOUNT", "MANUAL_MATCH")

# -- gl_account_roles.role_code -----------------------------------------------
# The fixed set of semantic accounts gl_posting.py resolves per entity - see
# migrations/0029_gl_account_roles.sql for what each one is for.
GL_ROLE_AR_CONTROL = "AR_CONTROL"
GL_ROLE_CASH_CONTROL = "CASH_CONTROL"
GL_ROLE_BANK_CHARGES = "BANK_CHARGES"
GL_ROLE_TDS_RECEIVABLE = "TDS_RECEIVABLE"
GL_ROLE_WRITE_OFF = "WRITE_OFF"
GL_ROLE_ON_ACCOUNT_ADVANCE = "ON_ACCOUNT_ADVANCE"
GL_ROLE_SUSPENSE = "SUSPENSE"
GL_ROLE_FX_GAIN_LOSS = "FX_GAIN_LOSS"

GL_ROLE_CODES = (
    GL_ROLE_AR_CONTROL, GL_ROLE_CASH_CONTROL, GL_ROLE_BANK_CHARGES, GL_ROLE_TDS_RECEIVABLE,
    GL_ROLE_WRITE_OFF, GL_ROLE_ON_ACCOUNT_ADVANCE, GL_ROLE_SUSPENSE, GL_ROLE_FX_GAIN_LOSS,
)

# Which GL role absorbs the gap when a settlement closes an invoice for less
# cash than its full balance (app/reconciliation/rules/__init__.py's
# InvoiceAllocation.close_full=True) - a withheld TDS amount, or a decoupled
# bank fee, or a written-off dust residual. These used to be three separate,
# standalone catalog rules (tds-match/bank-fee/write-off); now every Phase 2
# rule runs the same settlement check
# (app/reconciliation/rules/allocation.py::resolve_invoice_settlement)
# against whichever invoice(s) it identifies, so these keys are just variance-
# type tags, not catalog `kind`s to look up - no `reconciliation_rules` row
# with this kind needs to exist. Used by both engine.py (to tag each
# allocation with its gap's destination while it's still in memory) and
# gl_posting.py (M3, to actually post it).
GAP_ROLE_BY_RULE_KIND: dict[str, str] = {
    "tds-match": GL_ROLE_TDS_RECEIVABLE,
    "bank-fee": GL_ROLE_BANK_CHARGES,
    "write-off": GL_ROLE_WRITE_OFF,
}

# Baseline chart-of-accounts entry created per role when seeding a new entity
# (account_code, account_name, account_type, normal_balance) - see
# app/reconciliation/dao.py:seed_gl_account_roles. A real entity can later
# repoint a role at its actual chart-of-accounts code without any code change,
# since gl_posting.py only ever looks up by role_code.
GL_ROLE_DEFAULTS: dict[str, tuple[str, str, str, str]] = {
    GL_ROLE_AR_CONTROL:         ("1200", "Accounts Receivable Control", "Balance Sheet", "DEBIT"),
    GL_ROLE_CASH_CONTROL:       ("1100", "Cash / Bank Clearing",        "Balance Sheet", "DEBIT"),
    GL_ROLE_BANK_CHARGES:       ("5100", "Bank Charges",                "Income Statement", "DEBIT"),
    GL_ROLE_TDS_RECEIVABLE:     ("1250", "TDS Receivable",              "Balance Sheet", "DEBIT"),
    GL_ROLE_WRITE_OFF:          ("5200", "Write-Off Expense",           "Income Statement", "DEBIT"),
    GL_ROLE_ON_ACCOUNT_ADVANCE: ("2400", "Customer Advances (On-Account)", "Balance Sheet", "CREDIT"),
    GL_ROLE_SUSPENSE:           ("2900", "Suspense",                    "Balance Sheet", "CREDIT"),
    GL_ROLE_FX_GAIN_LOSS:       ("5300", "FX Gain / Loss",              "Income Statement", "DEBIT"),
}

# -- Default AR rule catalog --------------------------------------------------
# Seeded onto every new AR reconciliation_definition. (phase, kind, name,
# priority, confidence, config) - first-match-wins within a phase, ordered by
# priority ascending. `kind` dispatches to a callable in
# app/reconciliation/rules/{identification,pooling,allocation}.py (M1/M2) -
# this module only owns the data, not the rule implementations.
DEFAULT_AR_RULE_CATALOG: tuple[tuple[str, str, str, int, int | None, dict], ...] = (
    # kind/name/priority/confidence below match the recon-frontend prototype's
    # canonical rule catalog verbatim - src/reference/index copy.html's
    # `AR_RULE_KINDS` + `arRuleC(...)` seed calls, NOT src/mocks/ar.ts (looser/
    # older text) and NOT ARRuleCard.tsx's `RULE_METADATA` (missing labels for
    # subset-sum/bank-fee/write-off) - `index copy.html` is the only one of
    # the three with a complete, consistent definition for every kind. See
    # docs/reconciliation.md §8.
    #
    # `period-cutoff-guard`/`memo-netoff-guard` (Phase 2.0a/2.0b in the
    # original build plan) are deliberately NOT in this catalog, even though
    # the engine still performs both checks unconditionally - they were
    # represented as catalog rows through M2, but the plan's intent that
    # their DB `config` actually drive the engine (see the plan's "the DB
    # reconciliation_rules rows drive which fire and their tunables") was
    # never implemented: the period cutoff is hardcoded into
    # `ReconciliationDAO.load_open_invoices`'s SQL query, and the memo net-off
    # is hardcoded into `engine.py::run_phase_2`. Neither row's `enabled`/
    # `config` was ever actually read - they were inert placeholders a user
    # could toggle in Rules Studio with zero effect, and the frontend
    # prototype never modeled them as rules at all. Removed rather than
    # finished, since nothing consumes their configurability today.
    #
    # Phase 0 - INTAKE_VALIDATION (runs once per bank_txn, before customer
    # identification even starts; a reject here means Phase 1a/1b never run
    # for that row at all - see engine.py's run_phase_1)
    (PHASE_INTAKE_VALIDATION, "dup-utr", "Duplicate UTR Check", 1, 100,
     {"description": "Reject a bank_reference already MATCHED in a prior run for this entity"}),

    # Phase 1a - CUSTOMER_LOCK (lock the paying customer; first match wins)
    (PHASE_CUSTOMER_LOCK, "expected-utr", "Pre-Advised UTR Match", 1, 98,
     {"source": "expected_remittances", "match_field": "utr_number"}),
    (PHASE_CUSTOMER_LOCK, "account-ifsc", "Payer Account & IFSC Match", 2, 97,
     {"source": "customer_bank_accounts", "match_fields": ["bank_account_no", "ifsc_code"]}),
    (PHASE_CUSTOMER_LOCK, "upi", "UPI Handle Match", 3, 95,
     {"source": "customers", "match_field": "vpa_handle", "extract": "vpa"}),
    (PHASE_CUSTOMER_LOCK, "customer-code", "Customer Code in Narration Match", 4, 90,
     {"source": "customer_reference_codes", "extract": "narration_substring"}),
    (PHASE_CUSTOMER_LOCK, "gstin-pan", "Tax ID & PAN Match", 5, 92,
     {"source": "customers", "extract": ["gstin", "pan"]}),
    (PHASE_CUSTOMER_LOCK, "fuzzy-name", "Company Name Match", 6, 85,
     {"source": "customers", "match_field": "company_name", "min_similarity": 0.85}),
    # Last resort: no UTR/account/VPA/customer-code/GSTIN/PAN/fuzzy-name
    # match at all - e.g. a remittance with no rich payer data on the bank
    # side, only a narration. Only fires if the referenced invoice already
    # has its own customer_id (from ERP ingestion) - see
    # app/reconciliation/rules/identification.py::document_number_match.
    (PHASE_CUSTOMER_LOCK, "document-number-narration", "Document Number in Narration Match", 7, 85,
     {"source": "invoices", "match_field": "invoice_number", "location": "narration"}),

    # Phase 1b - CANDIDATE_POOL (only reached if Phase 1a locked nothing)
    (PHASE_CANDIDATE_POOL, "account-suffix", "Masked Account Suffix Match", 1, 60,
     {"source": "customer_bank_accounts", "match_field": "bank_account_no", "mode": "suffix"}),
    (PHASE_CANDIDATE_POOL, "narration-tokens", "Token-Based Narration Match", 2, 50,
     {"source": "customers", "match_field": "company_name", "mode": "token_substring"}),

    # Phase 1c - NARRATION_CHECK. Runs after both CUSTOMER_LOCK and
    # CANDIDATE_POOL have had their chance for a row - not a normal
    # first-match-wins rule itself, and never locks a customer on its own.
    # It independently resolves which customer owns whatever invoice number
    # the narration references (searched across every customer's invoices,
    # not just whoever the phases above identified) and cross-checks that
    # against their result: agreement is recorded
    # (payments.narration_crosscheck_rule_id, surfaced in the "Resolved Via"
    # UI); disagreement raises a CUSTOMER_INVOICE_MISMATCH exception instead
    # of letting the lock stand unquestioned; and if 1a/1b found nobody at
    # all, it seeds a single-candidate pool as a last-resort suggestion. See
    # engine.py::run_phase_1.
    (PHASE_NARRATION_CHECK, "invoice-number-in-narration", "Invoice Number in Narration", 1, 100,
     {"description": "Cross-check: does the narration reference a real invoice belonging to a different customer than the one Phase 1a/1b identified?"}),

    # Phase 2 - ALLOCATION (scoped to the locked customer or candidate pool).
    # tds-match/bank-fee/write-off/overpayment used to be standalone rules
    # here (priorities 4/6/7/8) - removed. Every rule below now runs the same
    # settlement check (allocation.py::resolve_invoice_settlement) against
    # whichever invoice(s) it identifies: TDS/bank-fee/dust-write-off
    # variance and overpayment are handled inline by exact-invoice-num,
    # invoice-suffix, exact-amount, and subset-sum alike, not as their own
    # later-priority fallback pass (2026-08 note - see engine.py's
    # `_commit_direct_match` and allocation.py's rule docstrings).
    (PHASE_ALLOCATION, "exact-invoice-num", "Exact Invoice Number Match", 1, 98,
     {"match_field": "invoice_number", "location": "narration"}),
    (PHASE_ALLOCATION, "invoice-suffix", "Truncated Invoice Number Match", 2, 90,
     {"match_field": "invoice_number", "mode": "suffix", "min_length": 4}),
    (PHASE_ALLOCATION, "exact-amount", "Exact Amount Match", 3, 95,
     {"amount": {"mode": "exact", "field": "balance_due_minor"}, "tie_break": "ambiguous_exception"}),
    (PHASE_ALLOCATION, "subset-sum", "Combined Invoice Match (Many-to-Many)", 4, 85,
     {"amount": {"mode": "subset_sum"}, "order_by": "due_date", "max_invoices": 10}),
    (PHASE_ALLOCATION, "partial-payment", "Partial Payment Allocation", 5, 100,
     {"mode": "partial", "allow_short_pay": True}),

    # Phase 3.0/3.1/3.2 - SHORT_PAY / UNAPPLIED / GL_CHECK: one `threshold`
    # rule per phase, actually read by the engine (engine.py::run_phase_2 for
    # the first two, gl_posting.py::post_run for the third) via
    # rules.get_threshold_minor - not placeholders, unlike the removed
    # guardrail rows above. Disabling or deleting a row here reverts that
    # phase's check to zero tolerance (today's original strict behavior),
    # not to "no check at all".
    (PHASE_SHORT_PAY, "threshold", "Shortfall Tolerance", 1, 100,
     {"amount": {"mode": "abs", "value_minor": 100}}),
    (PHASE_UNAPPLIED, "threshold", "Unapplied Cash Threshold", 1, 100,
     {"amount": {"mode": "abs", "value_minor": 0}}),
    (PHASE_GL_CHECK, "threshold", "GL Control Variance Tolerance", 1, 100,
     {"amount": {"mode": "abs", "value_minor": 0}}),
)
