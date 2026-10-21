"""Exception hierarchy for the Business Central integration.

These let callers distinguish between permanent (do-not-retry) and transient
(retryable) failures. The client raises the specific subclass; the sync layer
and Celery tasks decide retry/backoff based on the type.
"""


class BusinessCentralError(Exception):
    """Base class for all Business Central integration errors.

    ``category`` is a short operator-facing label (used by the management
    commands) so a failure can be read without knowing the exception hierarchy.
    """

    category = "Business Central error"


class BusinessCentralConfigurationError(BusinessCentralError):
    """Configuration is missing or invalid (tenant, environment, company, credentials).

    Permanent — retrying without fixing configuration will not help.
    """

    category = "Configuration error"


class BusinessCentralEnvironmentNotFoundError(BusinessCentralConfigurationError):
    """The configured environment does not exist for the tenant (or is not reachable)."""

    category = "Invalid environment"


class BusinessCentralCompanyNotFoundError(BusinessCentralConfigurationError):
    """The configured company id is not visible in the configured environment."""

    category = "Invalid company"


class BusinessCentralAuthenticationError(BusinessCentralError):
    """OAuth2 token acquisition failed or the API rejected the token (401).

    Permanent after a single token refresh attempt.
    """

    category = "Authentication failure"


class BusinessCentralConnectionError(BusinessCentralError):
    """Network-level failure — timeout, DNS, or connection reset.

    Transient — safe to retry with backoff.
    """

    category = "Network error"


class BusinessCentralRateLimitError(BusinessCentralError):
    """The API returned HTTP 429 (too many requests).

    Transient — retry after backoff (honouring Retry-After when present).
    """

    category = "Rate limited"

    def __init__(self, message: str = "", *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class BusinessCentralSyncInProgressError(BusinessCentralError):
    """A sync for this integration + resource is already running.

    Raised when the sync lock cannot be acquired. Not retryable — the other run
    is doing the work.
    """

    category = "Sync already running"


class BusinessCentralResponseError(BusinessCentralError):
    """The API returned an unexpected status or an unparseable/invalid body.

    Whether this is retryable depends on the status code (5xx transient,
    4xx permanent); the client sets ``status_code`` so callers can decide.
    ``error_code`` is Business Central's own error code (e.g.
    ``Authorization_InsufficientPermissions``) when the body carried one.
    """

    category = "Unexpected API response"

    def __init__(self, message: str = "", *, status_code: int | None = None, error_code: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class BusinessCentralPermissionError(BusinessCentralResponseError):
    """HTTP 403 — authenticated, but the app lacks Business Central permissions."""

    category = "Missing Business Central permissions"


class BusinessCentralNotFoundError(BusinessCentralResponseError):
    """HTTP 404 — the requested environment, company or resource does not exist."""

    category = "Not found"
