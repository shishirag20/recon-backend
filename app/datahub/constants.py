"""Static configuration and messages for the data-hub module."""

# -- Router --------------------------------------------------------------
ROUTER_PREFIX = ""
ROUTER_TAGS = ["Data Hub"]

# -- Ingestion -------------------------------------------------------------
# Only CSV is implemented so far; XLSX/MT940/OFX are recognized as valid
# `data_sources`/job formats in the schema but the worker doesn't parse them
# yet - uploading one will fail the job with a clear "unsupported format"
# error rather than silently mis-parsing it.
SUPPORTED_UPLOAD_FORMATS = ("CSV",)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB
UPLOAD_CHUNK_BYTES = 1024 * 1024

# Only BANK/INVOICE/CUSTOMER have a canonical direct-to-table inserter today
# (see app/datahub/canonical.py) - LEDGER/GATEWAY are valid values but a job
# using them will fail with "unsupported stream".
STREAM_VALUES = ("BANK", "LEDGER", "INVOICE", "GATEWAY", "CUSTOMER")
DATA_SOURCE_KINDS = ("BANK_FEED", "GATEWAY", "ERP", "MANUAL_UPLOAD")
DATA_SOURCE_STATUSES = ("CONNECTED", "PENDING", "ERROR")


class DataHubErrors:
    ENTITY_NOT_FOUND = "Entity not found"
    DATA_SOURCE_NOT_FOUND = "Data source not found"
    JOB_NOT_FOUND = "Ingestion job not found"
    RECORD_NOT_FOUND = "Record not found"
    UNSUPPORTED_FORMAT = "Unsupported upload format"
    UNSUPPORTED_STREAM_FOR_EXPLORER = "This stream has no canonical table registered for Data Explorer"
    FILE_TOO_LARGE = "Uploaded file exceeds the maximum allowed size"
    INVALID_STREAM = "Invalid stream value"
    JOB_NOT_RETRYABLE = "Only a FAILED job can be retried"
    NO_ACTIVE_MAPPING = "This stream has no active field mapping"
    DUPLICATE_UPLOAD = "An identical file has already been uploaded against this data source"
