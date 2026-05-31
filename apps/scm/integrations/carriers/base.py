# Base classes and protocols for carrier tracking integrations.
# Each carrier module must implement BaseCarrierClient and a parser.
from dataclasses import dataclass, field


@dataclass
class CarrierCapability:
    """Describes what a carrier integration supports."""

    supports_pull: bool = False
    supports_webhooks: bool = False
    supports_subscriptions: bool = False
    supports_tracking_by_container: bool = False
    supports_tracking_by_bl: bool = False
    supports_tracking_by_booking: bool = False
    supports_tracking_by_purchase_order: bool = False
    supports_dcsa: bool = False
    supports_schedules: bool = False
    supports_booking: bool = False
    requires_customer_approval: bool = False
    requires_account_number: bool = False


class BaseCarrierClient:
    """Base class for all carrier API clients.

    Subclasses must define provider_code and implement fetch_tracking.
    No real HTTP calls are made in this base class.
    """

    provider_code: str = ""
    capabilities: CarrierCapability = field(default_factory=CarrierCapability)

    def test_connection(self) -> dict:
        """Verify connectivity and credentials.

        Must return a dict with at least: {"success": bool, "message": str}.
        Raise an explicit exception on failure — never fail silently.
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement test_connection()")

    def fetch_tracking(
        self,
        *,
        container_number: str | None = None,
        bill_of_lading_number: str | None = None,
        booking_number: str | None = None,
        purchase_order_number: str | None = None,
    ) -> dict:
        """Fetch raw tracking data for a reference.

        At least one reference keyword argument must be provided.
        Returns the raw carrier response dict (to be stored and then parsed).
        Raise an explicit exception on failure — never fail silently.
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement fetch_tracking()")


class BaseCarrierParser:
    """Base class for all carrier payload parsers.

    Subclasses must define provider_code and implement parse_tracking_events.
    """

    provider_code: str = ""

    def parse_tracking_events(self, raw_payload: dict) -> list[dict]:
        """Parse a raw carrier payload into a list of normalised event dicts.

        Each dict must contain at minimum:
          - event_type (str): mapped to TrackingEvent.EventType
          - event_datetime (datetime | None)
          - description (str)

        Optional keys (use empty string / None when absent):
          - source_event_id, event_code, status
          - location_name, location_unlocode
          - event_timezone, confidence, raw_data

        Raise an explicit exception on failure — never fail silently.
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement parse_tracking_events()")


# ── Legacy aliases (kept for backwards-compat with existing maersk/hapag_lloyd/cma_cgm stubs) ──


class TrackingClient(BaseCarrierClient):
    """Deprecated alias — use BaseCarrierClient in new code."""

    def fetch_tracking(self, reference: str = "", reference_type: str = "", **kwargs) -> dict:  # type: ignore[override]
        raise NotImplementedError(f"{self.__class__.__name__} must implement fetch_tracking()")


class TrackingParser(BaseCarrierParser):
    """Deprecated alias — use BaseCarrierParser in new code."""

    def parse_events(self, payload: dict) -> list[dict]:
        raise NotImplementedError(f"{self.__class__.__name__} must implement parse_events()")

    def parse_tracking_events(self, raw_payload: dict) -> list[dict]:
        return self.parse_events(raw_payload)
