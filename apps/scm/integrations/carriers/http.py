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

Every request is logged to IntegrationRequestLog with the path only — never the
query string, never headers, never the body — so a credential in a query
parameter or an Authorization header cannot end up in the log table.
"""

from __future__ import annotations

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

        return cls(
            timeout_seconds=_int("request_timeout_seconds", 30),
            max_retries=_int("max_retries", 3),
            retry_backoff_seconds=backoff,
            max_retry_after_wait_seconds=_int("max_retry_after_wait_seconds", 30),
            no_data_statuses=no_data,
        )


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
    """A small GET client with carrier-typed errors and sanitised request logging."""

    def __init__(
        self,
        *,
        provider_code: str,
        config: HttpConfig | None = None,
        auth=None,
        integration: Integration | None = None,
        extra_headers: dict | None = None,
        session=None,
    ) -> None:
        self.provider_code = provider_code
        self.config = config or HttpConfig()
        self.auth = auth
        self.integration = integration
        self.extra_headers = extra_headers or {}
        # Injectable for tests; defaults to the requests module's functional API.
        self._session = session or requests

    def get(self, url: str, *, params: dict | None = None) -> dict:
        """GET ``url`` and return parsed JSON, raising a typed CarrierError on failure."""
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

                if status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        error_message = "Carrier response was not valid JSON"
                        raise CarrierInvalidResponseError(
                            error_message, provider_code=self.provider_code, status_code=status_code
                        ) from exc
                    succeeded = True
                    return payload

                if status_code in self.config.no_data_statuses:
                    # Not an error: the carrier simply does not know this reference.
                    succeeded = True
                    raise CarrierNoDataError(
                        f"{self.provider_code} has no data for this reference (HTTP {status_code}).",
                        provider_code=self.provider_code,
                    )

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
    ) -> None:
        """Write a sanitised IntegrationRequestLog entry; never secrets, never bodies."""
        if self.integration is None:
            return
        from apps.scm.integrations.services import log_integration_request

        try:
            log_integration_request(
                team=self.integration.team,
                provider_code=self.provider_code,
                method="GET",
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
