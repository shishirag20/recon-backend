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

STREAM_VALUES = ("BANK", "LEDGER", "INVOICE", "GATEWAY", "CUSTOMER")
DATA_SOURCE_KINDS = ("BANK_FEED", "GATEWAY", "ERP", "MANUAL_UPLOAD")
DATA_SOURCE_STATUSES = ("CONNECTED", "PENDING", "ERROR")

JOB_TYPE_INGEST = "INGEST"
JOB_TYPE_PROMOTE = "PROMOTE"


class DataHubErrors:
    ENTITY_NOT_FOUND = "Entity not found"
    DATA_SOURCE_NOT_FOUND = "Data source not found"
    JOB_NOT_FOUND = "Ingestion job not found"
    STAGING_RECORD_NOT_FOUND = "Staging record not found"
    UNSUPPORTED_FORMAT = "Unsupported upload format"
    FILE_TOO_LARGE = "Uploaded file exceeds the maximum allowed size"
    INVALID_STREAM = "Invalid stream value"
    JOB_NOT_RETRYABLE = "Only a FAILED job can be retried"
    JOB_NOT_PROMOTABLE = "Only a completed INGEST job (SUCCESS or PARTIAL) can be promoted"
    NO_ACTIVE_MAPPING = "Data source has no active field mapping"
