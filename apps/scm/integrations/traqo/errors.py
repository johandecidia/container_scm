"""Turning Traqo's HTTP statuses into the tracking layer's existing error semantics.

Traqo answers with ``{"success": false, "statusCode": N, "message": "...", "data": {...}}``
and uses two statuses no carrier does — 402 for an account that cannot take another
shipment, 403 for an account with developer access switched off. Both need a human, and
neither is a reason to stop believing the tracking events we already hold.

**The machine-readable reason is ``data.error``.** Confirmed against the live sandbox
(``/container/SBXU4020008`` and ``/container/SBXU4029999``), which returns the real
production payloads:

    {"success": false, "statusCode": 402,
     "message": "Shipment limit reached (20 of 20). Add more slots or upgrade ...",
     "data": {"error": "shipment_limit_reached", "limit": 20, "used": 20,
              "plan": "professional", "manageUrl": "...", "usageApi": "/api/v1/account/usage"}}

Everything in ``data`` beside ``error`` describes *our* account, and the top-level
``message`` states the plan and the numbers outright. None of it may reach a customer,
so it is kept on the error as ``provider_detail`` for the log and the platform view and
never reaches ``safe_message`` — see ``carriers/exceptions.py``.

So each status is mapped onto the carrier error hierarchy the sync layer already
classifies, choosing by *consequence* rather than by resemblance:

    400  CarrierInvalidResponseError   the request was wrong; retrying it is pointless
    401  CarrierAuthenticationError    the key is missing or rejected
    402  shipment_limit_reached  → provider-quota family: the account's allowance for
                                   this billing cycle is spent
         payment_overdue         → provider-billing family: needs a human, not a retry
    403  CarrierConfigurationError     developer mode is off; needs a human
    404  CarrierNoDataError            handled by the shared transport's no_data_statuses
    429  CarrierRateLimitError         handled by the shared transport, Retry-After honoured
    502  CarrierServerError            handled by the shared transport, retried then transient

The two that stay with the shared transport are left alone deliberately: 429 and 5xx
are exactly what its retry-then-classify logic is for, and a temporary Traqo or
upstream-carrier failure must never end up marking a container untrackable.

**A quota is not a rate limit.** ``shipment_limit_reached`` used to live in the
rate-limit family, which made the sync engine treat it as transient and retry it from
15 minutes out to a 12-hour cap. Traqo's allowance resets on the *billing cycle*, so
every one of those retries was a request spent to be told the same thing. Both 402s are
now provider-account errors: non-transient, recorded as SKIPPED, and polled on the slow
cadence. See ``apps/scm/tracking/sources.py`` and ``polling.py``.

Nothing here reads a request header, so the API key cannot reach an error message.
"""

from __future__ import annotations

from apps.scm.integrations.carriers.exceptions import (
    CarrierAuthenticationError,
    CarrierConfigurationError,
    CarrierError,
    CarrierInvalidResponseError,
    CarrierProviderBillingError,
    CarrierProviderQuotaError,
)

from . import PROVIDER_CODE

# The two 402 reasons Traqo distinguishes. Matched as exact tokens — on a machine
# field first, then in the message — never as a fuzzy substring of arbitrary prose.
REASON_SHIPMENT_LIMIT_REACHED = "shipment_limit_reached"
REASON_PAYMENT_OVERDUE = "payment_overdue"

# Body keys that may carry the machine-readable reason for a 402. ``data.error`` is
# where Traqo actually puts it, and is checked first; the others are kept as a fallback
# for a body shaped differently than the one recorded above.
_REASON_KEYS = ("reason", "code", "error_code", "error")

# Fields inside ``data`` worth keeping for the log and the superuser view. Chosen
# explicitly rather than by copying the whole object, so a field Traqo adds later cannot
# arrive somewhere it has not been considered.
_DETAIL_KEYS = ("error", "plan", "limit", "used", "remaining", "maxedOut", "overdueDays", "graceDays")


class TraqoShipmentLimitReachedError(CarrierProviderQuotaError):
    """Traqo will not accept another shipment on this account (HTTP 402).

    The allowance counts references *added* during the billing cycle, so it is spent
    rather than busy: it frees up when the cycle resets, not in a few minutes. The
    containers already tracked are unaffected and keep updating — this is only ever an
    answer about taking on something new.
    """


class TraqoPaymentOverdueError(CarrierProviderBillingError):
    """Traqo reports the account's payment as overdue (HTTP 402).

    The grace period has ended and API tracking is paused. Permanent until somebody
    settles it, so retrying cannot help. Existing shipments are explicitly unaffected
    at Traqo's end.
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


def _data(body: dict) -> dict:
    """Return the body's ``data`` object, or {} when there is not one."""
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _payment_reason(body: dict) -> str:
    """Return which 402 this is, or "" when the response does not say.

    ``data.error`` first, because that is where Traqo documents the identifier and
    where the recorded sandbox payloads actually carry it. The top-level keys and the
    prose scan that follow are a fallback for a differently shaped body — worth keeping,
    but they are not the contract: matching "shipment limit reached" inside a sentence
    happened to work while nothing read ``data.error``, and a reworded message would
    have silently turned both 402s into the unknown case below.

    Anything unrecognised stays "" rather than being guessed into one of the two.
    """
    data = _data(body)
    candidates = [data.get(key) for key in _REASON_KEYS]
    candidates += [body.get(key) for key in _REASON_KEYS]
    for candidate in candidates:
        value = str(candidate or "").strip().lower()
        if value in (REASON_SHIPMENT_LIMIT_REACHED, REASON_PAYMENT_OVERDUE):
            return value

    text = str(body.get("message") or "").strip().lower()
    for reason in (REASON_SHIPMENT_LIMIT_REACHED, REASON_PAYMENT_OVERDUE):
        if reason in text or reason.replace("_", " ") in text:
            return reason
    return ""


def _provider_detail(body: dict) -> dict:
    """The account facts Traqo sent with a 402, for the log and the platform view.

    Never rendered to a team and never folded into ``safe_message``: every value here
    describes our own plan, allowance or billing state. Kept because a superuser asking
    "why did tracking stop" is answered by exactly these numbers.
    """
    data = _data(body)
    return {key: data[key] for key in _DETAIL_KEYS if key in data}


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
    """Split a 402 into a quota problem and a billing problem where Traqo says which.

    The error carries two things and keeps them apart: ``str(exc)`` is Traqo's own
    message, which states the plan and the numbers and is therefore log-only, and
    ``provider_detail`` is the same facts structured, for the platform view. What a
    team sees is neither — it is ``safe_message``, which both classes fix.
    """
    body = _body(response)
    message = _message(body, 402)
    reason = _payment_reason(body)
    detail = _provider_detail(body)

    if reason == REASON_SHIPMENT_LIMIT_REACHED:
        error: CarrierError = TraqoShipmentLimitReachedError(
            message, provider_code=PROVIDER_CODE, retry_after=_retry_after(response)
        )
    elif reason == REASON_PAYMENT_OVERDUE:
        error = TraqoPaymentOverdueError(message, provider_code=PROVIDER_CODE)
    else:
        # Traqo said 402 without saying which. Treated as the billing case, because that
        # is the one a retry cannot fix: assuming a quota would poll a suspended account
        # indefinitely, while assuming billing surfaces it to a human who can look.
        error = TraqoPaymentOverdueError(
            f"{message} (Traqo did not state whether this is a shipment limit or a payment problem.)",
            provider_code=PROVIDER_CODE,
        )

    error.provider_detail = detail
    return error
