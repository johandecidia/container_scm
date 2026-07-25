"""Base classes and shared contract for carrier tracking integrations.

Every carrier adapter consists of a client (transport) and a parser
(normalisation). Both are resolved through the carrier registry and built by
:mod:`apps.scm.integrations.carriers.factory`, which injects the team's
``Integration`` and its decrypted credentials.

A client never reads global settings for secrets and never looks up team
configuration itself — everything it needs arrives through the constructor. That
keeps credentials team-scoped and makes adapters trivially testable.

Unimplemented methods raise :class:`CarrierNotImplementedError` (which is also a
``NotImplementedError``), so a stub adapter can never silently produce an empty
but "successful" tracking result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .exceptions import (
    CarrierConfigurationError,
    CarrierNotImplementedError,
    CarrierUnsupportedReferenceError,
)
from .schemas import ContainerDiscoveryResult

if TYPE_CHECKING:
    from apps.scm.integrations.models import Integration

    from .dcsa.schemas import NormalisedTrackingEvent


class ReferenceKind:
    """The reference types a carrier can be queried by.

    Values match the keyword arguments of :meth:`BaseCarrierClient.fetch_tracking`.
    """

    CONTAINER_NUMBER = "container_number"
    BILL_OF_LADING = "bill_of_lading_number"
    BOOKING_NUMBER = "booking_number"
    SHIPMENT_REFERENCE = "shipment_reference"
    PURCHASE_ORDER = "purchase_order_number"


@dataclass(frozen=True)
class TrackingReference:
    """A single validated reference to track by."""

    kind: str
    value: str


@dataclass
class CarrierCapability:
    """Describes what a carrier integration supports.

    These flags describe what the carrier's API offers on paper. They are not
    evidence that a live integration is configured or working — that is the
    ``Integration`` record's job.
    """

    supports_pull: bool = False
    supports_webhooks: bool = False
    supports_subscriptions: bool = False
    supports_tracking_by_container: bool = False
    supports_tracking_by_bl: bool = False
    supports_tracking_by_booking: bool = False
    supports_tracking_by_shipment_reference: bool = False
    supports_tracking_by_purchase_order: bool = False
    supports_dcsa: bool = False
    supports_schedules: bool = False
    supports_booking: bool = False
    supports_discovery: bool = False
    requires_customer_approval: bool = False
    requires_account_number: bool = False


# Which capability flag gates each reference kind.
_CAPABILITY_FOR_REFERENCE: dict[str, str] = {
    ReferenceKind.CONTAINER_NUMBER: "supports_tracking_by_container",
    ReferenceKind.BILL_OF_LADING: "supports_tracking_by_bl",
    ReferenceKind.BOOKING_NUMBER: "supports_tracking_by_booking",
    ReferenceKind.SHIPMENT_REFERENCE: "supports_tracking_by_shipment_reference",
    ReferenceKind.PURCHASE_ORDER: "supports_tracking_by_purchase_order",
}


def resolve_tracking_reference(
    *,
    capabilities: CarrierCapability,
    provider_code: str = "",
    container_number: str | None = None,
    bill_of_lading_number: str | None = None,
    booking_number: str | None = None,
    shipment_reference: str | None = None,
    purchase_order_number: str | None = None,
) -> TrackingReference:
    """Validate that exactly one supported reference was supplied.

    Raises :class:`CarrierUnsupportedReferenceError` when no reference, more than
    one reference, or a reference the carrier does not support is given. Blank
    strings count as absent.
    """
    supplied = [
        TrackingReference(kind, value.strip())
        for kind, value in (
            (ReferenceKind.CONTAINER_NUMBER, container_number),
            (ReferenceKind.BILL_OF_LADING, bill_of_lading_number),
            (ReferenceKind.BOOKING_NUMBER, booking_number),
            (ReferenceKind.SHIPMENT_REFERENCE, shipment_reference),
            (ReferenceKind.PURCHASE_ORDER, purchase_order_number),
        )
        if value and value.strip()
    ]

    if not supplied:
        raise CarrierUnsupportedReferenceError(
            "Exactly one tracking reference must be supplied; none were given.",
            provider_code=provider_code,
        )
    if len(supplied) > 1:
        kinds = ", ".join(sorted(ref.kind for ref in supplied))
        raise CarrierUnsupportedReferenceError(
            f"Exactly one tracking reference must be supplied; got {len(supplied)} ({kinds}).",
            provider_code=provider_code,
        )

    reference = supplied[0]
    capability_name = _CAPABILITY_FOR_REFERENCE[reference.kind]
    if not getattr(capabilities, capability_name, False):
        raise CarrierUnsupportedReferenceError(
            f"Carrier '{provider_code or 'unknown'}' does not support tracking by {reference.kind}.",
            provider_code=provider_code,
        )
    return reference


class BaseCarrierClient:
    """Base class for all carrier API clients.

    Subclasses define ``provider_code`` and ``capabilities`` and implement
    :meth:`test_connection`, :meth:`fetch_tracking` and — when the carrier
    supports it — :meth:`discover_containers`.

    Construction is dependency-injected: pass the team's ``Integration`` (live
    mode) or nothing (stub/offline mode). Credentials are read lazily from the
    credential service for that integration only.
    """

    provider_code: str = ""
    capabilities: CarrierCapability = CarrierCapability()

    def __init__(
        self,
        integration: Integration | None = None,
        *,
        credentials: dict | None = None,
    ) -> None:
        self.integration = integration
        self._credentials = credentials

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    @property
    def config(self) -> dict:
        """Non-secret configuration for this integration (never contains secrets)."""
        if self.integration is None:
            return {}
        return self.integration.config or {}

    @property
    def credentials(self) -> dict:
        """Decrypted credentials for this integration, resolved on first use.

        Returns an empty dict when no integration was injected. Never falls back
        to global settings or another team's configuration.
        """
        if self._credentials is not None:
            return self._credentials
        if self.integration is None:
            self._credentials = {}
            return self._credentials

        from apps.scm.integrations.credentials import get_integration_credentials

        self._credentials = get_integration_credentials(self.integration)
        return self._credentials

    @property
    def is_configured(self) -> bool:
        """True when this client has an integration to work with."""
        return self.integration is not None

    def require_integration(self) -> Integration:
        """Return the injected integration, or raise a configuration error."""
        if self.integration is None:
            raise CarrierConfigurationError(
                f"{self.__class__.__name__} requires a configured Integration for live access.",
                provider_code=self.provider_code,
            )
        return self.integration

    def resolve_reference(self, **kwargs) -> TrackingReference:
        """Validate the caller's reference arguments against this carrier's capabilities."""
        return resolve_tracking_reference(
            capabilities=self.capabilities,
            provider_code=self.provider_code,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Carrier contract
    # ------------------------------------------------------------------

    def test_connection(self) -> dict:
        """Verify connectivity and credentials.

        Must return a dict with at least: {"success": bool, "message": str}.
        Raise a typed :class:`CarrierError` on failure — never fail silently, and
        never include secrets in the message.
        """
        raise CarrierNotImplementedError(
            f"{self.__class__.__name__} does not implement test_connection() yet.",
            provider_code=self.provider_code,
        )

    def fetch_tracking(
        self,
        *,
        container_number: str | None = None,
        bill_of_lading_number: str | None = None,
        booking_number: str | None = None,
        shipment_reference: str | None = None,
        purchase_order_number: str | None = None,
    ) -> dict:
        """Fetch raw tracking data for exactly one reference.

        Returns the raw carrier response dict, to be stored verbatim and then
        parsed. Raise a typed :class:`CarrierError` on failure;
        :class:`CarrierNoDataError` when the carrier has no data for the
        reference.
        """
        raise CarrierNotImplementedError(
            f"{self.__class__.__name__} does not implement fetch_tracking() yet.",
            provider_code=self.provider_code,
        )

    def discover_containers(
        self,
        *,
        booking_number: str | None = None,
        bill_of_lading_number: str | None = None,
        shipment_reference: str | None = None,
    ) -> list[ContainerDiscoveryResult]:
        """Discover containers for a shipment via booking/BL/reference.

        At least one reference must be provided. Returns a list of
        ContainerDiscoveryResult — an empty list means "no containers found".
        Never return None. Raise a typed :class:`CarrierError` on failure.
        """
        raise CarrierNotImplementedError(
            f"{self.__class__.__name__} does not implement discover_containers() yet.",
            provider_code=self.provider_code,
        )


class BaseCarrierParser:
    """Base class for all carrier payload parsers.

    Subclasses define ``provider_code`` and implement
    :meth:`parse_tracking_events`. DCSA-compliant carriers should delegate to
    :class:`apps.scm.integrations.carriers.dcsa.parser.DcsaParser` and only
    handle genuine carrier-specific deviations.
    """

    provider_code: str = ""

    def parse_tracking_events(self, raw_payload: dict) -> list[NormalisedTrackingEvent]:
        """Parse a raw carrier payload into normalised events.

        Returns a list of
        :class:`apps.scm.integrations.carriers.dcsa.schemas.NormalisedTrackingEvent`.
        An empty list is a valid result (payload contained no events).

        Raise :class:`CarrierInvalidResponseError` when the payload cannot be
        interpreted at all — never fail silently.
        """
        raise CarrierNotImplementedError(
            f"{self.__class__.__name__} does not implement parse_tracking_events() yet.",
            provider_code=self.provider_code,
        )
