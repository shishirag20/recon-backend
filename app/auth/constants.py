"""Static configuration and messages for the auth module.

Keep literals (routes, token settings, error text, roles) here so the router,
service, and dao layers all reference one source of truth.
"""

# ── Router ────────────────────────────────────────────────────────────────
ROUTER_PREFIX = "/auth"
ROUTER_TAGS = ["Auth"]

# ── Tokens / security ─────────────────────────────────────────────────────
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 7
JWT_ALGORITHM = "HS256"

# ── Roles ─────────────────────────────────────────────────────────────────
ROLE_PREPARER = "PREPARER"
ROLE_REVIEWER = "REVIEWER"
ROLE_ADMIN = "ADMIN"
DEFAULT_ROLE = ROLE_PREPARER


class AuthErrors:
    """User-facing error messages raised by the auth service."""

    INVALID_CREDENTIALS = "Invalid email or password"
    EMAIL_TAKEN = "An account with this email already exists"
    USER_NOT_FOUND = "User not found"
    INACTIVE_USER = "User account is inactive"
    INVALID_TOKEN = "Could not validate credentials"
    EXPIRED_TOKEN = "Token has expired"
