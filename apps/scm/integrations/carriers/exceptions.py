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

**``str(exc)`` is for the log; ``safe_message`` is for everything else.** A provider's
own error text can carry a response body, an echoed credential, or — as Traqo's HTTP 402
does — our central account's plan, quota and billing URL. None of that may reach a
customer's screen, and the fields it used to reach it through (``last_error_message`` on
a subscription, ``error_message`` on a sync run) are rendered on team-facing pages. So
every error carries a sanitised sentence alongside its technical one, the sync layer
persists only the sanitised one, and the technical one is logged. That makes the
distinction structural: no template has to remember to hide anything, and a provider
added later cannot leak by forgetting to.
"""


class CarrierError(Exception):
    """Base class for all carrier integration errors.

    ``transient`` tells the caller whether a retry is worthwhile.
    """

    transient: bool = False

    # What may be shown to a customer and stored on team-readable rows. Deliberately
    # says nothing about *our* configuration, credentials, account or provider contract:
    # the reader is an operator who needs to know whether their container is being
    # tracked, and everything beyond that is ours to fix. Subclasses override it where
    # a different consequence deserves different words; none of them interpolate the
    # provider's own text.
    safe_message_template: str = "Tracking is temporarily unavailable for this reference."

    def __init__(self, message: str = "", *, provider_code: str = "") -> None:
        super().__init__(message)
        self.provider_code = provider_code
        # Structured, machine-readable facts a provider chose to send with the error.
        # For logs and platform-level views only — never rendered to a team, and never
        # folded into ``safe_message``.
        self.provider_detail: dict = {}

    @property
    def safe_message(self) -> str:
        """A sanitised description of this failure, safe to persist and to render.

        Never derived from the provider's own message. ``str(exc)`` remains the
        technical text and belongs in the log; see this module's docstring.
        """
        return self.safe_message_template


class CarrierNotImplementedError(CarrierError, NotImplementedError):
    """The adapter is a stub — the carrier has no implementation yet.

    Also a :class:`NotImplementedError` so that stub adapters remain detectable
    with the standard Python contract. The sync layer maps this to a SKIPPED
    sync run: nothing was attempted, so it is neither success nor failure.
    """

    safe_message_template = "Tracking is not available for this carrier yet."


class CarrierConfigurationError(CarrierError):
    """Required configuration or credentials are missing or invalid.

    Permanent — the integration must be configured before the call can work.
    """

    # "Not configured" is as much as a customer needs: which credential, which setting
    # and which account are all an administrator's business.
    safe_message_template = "Tracking is not configured for this reference."


class CarrierAuthenticationError(CarrierError):
    """The carrier rejected the credentials (401/403).

    Permanent until the credentials are fixed or refreshed.
    """

    # Deliberately indistinguishable from any other outage to the reader. "The key was
    # rejected" is a fact about our setup, and it is the one fact worth withholding from
    # somebody probing it.
    safe_message_template = "Tracking is temporarily unavailable for this reference."


class CarrierRateLimitError(CarrierError):
    """The carrier returned HTTP 429.

    Transient — honour ``retry_after`` (seconds) when the carrier supplied it.
    """

    transient = True

    safe_message_template = "The carrier is rate limiting us right now. Try again shortly."

    def __init__(self, message: str = "", *, provider_code: str = "", retry_after: int | None = None) -> None:
        super().__init__(message, provider_code=provider_code)
        self.retry_after = retry_after


class CarrierTimeoutError(CarrierError):
    """A network-level failure — timeout, DNS, or connection reset.

    Transient — safe to retry with backoff.
    """

    transient = True

    safe_message_template = "The carrier could not be reached. Try again shortly."


class CarrierServerError(CarrierError):
    """The carrier returned a 5xx server error.

    Transient — safe to retry with backoff.
    """

    transient = True

    safe_message_template = "The carrier's own system is failing right now. Try again shortly."

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

    safe_message_template = "The carrier has no tracking data for this reference yet."


class CarrierUnsupportedReferenceError(CarrierError):
    """The requested reference type is not supported by this carrier.

    Raised when no reference, more than one reference, or a reference the
    carrier's capabilities exclude was supplied. Permanent.
    """

    safe_message_template = "This reference cannot be tracked with this carrier."


# ---------------------------------------------------------------------------
# The provider's account, rather than the container
#
# Everything above is about a reference or a carrier. These two are about *our*
# agreement with a provider: the account cannot take on more work, or cannot be
# billed. They are generic rather than Traqo-specific so the tracking layer can
# classify them without importing a provider package — see ``tracking/sync.py``'s
# ``_SKIP_ERRORS`` — and so the second aggregator to have a quota inherits these
# semantics rather than inventing its own.
#
# Neither is transient. A quota resets on a billing cycle and an unpaid account
# needs a person, so retrying either on a network-failure cadence spends requests
# to be told the same thing. That noise is what these classes exist to stop.
# ---------------------------------------------------------------------------


class CarrierProviderQuotaError(CarrierError):
    """The provider's account cannot take on another reference this billing cycle.

    Not a rate limit, despite both meaning "not right now". A rate limit clears in
    seconds and is about the pace of requests; a quota is a purchased allowance that
    clears when the cycle does. Classifying this as a rate limit is what produced a
    15-minute-to-12-hour retry ladder against an account that could not accept
    anything for days.

    ``retry_after`` is accepted because providers send it, and is honoured as a lower
    bound wherever anything retries at all — it is not a promise that waiting that
    long will help.
    """

    safe_message_template = (
        "Tracking could not be started with the configured provider. Contact your system administrator."
    )

    def __init__(self, message: str = "", *, provider_code: str = "", retry_after: int | None = None) -> None:
        super().__init__(message, provider_code=provider_code)
        self.retry_after = retry_after


class CarrierProviderBillingError(CarrierError):
    """The provider has suspended the account for a billing reason.

    Permanent until somebody settles it, so it is configuration-shaped rather than an
    outage — but kept apart from :class:`CarrierConfigurationError` because nothing
    about *this installation* is misconfigured, and the person who can fix it is not
    the person looking at the container.
    """

    safe_message_template = (
        "Tracking could not be started with the configured provider. Contact your system administrator."
    )
