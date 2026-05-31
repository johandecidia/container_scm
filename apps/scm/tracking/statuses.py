# Tracking status constants, choices and normalization mappings.
# All TextChoices classes mirror the inner classes on the model but are exported
# here so services, tasks, and tests can import from one place.

from .models import (
    TrackingEvent,
    TrackingProvider,
    TrackingRawPayload,
    TrackingSubscription,
    TrackingSyncRun,
)

# Re-export choices for convenience.
TrackingProviderType = TrackingProvider.ProviderType
TrackingReferenceType = TrackingSubscription.ReferenceType
TrackingSubscriptionStatus = TrackingSubscription.Status
TrackingEventType = TrackingEvent.EventType
TrackingPayloadType = TrackingRawPayload.PayloadType
TrackingSyncStatus = TrackingSyncRun.Status

# ---------------------------------------------------------------------------
# Normalisation mapping: external status strings → TrackingEventType values
# Keys are lowercase-stripped strings from carrier APIs / webhooks.
# ---------------------------------------------------------------------------
_NORMALISE_MAP: dict[str, str] = {
    # Loaded
    "loaded on board": TrackingEventType.LOADED_ON_VESSEL,
    "loaded on vessel": TrackingEventType.LOADED_ON_VESSEL,
    "load": TrackingEventType.LOADED_ON_VESSEL,
    # Discharged
    "discharged": TrackingEventType.DISCHARGED,
    "discharge": TrackingEventType.DISCHARGED,
    # Gate in / out
    "gate in": TrackingEventType.GATE_IN,
    "gate-in": TrackingEventType.GATE_IN,
    "gate out": TrackingEventType.GATE_OUT,
    "gate-out": TrackingEventType.GATE_OUT,
    # ETA
    "eta updated": TrackingEventType.ETA_UPDATED,
    "eta update": TrackingEventType.ETA_UPDATED,
    # Vessel
    "vessel departed": TrackingEventType.VESSEL_DEPARTED,
    "vessel arrived": TrackingEventType.VESSEL_ARRIVED,
    "departure": TrackingEventType.VESSEL_DEPARTED,
    "arrival": TrackingEventType.VESSEL_ARRIVED,
    # Transshipment
    "transshipment arrived": TrackingEventType.TRANSSHIPMENT_ARRIVED,
    "transshipment departed": TrackingEventType.TRANSSHIPMENT_DEPARTED,
    # Delivery
    "delivered": TrackingEventType.DELIVERED,
    "delivery": TrackingEventType.DELIVERED,
    # Booking
    "booking created": TrackingEventType.BOOKING_CREATED,
    "booking confirmed": TrackingEventType.BOOKING_CREATED,
    # Empty
    "empty released": TrackingEventType.EMPTY_RELEASED,
    "empty container released": TrackingEventType.EMPTY_RELEASED,
    # Customs
    "customs hold": TrackingEventType.CUSTOMS_HOLD,
    "customs": TrackingEventType.CUSTOMS_HOLD,
    # Delay
    "delay": TrackingEventType.DELAY,
    "delayed": TrackingEventType.DELAY,
}


def normalize_event_type(external_status: str) -> str:
    """Map an external status string to an internal TrackingEventType value.

    Comparison is case-insensitive.  Returns UNKNOWN if no mapping is found.
    """
    return _NORMALISE_MAP.get(external_status.lower().strip(), TrackingEventType.UNKNOWN)
