# DCSA tracking schemas — DTO layer only, not Django models.
# Based on DCSA Track & Trace standard (https://dcsa.org/standards/track-trace/).
from dataclasses import dataclass, field
from datetime import datetime


class DcsaEventType:
    """DCSA top-level event type classifiers."""

    SHIPMENT = "SHIPMENT"
    EQUIPMENT = "EQUIPMENT"
    TRANSPORT = "TRANSPORT"
    MILESTONE = "MILESTONE"


class DcsaEventClassifier:
    """DCSA event classifier — estimated vs actual."""

    ESTIMATED = "EST"
    ACTUAL = "ACT"
    PLANNED = "PLN"
    REQUESTED = "REQ"


@dataclass
class NormalisedTrackingEvent:
    """Normalised tracking event produced by a carrier parser.

    This is a DTO — it carries parsed data from a carrier response before it is
    persisted as a TrackingEvent model instance.

    ``event_classifier`` is the single source of truth for whether the event time
    is actual, estimated, planned or requested. ``is_actual`` / ``is_estimated``
    are derived from it rather than stored, so the two can never contradict
    each other.
    """

    # Event classification
    event_type: str = ""  # DcsaEventType or carrier-specific type
    event_classifier: str = ""  # DcsaEventClassifier
    event_code: str = ""  # Carrier-specific code, e.g. "ARRI", "DEPA"
    description: str = ""  # Carrier's own human-readable text, verbatim

    # Timing
    event_datetime: datetime | None = None
    event_datetime_timezone: str = ""

    # Location
    location_name: str = ""
    location_unlocode: str = ""
    facility_name: str = ""
    latitude: str = ""
    longitude: str = ""

    # Vessel
    vessel_name: str = ""
    vessel_imo: str = ""
    voyage_number: str = ""
    transport_mode: str = ""  # e.g. "VESSEL", "TRUCK", "RAIL"

    # References
    container_number: str = ""
    booking_number: str = ""
    bill_of_lading_number: str = ""
    purchase_order_number: str = ""

    # Source / deduplication
    raw_event_id: str = ""

    # Meta
    source_provider: str = ""
    raw_payload: dict = field(default_factory=dict)

    @property
    def is_actual(self) -> bool:
        """True when the carrier reported this as an actual (observed) event."""
        return self.event_classifier == DcsaEventClassifier.ACTUAL

    @property
    def is_estimated(self) -> bool:
        """True when the event time is a forecast (estimated or planned).

        Estimated events must never be treated as proof that something happened.
        """
        return self.event_classifier in (DcsaEventClassifier.ESTIMATED, DcsaEventClassifier.PLANNED)
