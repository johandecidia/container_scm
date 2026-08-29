"""Shared HTTP transport for carrier APIs.

One place decides how a carrier's HTTP response becomes a typed outcome, so every
carrier retries, backs off, honours Retry-After and classifies failures the same
way:

    200            parsed JSON
    404 (default)  CarrierNoDataError — a valid answer, not a failure
    401/403        CarrierAuthenticationError, after one token refresh
    429            CarrierRateLimitError carrying Retry-After, after retries
    5xx / timeout  CarrierServerError / CarrierTimeoutError, after retries
    other 4xx      CarrierInvalidResponseError

A provider whose API gives a status a meaning of its own can supply an
``error_classifier`` to name it, in the same spirit as ``no_data_statuses``: it sees
the status and the response before the rules above run, and returns either a typed
error to raise or None to fall through to them. That keeps status-to-outcome
classification in this one module instead of growing a second transport beside it.
A classifier that returns None for 429 and 5xx keeps their retry and Retry-After
behaviour, which is the point of having them here.

Every request is logged to IntegrationRequestLog with the path only — never the
query string, never headers, never the body — so a credential in a query
parameter or an Authorization header cannot end up in the log table.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import requests

from .exceptions import (
    CarrierAuthenticationError,
    CarrierInvalidResponseError,
    CarrierNoDataError,
    CarrierRateLimitError,
    CarrierServerError,
    CarrierTimeoutError,
)

if TYPE_CHECKING:
    from apps.scm.integrations.models import Integration

logger = logging.getLogger(__name__)

# Statuses worth retrying. 401 is handled separately (one token refresh).
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Ceilings applied to any carrier call made while someone is waiting for the page.
# A background poll can afford 30s × 4 attempts; a person pressing a button cannot,
# and a slow carrier must not be able to hold a web worker for minutes. These only
# ever lower a configured value — a team that has already configured something
# faster keeps it.
INTERACTIVE_TIMEOUT_SECONDS = 15
INTERACTIVE_MAX_RETRIES = 1
INTERACTIVE_MAX_RETRY_AFTER_WAIT_SECONDS = 5

_interactive = contextvars.ContextVar("carrier_http_interactive", default=False)


@contextlib.contextmanager
def interactive_carrier_requests():
    """Bound carrier HTTP behaviour for calls made inside a user's request.

    Wraps the *existing* transport rather than adding a second one: every client
    resolves its behaviour through :meth:`HttpConfig.from_config`, which reads this
    flag, so nothing about retries, backoff or error classification is duplicated.
    """
    token = _interactive.set(True)
    try:
        yield
    finally:
        _interactive.reset(token)


def in_interactive_request() -> bool:
    """True when carrier calls are currently bounded by the interactive ceilings."""
    return _interactive.get()


@dataclass(frozen=True)
class HttpConfig:
    """Transport behaviour, resolved from the integration's config."""

    timeout_seconds: int = 30
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5
    # Longest Retry-After we will wait out inside the request. A carrier asking for
    # more than this is answered by giving up now and letting the polling policy
    # reschedule, rather than by holding a worker and its sync lock idle for minutes.
    max_retry_after_wait_seconds: int = 30
    # Statuses that mean "the carrier has no data for this reference".
    no_data_statuses: frozenset[int] = field(default_factory=lambda: frozenset({404}))

    @classmethod
    def from_config(cls, config: dict) -> HttpConfig:
        def _int(key: str, default: int) -> int:
            try:
                return int(config.get(key) or default)
            except TypeError, ValueError:
                return default

        try:
            backoff = float(config.get("retry_backoff_seconds") or 0.5)
        except TypeError, ValueError:
            backoff = 0.5

        statuses = config.get("no_data_statuses")
        no_data = frozenset(int(status) for status in statuses) if statuses else frozenset({404})

        timeout = _int("request_timeout_seconds", 30)
        retries = _int("max_retries", 3)
        retry_after_wait = _int("max_retry_after_wait_seconds", 30)
        if in_interactive_request():
            timeout = min(timeout, INTERACTIVE_TIMEOUT_SECONDS)
            retries = min(retries, INTERACTIVE_MAX_RETRIES)
            retry_after_wait = min(retry_after_wait, INTERACTIVE_MAX_RETRY_AFTER_WAIT_SECONDS)

        return cls(
            timeout_seconds=timeout,
            max_retries=retries,
            retry_backoff_seconds=backoff,
            max_retry_after_wait_seconds=retry_after_wait,
            no_data_statuses=no_data,
        )


@dataclass(frozen=True)
class CarrierResponse:
    """A successful carrier response: the parsed JSON and the headers that came with it.

    Headers are exposed because some carriers paginate through them rather than
    through the body — DCSA Track & Trace advertises the next cursor in a
    ``Next-Page`` header. Lookup is case-insensitive, so a plain dict from a test
    double behaves like the ``CaseInsensitiveDict`` requests returns.
    """

    payload: dict | list
    headers: dict = field(default_factory=dict)

    def header(self, name: str) -> str:
        """Return the named header's value, stripped, or "" when it is absent."""
        if not name or not self.headers:
            return ""
        value = self.headers.get(name)
        if value is None:
            lowered = name.lower()
            for key, candidate in self.headers.items():
                if str(key).lower() == lowered:
                    value = candidate
                    break
        return str(value or "").strip()


def _retry_after_seconds(response) -> int | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return int(float(value))
    except TypeError, ValueError:
        return None


def log_path(url: str) -> str:
    """Return the path of a URL, dropping host and query string.

    The query string is deliberately discarded: some carriers accept credentials as
    query parameters, and a log table is not a place for those.
    """
    return urlsplit(url).path or "/"


class CarrierHttpClient:
    """A small GET client with carrier-typed errors and sanitised request logging.

    ``error_classifier`` is an optional ``(status_code, response) -> CarrierError |
    None`` hook for a provider that gives a status a meaning the defaults do not
    cover. It is consulted after a 200 and after ``no_data_statuses``, and before the
    401/429/5xx rules, so it can name a status those would otherwise generalise. None
    means "not mine" and the default handling applies.
    """

    def __init__(
        self,
        *,
        provider_code: str,
        config: HttpConfig | None = None,
        auth=None,
        integration: Integration | None = None,
        extra_headers: dict | None = None,
        session=None,
        error_classifier=None,
    ) -> None:
        self.provider_code = provider_code
        self.config = config or HttpConfig()
        self.auth = auth
        self.integration = integration
        self.extra_headers = extra_headers or {}
        self.error_classifier = error_classifier
        # Injectable for tests; defaults to the requests module's functional API.
        self._session = session or requests

    def get(self, url: str, *, params: dict | None = None) -> dict:
        """GET ``url`` and return parsed JSON, raising a typed CarrierError on failure."""
        return self.get_with_headers(url, params=params).payload

    def get_with_headers(self, url: str, *, params: dict | None = None) -> CarrierResponse:
        """GET ``url`` and return the parsed JSON together with the response headers.

        Same request handling, retries and error classification as :meth:`get` — the
        only difference is that the headers survive, for carriers that paginate
        through them.
        """
        return self._request("GET", url, params=params)

    def post(self, url: str, *, json_body: dict | None = None, params: dict | None = None) -> dict:
        """POST ``url`` and return parsed JSON, raising a typed CarrierError on failure.

        Exists because a provider can require a write to *start* tracking rather than a
        read to fetch it — Vizion creates a reference with POST /references before any
        update can be retrieved. It shares the whole of :meth:`get`'s behaviour: same
        retries, same backoff, same Retry-After handling, same error classification and
        the same sanitised logging, so there is one transport in the codebase rather
        than one per HTTP verb.

        Retries are the same as for GET, which is safe for the creates this is used for:
        Vizion's POST /references is idempotent per (organisation, reference) and returns
        the existing reference rather than a second one. A provider whose POST is not
        idempotent must configure ``max_retries=0`` rather than rely on this.
        """
        return self._request("POST", url, params=params, json_body=json_body).payload

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> CarrierResponse:
        """Perform one logical request, retries and typed-error classification included."""
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        endpoint = log_path(url)
        attempt = 0
        refreshed = False
        status_code: int | None = None
        error_message = ""
        # "No data" counts as a successful call: the carrier answered.
        succeeded = False

        try:
            while True:
                attempt += 1
                headers = {"Accept": "application/json", **self.extra_headers}
                if self.auth is not None:
                    headers.update(self.auth.auth_headers())

                try:
                    if method == "POST":
                        response = self._session.post(
                            url,
                            headers=headers,
                            params=params,
                            json=json_body,
                            timeout=self.config.timeout_seconds,
                        )
                    else:
                        response = self._session.get(
                            url, headers=headers, params=params, timeout=self.config.timeout_seconds
                        )
                except (requests.Timeout, requests.ConnectionError) as exc:
                    if attempt <= self.config.max_retries:
                        self._backoff(attempt)
                        continue
                    error_message = f"{type(exc).__name__} contacting {self.provider_code}"
                    raise CarrierTimeoutError(error_message, provider_code=self.provider_code) from exc

                status_code = response.status_code

                # 201 is accepted for POST only, so a GET's handling is bit-for-bit
                # unchanged: a create that answers "201 Created" is a success, and no
                # existing carrier read can reach this branch.
                if status_code == 200 or (method == "POST" and status_code == 201):
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        error_message = "Carrier response was not valid JSON"
                        raise CarrierInvalidResponseError(
                            error_message, provider_code=self.provider_code, status_code=status_code
                        ) from exc
                    succeeded = True
                    return CarrierResponse(payload=payload, headers=response.headers or {})

                if status_code in self.config.no_data_statuses:
                    # Not an error: the carrier simply does not know this reference.
                    succeeded = True
                    raise CarrierNoDataError(
                        f"{self.provider_code} has no data for this reference (HTTP {status_code}).",
                        provider_code=self.provider_code,
                    )

                if self.error_classifier is not None:
                    classified = self.error_classifier(status_code, response)
                    if classified is not None:
                        error_message = str(classified)
                        raise classified

                if status_code == 401 and not refreshed and self.auth is not None:
                    # One token refresh, then retry immediately.
                    refreshed = True
                    self.auth.invalidate_token()
                    continue

                if status_code in (401, 403):
                    error_message = f"{self.provider_code} rejected the credentials (HTTP {status_code})"
                    raise CarrierAuthenticationError(error_message, provider_code=self.provider_code)

                if status_code == 429:
                    retry_after = _retry_after_seconds(response)
                    waitable = retry_after is None or retry_after <= self.config.max_retry_after_wait_seconds
                    if waitable and attempt <= self.config.max_retries:
                        self._backoff(attempt, retry_after=retry_after)
                        continue
                    error_message = f"{self.provider_code} rate limit exceeded (429)"
                    # retry_after travels with the error so the polling policy can honour
                    # it as a lower bound on the next attempt.
                    raise CarrierRateLimitError(
                        error_message, provider_code=self.provider_code, retry_after=retry_after
                    )

                if status_code in _RETRYABLE_STATUSES:
                    if attempt <= self.config.max_retries:
                        self._backoff(attempt)
                        continue
                    error_message = f"{self.provider_code} server error (HTTP {status_code})"
                    raise CarrierServerError(error_message, provider_code=self.provider_code, status_code=status_code)

                error_message = f"{self.provider_code} returned HTTP {status_code}"
                error_message = f"{self.provider_code} returned HTTP {status_code}"
                raise CarrierInvalidResponseError(
                    error_message, provider_code=self.provider_code, status_code=status_code
                )
        finally:
            self._log(
                endpoint,
                request_id,
                status_code,
                started,
                success=succeeded,
                error_message="" if succeeded else error_message,
                method=method,
            )

    def _backoff(self, attempt: int, *, retry_after: int | None = None) -> None:
        delay = float(retry_after) if retry_after else self.config.retry_backoff_seconds * (2 ** (attempt - 1))
        time.sleep(delay)

    def _log(
        self,
        endpoint: str,
        request_id: str,
        status_code: int | None,
        started: float,
        *,
        success: bool,
        error_message: str,
        method: str = "GET",
    ) -> None:
        """Write a sanitised IntegrationRequestLog entry; never secrets, never bodies."""
        if self.integration is None:
            return
        from apps.scm.integrations.services import log_integration_request

        try:
            log_integration_request(
                team=self.integration.team,
                provider_code=self.provider_code,
                method=method,
                endpoint=endpoint[:500],
                integration=self.integration,
                status_code=status_code,
                duration_ms=int((time.monotonic() - started) * 1000),
                request_id=request_id,
                success=success,
                error_message=error_message[:500],
            )
        except Exception as exc:  # noqa: BLE001 — logging must never break a sync
            logger.warning("Failed to write IntegrationRequestLog: %s", type(exc).__name__)
