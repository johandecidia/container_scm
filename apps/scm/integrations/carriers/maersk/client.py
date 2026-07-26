"""Maersk Track & Trace client.

Everything endpoint-specific — base URL, path, query parameter names, auth style —
comes from the team's ``Integration.config``. Nothing is guessed and no URL is
hardcoded: without a verified configuration the client raises
CarrierConfigurationError and the sync layer records the run as SKIPPED, so an
unconfigured integration can never look like a carrier with no data.

See ``README.md`` in this package for the exact configuration and credential keys,
and for what still has to be confirmed against Maersk's API documentation and the
customer agreement before live traffic is enabled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.scm.integrations.carriers.base import BaseCarrierClient, CarrierCapability, ReferenceKind
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

PROVIDER_CODE = "maersk"

# Supported authentication styles, selected with config["auth_style"].
AUTH_API_KEY = "api_key_header"
AUTH_OAUTH2 = "oauth2_client_credentials"
SUPPORTED_AUTH_STYLES = (AUTH_API_KEY, AUTH_OAUTH2)

_SUPPORTED_REFERENCE_KINDS = frozenset(
    {
        ReferenceKind.CONTAINER_NUMBER,
        ReferenceKind.BILL_OF_LADING,
        ReferenceKind.BOOKING_NUMBER,
    }
)


@dataclass(frozen=True)
class MaerskConfig:
    """Validated live settings, resolved from Integration.config.

    ``reference_params`` maps a reference kind to the query parameter Maersk expects
    for it, e.g. {"container_number": "equipmentReference"}. It is configuration
    rather than a constant because the parameter names differ per API product and
    must come from the contract's documentation, not from a guess.
    """

    base_url: str
    tracking_path: str
    auth_style: str
    reference_params: dict[str, str]
    api_key_header_name: str = ""
    token_url: str = ""
    scope: str = ""
    extra_headers: dict | None = None

    @property
    def tracking_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.tracking_path.lstrip('/')}"


def resolve_config(config: dict, *, provider_code: str = PROVIDER_CODE) -> MaerskConfig:
    """Build and validate a MaerskConfig, or explain exactly what is missing."""
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
            f"Maersk integration is missing required configuration: {', '.join(missing)}.",
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

    unknown = set(reference_params) - _SUPPORTED_REFERENCE_KINDS
    if unknown:
        raise CarrierConfigurationError(
            f"reference_params contains unsupported reference kinds: {', '.join(sorted(unknown))}.",
            provider_code=provider_code,
        )

    if auth_style == AUTH_OAUTH2 and not str(config.get("token_url") or "").strip():
        raise CarrierConfigurationError(
            "Maersk integration is missing required configuration: token_url.",
            provider_code=provider_code,
        )
    if auth_style == AUTH_API_KEY and not str(config.get("api_key_header_name") or "").strip():
        raise CarrierConfigurationError(
            "Maersk integration is missing required configuration: api_key_header_name.",
            provider_code=provider_code,
        )

    return MaerskConfig(
        base_url=base_url,
        tracking_path=tracking_path,
        auth_style=auth_style,
        reference_params=dict(reference_params),
        api_key_header_name=str(config.get("api_key_header_name") or "").strip(),
        token_url=str(config.get("token_url") or "").strip(),
        scope=str(config.get("scope") or "").strip(),
        extra_headers=config.get("extra_headers") or {},
    )


class MaerskClient(BaseCarrierClient):
    """Maersk Track & Trace client.

    Live access requires a configured Integration; without one every network method
    raises CarrierConfigurationError rather than reaching out to a guessed endpoint.
    """

    provider_code = PROVIDER_CODE
    capabilities = CarrierCapability(
        supports_pull=True,
        supports_webhooks=True,
        supports_subscriptions=True,
        supports_tracking_by_container=True,
        supports_tracking_by_bl=True,
        supports_tracking_by_booking=True,
        supports_dcsa=True,
        supports_schedules=True,
        supports_discovery=True,
        requires_customer_approval=True,
        requires_account_number=True,
    )

    def __init__(self, integration: Integration | None = None, *, credentials: dict | None = None, session=None):
        super().__init__(integration, credentials=credentials)
        self._session = session
        self._config: MaerskConfig | None = None
        self._http: CarrierHttpClient | None = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @property
    def maersk_config(self) -> MaerskConfig:
        """The validated live configuration; raises if the integration is not set up."""
        if self._config is None:
            self.require_integration()
            self._config = resolve_config(self.config, provider_code=self.provider_code)
        return self._config

    def _build_auth(self):
        config = self.maersk_config
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
            config = self.maersk_config
            self._http = CarrierHttpClient(
                provider_code=self.provider_code,
                config=HttpConfig.from_config(self.config),
                auth=self._build_auth(),
                integration=self.integration,
                extra_headers=config.extra_headers or {},
                session=self._session,
            )
        return self._http

    # ------------------------------------------------------------------
    # Carrier contract
    # ------------------------------------------------------------------

    def test_connection(self) -> dict:
        """Verify configuration, credentials and endpoint with one small request.

        A "no data" answer still proves the endpoint and credentials work, so it
        counts as a successful connection test.
        """
        config = self.maersk_config
        probe_kind, probe_param = next(iter(config.reference_params.items()))
        probe_value = str(self.config.get("test_connection_reference") or "").strip()
        if not probe_value:
            raise CarrierConfigurationError(
                "Maersk integration is missing required configuration: test_connection_reference "
                "(a reference known to the account, used only to verify connectivity).",
                provider_code=self.provider_code,
            )

        try:
            self.http.get(config.tracking_url, params={probe_param: probe_value})
        except CarrierNoDataError:
            return {"success": True, "message": f"Connected to Maersk; no data for the test {probe_kind}."}
        return {"success": True, "message": "Connected to Maersk."}

    def fetch_tracking(
        self,
        *,
        container_number: str | None = None,
        bill_of_lading_number: str | None = None,
        booking_number: str | None = None,
        shipment_reference: str | None = None,
        purchase_order_number: str | None = None,
    ) -> dict:
        """Fetch raw Maersk tracking data for exactly one reference."""
        reference = self.resolve_reference(
            container_number=container_number,
            bill_of_lading_number=bill_of_lading_number,
            booking_number=booking_number,
            shipment_reference=shipment_reference,
            purchase_order_number=purchase_order_number,
        )
        config = self.maersk_config
        param = config.reference_params.get(reference.kind)
        if not param:
            raise CarrierConfigurationError(
                f"No Maersk query parameter is configured for {reference.kind}; add it to reference_params.",
                provider_code=self.provider_code,
            )

        payload = self.http.get(config.tracking_url, params={param: reference.value})
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            return {"events": payload}
        raise CarrierInvalidResponseError(
            "Maersk response was not a JSON object or array.",
            provider_code=self.provider_code,
        )

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
        from .parser import MaerskParser

        try:
            payload = self.fetch_tracking(
                booking_number=booking_number,
                bill_of_lading_number=bill_of_lading_number,
                shipment_reference=shipment_reference,
            )
        except CarrierNoDataError:
            return []

        results: dict[str, ContainerDiscoveryResult] = {}
        for event in MaerskParser().parse_tracking_events(payload):
            number = (event.container_number or "").strip().upper()
            if not number or number in results:
                continue
            results[number] = ContainerDiscoveryResult(
                container_number=number,
                carrier_code=self.provider_code,
                carrier_name="Maersk",
                booking_number=event.booking_number or booking_number,
                bl_number=event.bill_of_lading_number or bill_of_lading_number,
                shipment_reference=shipment_reference,
            )
        return list(results.values())
