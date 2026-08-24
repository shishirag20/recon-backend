"""
Verified seed script derived from the reference HTML design file.
Covers all 21 rules across 7 phases. Each rule's pipeline steps, datasets, and
outcomes are exactly aligned to the reference reconciliation engine logic,
using the exact dataset names from the data_sources table and the canonical fields.

Phases:
  0 (intake)         → 1 rule  : dup-utr
  1A (customer-lock) → 6 rules : expected-utr, account-ifsc, upi, customer-code, gstin-pan, fuzzy-name
  1B (candidate-pool)→ 2 rules : account-suffix, narration-tokens
  2 (allocation)     → 9 rules : exact-invoice-num, invoice-suffix, exact-amount, tds-match,
                                  subset-sum, bank-fee, write-off, overpayment, partial-payment
  3 (short-pay)      → 1 rule  : threshold
  4 (unapplied)      → 1 rule  : threshold
  5 (gl-check)       → 1 rule  : threshold
"""
import asyncio
import json
import asyncpg
import os

PIPELINE_CATALOG = {

    # ─── PHASE 1A: CUSTOMER IDENTIFICATION ─────────────────────────────────────

    "expected-utr": [
        {
            "id": "blk-eutr-1",
            "type": "filter",
            "title": "Verify Bank Reference Number Is Present",
            "tag": "STEP 1",
            "dataset": "Bank Statement",
            "conditions": [
                {
                    "entity": "Bank Statement",
                    "attribute": "bank_reference",
                    "operator": "!=",
                    "value": "''"
                }
            ]
        },
        {
            "id": "blk-eutr-2",
            "type": "match",
            "title": "Match Reference Number Against Customer Expected UTR",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "bank_reference",
            "targetEntity": "Expected Remittances",
            "targetAttribute": "utr_number",
            "matchMode": "exact",
            "confidence": 98,
            "fetchedOutputs": ["Customer ID", "Reconciled"]
        }
    ],

    "account-ifsc": [
        {
            "id": "blk-acct-1",
            "type": "filter",
            "title": "Verify Payer Account Number and IFSC Are Present",
            "tag": "STEP 1",
            "dataset": "Bank Statement",
            "conditions": [
                {
                    "entity": "Bank Statement",
                    "attribute": "payer_account_no",
                    "operator": "!=",
                    "value": "''"
                },
                {
                    "entity": "Bank Statement",
                    "attribute": "payer_ifsc",
                    "operator": "!=",
                    "value": "''"
                }
            ]
        },
        {
            "id": "blk-acct-2",
            "type": "match",
            "title": "Dual Match: Payer Account Number AND IFSC Code",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "payer_account_no, payer_ifsc",
            "targetEntity": "Customers",
            "targetAttribute": "account_number, ifsc_code",
            "matchMode": "exact_dual",
            "confidence": 97,
            "fetchedOutputs": ["Customer ID"]
        }
    ],

    "upi": [
        {
            "id": "blk-upi-1",
            "type": "filter",
            "title": "Verify Bank Narration Is Present",
            "tag": "STEP 1",
            "dataset": "Bank Statement",
            "conditions": [
                {
                    "entity": "Bank Statement",
                    "attribute": "narration",
                    "operator": "!=",
                    "value": "''"
                }
            ]
        },
        {
            "id": "blk-upi-2",
            "type": "extract",
            "title": "Extract UPI VPA Handle From Narration",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "narration",
            "pattern": "[a-zA-Z0-9.\\-_]{2,}@[a-zA-Z0-9.\\-_]{2,}",
            "outputVar": "extracted_vpa"
        },
        {
            "id": "blk-upi-3",
            "type": "match",
            "title": "Match Extracted VPA Against Customer UPI Handle",
            "tag": "STEP 3",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "extracted_vpa",
            "targetEntity": "Customers",
            "targetAttribute": "vpa_handle",
            "matchMode": "exact",
            "confidence": 95,
            "fetchedOutputs": ["Customer ID"]
        }
    ],

    "customer-code": [
        {
            "id": "blk-cc-1",
            "type": "filter",
            "title": "Verify Bank Narration Is Present",
            "tag": "STEP 1",
            "dataset": "Bank Statement",
            "conditions": [
                {
                    "entity": "Bank Statement",
                    "attribute": "narration",
                    "operator": "!=",
                    "value": "''"
                }
            ]
        },
        {
            "id": "blk-cc-2",
            "type": "match",
            "title": "Match Customer Code Token Found in Narration",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "narration",
            "targetEntity": "Customers",
            "targetAttribute": "customer_code",
            "matchMode": "contains",
            "confidence": 90,
            "fetchedOutputs": ["Customer ID"]
        }
    ],

    "gstin-pan": [
        {
            "id": "blk-gst-1",
            "type": "filter",
            "title": "Verify Bank Narration Is Present",
            "tag": "STEP 1",
            "dataset": "Bank Statement",
            "conditions": [
                {
                    "entity": "Bank Statement",
                    "attribute": "narration",
                    "operator": "!=",
                    "value": "''"
                }
            ]
        },
        {
            "id": "blk-gst-2",
            "type": "extract",
            "title": "Extract GSTIN / PAN Token From Narration",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "narration",
            "pattern": "[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]",
            "outputVar": "extracted_tax_id"
        },
        {
            "id": "blk-gst-3",
            "type": "match",
            "title": "Match Extracted Tax ID Against Customer GSTIN / PAN",
            "tag": "STEP 3",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "extracted_tax_id",
            "targetEntity": "Customers",
            "targetAttribute": "gstin",
            "matchMode": "contains",
            "confidence": 92,
            "fetchedOutputs": ["Customer ID"]
        }
    ],

    "fuzzy-name": [
        {
            "id": "blk-fuz-1",
            "type": "filter",
            "title": "Verify Payer Name Is Present on Bank Statement",
            "tag": "STEP 1",
            "dataset": "Bank Statement",
            "conditions": [
                {
                    "entity": "Bank Statement",
                    "attribute": "payer_name",
                    "operator": "!=",
                    "value": "''"
                }
            ]
        },
        {
            "id": "blk-fuz-2",
            "type": "match",
            "title": "Fuzzy Name Similarity: Payer Name vs Customer Company Name",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "payer_name",
            "targetEntity": "Customers",
            "targetAttribute": "company_name",
            "matchMode": "fuzzy_score",
            "confidence": 85,
            "fetchedOutputs": ["Customer ID"]
        }
    ],

    # ─── PHASE 1B: CANDIDATE POOL ──────────────────────────────────────────────
    "account-suffix": [
        {
            "id": "blk-sfx-1",
            "type": "filter",
            "title": "Verify Payer Account Number Is Not Empty",
            "tag": "STEP 1",
            "dataset": "Bank Statement",
            "conditions": [
                {
                    "entity": "Bank Statement",
                    "attribute": "payer_account_no",
                    "operator": "!=",
                    "value": "''"
                }
            ]
        },
        {
            "id": "blk-sfx-2",
            "type": "extract",
            "title": "Extract Masked Suffix (Last Digits) from Account Number",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "payer_account_no",
            "pattern": "[0-9]+$",
            "outputVar": "account_suffix"
        },
        {
            "id": "blk-sfx-3",
            "type": "match",
            "title": "Match Extracted Suffix Against Customer Bank Account",
            "tag": "STEP 3",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "account_suffix",
            "targetEntity": "Customers",
            "targetAttribute": "account_number",
            "matchMode": "suffix_ends_with",
            "confidence": 60,
            "fetchedOutputs": ["Customer ID"]
        }
    ],

    "narration-tokens": [
        {
            "id": "blk-ntk-1",
            "type": "filter",
            "title": "Verify Bank Narration Is Not Empty",
            "tag": "STEP 1",
            "dataset": "Bank Statement",
            "conditions": [
                {
                    "entity": "Bank Statement",
                    "attribute": "narration",
                    "operator": "!=",
                    "value": "''"
                }
            ]
        },
        {
            "id": "blk-ntk-2",
            "type": "extract",
            "title": "Extract 3+ Letter Keywords from Narration",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "narration",
            "pattern": "[A-Z0-9]{3,}",
            "outputVar": "narration_tokens"
        },
        {
            "id": "blk-ntk-3",
            "type": "match",
            "title": "Match Any Keyword to Customer Company Name",
            "tag": "STEP 3",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "narration_tokens",
            "targetEntity": "Customers",
            "targetAttribute": "company_name",
            "matchMode": "token_substring",
            "confidence": 50,
            "fetchedOutputs": ["Customer ID"]
        }
    ],

    # ─── PHASE 1C: NARRATION CROSS CHECK ───────────────────────────────────────
    "narration-invoice-check": [
        {
            "id": "blk-nic-1",
            "type": "filter",
            "title": "Verify Bank Narration Is Present",
            "tag": "STEP 1",
            "dataset": "Bank Statement",
            "conditions": [
                {
                    "entity": "Bank Statement",
                    "attribute": "narration",
                    "operator": "!=",
                    "value": "''"
                }
            ]
        },
        {
            "id": "blk-nic-2",
            "type": "match",
            "title": "Invoice Number in Narration",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "narration",
            "targetEntity": "Sub-Ledger",
            "targetAttribute": "invoice_number",
            "matchMode": "contains",
            "confidence": 100,
            "description": "Independently checks whether the transaction narration references a real invoice belonging to a different customer than the one Customer Identification / Candidate Pool already identified. Runs after both, for every row - a disagreement is flagged for review instead of letting the identified customer stand unquestioned."
        }
    ],

    # ─── PHASE 2: SCOPED INVOICE ALLOCATION ─────────────────────────────────────
    "exact-invoice-num": [
        {
            "id": "blk-inv-1",
            "type": "filter",
            "title": "Choose Candidate Invoices",
            "tag": "STEP 1",
            "dataset": "Sub-Ledger",
            "conditions": [
                {
                    "entity": "Invoice",
                    "attribute": "Status",
                    "operator": "IS IN",
                    "value": ["'OPEN'", "'Partially Settled'"]
                },
                {
                    "entity": "Invoice",
                    "attribute": "Invoice Date",
                    "operator": "<=",
                    "value": "Period End Date (As-Of)"
                },
                {
                    "entity": "Invoice",
                    "attribute": "Customer ID",
                    "operator": "==",
                    "value": "Locked Payer ID (Phase 1)"
                }
            ]
        },
        {
            "id": "blk-inv-2",
            "type": "match",
            "title": "Exact Invoice Number Found in Bank Narration",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "narration",
            "targetEntity": "Sub-Ledger",
            "targetAttribute": "invoice_number",
            "matchMode": "contains",
            "confidence": 98,
            "fetchedOutputs": ["Invoice ID", "Invoice Amount"]
        }
    ],

    "invoice-suffix": [
        {
            "id": "blk-isf-1",
            "type": "filter",
            "title": "Choose Candidate Invoices",
            "tag": "STEP 1",
            "dataset": "Sub-Ledger",
            "conditions": [
                {
                    "entity": "Invoice",
                    "attribute": "Status",
                    "operator": "IS IN",
                    "value": ["'OPEN'", "'Partially Settled'"]
                },
                {
                    "entity": "Invoice",
                    "attribute": "Invoice Date",
                    "operator": "<=",
                    "value": "Period End Date (As-Of)"
                },
                {
                    "entity": "Invoice",
                    "attribute": "Customer ID",
                    "operator": "==",
                    "value": "Locked Payer ID (Phase 1)"
                }
            ]
        },
        {
            "id": "blk-isf-2",
            "type": "extract",
            "title": "Extract Numeric Suffix / Strip Masked 'X's (e.g. INV-XXXX1046 → 1046)",
            "tag": "STEP 2",
            "sourceEntity": "Sub-Ledger",
            "sourceAttribute": "invoice_number",
            "pattern": "\\d{4,}$",
            "outputVar": "invoice_suffix"
        },
        {
            "id": "blk-isf-3",
            "type": "match",
            "title": "Match Extracted Suffix ($invoice_suffix) Against Bank Narration",
            "tag": "STEP 3",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "narration",
            "targetEntity": "Sub-Ledger",
            "targetAttribute": "$invoice_suffix",
            "matchMode": "suffix_ends_with",
            "confidence": 90,
            "fetchedOutputs": ["Invoice ID", "Invoice Amount"]
        }
    ],

    "exact-amount": [
        {
            "id": "blk-amt-1",
            "type": "filter",
            "title": "Choose Candidate Invoices",
            "tag": "STEP 1",
            "dataset": "Sub-Ledger",
            "conditions": [
                {
                    "entity": "Invoice",
                    "attribute": "Status",
                    "operator": "IS IN",
                    "value": ["'OPEN'", "'Partially Settled'"]
                },
                {
                    "entity": "Invoice",
                    "attribute": "Invoice Date",
                    "operator": "<=",
                    "value": "Period End Date (As-Of)"
                },
                {
                    "entity": "Invoice",
                    "attribute": "Customer ID",
                    "operator": "==",
                    "value": "Locked Payer ID (Phase 1)"
                }
            ]
        },
        {
            "id": "blk-amt-2",
            "type": "match",
            "title": "Payment Amount Exactly Equals Invoice Outstanding Balance",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "amount_minor",
            "targetEntity": "Sub-Ledger",
            "targetAttribute": "balance_due_minor",
            "matchMode": "exact_amount",
            "confidence": 95,
            "fetchedOutputs": ["Invoice ID", "Invoice Amount"]
        }
    ],

    "tds-match": [
        {
            "id": "blk-tds-1",
            "type": "filter",
            "title": "Choose Candidate Invoices",
            "tag": "STEP 1",
            "dataset": "Sub-Ledger",
            "conditions": [
                {
                    "entity": "Invoice",
                    "attribute": "Status",
                    "operator": "IS IN",
                    "value": ["'OPEN'", "'Partially Settled'"]
                },
                {
                    "entity": "Invoice",
                    "attribute": "Invoice Date",
                    "operator": "<=",
                    "value": "Period End Date (As-Of)"
                },
                {
                    "entity": "Invoice",
                    "attribute": "Customer ID",
                    "operator": "==",
                    "value": "Locked Payer ID (Phase 1)"
                }
            ]
        },
        {
            "id": "blk-tds-2",
            "type": "match",
            "title": "Payment = Invoice Balance Minus Allowed TDS Deduction",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "amount_minor",
            "targetEntity": "Sub-Ledger",
            "targetAttribute": "balance_due_minor",
            "matchMode": "tds_deduction",
            "confidence": 93,
            "fetchedOutputs": ["Invoice ID", "Invoice Amount"]
        }
    ],

    "subset-sum": [
        {
            "id": "blk-ss-1",
            "type": "filter",
            "title": "Choose Candidate Invoices",
            "tag": "STEP 1",
            "dataset": "Sub-Ledger",
            "conditions": [
                {
                    "entity": "Invoice",
                    "attribute": "Status",
                    "operator": "IS IN",
                    "value": ["'OPEN'", "'Partially Settled'"]
                },
                {
                    "entity": "Invoice",
                    "attribute": "Invoice Date",
                    "operator": "<=",
                    "value": "Period End Date (As-Of)"
                },
                {
                    "entity": "Invoice",
                    "attribute": "Customer ID",
                    "operator": "==",
                    "value": "Locked Payer ID (Phase 1)"
                }
            ]
        },
        {
            "id": "blk-ss-2",
            "type": "match",
            "title": "N Payments Sum Exactly to 1 Invoice, or 1 Payment Covers N Invoices",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "amount_minor",
            "targetEntity": "Sub-Ledger",
            "targetAttribute": "balance_due_minor",
            "matchMode": "subset_sum",
            "confidence": 85,
            "fetchedOutputs": ["Invoice ID", "Invoice Amount"]
        }
    ],

    "deduction-settlement": [
        {
            "id": "blk-ds-1",
            "type": "filter",
            "title": "Verify Transaction is a Debit (Withdrawal)",
            "tag": "STEP 1",
            "dataset": "Bank Statement",
            "conditions": [
                {
                    "entity": "Bank Statement",
                    "attribute": "dr_cr",
                    "operator": "==",
                    "value": "'DEBIT'"
                }
            ]
        },
        {
            "id": "blk-ds-2",
            "type": "match",
            "title": "Standalone Fee: Match Narration Against Magic Words",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "narration",
            "targetEntity": "Rules Configuration",
            "targetAttribute": "magic_words",
            "matchMode": "regex_contains",
            "confidence": 100,
            "description": "Matches words like FEE, CHG, SERV, SVC, WIRE, MONTHLY, ANALYSIS when customer identification fails, booking the total to Bank Fees GL."
        },
        {
            "id": "blk-ds-3",
            "type": "match",
            "title": "Transactional Fee: Match Net Settlement Against Invoice Balance",
            "tag": "STEP 3",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "amount_minor + explicit_fee_minor",
            "targetEntity": "Sub-Ledger",
            "targetAttribute": "balance_due_minor",
            "matchMode": "exact_amount",
            "confidence": 100,
            "description": "If customer is identified, checks payment amount + explicit fee against open partial invoice balances."
        }
    ],

    "write-off": [
        {
            "id": "blk-wof-1",
            "type": "filter",
            "title": "Choose Candidate Invoices",
            "tag": "STEP 1",
            "dataset": "Sub-Ledger",
            "conditions": [
                {
                    "entity": "Invoice",
                    "attribute": "Status",
                    "operator": "IS IN",
                    "value": ["'OPEN'", "'Partially Settled'"]
                },
                {
                    "entity": "Invoice",
                    "attribute": "Invoice Date",
                    "operator": "<=",
                    "value": "Period End Date (As-Of)"
                },
                {
                    "entity": "Invoice",
                    "attribute": "Customer ID",
                    "operator": "==",
                    "value": "Locked Payer ID (Phase 1)"
                }
            ]
        },
        {
            "id": "blk-wof-2",
            "type": "match",
            "title": "Write Off Residual Balance Below Materiality Threshold",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "amount_minor",
            "targetEntity": "Sub-Ledger",
            "targetAttribute": "balance_due_minor",
            "matchMode": "variance_tolerance",
            "confidence": 100,
            "fetchedOutputs": ["Invoice ID", "Invoice Amount"]
        }
    ],

    "overpayment": [
        {
            "id": "blk-ovp-1",
            "type": "filter",
            "title": "Choose Candidate Invoices",
            "tag": "STEP 1",
            "dataset": "Sub-Ledger",
            "conditions": [
                {
                    "entity": "Invoice",
                    "attribute": "Status",
                    "operator": "IS IN",
                    "value": ["'OPEN'", "'Partially Settled'"]
                },
                {
                    "entity": "Invoice",
                    "attribute": "Invoice Date",
                    "operator": "<=",
                    "value": "Period End Date (As-Of)"
                },
                {
                    "entity": "Invoice",
                    "attribute": "Customer ID",
                    "operator": "==",
                    "value": "Locked Payer ID (Phase 1)"
                }
            ]
        },
        {
            "id": "blk-ovp-2",
            "type": "match",
            "title": "Match Invoice with Smallest Excess (Payment > Balance Due)",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "amount_minor",
            "targetEntity": "Sub-Ledger",
            "targetAttribute": "balance_due_minor",
            "matchMode": "overpayment",
            "confidence": 100,
            "fetchedOutputs": ["Invoice ID", "Invoice Amount"]
        },
        {
            "id": "blk-ovp-3",
            "type": "action",
            "title": "Fully Settle Invoice & Book Excess Cash to On-Account Credit",
            "tag": "STEP 3",
            "description": "Fully clears the target invoice (close_full=true). The remaining excess cash (Payment Received − Invoice Balance Due) is recorded as On-Account Credit on the customer account (payments.unapplied_minor) for future invoices or refund."
        }
    ],

    "partial-payment": [
        {
            "id": "blk-par-1",
            "type": "filter",
            "title": "Filter Candidate Invoices Sorted by Due Date (FIFO)",
            "tag": "STEP 1",
            "dataset": "Sub-Ledger",
            "conditions": [
                {
                    "entity": "Invoice",
                    "attribute": "Status",
                    "operator": "IS IN",
                    "value": ["'OPEN'", "'Partially Settled'"]
                },
                {
                    "entity": "Invoice",
                    "attribute": "Invoice Date",
                    "operator": "<=",
                    "value": "Period End Date (As-Of)"
                },
                {
                    "entity": "Invoice",
                    "attribute": "Customer ID",
                    "operator": "==",
                    "value": "Locked Payer ID (Phase 1)"
                }
            ]
        },
        {
            "id": "blk-par-2",
            "type": "match",
            "title": "Allocate Cash to Oldest-Due Open Invoice (FIFO Fallback)",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "amount_minor",
            "targetEntity": "Sub-Ledger",
            "targetAttribute": "balance_due_minor",
            "matchMode": "partial_payment",
            "confidence": 100,
            "fetchedOutputs": ["Invoice ID", "Invoice Amount"]
        },
        {
            "id": "blk-par-3",
            "type": "action",
            "title": "Partially Reduce Invoice Balance & Keep Residual Open",
            "tag": "STEP 3",
            "description": "Applies received cash to reduce the oldest invoice balance (close_full=false). The remaining unpaid balance stays open on the sub-ledger (or escalates to Phase 3 Short-Pay review if the shortfall exceeds the configured tolerance limit)."
        }
    ],

    # ─── PHASES 3–5: SINGLE-THRESHOLD CHECKS ──────────────────────────────────
    "threshold": [
        {
            "id": "blk-thr-1",
            "type": "filter",
            "title": "Evaluate Shortfall / Variance Amount",
            "tag": "STEP 1",
            "dataset": "Bank Statement",
            "conditions": [
                {
                    "entity": "Bank Statement",
                    "attribute": "amount_minor",
                    "operator": ">",
                    "value": "0"
                }
            ]
        },
        {
            "id": "blk-thr-2",
            "type": "match",
            "title": "Compare Against Configured Tolerance Threshold",
            "tag": "STEP 2",
            "sourceEntity": "Bank Statement",
            "sourceAttribute": "amount_minor",
            "targetEntity": "Sub-Ledger",
            "targetAttribute": "balance_due_minor",
            "matchMode": "threshold_check",
            "confidence": 100,
            "fetchedOutputs": ["Variance Amount", "Threshold Configured", "Exception Raised"]
        }
    ],
}

OUTCOME_CATALOG = {
    "expected-utr": {
        "ifMatched": "Bank row is immediately locked to the matching customer using pre-advised UTR from Expected Remittances dataset. Processing advances to Scoped Invoice Allocation.",
        "else": "Rule skips — no record in Expected Remittances table matches this bank reference number. Waterfall continues to Payer Account & IFSC Match."
    },
    "account-ifsc": {
        "ifMatched": "Bank row is locked to the customer whose registered bank account number AND IFSC code both match the payer details on the statement. Advances to Phase 2 Invoice Allocation.",
        "else": "Rule skips — either no account/IFSC present on the bank row, or no customer record matches both fields. Waterfall continues to UPI Handle Match."
    },
    "upi": {
        "ifMatched": "Bank row is locked to the customer whose saved UPI handle matches the VPA extracted from the narration. Advances to Phase 2 Invoice Allocation.",
        "else": "Rule skips — no valid UPI handle found in narration, or no customer handle matches. Waterfall continues to Customer Code in Narration Match."
    },
    "customer-code": {
        "ifMatched": "Bank row is locked to the customer whose unique customer code appears as a token in the bank narration. Advances to Phase 2 Invoice Allocation.",
        "else": "Rule skips — customer code not found in narration. Waterfall continues to Tax ID & PAN Match."
    },
    "gstin-pan": {
        "ifMatched": "Bank row is locked to the customer whose GSTIN or PAN was extracted from the narration. Advances to Phase 2 Invoice Allocation.",
        "else": "Rule skips — no valid GSTIN/PAN found in narration, or no customer matches. Waterfall continues to Company Name fuzzy match (last resort)."
    },
    "fuzzy-name": {
        "ifMatched": "Bank row is locked to the customer whose company name is sufficiently similar (Levenshtein similarity >= configured threshold) to the payer name on the bank statement. Advances to Phase 2 Invoice Allocation.",
        "else": "Rule skips — payer name similarity below threshold for all customers. Bank row proceeds to Phase 1B Candidate Pool fallback."
    },
    "account-suffix": {
        "ifMatched": "Customer added to candidate pool — the masked payer account's last 4 digits match this customer's registered account suffix. Customer identification is not finalised here; pool is handed off to exact-amount resolution.",
        "else": "Rule skips — no matching account suffix, or bank account is full-length (not masked). Narration token matching continues."
    },
    "narration-tokens": {
        "ifMatched": "Customer added to candidate pool — at least one narration token (>= 3 chars) matched a token in the customer's company name. Pool is handed off to exact-amount resolution.",
        "else": "Rule skips — no meaningful token overlap. Customer not added to pool. If pool remains empty, bank row surfaces as a Suspense exception."
    },
    "narration-invoice-check": {
        "ifMatched": "If the narration references a real invoice for a DIFFERENT customer than the one identified in Phase 1A/1B, flags for review. If it matches the same customer, or no invoice is found, it proceeds without raising an exception.",
        "else": "Rule skips and proceeds.",
        "cards": []
    },
    "exact-invoice-num": {
        "ifMatched": "Automatically allocates payment to the exact sub-ledger invoice number.",
        "else": "Rule skips and proceeds to the next Allocation rule.",
        "cards": ["full", "partial", "overpayment"]
    },
    "invoice-suffix": {
        "ifMatched": "Automatically allocates payment by finding a truncated invoice number suffix.",
        "else": "Rule skips and proceeds to the next Allocation rule.",
        "cards": ["full", "partial", "overpayment"]
    },
    "exact-amount": {
        "ifMatched": "Automatically allocates payment when it exactly matches an open invoice balance.",
        "else": "Rule skips and proceeds to the next Allocation rule.",
        "cards": ["full"]
    },
    "tds-match": {
        "ifMatched": "Automatically allocates payment by adjusting for allowable TDS (Tax Deducted at Source) deductions.",
        "else": "Rule skips and proceeds to the next Allocation rule.",
        "cards": ["full"]
    },
    "subset-sum": {
        "ifMatched": "Automatically allocates payment across multiple invoices where the combined balance exactly matches the payment.",
        "else": "Rule skips and proceeds to the next Allocation rule.",
        "cards": ["full"]
    },
    "deduction-settlement": {
        "ifMatched": "If unidentified, matches magic words in narration to route standalone fees directly to the Bank Fees GL. If customer is identified, matches transaction amount (plus any explicit transactional fee) against open partial invoices, settling them with a bank fee gap.",
        "else": "The payment is not a debit, or narration does not contain magic words (unidentified), or it does not match any open partial invoice balance (identified).",
        "cards": ["full", "partial"]
    },
    "write-off": {
        "ifMatched": "Automatically writes off any remaining small balance that falls below the configured materiality threshold.",
        "else": "Rule skips and proceeds to the next Allocation rule.",
        "cards": ["full"]
    },
    "overpayment": {
        "ifMatched": "Automatically applies payment to the invoice and records the excess amount as an On-Account Credit.",
        "else": "Rule skips and proceeds to the next Allocation rule.",
        "cards": ["overpayment"]
    },
    "partial-payment": {
        "ifMatched": "Automatically applies payment as a partial settlement to the oldest open invoice.",
        "else": "Rule skips and proceeds to the next Allocation rule.",
        "cards": ["partial"]
    },
    "threshold": {
        "ifMatched": "Automatically absorbs the shortfall/variance within the configured tolerance without raising an exception.",
        "else": "Rule skips and proceeds to raise an exception for human review.",
        "cards": []
    },
}

DATASET_CONFIGS = {
    "expected-utr":      {"primaryDataset": "Bank Statement", "primaryField": "bank_reference",       "targetDataset": "Expected Remittances", "targetField": "utr_number"},
    "account-ifsc":      {"primaryDataset": "Bank Statement", "primaryField": "payer_account_no, payer_ifsc",   "targetDataset": "Customers",       "targetField": "account_number, ifsc_code"},
    "upi":               {"primaryDataset": "Bank Statement", "primaryField": "narration",              "targetDataset": "Customers",       "targetField": "vpa_handle"},
    "customer-code":     {"primaryDataset": "Bank Statement", "primaryField": "narration",              "targetDataset": "Customers",       "targetField": "customer_code"},
    "gstin-pan":         {"primaryDataset": "Bank Statement", "primaryField": "narration",              "targetDataset": "Customers",       "targetField": "gstin"},
    "fuzzy-name":        {"primaryDataset": "Bank Statement", "primaryField": "payer_name",             "targetDataset": "Customers",       "targetField": "company_name"},
    "account-suffix":    {"primaryDataset": "Bank Statement", "primaryField": "payer_account_no",   "targetDataset": "Customers",       "targetField": "account_number"},
    "narration-tokens":  {"primaryDataset": "Bank Statement", "primaryField": "narration",              "targetDataset": "Customers",       "targetField": "company_name"},
    "narration-invoice-check": {"primaryDataset": "Bank Statement", "primaryField": "narration",              "targetDataset": "Sub-Ledger",        "targetField": "invoice_number"},
    "exact-invoice-num": {"primaryDataset": "Bank Statement", "primaryField": "narration",              "targetDataset": "Sub-Ledger",        "targetField": "invoice_number"},
    "invoice-suffix":    {"primaryDataset": "Bank Statement", "primaryField": "narration",              "targetDataset": "Sub-Ledger",        "targetField": "invoice_number"},
    "exact-amount":      {"primaryDataset": "Bank Statement", "primaryField": "amount_minor",                 "targetDataset": "Sub-Ledger",        "targetField": "balance_due_minor"},
    "tds-match":         {"primaryDataset": "Bank Statement", "primaryField": "amount_minor",                 "targetDataset": "Sub-Ledger",        "targetField": "balance_due_minor"},
    "subset-sum":        {"primaryDataset": "Bank Statement", "primaryField": "amount_minor",                 "targetDataset": "Sub-Ledger",        "targetField": "balance_due_minor"},
    "deduction-settlement": {"primaryDataset": "Bank Statement", "primaryField": "amount_minor",                 "targetDataset": "Sub-Ledger",        "targetField": "balance_due_minor"},
    "overpayment":       {"primaryDataset": "Bank Statement", "primaryField": "amount_minor",                 "targetDataset": "Sub-Ledger",        "targetField": "balance_due_minor"},
    "partial-payment":   {"primaryDataset": "Bank Statement", "primaryField": "amount_minor",                 "targetDataset": "Sub-Ledger",        "targetField": "balance_due_minor"},
    "threshold":         {"primaryDataset": "Bank Statement", "primaryField": "amount_minor",                 "targetDataset": "Sub-Ledger",        "targetField": "balance_due_minor"},
}

async def main():
    db_url = os.getenv("DATABASE_URL", "postgresql://recon:recon@db:5432/recon")
    if "localhost" not in db_url and os.getenv("RUNNING_ON_HOST") == "1":
        db_url = "postgresql://recon:recon@localhost:5433/recon"
    conn = await asyncpg.connect(db_url)
    try:
        # Temporarily offset priorities across all rules to avoid unique constraint collisions
        await conn.execute("UPDATE reconciliation_rules SET priority = priority + 1000")

        # Insert expected-utr rule if missing
        e_utr = await conn.fetchrow("SELECT rule_id FROM reconciliation_rules WHERE kind = 'expected-utr'")
        if not e_utr:
            def_id = await conn.fetchval("SELECT definition_id FROM reconciliation_definitions LIMIT 1")
            if def_id:
                await conn.execute(
                    "INSERT INTO reconciliation_rules (rule_id, definition_id, phase, kind, name, priority, enabled, confidence, config) "
                    "VALUES (gen_random_uuid(), $1, 'CUSTOMER_LOCK', 'expected-utr', 'Pre-Advised UTR Match', 9999, true, 98, '{}'::jsonb)",
                    def_id
                )
                print("Inserted expected-utr rule into reconciliation_rules")

        # Insert narration-invoice-check rule if missing
        n_check = await conn.fetchrow("SELECT rule_id FROM reconciliation_rules WHERE kind = 'narration-invoice-check'")
        if not n_check:
            def_id = await conn.fetchval("SELECT definition_id FROM reconciliation_definitions LIMIT 1")
            if def_id:
                await conn.execute(
                    "INSERT INTO reconciliation_rules (rule_id, definition_id, phase, kind, name, priority, enabled, confidence, config) "
                    "VALUES (gen_random_uuid(), $1, 'NARRATION_CROSS_CHECK', 'narration-invoice-check', 'Invoice Number in Narration', 1, true, 100, '{}'::jsonb)",
                    def_id
                )
                print("Inserted narration-invoice-check rule into reconciliation_rules")

        # Delete dup-utr if present
        await conn.execute("DELETE FROM reconciliation_rules WHERE kind = 'dup-utr'")
        print("Deleted dup-utr from reconciliation_rules")

        # Update customer-lock priorities
        priorities = {
            "expected-utr": ("CUSTOMER_LOCK", 1),
            "account-ifsc": ("CUSTOMER_LOCK", 2),
            "upi": ("CUSTOMER_LOCK", 3),
            "customer-code": ("CUSTOMER_LOCK", 4),
            "gstin-pan": ("CUSTOMER_LOCK", 5),
            "fuzzy-name": ("CUSTOMER_LOCK", 6),
        }
        for k, (ph, p) in priorities.items():
            await conn.execute(
                "UPDATE reconciliation_rules SET phase = $1, priority = $2 WHERE kind = $3",
                ph, p, k
            )

        rows = await conn.fetch("SELECT rule_id, kind, config FROM reconciliation_rules")
        print(f"Found {len(rows)} rules in database.")
        updated = 0
        skipped = 0
        for r in rows:
            rule_id = r["rule_id"]
            kind = r["kind"]
            cfg = json.loads(r["config"]) if isinstance(r["config"], str) else dict(r["config"])

            pipeline = PIPELINE_CATALOG.get(kind)
            outcome = OUTCOME_CATALOG.get(kind)
            dataset_cfg = DATASET_CONFIGS.get(kind)

            if pipeline:
                cfg["pipeline"] = pipeline
            if outcome:
                cfg["outcome"] = outcome
                cfg["rule_outcomes"] = outcome
            if dataset_cfg:
                cfg.update(dataset_cfg)

            if not pipeline and not outcome and not dataset_cfg:
                print(f"  SKIP  rule_id={rule_id} kind={kind!r} — no config entry defined")
                skipped += 1
                continue

            await conn.execute(
                "UPDATE reconciliation_rules SET config = $1::jsonb WHERE rule_id = $2",
                json.dumps(cfg), rule_id
            )
            updated += 1
            print(f"  OK    rule_id={rule_id} kind={kind!r}")

        print(f"\nDone. Updated={updated}  Skipped={skipped}  Total={len(rows)}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
