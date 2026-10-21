"""Business Central API client.

Read-only client for the Microsoft Business Central OData v2.0 API. Business
Central is the system of record; this client only ever performs GET requests.

Two modes:
  - dummy (``use_dummy=True``): loads data from local JSON fixtures. Used in
    tests and local development; performs no network access.
  - live (``integration=<Integration>``): authenticates with OAuth2 client
    credentials and calls the live OData API with pagination, controlled retry
    and sanitised request logging.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import requests

from apps.scm.integrations.business_systems.base import BaseBusinessSystemClient, BusinessSystemCapability

from .auth import BusinessCentralAuth
from .exceptions import (
    BusinessCentralAuthenticationError,
    BusinessCentralCompanyNotFoundError,
    BusinessCentralConfigurationError,
    BusinessCentralConnectionError,
    BusinessCentralEnvironmentNotFoundError,
    BusinessCentralNotFoundError,
    BusinessCentralPermissionError,
    BusinessCentralRateLimitError,
    BusinessCentralResponseError,
)

if TYPE_CHECKING:
    from apps.scm.integrations.models import Integration

logger = logging.getLogger(__name__)

_DEFAULT_FIXTURES_PATH = Path(__file__).parent / "tests" / "fixtures"
_API_HOST = "https://api.businesscentral.dynamics.com"
# Version of the Business Central *service endpoint* (the first path segment). It
# is fixed by Microsoft and independent of the API version further down the path.
_SERVICE_VERSION = "v2.0"

# HTTP statuses worth retrying (transient). 401 is handled separately (one refresh).
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
# Cap pagination to protect against a malformed nextLink loop.
_MAX_PAGES = 1000
# Business Central online rejects odata.maxpagesize above this.
_MAX_PAGE_SIZE = 20000
# Business Central error codes are identifiers; anything else is not echoed.
_ERROR_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")

# Fields requested from Business Central (keeps payloads small and explicit).
# Names follow the standard API v2.0 purchaseOrder / purchaseOrderLine resources —
# a property that does not exist makes Business Central reject the whole request.
_PO_SELECT = [
    "id",
    "number",
    "vendorNumber",
    "vendorName",
    "status",
    "fullyReceived",
    "orderDate",
    "requestedReceiptDate",
    "currencyCode",
    "lastModifiedDateTime",
]
_PO_LINE_SELECT = [
    "id",
    "documentId",
    "sequence",
    "lineType",
    "itemId",
    "lineObjectNumber",
    "description",
    "unitOfMeasureCode",
    "quantity",
    "receivedQuantity",
    "invoicedQuantity",
    "directUnitCost",
    "expectedReceiptDate",
]


def _require_guid(value: str, name: str) -> str:
    """Return ``value`` as a canonical GUID string or raise a configuration error.

    Business Central addresses companies and documents by GUID inside the URL
    path; validating it keeps arbitrary text out of the path.
    """
    try:
        return str(uuid.UUID(str(value)))
    except ValueError, TypeError, AttributeError:
        raise BusinessCentralConfigurationError(f"Business Central {name} must be a GUID") from None


@dataclass(frozen=True)
class BusinessCentralConfig:
    """Validated live connection settings, resolved from Integration.config."""

    tenant_id: str
    environment: str
    company_id: str
    api_version: str = "v2.0"
    page_size: int = 100
    request_timeout_seconds: int = 30
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5

    @property
    def api_root(self) -> str:
        """``https://api.businesscentral.dynamics.com/v2.0/{tenant}/{environment}/api/{api_version}``."""
        tenant = quote(self.tenant_id, safe="")
        environment = quote(self.environment, safe="")
        return f"{_API_HOST}/{_SERVICE_VERSION}/{tenant}/{environment}/api/{quote(self.api_version, safe='')}"

    @property
    def company_path(self) -> str:
        return f"companies({self.company_id})"


def _resolve_config(config: dict) -> BusinessCentralConfig:
    """Build and validate a BusinessCentralConfig from an Integration.config dict."""
    tenant_id = (config.get("tenant_id") or "").strip()
    environment = (config.get("environment") or "").strip()
    company_id = (config.get("company_id") or "").strip()

    missing = [
        name
        for name, value in (("tenant_id", tenant_id), ("environment", environment), ("company_id", company_id))
        if not value
    ]
    if missing:
        raise BusinessCentralConfigurationError(
            f"Business Central config missing required fields: {', '.join(missing)}"
        )

    page_size = int(config.get("page_size") or 100)
    if not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise BusinessCentralConfigurationError(f"Business Central page_size must be between 1 and {_MAX_PAGE_SIZE}")

    return BusinessCentralConfig(
        tenant_id=tenant_id,
        environment=environment,
        company_id=_require_guid(company_id, "company_id"),
        api_version=(config.get("api_version") or "v2.0").strip(),
        page_size=page_size,
        request_timeout_seconds=int(config.get("request_timeout_seconds") or 30),
        max_retries=int(config.get("max_retries") or 3),
        retry_backoff_seconds=float(config.get("retry_backoff_seconds") or 0.5),
    )


def _error_code(response) -> str:
    """Business Central's ``error.code`` from an error body, or "" (never the message)."""
    try:
        code = (response.json().get("error") or {}).get("code") or ""
    except ValueError, AttributeError, TypeError:
        return ""
    return code if isinstance(code, str) and _ERROR_CODE_PATTERN.match(code) else ""


def _with_code(message: str, code: str) -> str:
    return f"{message} [code={code}]" if code else message


class BusinessCentralClient(BaseBusinessSystemClient):
    """Microsoft Business Central read-only OData v2.0 client.

    Pass ``use_dummy=True`` to load data from local JSON fixtures, or an
    ``integration`` for live access. Business Central is read-only from SCM —
    this client never writes back.
    """

    system_code = "business_central"
    capabilities = BusinessSystemCapability(
        supports_sales_orders=True,
        supports_purchase_orders=True,
        supports_customers=True,
        supports_vendors=True,
        supports_items=True,
        # No BC-specific webhook processor exists yet — polling only.
        supports_webhooks=False,
        supports_polling=True,
    )

    def __init__(
        self,
        integration: Integration | None = None,
        *,
        use_dummy: bool = False,
        fixtures_path: str | None = None,
    ) -> None:
        self.integration = integration
        self.use_dummy = use_dummy
        self.fixtures_path = Path(fixtures_path) if fixtures_path else _DEFAULT_FIXTURES_PATH

        self._config: BusinessCentralConfig | None = None
        self._auth: BusinessCentralAuth | None = None
        if not use_dummy:
            if integration is None:
                raise BusinessCentralConfigurationError(
                    "BusinessCentralClient requires an integration in live mode (or use_dummy=True)"
                )
            self._config = _resolve_config(integration.config or {})
            self._auth = self._build_auth(integration, self._config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def test_connection(self) -> dict:
        """Verify, in order: authentication, environment, company, purchase-order read.

        Each step raises its own typed BusinessCentralError so the caller can tell
        an authentication failure from a missing permission, an unknown
        environment, an unknown company or a network problem. Authentication on
        its own is never reported as a working connection.
        """
        if self.use_dummy:
            return {"success": True, "message": "Dummy Business Central client", "checks": []}

        cfg = self._require_config()
        checks: list[str] = []

        self._auth.get_access_token()  # type: ignore[union-attr]
        checks.append("OAuth2 token acquired")

        try:
            companies = self._fetch_all_pages("companies", params={"$select": "id,name"})
        except BusinessCentralNotFoundError as exc:
            raise BusinessCentralEnvironmentNotFoundError(
                _with_code(f"Environment '{cfg.environment}' was not found for the configured tenant", exc.error_code)
            ) from exc
        checks.append(f"Environment '{cfg.environment}' reachable ({len(companies)} company/companies visible)")

        company = next((c for c in companies if str(c.get("id", "")).lower() == cfg.company_id), None)
        if company is None:
            raise BusinessCentralCompanyNotFoundError(
                f"Company {cfg.company_id} is not visible in environment '{cfg.environment}' "
                f"({len(companies)} company/companies visible to this app)"
            )
        company_name = str(company.get("name") or "")
        checks.append(f"Company accessible: {company_name or cfg.company_id}")

        self._get(f"{cfg.company_path}/purchaseOrders", params={"$top": 1, "$select": "id"})
        checks.append("Purchase orders readable")

        return {
            "success": True,
            "message": "Connected to Business Central",
            "environment": cfg.environment,
            "company_name": company_name,
            "checks": checks,
        }

    def source_identity(self) -> dict[str, str]:
        """Which Business Central data source this client reads from.

        Recorded on every sync run so data and watermarks from one environment or
        company are never mixed with another's.
        """
        if self.use_dummy:
            return {"mode": "dummy"}
        cfg = self._require_config()
        return {
            "mode": "live",
            "tenant_id": cfg.tenant_id.lower(),
            "environment": cfg.environment.lower(),
            "company_id": cfg.company_id,
        }

    def fetch_purchase_orders(
        self,
        *,
        modified_since: str | None = None,
        top: int | None = None,
        **kwargs,
    ) -> list[dict]:
        """Return purchase orders.

        In dummy mode reads ``business_central_purchase_orders.json``. In live
        mode pages through the OData endpoint, optionally filtering by
        ``lastModifiedDateTime`` (``modified_since``, ISO-8601 UTC). ``top``
        bounds the number of records Business Central returns (``$top``), not
        just how many are kept.
        """
        if self.use_dummy:
            records = self._load_fixture("business_central_purchase_orders.json")
            return records[:top] if top else records

        cfg = self._require_config()
        params: dict[str, Any] = {"$select": ",".join(_PO_SELECT)}
        if modified_since:
            params["$filter"] = f"lastModifiedDateTime gt {modified_since}"
        return self._fetch_all_pages(f"{cfg.company_path}/purchaseOrders", params=params, limit=top)

    def fetch_purchase_order_lines(self, purchase_order_id: str, *, top: int | None = None) -> list[dict]:
        """Return lines for a purchase order.

        In dummy mode ``purchase_order_id`` is the PO number (fixture filename).
        In live mode it must be the Business Central GUID.
        """
        if self.use_dummy:
            records = self._load_fixture(f"business_central_purchase_order_lines_{purchase_order_id}.json")
            return records[:top] if top else records

        cfg = self._require_config()
        po_id = _require_guid(purchase_order_id, "purchase order id")
        params = {"$select": ",".join(_PO_LINE_SELECT)}
        endpoint = f"{cfg.company_path}/purchaseOrders({po_id})/purchaseOrderLines"
        return self._fetch_all_pages(endpoint, params=params, limit=top)

    # ------------------------------------------------------------------
    # Internal helpers — configuration & auth
    # ------------------------------------------------------------------

    def _require_config(self) -> BusinessCentralConfig:
        if self._config is None:
            raise BusinessCentralConfigurationError("Business Central client is not configured for live access")
        return self._config

    @staticmethod
    def _build_auth(integration: Integration, cfg: BusinessCentralConfig) -> BusinessCentralAuth:
        from apps.scm.integrations.credentials import get_integration_credentials

        creds = get_integration_credentials(integration)
        return BusinessCentralAuth(
            tenant_id=cfg.tenant_id,
            client_id=creds.get("client_id", ""),
            client_secret=creds.get("client_secret", ""),
            timeout_seconds=cfg.request_timeout_seconds,
        )

    # ------------------------------------------------------------------
    # Internal helpers — HTTP
    # ------------------------------------------------------------------

    def _build_url(self, endpoint: str) -> str:
        cfg = self._require_config()
        return f"{cfg.api_root}/{endpoint.lstrip('/')}"

    def _get(self, endpoint: str, *, params: dict | None = None) -> dict:
        """Perform a single GET against a relative endpoint and return parsed JSON."""
        return self._request(self._build_url(endpoint), params=params, log_endpoint=endpoint)

    def _fetch_all_pages(self, endpoint: str, *, params: dict | None = None, limit: int | None = None) -> list[dict]:
        """Fetch an OData collection, following @odata.nextLink.

        Page size is requested with ``Prefer: odata.maxpagesize`` (re-sent on every
        page). ``$top`` is *not* a page size in OData — it caps the whole result
        and suppresses nextLink — so it is only sent when ``limit`` is given.
        """
        cfg = self._require_config()
        query = dict(params or {})
        if limit:
            query["$top"] = limit
        headers = {"Prefer": f"odata.maxpagesize={cfg.page_size}"}

        results: list[dict] = []
        payload = self._request(self._build_url(endpoint), params=query, log_endpoint=endpoint, headers=headers)
        pages = 0
        while True:
            pages += 1
            page = payload.get("value", [])
            if not isinstance(page, list):
                raise BusinessCentralResponseError(f"Business Central returned a non-list 'value' for {endpoint}")
            results.extend(page)
            next_link = payload.get("@odata.nextLink")
            # Business Central can emit a nextLink after the last full page whose
            # follow-up is empty; an empty page ends the collection.
            if not next_link or not page or (limit and len(results) >= limit):
                break
            if pages >= _MAX_PAGES:
                raise BusinessCentralResponseError(f"Pagination exceeded {_MAX_PAGES} pages for {endpoint}")
            # The bearer token is only ever sent to the Business Central API host.
            if not str(next_link).startswith(f"{_API_HOST}/"):
                raise BusinessCentralResponseError("Business Central returned a nextLink outside the API host")
            # nextLink is an absolute URL that already carries the query state.
            payload = self._request(next_link, params=None, log_endpoint=endpoint, headers=headers)
        return results[:limit] if limit else results

    def _request(self, url: str, *, params: dict | None, log_endpoint: str, headers: dict | None = None) -> dict:
        """GET ``url`` with auth, retry, one 401 refresh, and request logging.

        Returns parsed JSON. Raises a typed BusinessCentralError on failure.
        """
        cfg = self._require_config()
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        attempt = 0
        refreshed = False
        status_code: int | None = None
        error_message = ""

        try:
            while True:
                attempt += 1
                token = self._auth.get_access_token()  # type: ignore[union-attr]
                request_headers = {**(headers or {}), "Authorization": f"Bearer {token}", "Accept": "application/json"}
                try:
                    response = requests.get(
                        url, headers=request_headers, params=params, timeout=cfg.request_timeout_seconds
                    )
                except (requests.Timeout, requests.ConnectionError) as exc:
                    if attempt <= cfg.max_retries:
                        self._backoff(cfg, attempt)
                        continue
                    error_message = f"{type(exc).__name__} contacting Business Central"
                    raise BusinessCentralConnectionError(error_message) from exc

                status_code = response.status_code

                if status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        error_message = "Invalid JSON in Business Central response"
                        raise BusinessCentralResponseError(error_message, status_code=status_code) from exc

                if status_code == 401 and not refreshed:
                    # One token refresh, then retry immediately (does not count as a backoff retry).
                    refreshed = True
                    self._auth.invalidate_token()  # type: ignore[union-attr]
                    continue

                if status_code == 401:
                    # Entra issued the token, but Business Central refused it — usually
                    # the app is not registered (or not enabled) on the BC side.
                    error_message = _with_code(
                        "Business Central rejected the access token (401). Check that the Entra app is "
                        "registered in Business Central (Microsoft Entra Applications) with permission sets",
                        _error_code(response),
                    )
                    raise BusinessCentralAuthenticationError(error_message)

                if status_code == 429:
                    if attempt <= cfg.max_retries:
                        self._backoff(cfg, attempt, retry_after=response.headers.get("Retry-After"))
                        continue
                    error_message = "Business Central rate limit exceeded (429)"
                    raise BusinessCentralRateLimitError(error_message)

                if status_code in _RETRYABLE_STATUSES:
                    if attempt <= cfg.max_retries:
                        self._backoff(cfg, attempt)
                        continue
                    error_message = f"Business Central server error (HTTP {status_code})"
                    raise BusinessCentralResponseError(error_message, status_code=status_code)

                # Permanent client/other errors (400, 403, 404, ...).
                code = _error_code(response)
                if status_code == 403:
                    error_message = _with_code(
                        "Business Central denied access (403). The app needs read permission sets in Business Central",
                        code,
                    )
                    raise BusinessCentralPermissionError(error_message, status_code=status_code, error_code=code)
                if status_code == 404:
                    error_message = _with_code("Business Central returned HTTP 404 (not found)", code)
                    raise BusinessCentralNotFoundError(error_message, status_code=status_code, error_code=code)
                error_message = _with_code(f"Business Central returned HTTP {status_code}", code)
                raise BusinessCentralResponseError(error_message, status_code=status_code, error_code=code)
        finally:
            self._log_request(
                log_endpoint=log_endpoint,
                request_id=request_id,
                status_code=status_code,
                duration_ms=int((time.monotonic() - started) * 1000),
                success=not error_message and status_code == 200,
                error_message=error_message,
            )

    def _backoff(self, cfg: BusinessCentralConfig, attempt: int, *, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except TypeError, ValueError:
                delay = cfg.retry_backoff_seconds * (2 ** (attempt - 1))
        else:
            delay = cfg.retry_backoff_seconds * (2 ** (attempt - 1))
        time.sleep(delay)

    def _log_request(
        self,
        *,
        log_endpoint: str,
        request_id: str,
        status_code: int | None,
        duration_ms: int,
        success: bool,
        error_message: str,
    ) -> None:
        """Record a sanitised IntegrationRequestLog entry (never logs secrets/tokens)."""
        if self.integration is None:
            return
        from apps.scm.integrations.services import log_integration_request

        try:
            log_integration_request(
                team=self.integration.team,
                provider_code=self.system_code,
                method="GET",
                endpoint=log_endpoint[:500],
                integration=self.integration,
                status_code=status_code,
                duration_ms=duration_ms,
                request_id=request_id,
                success=success,
                error_message=error_message[:500],
            )
        except Exception as exc:  # noqa: BLE001 — logging must never break the sync
            logger.warning("Failed to write IntegrationRequestLog: %s", type(exc).__name__)

    def _load_fixture(self, filename: str) -> list[dict]:
        path = self.fixtures_path / filename
        with open(path) as f:
            data = json.load(f)
        return data.get("value", [])
