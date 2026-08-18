"""Turning Traqo's HTTP statuses into the tracking layer's existing error semantics.

Traqo answers with ``{"success": false, "statusCode": N, "message": "..."}`` and uses
two statuses no carrier does — 402 for an account that cannot take another shipment,
403 for an account with developer access switched off. Both need a human, and neither
is a reason to stop believing the tracking events we already hold.

So each status is mapped onto the carrier error hierarchy the sync layer already
classifies, choosing by *consequence* rather than by resemblance:

    400  CarrierInvalidResponseError   the request was wrong; retrying it is pointless
    401  CarrierAuthenticationError    the key is missing or rejected
    402  shipment_limit_reached  → rate-limit family: a quota, transient by nature
         payment_overdue         → configuration family: needs a human, not a retry
    403  CarrierConfigurationError     developer mode is off; needs a human
    404  CarrierNoDataError            handled by the shared transport's no_data_statuses
    429  CarrierRateLimitError         handled by the shared transport, Retry-After honoured
    502  CarrierServerError            handled by the shared transport, retried then transient

The two that stay with the shared transport are left alone deliberately: 429 and 5xx
are exactly what its retry-then-classify logic is for, and a temporary Traqo or
upstream-carrier failure must never end up marking a container untrackable.

Nothing here reads a request header, so the API key cannot reach an error message.
"""

from __future__ import annotations

from apps.scm.integrations.carriers.exceptions import (
    CarrierAuthenticationError,
    CarrierConfigurationError,
    CarrierError,
    CarrierInvalidResponseError,
    CarrierRateLimitError,
)

from . import PROVIDER_CODE

# The two 402 reasons Traqo distinguishes. Matched as exact tokens — on a machine
# field first, then in the message — never as a fuzzy substring of arbitrary prose.
REASON_SHIPMENT_LIMIT_REACHED = "shipment_limit_reached"
REASON_PAYMENT_OVERDUE = "payment_overdue"

# Body keys that may carry the machine-readable reason for a 402.
_REASON_KEYS = ("reason", "code", "error_code", "error")


class TraqoShipmentLimitReachedError(CarrierRateLimitError):
    """Traqo will not accept another shipment on this account (HTTP 402).

    A quota, so it lives in the rate-limit family: transient, honours Retry-After,
    and leaves the subscription tracking rather than marking it unconfigured. Slots
    free up as shipments close, and the containers we already track are unaffected.
    """


class TraqoPaymentOverdueError(CarrierConfigurationError):
    """Traqo reports the account's payment as overdue (HTTP 402).

    Permanent until somebody settles it, so retrying cannot help — which is what the
    configuration family means to the sync layer.
    """


class TraqoDeveloperModeDisabledError(CarrierConfigurationError):
    """Developer/API access is switched off for this Traqo account (HTTP 403).

    Not a rejected credential: the key may be perfectly valid. It is an account
    setting, and only a human can change it.
    """


def _body(response) -> dict:
    """Return the response body as a dict, or {} when it is not JSON.

    A non-JSON error body is not worth failing over — the status alone already
    carries the classification.
    """
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 — any unparseable body is simply absent
        return {}
    return payload if isinstance(payload, dict) else {}


def _message(body: dict, status_code: int) -> str:
    """Return Traqo's own explanation, or a plain statement of the status.

    Traqo's messages describe the problem and never echo the Authorization header,
    so they are safe to log and to attach to the error.
    """
    message = str(body.get("message") or "").strip()
    return message or f"Traqo returned HTTP {status_code}."


def _payment_reason(body: dict) -> str:
    """Return which 402 this is, or "" when the response does not say.

    Checked on the machine-readable keys first; only then on the message, and only
    for the two exact tokens Traqo documents. Anything else stays unknown rather than
    being guessed into one of them.
    """
    for key in _REASON_KEYS:
        value = str(body.get(key) or "").strip().lower()
        if value in (REASON_SHIPMENT_LIMIT_REACHED, REASON_PAYMENT_OVERDUE):
            return value

    text = str(body.get("message") or "").strip().lower()
    for reason in (REASON_SHIPMENT_LIMIT_REACHED, REASON_PAYMENT_OVERDUE):
        if reason in text or reason.replace("_", " ") in text:
            return reason
    return ""


def _retry_after(response) -> int | None:
    value = (getattr(response, "headers", None) or {}).get("Retry-After")
    if not value:
        return None
    try:
        return int(float(value))
    except TypeError, ValueError:
        return None


def classify_traqo_error(status_code: int, response) -> CarrierError | None:
    """Return the typed error for a Traqo status, or None to use the shared handling.

    Passed to :class:`~apps.scm.integrations.carriers.http.CarrierHttpClient` as its
    ``error_classifier``. Returning None for 429 and 5xx is what keeps their retry,
    backoff and Retry-After behaviour in the one place that owns it.
    """
    if status_code == 400:
        body = _body(response)
        return CarrierInvalidResponseError(
            _message(body, status_code), provider_code=PROVIDER_CODE, status_code=status_code
        )

    if status_code == 401:
        body = _body(response)
        # Raised without a token refresh: a Traqo API key is static, so retrying the
        # same key can only fail the same way.
        return CarrierAuthenticationError(_message(body, status_code), provider_code=PROVIDER_CODE)

    if status_code == 402:
        return _payment_required_error(response)

    if status_code == 403:
        body = _body(response)
        return TraqoDeveloperModeDisabledError(_message(body, status_code), provider_code=PROVIDER_CODE)

    return None


def _payment_required_error(response) -> CarrierError:
    """Split a 402 into a quota problem and a billing problem where Traqo says which."""
    body = _body(response)
    message = _message(body, 402)
    reason = _payment_reason(body)

    if reason == REASON_SHIPMENT_LIMIT_REACHED:
        return TraqoShipmentLimitReachedError(message, provider_code=PROVIDER_CODE, retry_after=_retry_after(response))
    if reason == REASON_PAYMENT_OVERDUE:
        return TraqoPaymentOverdueError(message, provider_code=PROVIDER_CODE)

    # Traqo said 402 without saying which. Treated as the billing case, because that
    # is the one a retry cannot fix: assuming a quota would poll a suspended account
    # indefinitely, while assuming billing surfaces it to a human who can look.
    return TraqoPaymentOverdueError(
        f"{message} (Traqo did not state whether this is a shipment limit or a payment problem.)",
        provider_code=PROVIDER_CODE,
    )
