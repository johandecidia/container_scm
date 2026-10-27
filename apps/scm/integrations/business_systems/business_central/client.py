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
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict
from urllib.parse import quote, urlsplit

import requests

from apps.scm.integrations.business_systems.base import BaseBusinessSystemClient, BusinessSystemCapability

from .auth import BusinessCentralAuth
from .diagnostics import failure_diagnostic, format_diagnostic, parse_permission_error
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

# Business Central APIs this client may call, by name. ``standard`` is the
# standard API at the configured ``api_version`` (``/api/v2.0``); every other API
# is a fixed path under ``/api/``. Nothing outside this allowlist can be reached.
API_STANDARD = "standard"
API_ANALYTICS = "analytics"
_API_PATHS = {
    API_ANALYTICS: "microsoft/analytics/v1.0",
}

# HTTP statuses worth retrying (transient). 401 is handled separately (one refresh).
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
# Cap pagination to protect against a malformed nextLink loop.
_MAX_PAGES = 1000
# Business Central online rejects odata.maxpagesize above this.
_MAX_PAGE_SIZE = 20000
# Item number prefixes become part of an OData filter; only identifier characters
# are allowed so a configured value can never change the filter's meaning.
_ITEM_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,20}$")
# Pages of the dummy item ledger fixture (small, so tests exercise paging).
_DUMMY_PAGE_SIZE = 3
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
# Variant and location codes are not line properties; the line only carries their
# GUIDs. Expanding the two navigations reads the codes in the same request (verified
# against the sandbox: a line without one returns ``null``). Each is its own API page
# (30052 / 30076) with its own permission, so a 403 here is not a line failure.
_PO_LINE_EXPAND = "itemVariant($select=code),location($select=code,displayName)"


# Standard API v2.0 purchaseReceipt / purchaseReceiptLine (verified, stage 2). Neither
# carries a location code, variant or serial number; those come from the item
# ledger entries the receipt posted (documentType "Purchase Receipt", documentNo).
_RECEIPT_SELECT = ["id", "number", "postingDate", "vendorNumber", "vendorName", "orderNumber", "lastModifiedDateTime"]
_RECEIPT_LINE_SELECT = [
    "id",
    "sequence",
    "lineType",
    "lineObjectNumber",
    "description",
    "unitOfMeasureCode",
    "quantity",
]


def require_item_prefix(value: str) -> str:
    """Return ``value`` if it is safe to place inside an OData ``startswith`` filter."""
    if not isinstance(value, str) or not _ITEM_PREFIX_PATTERN.match(value):
        raise BusinessCentralConfigurationError(
            "Business Central item number prefixes may only contain letters, digits, '_', '.' and '-'"
        )
    return value


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
        return self.api_root_for(API_STANDARD)

    def api_path(self, api: str) -> str:
        """The path after ``/api/`` for an allowlisted API name."""
        if api == API_STANDARD:
            return quote(self.api_version, safe="")
        try:
            return _API_PATHS[api]
        except KeyError:
            raise BusinessCentralConfigurationError(f"Business Central API '{api}' is not allowed") from None

    def api_root_for(self, api: str) -> str:
        """Root URL of an allowlisted API, e.g. ``…/{environment}/api/microsoft/analytics/v1.0``."""
        tenant = quote(self.tenant_id, safe="")
        environment = quote(self.environment, safe="")
        return f"{_API_HOST}/{_SERVICE_VERSION}/{tenant}/{environment}/api/{self.api_path(api)}"

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


class _ErrorContext(TypedDict):
    """Keyword arguments shared by the BusinessCentralResponseError family."""

    status_code: int | None
    error_code: str
    endpoint: str


def _error_field(response, field: str) -> str:
    """A string field of the ``error`` object in a Business Central error body, or ""."""
    try:
        value = (response.json().get("error") or {}).get(field) or ""
    except ValueError, AttributeError, TypeError:
        return ""
    return value if isinstance(value, str) else ""


def _error_code(response) -> str:
    """Business Central's ``error.code`` from an error body, or "" (never the message)."""
    code = _error_field(response, "code")
    return code if _ERROR_CODE_PATTERN.match(code) else ""


def _with_code(message: str, code: str) -> str:
    return f"{message} [code={code}]" if code else message


def _next_link_in_api(next_link: str, api_path: str) -> bool:
    """True when a nextLink's path stays inside ``/{service}/{tenant}/{environment}/api/{api_path}/``.

    Compared case-insensitively: Business Central echoes tenant and environment
    back in its own casing.
    """
    parts = urlsplit(next_link)
    segments = parts.path.lower().split("/")
    # ['', 'v2.0', tenant, environment, 'api', *api_path, resource, ...]
    expected = api_path.lower().split("/")
    return (
        parts.scheme == "https"
        and len(segments) > 5 + len(expected)
        and segments[1] == _SERVICE_VERSION
        and segments[4] == "api"
        and segments[5 : 5 + len(expected)] == expected
        and ".." not in segments
    )


class BusinessCentralClient(BaseBusinessSystemClient):
    """Microsoft Business Central read-only OData v2.0 client.

    Pass ``use_dummy=True`` to load data from local JSON fixtures, or an
    ``integration`` for live access. Business Central is read-only from SCM —
    this client never writes back.
    """

    system_code = "business_central"
    # What this client actually implements today — not what Business Central offers.
    # Purchase orders, posted purchase receipts and item ledger entries are fetched;
    # no BC webhook processor exists (polling only).
    capabilities = BusinessSystemCapability(
        supports_purchase_orders=True,
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
        # Set once Business Central refuses the variant/location expansion, so the rest
        # of this client's line fetches go straight to the plain request.
        self._line_references_denied = False
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

        Lines are requested with their variant and location expanded. If Business
        Central denies that expansion (an app without the item-variant or location
        permission), the lines are fetched again without it: the order still syncs,
        and the lines carry no ``itemVariant``/``location`` key, which the mapper
        reads as "not known" rather than "none".
        """
        if self.use_dummy:
            records = self._load_fixture(f"business_central_purchase_order_lines_{purchase_order_id}.json")
            return records[:top] if top else records

        cfg = self._require_config()
        po_id = _require_guid(purchase_order_id, "purchase order id")
        params = {"$select": ",".join(_PO_LINE_SELECT)}
        endpoint = f"{cfg.company_path}/purchaseOrders({po_id})/purchaseOrderLines"
        if not self._line_references_denied:
            try:
                return self._fetch_all_pages(endpoint, params={**params, "$expand": _PO_LINE_EXPAND}, limit=top)
            except BusinessCentralPermissionError:
                self._line_references_denied = True
                logger.warning(
                    "Business Central denied the purchase order line variant/location expansion; "
                    "syncing lines without variant and location codes"
                )
        return self._fetch_all_pages(endpoint, params=params, limit=top)

    def iter_item_ledger_entry_pages(
        self,
        *,
        after_entry_no: int | None = None,
        item_no_prefixes: Sequence[str] = (),
        serial_only: bool = False,
    ) -> Iterator[list[dict]]:
        """Yield pages of item ledger entries in ascending ``entryNo`` order.

        Analytics API (``…/api/microsoft/analytics/v1.0/companies({id})/itemLedgerEntries``).
        Verified against BC: ``entryNo gt N``, ``startswith(itemNo, …)`` joined with
        ``or``, ``serialNo ne ''`` and ``$orderby=entryNo asc`` are supported (a
        descending order and ``not`` are not — HTTP 501), and ``@odata.nextLink``
        continues the same filter and order. Pages are fetched lazily, so a caller
        can stop after any page. In dummy mode the same filters are applied to
        ``business_central_item_ledger_entries.json``.
        """
        prefixes = [require_item_prefix(prefix) for prefix in item_no_prefixes]
        if after_entry_no is not None and (isinstance(after_entry_no, bool) or not isinstance(after_entry_no, int)):
            raise BusinessCentralConfigurationError("after_entry_no must be an integer")

        if self.use_dummy:
            yield from self._dummy_item_ledger_pages(after_entry_no, prefixes, serial_only)
            return

        cfg = self._require_config()
        clauses = []
        if after_entry_no is not None:
            clauses.append(f"entryNo gt {after_entry_no}")
        if prefixes:
            clauses.append("(" + " or ".join(f"startswith(itemNo,'{prefix}')" for prefix in prefixes) + ")")
        if serial_only:
            clauses.append("serialNo ne ''")
        params: dict[str, Any] = {"$orderby": "entryNo asc"}
        if clauses:
            params["$filter"] = " and ".join(clauses)
        endpoint = f"{cfg.company_path}/itemLedgerEntries"
        yield from self._iter_pages(endpoint, params=params, api=API_ANALYTICS)

    def iter_purchase_receipt_pages(self, *, modified_since: str | None = None) -> Iterator[list[dict]]:
        """Yield pages of posted purchase receipts, each with its ``purchaseReceiptLines`` expanded.

        ``modified_since`` (ISO-8601 UTC) filters on ``lastModifiedDateTime``. In
        dummy mode reads ``business_central_purchase_receipts.json``.
        """
        if self.use_dummy:
            records = self._load_fixture("business_central_purchase_receipts.json")
            if modified_since:
                records = [r for r in records if str(r.get("lastModifiedDateTime", "")) > modified_since]
            for start in range(0, len(records), _DUMMY_PAGE_SIZE):
                yield records[start : start + _DUMMY_PAGE_SIZE]
            return

        cfg = self._require_config()
        params: dict[str, Any] = {
            "$select": ",".join(_RECEIPT_SELECT),
            "$expand": f"purchaseReceiptLines($select={','.join(_RECEIPT_LINE_SELECT)})",
        }
        if modified_since:
            params["$filter"] = f"lastModifiedDateTime gt {modified_since}"
        yield from self._iter_pages(f"{cfg.company_path}/purchaseReceipts", params=params)

    def _dummy_item_ledger_pages(
        self, after_entry_no: int | None, prefixes: list[str], serial_only: bool
    ) -> Iterator[list[dict]]:
        records = sorted(self._load_fixture("business_central_item_ledger_entries.json"), key=lambda r: r["entryNo"])
        records = [
            r
            for r in records
            if (after_entry_no is None or r["entryNo"] > after_entry_no)
            and (not prefixes or any(str(r.get("itemNo", "")).startswith(p) for p in prefixes))
            and (not serial_only or r.get("serialNo"))
        ]
        page_size = _DUMMY_PAGE_SIZE
        for start in range(0, len(records), page_size):
            yield records[start : start + page_size]

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

    def _build_url(self, endpoint: str, *, api: str = API_STANDARD) -> str:
        cfg = self._require_config()
        if "://" in endpoint or endpoint.startswith("//"):
            # Endpoints are relative to an allowlisted API root, never a URL of their own.
            raise BusinessCentralConfigurationError("Business Central endpoint must be a relative path")
        return f"{cfg.api_root_for(api)}/{endpoint.lstrip('/')}"

    def _get(self, endpoint: str, *, params: dict | None = None, api: str = API_STANDARD) -> dict:
        """Perform a single GET against a relative endpoint and return parsed JSON."""
        return self._request(self._build_url(endpoint, api=api), params=params, log_endpoint=endpoint)

    def _fetch_all_pages(
        self,
        endpoint: str,
        *,
        params: dict | None = None,
        limit: int | None = None,
        api: str = API_STANDARD,
    ) -> list[dict]:
        """Fetch an OData collection of an allowlisted ``api``, following @odata.nextLink.

        ``$top`` is *not* a page size in OData — it caps the whole result — so it
        is only sent when ``limit`` is given.
        """
        query = dict(params or {})
        if limit:
            query["$top"] = limit
        results: list[dict] = []
        for page in self._iter_pages(endpoint, params=query, api=api):
            results.extend(page)
            if limit and len(results) >= limit:
                break  # the next page is never requested
        return results[:limit] if limit else results

    def _iter_pages(
        self, endpoint: str, *, params: dict | None = None, api: str = API_STANDARD
    ) -> Iterator[list[dict]]:
        """Yield the pages of an OData collection, following @odata.nextLink lazily.

        Page size is requested with ``Prefer: odata.maxpagesize`` (re-sent on every
        page). The next page is only requested when the caller asks for it.
        """
        cfg = self._require_config()
        headers = {"Prefer": f"odata.maxpagesize={cfg.page_size}"}
        payload = self._request(
            self._build_url(endpoint, api=api), params=dict(params or {}), log_endpoint=endpoint, headers=headers
        )
        pages = 0
        while True:
            pages += 1
            page = payload.get("value", [])
            if not isinstance(page, list):
                raise BusinessCentralResponseError(f"Business Central returned a non-list 'value' for {endpoint}")
            yield page
            next_link = payload.get("@odata.nextLink")
            # Business Central can emit a nextLink after the last full page whose
            # follow-up is empty; an empty page ends the collection.
            if not next_link or not page:
                return
            if pages >= _MAX_PAGES:
                raise BusinessCentralResponseError(f"Pagination exceeded {_MAX_PAGES} pages for {endpoint}")
            # The bearer token is only ever sent to the Business Central API host,
            # and a page of one API may not continue into another.
            if not str(next_link).startswith(f"{_API_HOST}/"):
                raise BusinessCentralResponseError("Business Central returned a nextLink outside the API host")
            if not _next_link_in_api(str(next_link), cfg.api_path(api)):
                raise BusinessCentralResponseError("Business Central returned a nextLink outside the requested API")
            # nextLink is an absolute URL that already carries the query state.
            payload = self._request(next_link, params=None, log_endpoint=endpoint, headers=headers)

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
                error_kwargs: _ErrorContext = {"status_code": status_code, "error_code": code, "endpoint": log_endpoint}
                if status_code == 403:
                    # Only allowlisted fields of BC's message are kept — never the message itself.
                    permission = parse_permission_error(_error_field(response, "message"))
                    error_message = _with_code(
                        "Business Central denied access (403). The app needs read permission sets in Business Central",
                        code,
                    )
                    permission_error = BusinessCentralPermissionError(
                        error_message, permission=permission, **error_kwargs
                    )
                    # The request log is admin-only, so it records what was denied.
                    error_message = f"{error_message}. {format_diagnostic(failure_diagnostic(permission_error))}"
                    raise permission_error
                if status_code == 404:
                    error_message = _with_code("Business Central returned HTTP 404 (not found)", code)
                    raise BusinessCentralNotFoundError(error_message, **error_kwargs)
                error_message = _with_code(f"Business Central returned HTTP {status_code}", code)
                raise BusinessCentralResponseError(error_message, **error_kwargs)
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
