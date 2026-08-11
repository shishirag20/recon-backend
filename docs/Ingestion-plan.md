# Ingestion Job Enhancement: All-or-Nothing Two-Pass Ingestion

## 1. The Problem
Currently, the ingestion worker processes CSV files sequentially, inserting rows one by one into the database. If a user uploads a file with 10 rows and 5 of them contain data errors (e.g., type mismatches, missing fields, or constraint violations), the worker successfully inserts the 5 valid rows and marks the job status as `PARTIAL`.

This leads to a fragmented database state:
- The user is left with a partially ingested file.
- It is difficult to safely re-upload the file after fixing the 5 errors without risking duplicates for the 5 successful rows.
- Users generally expect an atomic, "all-or-nothing" operation where a batch of data is either completely accepted or completely rejected.

## 2. The Approach: Validate First, Insert Second (Two-Pass)
To ensure data integrity and prevent partial states, we will transition the ingestion worker to a **Two-Pass (Validate First, Insert Second)** architecture.

1. **Pass 1 (Validation in Memory):** Loop through the entire CSV in memory, apply the dynamic field mappings, and run strict data validation on the resulting canonical rows (e.g., ensuring strings can cast to booleans, dates are valid, and required fields are present). We will use Python schemas (like Pydantic) to validate constraints *before* touching the database.
2. **Failure Handling:** If *any* row fails validation during Pass 1, we immediately abort the ingestion process. The database remains completely untouched, the job status is set to `FAILED`, and the exact validation errors are saved to `failed_rows` so the user can fix their CSV.
3. **Pass 2 (Atomic Insertion):** If and only if all rows are 100% valid, we proceed to insert all rows into the database inside a single, bulk database transaction.

## 3. The Implementation Plan

### Step A: Introduce Pydantic Validation Schemas
In `app/datahub/canonical.py`, we will define Pydantic models to mirror the database constraints for our supported streams.

```python
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import date
from app.datahub.canonical import RowRejected

class BankStatementValidator(BaseModel):
    transaction_date: date
    currency: str = Field(min_length=3, max_length=3)
    amount_minor: int
    amount_home_minor: int
    dr_cr: str
    is_bank_charge: bool = False
    
    # Example custom validator to catch string booleans
    @field_validator('is_bank_charge', mode='before')
    @classmethod
    def parse_boolean(cls, v):
        if isinstance(v, str):
            if v.lower() in ('true', '1', 't', 'yes'): return True
            if v.lower() in ('false', '0', 'f', 'no'): return False
        return v
```

### Step B: Refactor `process_ingestion_job` in `ingestion_worker.py`
We will rewrite the core loop in the worker to execute the two passes.

```python
        row_count = 0
        error_count = 0
        failed_rows: list[dict] = []
        valid_records: list[tuple] = []

        # ==========================================
        # PASS 1: Validate all rows in memory
        # ==========================================
        for raw_row in rows:
            row_count += 1
            canonical, issues = apply_mapping(raw_row, mappings)
            issues = issues + unknown_field_issues(job["stream"], canonical)
            
            # Run schema validation
            try:
                if job["stream"] == "BANK":
                    BankStatementValidator(**canonical)
                # elif job["stream"] == "INVOICE":
                #     InvoiceValidator(**canonical)
            except Exception as exc:
                issues.append(f"Validation failed: {str(exc)}")

            if issues:
                error_count += 1
                failed_rows.append({"raw": raw_row, "issues": issues})
            else:
                valid_records.append((raw_row, canonical))

        # ==========================================
        # PASS 2: Insert ONLY if the whole file is clean
        # ==========================================
        if error_count > 0:
            # Abort insertion. Record the failures.
            pass
        else:
            # Everything is clean. Safe to insert everything.
            async with conn.transaction():
                for raw_row, canonical in valid_records:
                    await insert_fn(
                        conn,
                        entity_id=entity_id,
                        source_job_id=job["job_id"],
                        canonical=canonical,
                        raw=raw_row,
                        issues=[],
                        home_currency=home_currency,
                    )

        # Update mapping version logic remains the same...
        return row_count, error_count, failed_rows
```

### Step C: Update Status Assignment in `run_one_job`
Ensure that if `error_count > 0`, the job is marked strictly as `FAILED` rather than `PARTIAL`.

```python
        if error_count == 0:
            status = "SUCCESS"
        else:
            # If any errors exist, 0 rows were inserted due to our two-pass logic.
            status = "FAILED"
```