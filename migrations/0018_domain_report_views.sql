CREATE VIEW v_report_matched AS
SELECT
    r.run_id,
    r.run_no,
    r.status AS run_status,
    mg.match_group_id,
    mg.match_type,
    mg.status AS match_status,
    ia.allocation_id,
    ia.allocated_minor,
    i.invoice_id,
    i.invoice_number,
    c.customer_id,
    c.company_name,
    bs.bank_txn_id,
    bs.bank_reference,
    u.full_name AS matched_by
FROM invoice_allocations ia
JOIN match_groups mg ON mg.match_group_id = ia.match_group_id
JOIN reconciliation_runs r ON r.run_id = mg.run_id
JOIN invoices i ON i.invoice_id = ia.invoice_id
JOIN customers c ON c.customer_id = i.customer_id
LEFT JOIN bank_statements bs ON bs.bank_txn_id = ia.bank_txn_id
LEFT JOIN users u ON u.id = mg.created_by
WHERE r.status IN ('APPROVED', 'CLOSED');

CREATE VIEW v_report_runs AS
SELECT
    r.run_id,
    r.run_no,
    r.status,
    r.period_start,
    r.period_end,
    r.matched_count,
    r.exception_count,
    r.matched_value_minor,
    r.exception_value_minor,
    r.unapplied_minor,
    (SELECT full_name FROM users WHERE id = r.prepared_by) AS prepared_by_name,
    (SELECT full_name FROM users WHERE id = r.reviewed_by) AS reviewed_by_name,
    r.signed_at,
    r.run_hash
FROM reconciliation_runs r;
