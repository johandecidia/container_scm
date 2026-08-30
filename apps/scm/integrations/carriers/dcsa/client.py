"""A configured client for DCSA-conformant Track & Trace APIs.

Carriers that follow the DCSA standard differ in host, path, parameter names and
authentication, but not in the shape of the conversation: ask by one reference,
receive a list of events, optionally page through the rest with a cursor. That shared
shape lives here, so adding a DCSA carrier is a subclass with its capabilities and
name rather than another copy of the transport.

Nothing endpoint-specific is hardcoded. Every such value comes from the team's
``Integration.config``, and a missing one raises CarrierConfigurationError, which
the sync layer records as SKIPPED. A carrier that has not been configured against
its real documentation therefore cannot call anything at all — which is the point:
a guessed URL that 404s is indistinguishable from a container the carrier has never
heard of.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.scm.integrations.carriers.base import BaseCarrierClient, ReferenceKind
from apps.scm.integrations.carriers.exceptions import (
    CarrierConfigurationError,
    CarrierInvalidResponseError,
    CarrierNoDataError,
)
from apps.scm.integrations.carriers.http import CarrierHttpClient, HttpConfig
from apps.scm.integrations.carriers.oauth import ApiKeyAuth, ClientCredentialsAuth
from apps.scm.integrations.carriers.schemas import ContainerDiscoveryResult

if TYPE_CHECKING:
    from apps.scm.integrations.models import Integration

logger = logging.getLogger(__name__)

# Supported authentication styles, selected with config["auth_style"].
AUTH_API_KEY = "api_key_header"
AUTH_OAUTH2 = "oauth2_client_credentials"
SUPPORTED_AUTH_STYLES = (AUTH_API_KEY, AUTH_OAUTH2)

SUPPORTED_REFERENCE_KINDS = frozenset(
    {
        ReferenceKind.CONTAINER_NUMBER,
        ReferenceKind.BILL_OF_LADING,
        ReferenceKind.BOOKING_NUMBER,
    }
)

# Ceiling on how many pages one tracking call will follow. A cursor loop that never
# terminates — because a carrier keeps advertising a next page — must stop somewhere
# rather than hold a worker and its sync lock indefinitely.
DEFAULT_MAX_PAGES = 20


@dataclass(frozen=True)
class DcsaPaginationConfig:
    """How a carrier advertises and accepts its next page, from Integration.config.

    DCSA Track & Trace carriers page through a cursor: the response carries the next
    one in a header, and it is sent back as a query parameter. The names differ per
    carrier, so they are configuration rather than constants.

    Pagination stays off unless both ``cursor_param`` and ``next_page_header`` are
    configured — following a cursor a carrier never advertised would be guesswork,
    and a carrier that answers in one page must keep making exactly one request.
    """

    cursor_param: str = ""
    next_page_header: str = ""
    limit_param: str = ""
    page_size: int = 0
    max_pages: int = DEFAULT_MAX_PAGES

    @property
    def enabled(self) -> bool:
        return bool(self.cursor_param and self.next_page_header)


@dataclass(frozen=True)
class DcsaClientConfig:
    """Validated live settings for a DCSA carrier, from Integration.config.

    ``reference_params`` maps a reference kind to the query parameter the carrier
    expects, e.g. {"container_number": "equipmentReference"}. It is configuration
    rather than a constant because the names differ per carrier and per API product
    and must come from documentation, not from a guess.
    """

    base_url: str
    tracking_path: str
    auth_style: str
    reference_params: dict[str, str]
    api_key_header_name: str = ""
    token_url: str = ""
    scope: str = ""
    extra_headers: dict | None = None
    pagination: DcsaPaginationConfig = DcsaPaginationConfig()

    @property
    def tracking_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.tracking_path.lstrip('/')}"


def resolve_pagination_config(config: dict, *, provider_code: str) -> DcsaPaginationConfig:
    """Build the pagination settings from ``config["pagination"]``.

    Absent means "this carrier answers in one page". Half-configured is refused: a
    cursor parameter with no header to read it from (or the reverse) would silently
    fetch only the first page and look like a complete tracking history.
    """
    raw = config.get("pagination") or {}
    if not isinstance(raw, dict):
        raise CarrierConfigurationError(
            "pagination must be a mapping of cursor_param, next_page_header, limit_param, page_size and max_pages.",
            provider_code=provider_code,
        )

    cursor_param = str(raw.get("cursor_param") or "").strip()
    next_page_header = str(raw.get("next_page_header") or "").strip()
    if bool(cursor_param) != bool(next_page_header):
        missing = "next_page_header" if cursor_param else "cursor_param"
        raise CarrierConfigurationError(
            f"pagination is missing required configuration: {missing}. Cursor pagination needs "
            "both the query parameter to send and the response header to read it from.",
            provider_code=provider_code,
        )

    def _int(key: str, default: int) -> int:
        try:
            return int(raw.get(key) or default)
        except TypeError, ValueError:
            return default

    return DcsaPaginationConfig(
        cursor_param=cursor_param,
        next_page_header=next_page_header,
        limit_param=str(raw.get("limit_param") or "").strip(),
        page_size=max(_int("page_size", 0), 0),
        max_pages=max(_int("max_pages", DEFAULT_MAX_PAGES), 1),
    )


def resolve_dcsa_config(
    config: dict,
    *,
    provider_code: str,
    carrier_name: str,
) -> DcsaClientConfig:
    """Build and validate a DcsaClientConfig, or say exactly what is missing."""
    config = config or {}
    base_url = str(config.get("base_url") or "").strip()
    tracking_path = str(config.get("tracking_path") or "").strip()
    auth_style = str(config.get("auth_style") or "").strip()
    reference_params = config.get("reference_params") or {}

    missing = [
        name
        for name, value in (
            ("base_url", base_url),
            ("tracking_path", tracking_path),
            ("auth_style", auth_style),
            ("reference_params", reference_params),
        )
        if not value
    ]
    if missing:
        raise CarrierConfigurationError(
            f"{carrier_name} integration is missing required configuration: {', '.join(missing)}.",
            provider_code=provider_code,
        )

    if auth_style not in SUPPORTED_AUTH_STYLES:
        raise CarrierConfigurationError(
            f"Unsupported auth_style '{auth_style}'; expected one of {', '.join(SUPPORTED_AUTH_STYLES)}.",
            provider_code=provider_code,
        )

    if not isinstance(reference_params, dict):
        raise CarrierConfigurationError(
            "reference_params must be a mapping of reference kind to query parameter name.",
            provider_code=provider_code,
        )

    unknown = set(reference_params) - SUPPORTED_REFERENCE_KINDS
    if unknown:
        raise CarrierConfigurationError(
            f"reference_params contains unsupported reference kinds: {', '.join(sorted(unknown))}.",
            provider_code=provider_code,
        )

    if auth_style == AUTH_OAUTH2 and not str(config.get("token_url") or "").strip():
        raise CarrierConfigurationError(
            f"{carrier_name} integration is missing required configuration: token_url.",
            provider_code=provider_code,
        )
    if auth_style == AUTH_API_KEY and not str(config.get("api_key_header_name") or "").strip():
        raise CarrierConfigurationError(
            f"{carrier_name} integration is missing required configuration: api_key_header_name.",
            provider_code=provider_code,
        )

    return DcsaClientConfig(
        base_url=base_url,
        tracking_path=tracking_path,
        auth_style=auth_style,
        reference_params=dict(reference_params),
        api_key_header_name=str(config.get("api_key_header_name") or "").strip(),
        token_url=str(config.get("token_url") or "").strip(),
        scope=str(config.get("scope") or "").strip(),
        extra_headers=config.get("extra_headers") or {},
        pagination=resolve_pagination_config(config, provider_code=provider_code),
    )


class DcsaCarrierClient(BaseCarrierClient):
    """Base client for DCSA Track & Trace carriers.

    Subclasses set ``provider_code``, ``carrier_name`` and ``capabilities``. Live
    access requires a configured Integration; without one, every network method
    raises CarrierConfigurationError rather than reaching a guessed endpoint.
    """

    carrier_name: str = ""

    def __init__(self, integration: Integration | None = None, *, credentials: dict | None = None, session=None):
        super().__init__(integration, credentials=credentials)
        self._session = session
        self._dcsa_config: DcsaClientConfig | None = None
        self._http: CarrierHttpClient | None = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @property
    def dcsa_config(self) -> DcsaClientConfig:
        """The validated live configuration; raises if the integration is not set up."""
        if self._dcsa_config is None:
            self.require_integration()
            self._dcsa_config = resolve_dcsa_config(
                self.config,
                provider_code=self.provider_code,
                carrier_name=self.carrier_name or self.provider_code,
            )
        return self._dcsa_config

    def _build_auth(self):
        config = self.dcsa_config
        credentials = self.credentials
        if config.auth_style == AUTH_API_KEY:
            return ApiKeyAuth(
                header_name=config.api_key_header_name,
                api_key=credentials.get("api_key", ""),
                provider_code=self.provider_code,
            )
        return ClientCredentialsAuth(
            token_url=config.token_url,
            client_id=credentials.get("client_id", ""),
            client_secret=credentials.get("client_secret", ""),
            scope=config.scope,
            provider_code=self.provider_code,
            timeout_seconds=HttpConfig.from_config(self.config).timeout_seconds,
        )

    @property
    def http(self) -> CarrierHttpClient:
        if self._http is None:
            config = self.dcsa_config
            self._http = CarrierHttpClient(
                provider_code=self.provider_code,
                config=HttpConfig.from_config(self.config),
                auth=self._build_auth(),
                integration=self.integration,
                extra_headers=config.extra_headers or {},
                session=self._session,
            )
        return self._http

    def _parser(self):
        from apps.scm.integrations.carriers.factory import build_carrier_parser

        return build_carrier_parser(self.provider_code)

    # ------------------------------------------------------------------
    # Carrier contract
    # ------------------------------------------------------------------

    def test_connection(self) -> dict:
        """Verify configuration, credentials and endpoint with one small request.

        A "no data" answer still proves the endpoint and credentials work, so it
        counts as a successful connection test.
        """
        config = self.dcsa_config
        name = self.carrier_name or self.provider_code
        probe_kind, probe_param = next(iter(config.reference_params.items()))
        probe_value = str(self.config.get("test_connection_reference") or "").strip()
        if not probe_value:
            raise CarrierConfigurationError(
                f"{name} integration is missing required configuration: test_connection_reference "
                "(a reference known to the account, used only to verify connectivity).",
                provider_code=self.provider_code,
            )

        try:
            self.http.get(config.tracking_url, params={probe_param: probe_value})
        except CarrierNoDataError:
            return {"success": True, "message": f"Connected to {name}; no data for the test {probe_kind}."}
        return {"success": True, "message": f"Connected to {name}."}

    def fetch_tracking(
        self,
        *,
        container_number: str | None = None,
        bill_of_lading_number: str | None = None,
        booking_number: str | None = None,
        shipment_reference: str | None = None,
        purchase_order_number: str | None = None,
    ) -> dict:
        """Fetch raw tracking data for exactly one reference."""
        reference = self.resolve_reference(
            container_number=container_number,
            bill_of_lading_number=bill_of_lading_number,
            booking_number=booking_number,
            shipment_reference=shipment_reference,
            purchase_order_number=purchase_order_number,
        )
        config = self.dcsa_config
        param = config.reference_params.get(reference.kind)
        if not param:
            raise CarrierConfigurationError(
                f"No {self.carrier_name or self.provider_code} query parameter is configured for "
                f"{reference.kind}; add it to reference_params.",
                provider_code=self.provider_code,
            )

        return self._fetch_pages(config.tracking_url, {param: reference.value})

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _as_event_payload(self, payload) -> dict:
        """Return the response as a dict the parser can read.

        DCSA endpoints answer with either an event array or an object wrapping one;
        anything else cannot be interpreted at all and is rejected rather than
        quietly becoming an empty tracking history.
        """
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            return {"events": payload}
        raise CarrierInvalidResponseError(
            f"{self.carrier_name or self.provider_code} response was not a JSON object or array.",
            provider_code=self.provider_code,
        )

    @staticmethod
    def _page_events(payload: dict) -> list:
        """Return one page's event list, under either key the DCSA parser accepts.

        Reading only ``events`` would merge a ``trackingData`` response into an empty
        list — a silent loss dressed up as a carrier with nothing to report.
        """
        for key in ("events", "trackingData"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    def _fetch_pages(self, url: str, params: dict) -> dict:
        """GET ``url``, following the carrier's cursor pagination when it is configured.

        Returns a single payload in the shape the parser expects: the first page's
        object with every page's events appended, in the order the carrier listed
        them. A carrier without pagination configured makes exactly one request.
        """
        pagination = self.dcsa_config.pagination
        base_params = dict(params)
        if pagination.limit_param and pagination.page_size:
            base_params[pagination.limit_param] = pagination.page_size

        if not pagination.enabled:
            return self._as_event_payload(self.http.get(url, params=base_params))

        first_page: dict | None = None
        events: list = []
        cursor = ""
        seen_cursors: set[str] = set()

        for _page in range(pagination.max_pages):
            page_params = dict(base_params)
            if cursor:
                page_params[pagination.cursor_param] = cursor

            response = self.http.get_with_headers(url, params=page_params)
            payload = self._as_event_payload(response.payload)
            if first_page is None:
                first_page = payload
            events.extend(self._page_events(payload))

            cursor = response.header(pagination.next_page_header)
            if not cursor:
                break
            if cursor in seen_cursors:
                # A repeated cursor would page over the same events forever.
                logger.warning(
                    "%s repeated pagination cursor; stopping after %s page(s).",
                    self.provider_code,
                    len(seen_cursors) + 1,
                )
                break
            seen_cursors.add(cursor)
        else:
            logger.warning(
                "%s tracking response has more pages than the configured max_pages=%s; returning the first %s page(s).",
                self.provider_code,
                pagination.max_pages,
                pagination.max_pages,
            )

        merged = dict(first_page or {})
        # One list, under one key: leaving the first page's own key behind would give
        # the parser two competing event lists to choose between.
        merged.pop("trackingData", None)
        merged["events"] = events
        return merged

    def discover_containers(
        self,
        *,
        booking_number: str | None = None,
        bill_of_lading_number: str | None = None,
        shipment_reference: str | None = None,
    ) -> list[ContainerDiscoveryResult]:
        """Discover the containers on a booking or bill of lading.

        Reuses the tracking endpoint: its events carry equipment references, and the
        distinct set of those is the shipment's containers.
        """
        try:
            payload = self.fetch_tracking(
                booking_number=booking_number,
                bill_of_lading_number=bill_of_lading_number,
                shipment_reference=shipment_reference,
            )
        except CarrierNoDataError:
            return []

        results: dict[str, ContainerDiscoveryResult] = {}
        for event in self._parser().parse_tracking_events(payload):
            number = (event.container_number or "").strip().upper()
            if not number or number in results:
                continue
            results[number] = ContainerDiscoveryResult(
                container_number=number,
                carrier_code=self.provider_code,
                carrier_name=self.carrier_name or self.provider_code,
                booking_number=event.booking_number or booking_number,
                bl_number=event.bill_of_lading_number or bill_of_lading_number,
                shipment_reference=shipment_reference,
            )
        return list(results.values())
