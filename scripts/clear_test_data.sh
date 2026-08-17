#!/bin/sh
# Clears every uploaded/run AR record (customers, invoices, bank statements,
# payments, runs, match groups, allocations, GL entries, and the BANK/
# CUSTOMER/INVOICE ingestion job history) so the streams are empty and ready
# for a fresh upload. Leaves data_sources, field_mappings, GL config,
# reconciliation_definitions/rules, and users/orgs untouched.
#
# Usage: ./scripts/clear_test_data.sh

docker exec recon-db-1 psql -U recon -d recon -v ON_ERROR_STOP=1 -c "
BEGIN;
DELETE FROM invoice_allocations;
DELETE FROM gl_journal_entries;
DELETE FROM reconciliation_runs;
DELETE FROM customer_bank_accounts;
DELETE FROM customer_reference_codes;
DELETE FROM expected_remittances;
DELETE FROM credit_debit_memos;
DELETE FROM gateway_settlements;
DELETE FROM payments;
DELETE FROM bank_statements;
DELETE FROM invoices;
DELETE FROM customers;
DELETE FROM ingestion_jobs WHERE stream IN ('BANK', 'CUSTOMER', 'INVOICE');
COMMIT;
"
