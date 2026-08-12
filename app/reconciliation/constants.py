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
    # Phase 1a - CUSTOMER_LOCK (lock the paying customer; first match wins)
    (PHASE_CUSTOMER_LOCK, "dup-utr-check", "Duplicate UTR pre-check", 0, 100,
     {"description": "Reject a bank_reference already MATCHED in a prior run for this entity"}),
    (PHASE_CUSTOMER_LOCK, "utr-match", "Expected remittance UTR match", 10, 100,
     {"source": "expected_remittances", "match_field": "utr_number"}),
    (PHASE_CUSTOMER_LOCK, "bank-account-match", "Registered bank account + IFSC match", 20, 100,
     {"source": "customer_bank_accounts", "match_fields": ["bank_account_no", "ifsc_code"]}),
    (PHASE_CUSTOMER_LOCK, "vpa-match", "UPI VPA in narration", 30, 100,
     {"source": "customers", "match_field": "vpa_handle", "extract": "vpa"}),
    (PHASE_CUSTOMER_LOCK, "reference-code-match", "Customer reference code in narration", 40, 100,
     {"source": "customer_reference_codes", "extract": "narration_substring"}),
    (PHASE_CUSTOMER_LOCK, "gstin-pan-match", "GSTIN/PAN extracted from narration", 50, 100,
     {"source": "customers", "extract": ["gstin", "pan"]}),
    (PHASE_CUSTOMER_LOCK, "fuzzy-name-match", "Fuzzy company-name match (pg_trgm)", 60, 85,
     {"source": "customers", "match_field": "company_name", "min_similarity": 0.85}),

    # Phase 1b - CANDIDATE_POOL (only reached if Phase 1a locked nothing)
    (PHASE_CANDIDATE_POOL, "masked-account-pool", "Masked/partial account-number suffix", 10, 60,
     {"source": "customer_bank_accounts", "match_field": "bank_account_no", "mode": "suffix"}),
    (PHASE_CANDIDATE_POOL, "token-pool", "Company-name token substring", 20, 55,
     {"source": "customers", "match_field": "company_name", "mode": "token_substring"}),

    # Phase 2 - ALLOCATION (scoped to the locked customer or candidate pool)
    (PHASE_ALLOCATION, "period-cutoff-guard", "Invoice issue date <= period_end", 0, None,
     {"date_field": "issue_date", "compare": "lte_period_end"}),
    (PHASE_ALLOCATION, "memo-netoff-guard", "Net off open credit/debit memos first", 5, None,
     {"source": "credit_debit_memos", "filter": "memo_date_lte_period_end"}),
    (PHASE_ALLOCATION, "invoice-number-match", "Invoice number in narration", 10, 100,
     {"match_field": "invoice_number", "location": "narration"}),
    (PHASE_ALLOCATION, "truncated-suffix-match", "Truncated invoice-number suffix", 20, 90,
     {"match_field": "invoice_number", "mode": "suffix", "min_length": 4}),
    (PHASE_ALLOCATION, "exact-balance-match", "Exact balance-due match", 30, 100,
     {"amount": {"mode": "exact", "field": "balance_due_minor"}, "tie_break": "ambiguous_exception"}),
    (PHASE_ALLOCATION, "tds-net-match", "Amount matches balance net of allowed TDS", 40, 95,
     {"amount": {"mode": "net_of_tds", "field": "allowed_tds_minor"}}),
    (PHASE_ALLOCATION, "subset-sum-fifo", "Subset-sum across open invoices, FIFO by due date", 50, 90,
     {"amount": {"mode": "subset_sum"}, "order_by": "due_date", "max_invoices": 10}),
    (PHASE_ALLOCATION, "fee-tolerance-match", "Within fee/variance tolerance (fee decoupled)", 60, 80,
     {"amount": {"mode": "tolerance", "value_minor": 500}, "decouple_field": "explicit_fee_minor"}),
    (PHASE_ALLOCATION, "dust-writeoff", "Residual below dust threshold -> write off", 70, 100,
     {"amount": {"mode": "tolerance", "value_minor": 500}, "gl_role": GL_ROLE_WRITE_OFF}),
    (PHASE_ALLOCATION, "overpay-on-account", "Overpayment -> unapplied on-account", 80, 100,
     {"gl_role": GL_ROLE_ON_ACCOUNT_ADVANCE}),
    (PHASE_ALLOCATION, "partial-pay", "Universal partial-payment fallback", 90, 60,
     {"mode": "partial", "allow_short_pay": True}),
)
