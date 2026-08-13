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


# -- reconciliation_definitions ----------------------------------------------
RECON_TYPES = ("AR", "AP", "BANK")  # only AR has an engine implementation today

# -- reconciliation_rules.phase ----------------------------------------------
# Matches the phase vocabulary the original domain-schema plan fixed for this
# column - see migrations/0012_domain_reconciliation_definitions.sql.
PHASE_INTAKE_VALIDATION = "INTAKE_VALIDATION"
PHASE_CUSTOMER_LOCK = "CUSTOMER_LOCK"        # Phase 1a in the proposal doc
PHASE_CANDIDATE_POOL = "CANDIDATE_POOL"      # Phase 1b
PHASE_ALLOCATION = "ALLOCATION"              # Phase 2
PHASE_SHORT_PAY = "SHORT_PAY"
PHASE_UNAPPLIED = "UNAPPLIED"
PHASE_GL_CHECK = "GL_CHECK"

RECON_PHASES = (
    PHASE_INTAKE_VALIDATION, PHASE_CUSTOMER_LOCK, PHASE_CANDIDATE_POOL,
    PHASE_ALLOCATION, PHASE_SHORT_PAY, PHASE_UNAPPLIED, PHASE_GL_CHECK,
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
    "GATEWAY_VARIANCE", "NO_PAYMENT",
)
EXCEPTION_STATUSES = (
    "OPEN", "INVESTIGATING", "RESOLVED", "AUTO_RESOLVED", "DEFERRED",
    "WRITTEN_OFF", "ADJUSTED", "CARRIED_FORWARD",
)
EXCEPTION_RESOLUTION_OUTCOMES = ("WRITEOFF", "KEEPOPEN", "DISPUTE", "JOURNAL", "ON_ACCOUNT")

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

# Which GL role absorbs the gap when an allocation rule closes an invoice for
# less cash than its full balance (app/reconciliation/rules/__init__.py's
# InvoiceAllocation.close_full=True) - e.g. tds-match's withheld TDS, or
# bank-fee's decoupled bank fee. Rules not listed here never produce a gap
# (exact-amount, subset-sum, exact-invoice-num, invoice-suffix, partial-payment
# all either match exactly or leave the invoice genuinely still open - no gap
# to explain). Used by both engine.py (to tag each allocation with its gap's
# destination while it's still in memory) and gl_posting.py (M3, to actually
# post it).
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
    # kind/name below match the recon-frontend prototype's mocks/ar.ts catalog
    # verbatim - the prototype is the naming source of truth this catalog
    # was reconciled against (see docs/reconciliation.md §8).

    # Phase 0 - INTAKE_VALIDATION (runs once per bank_txn, before customer
    # identification even starts; a reject here means Phase 1a/1b never run
    # for that row at all - see engine.py's run_phase_1)
    (PHASE_INTAKE_VALIDATION, "dup-utr", "Duplicate transaction reference check", 0, 100,
     {"description": "Reject a bank_reference already MATCHED in a prior run for this entity"}),

    # Phase 1a - CUSTOMER_LOCK (lock the paying customer; first match wins)
    (PHASE_CUSTOMER_LOCK, "expected-utr", "Expected UTR match", 10, 100,
     {"source": "expected_remittances", "match_field": "utr_number"}),
    (PHASE_CUSTOMER_LOCK, "account-ifsc", "Payer account + IFSC match", 20, 100,
     {"source": "customer_bank_accounts", "match_fields": ["bank_account_no", "ifsc_code"]}),
    (PHASE_CUSTOMER_LOCK, "upi", "UPI handle match", 30, 100,
     {"source": "customers", "match_field": "vpa_handle", "extract": "vpa"}),
    (PHASE_CUSTOMER_LOCK, "customer-code", "Customer code in narration", 40, 100,
     {"source": "customer_reference_codes", "extract": "narration_substring"}),
    (PHASE_CUSTOMER_LOCK, "gstin-pan", "GSTIN / PAN extraction", 50, 100,
     {"source": "customers", "extract": ["gstin", "pan"]}),
    (PHASE_CUSTOMER_LOCK, "fuzzy-name", "Fuzzy company name match", 60, 85,
     {"source": "customers", "match_field": "company_name", "min_similarity": 0.85}),

    # Phase 1b - CANDIDATE_POOL (only reached if Phase 1a locked nothing)
    (PHASE_CANDIDATE_POOL, "account-suffix", "Masked account suffix match (last 4)", 10, 60,
     {"source": "customer_bank_accounts", "match_field": "bank_account_no", "mode": "suffix"}),
    (PHASE_CANDIDATE_POOL, "narration-tokens", "Token-based narration match", 20, 55,
     {"source": "customers", "match_field": "company_name", "mode": "token_substring"}),

    # Phase 2 - ALLOCATION (scoped to the locked customer or candidate pool)
    # period-cutoff-guard/memo-netoff-guard have no frontend-prototype
    # counterpart (they're context-prep, not user-facing matching rules) -
    # kind/name left as originally chosen.
    (PHASE_ALLOCATION, "period-cutoff-guard", "Invoice issue date <= period_end", 0, None,
     {"date_field": "issue_date", "compare": "lte_period_end"}),
    (PHASE_ALLOCATION, "memo-netoff-guard", "Net off open credit/debit memos first", 5, None,
     {"source": "credit_debit_memos", "filter": "memo_date_lte_period_end"}),
    (PHASE_ALLOCATION, "exact-invoice-num", "Exact invoice number in narration", 10, 100,
     {"match_field": "invoice_number", "location": "narration"}),
    (PHASE_ALLOCATION, "invoice-suffix", "Invoice suffix / truncated number", 20, 90,
     {"match_field": "invoice_number", "mode": "suffix", "min_length": 4}),
    (PHASE_ALLOCATION, "exact-amount", "Exact amount match", 30, 100,
     {"amount": {"mode": "exact", "field": "balance_due_minor"}, "tie_break": "ambiguous_exception"}),
    (PHASE_ALLOCATION, "tds-match", "TDS match (invoice - allowed TDS = payment)", 40, 95,
     {"amount": {"mode": "net_of_tds", "field": "allowed_tds_minor"}}),
    (PHASE_ALLOCATION, "subset-sum", "Subset sum (many-to-many)", 50, 90,
     {"amount": {"mode": "subset_sum"}, "order_by": "due_date", "max_invoices": 10}),
    (PHASE_ALLOCATION, "bank-fee", "Bank fee / minor variance", 60, 80,
     {"amount": {"mode": "tolerance", "value_minor": 500}, "decouple_field": "explicit_fee_minor"}),
    (PHASE_ALLOCATION, "write-off", "Small balance write-off", 70, 100,
     {"amount": {"mode": "tolerance", "value_minor": 500}, "gl_role": GL_ROLE_WRITE_OFF}),
    (PHASE_ALLOCATION, "overpayment", "Overpayment -> On Account", 80, 100,
     {"gl_role": GL_ROLE_ON_ACCOUNT_ADVANCE}),
    (PHASE_ALLOCATION, "partial-payment", "Universal partial payment", 90, 60,
     {"mode": "partial", "allow_short_pay": True}),
)
