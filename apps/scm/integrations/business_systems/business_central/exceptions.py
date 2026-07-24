"""Exception hierarchy for the Business Central integration.

These let callers distinguish between permanent (do-not-retry) and transient
(retryable) failures. The client raises the specific subclass; the sync layer
and Celery tasks decide retry/backoff based on the type.
"""


class BusinessCentralError(Exception):
    """Base class for all Business Central integration errors."""


class BusinessCentralConfigurationError(BusinessCentralError):
    """Configuration is missing or invalid (tenant, environment, company, credentials).

    Permanent — retrying without fixing configuration will not help.
    """


class BusinessCentralAuthenticationError(BusinessCentralError):
    """OAuth2 token acquisition failed or the API rejected the token (401).

    Permanent after a single token refresh attempt.
    """


class BusinessCentralConnectionError(BusinessCentralError):
    """Network-level failure — timeout, DNS, or connection reset.

    Transient — safe to retry with backoff.
    """


class BusinessCentralRateLimitError(BusinessCentralError):
    """The API returned HTTP 429 (too many requests).

    Transient — retry after backoff (honouring Retry-After when present).
    """

    def __init__(self, message: str = "", *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class BusinessCentralResponseError(BusinessCentralError):
    """The API returned an unexpected status or an unparseable/invalid body.

    Whether this is retryable depends on the status code (5xx transient,
    4xx permanent); the client sets ``status_code`` so callers can decide.
    """

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
