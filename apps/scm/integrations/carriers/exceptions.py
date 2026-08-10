"""Typed exception hierarchy for carrier tracking integrations.

Every carrier adapter raises one of these instead of a bare Exception so the
sync layer can decide — deterministically — whether an attempt was a permanent
configuration problem, a transient network problem, or simply a carrier that has
no data for the reference yet.

Two distinctions matter to callers:

``transient``
    True when retrying the same call later can reasonably succeed (timeout, rate
    limit, 5xx). False for configuration, auth and unsupported-reference errors,
    where retrying without a change is pointless.

"No data" is not a failure
    :class:`CarrierNoDataError` means the call succeeded and the carrier knows
    nothing about the reference yet. The sync layer records it as a successful
    sync with zero events, never as a technical error.
"""


class CarrierError(Exception):
    """Base class for all carrier integration errors.

    ``transient`` tells the caller whether a retry is worthwhile.
    """

    transient: bool = False

    def __init__(self, message: str = "", *, provider_code: str = "") -> None:
        super().__init__(message)
        self.provider_code = provider_code


class CarrierNotImplementedError(CarrierError, NotImplementedError):
    """The adapter is a stub — the carrier has no implementation yet.

    Also a :class:`NotImplementedError` so that stub adapters remain detectable
    with the standard Python contract. The sync layer maps this to a SKIPPED
    sync run: nothing was attempted, so it is neither success nor failure.
    """


class CarrierConfigurationError(CarrierError):
    """Required configuration or credentials are missing or invalid.

    Permanent — the integration must be configured before the call can work.
    """


class CarrierAuthenticationError(CarrierError):
    """The carrier rejected the credentials (401/403).

    Permanent until the credentials are fixed or refreshed.
    """


class CarrierRateLimitError(CarrierError):
    """The carrier returned HTTP 429.

    Transient — honour ``retry_after`` (seconds) when the carrier supplied it.
    """

    transient = True

    def __init__(self, message: str = "", *, provider_code: str = "", retry_after: int | None = None) -> None:
        super().__init__(message, provider_code=provider_code)
        self.retry_after = retry_after


class CarrierTimeoutError(CarrierError):
    """A network-level failure — timeout, DNS, or connection reset.

    Transient — safe to retry with backoff.
    """

    transient = True


class CarrierServerError(CarrierError):
    """The carrier returned a 5xx server error.

    Transient — safe to retry with backoff.
    """

    transient = True

    def __init__(self, message: str = "", *, provider_code: str = "", status_code: int | None = None) -> None:
        super().__init__(message, provider_code=provider_code)
        self.status_code = status_code


class CarrierInvalidResponseError(CarrierError):
    """The response could not be parsed, or did not match the expected schema.

    Permanent for this payload — the raw response is stored unparsed so it can be
    re-parsed once the adapter is corrected.
    """

    def __init__(self, message: str = "", *, provider_code: str = "", status_code: int | None = None) -> None:
        super().__init__(message, provider_code=provider_code)
        self.status_code = status_code


class CarrierNoDataError(CarrierError):
    """The carrier has no data for the requested reference (e.g. HTTP 404).

    This is a valid tracking outcome, not a technical failure: the call worked,
    the carrier simply does not know this reference yet. The sync layer records a
    successful run with zero events and schedules the next poll.
    """


class CarrierUnsupportedReferenceError(CarrierError):
    """The requested reference type is not supported by this carrier.

    Raised when no reference, more than one reference, or a reference the
    carrier's capabilities exclude was supplied. Permanent.
    """
