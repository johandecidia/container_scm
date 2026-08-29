"""Turning Vizion's HTTP statuses into the tracking layer's existing error semantics.

Vizion documents a small status set, and two of them are inverted relative to every
other provider in this codebase:

    401  "Provided API key lacks required permissions."
    403  "A valid API key was not provided."

Both are credential problems, so both become :class:`CarrierAuthenticationError` — but
401 is classified *here* rather than left to the shared transport, because that
transport treats a 401 as "the token may be stale, refresh once and retry". A Vizion
API key is static, so the retry can only fail the same way and would double every
rejected call.

The rest are mapped by consequence onto the hierarchy the sync layer already
classifies:

    400  CarrierInvalidResponseError   the request was malformed; retrying is pointless
    401  CarrierAuthenticationError    key lacks permission — no refresh attempted
    403  CarrierAuthenticationError    no valid key was provided
    404  CarrierNoDataError            handled by the shared transport's no_data_statuses
    422  CarrierUnsupportedReferenceError  well-formed, semantically rejected
    429  CarrierRateLimitError         left to the shared transport, Retry-After honoured
    5xx  CarrierServerError            left to the shared transport, retried then transient

429 and 5xx stay with the shared transport deliberately: its retry-then-classify logic
is exactly what keeps a temporary Vizion outage from marking a container untrackable.

Nothing here reads a request header, so the API key cannot reach an error message.
"""

from __future__ import annotations

from apps.scm.integrations.carriers.exceptions import (
    CarrierAuthenticationError,
    CarrierError,
    CarrierInvalidResponseError,
    CarrierUnsupportedReferenceError,
)

from . import PROVIDER_CODE


def _body(response) -> dict:
    """Return the response body as a dict, or {} when it is not JSON.

    A non-JSON error body is not worth failing over — the status alone already carries
    the classification.
    """
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 — any unparseable body is simply absent
        return {}
    return payload if isinstance(payload, dict) else {}


def _message(body: dict, status_code: int) -> str:
    """Return Vizion's own explanation, or a plain statement of the status.

    Vizion's error bodies describe the problem and never echo the X-API-Key header, so
    they are safe to log and to attach to the error.
    """
    for key in ("message", "error", "detail"):
        value = str(body.get(key) or "").strip()
        if value:
            return value
    return f"Vizion returned HTTP {status_code}."


def classify_vizion_error(status_code: int, response) -> CarrierError | None:
    """Return the typed error for a Vizion status, or None to use the shared handling.

    Passed to :class:`~apps.scm.integrations.carriers.http.CarrierHttpClient` as its
    ``error_classifier``. Returning None for 429 and 5xx is what keeps their retry,
    backoff and Retry-After behaviour in the one place that owns it.
    """
    if status_code == 400:
        return CarrierInvalidResponseError(
            _message(_body(response), status_code), provider_code=PROVIDER_CODE, status_code=status_code
        )

    if status_code in (401, 403):
        # Classified here rather than left to the shared transport, which would spend a
        # token refresh on a 401. A Vizion key is static; there is nothing to refresh.
        return CarrierAuthenticationError(_message(_body(response), status_code), provider_code=PROVIDER_CODE)

    if status_code == 422:
        # Well-formed but semantically rejected — an unusable container number, a BL and
        # a booking number together. Permanent for this request, and specifically not a
        # server fault, so it must not be retried.
        return CarrierUnsupportedReferenceError(_message(_body(response), status_code), provider_code=PROVIDER_CODE)

    return None
