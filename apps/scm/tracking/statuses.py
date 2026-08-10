# Tracking status constants, choices and normalization mappings.
# All TextChoices classes mirror the inner classes on the model but are exported
# here so services, tasks, and tests can import from one place.

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

from .models import (
    TrackingEvent,
    TrackingProvider,
    TrackingRawPayload,
    TrackingSubscription,
    TrackingSyncRun,
)

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

# Re-export choices for convenience.
TrackingProviderType = TrackingProvider.ProviderType
TrackingReferenceType = TrackingSubscription.ReferenceType
TrackingSubscriptionStatus = TrackingSubscription.Status
TrackingEventType = TrackingEvent.EventType
TrackingEventTimeType = TrackingEvent.EventTimeType
TrackingTransportMode = TrackingEvent.TransportMode
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


# ---------------------------------------------------------------------------
# DCSA normalisation
# ---------------------------------------------------------------------------

# DCSA eventClassifierCode → TrackingEvent.EventTimeType.
_CLASSIFIER_MAP: dict[str, str] = {
    "ACT": TrackingEventTimeType.ACTUAL,
    "EST": TrackingEventTimeType.ESTIMATED,
    "PLN": TrackingEventTimeType.PLANNED,
    "REQ": TrackingEventTimeType.REQUESTED,
}

# DCSA modeOfTransport → TrackingEvent.TransportMode.
_TRANSPORT_MODE_MAP: dict[str, str] = {
    "VESSEL": TrackingTransportMode.VESSEL,
    "RAIL": TrackingTransportMode.RAIL,
    "TRUCK": TrackingTransportMode.TRUCK,
    "BARGE": TrackingTransportMode.BARGE,
    "AIR": TrackingTransportMode.AIR,
}

# DCSA event codes → internal TrackingEventType, keyed by (top-level type, code).
# Only unambiguous codes are mapped; anything else stays UNKNOWN and the carrier's
# own code and description are preserved on the event so no information is lost.
_DCSA_EVENT_CODE_MAP: dict[tuple[str, str], str] = {
    ("EQUIPMENT", "LOAD"): TrackingEventType.LOADED_ON_VESSEL,
    ("EQUIPMENT", "DISC"): TrackingEventType.DISCHARGED,
    ("EQUIPMENT", "GTIN"): TrackingEventType.GATE_IN,
    ("EQUIPMENT", "GTOT"): TrackingEventType.GATE_OUT,
    ("TRANSPORT", "ARRI"): TrackingEventType.VESSEL_ARRIVED,
    ("TRANSPORT", "DEPA"): TrackingEventType.VESSEL_DEPARTED,
    ("SHIPMENT", "RECE"): TrackingEventType.BOOKING_CREATED,
    ("SHIPMENT", "CONF"): TrackingEventType.BOOKING_CREATED,
}


def normalize_event_time_type(classifier: str) -> str:
    """Map a DCSA eventClassifierCode to an EventTimeType.

    Returns UNKNOWN for absent or unrecognised classifiers — never guesses that an
    unclassified event actually happened.
    """
    return _CLASSIFIER_MAP.get((classifier or "").upper().strip(), TrackingEventTimeType.UNKNOWN)


def normalize_transport_mode(mode: str) -> str:
    """Map a carrier transport mode to a TransportMode value.

    An unrecognised but non-empty mode becomes OTHER; the original string is kept
    in the event's raw data.
    """
    cleaned = (mode or "").upper().strip()
    if not cleaned:
        return ""
    return _TRANSPORT_MODE_MAP.get(cleaned, TrackingTransportMode.OTHER)


def normalize_dcsa_event_type(carrier_event_type: str, event_code: str, description: str = "") -> str:
    """Map a DCSA event to an internal TrackingEventType.

    Tries the (event type, code) pair first, then falls back to matching the
    carrier's free-text description, then UNKNOWN.
    """
    key = ((carrier_event_type or "").upper().strip(), (event_code or "").upper().strip())
    mapped = _DCSA_EVENT_CODE_MAP.get(key)
    if mapped:
        return mapped
    if description:
        return normalize_event_type(description)
    return TrackingEventType.UNKNOWN


# ---------------------------------------------------------------------------
# Carrier wording for events we deliberately do not classify
# ---------------------------------------------------------------------------

# Codes seen in live DCSA responses that have no honest counterpart in
# TrackingEventType.
#
# The SHIPMENT ones are transport-document milestones rather than movements of a box,
# and there is no physical event type they could map to without claiming something
# happened that did not.
#
# PICK and DROP are movements, but DCSA defines them as generic pick-up and drop-off
# at a facility, which is not the same as a gate movement or a delivery. Reading a
# laden DROP as DELIVERED would be an inference, and an expensive one: DELIVERED is a
# terminal state that stops polling, so guessing it wrong silently ends tracking.
#
# These labels are display-only. They never influence event_type, status derivation,
# ETA or the polling lifecycle — they exist so an unclassified event can say what the
# carrier reported instead of only "Unknown". A code that is not listed here still
# shows its raw carrier type and code, so nothing the carrier sent is ever hidden.
_CARRIER_EVENT_LABELS: dict[tuple[str, str], StrOrPromise] = {
    ("SHIPMENT", "DRFT"): _("Transport document drafted"),
    ("SHIPMENT", "ISSU"): _("Transport document issued"),
    ("SHIPMENT", "PENA"): _("Pending approval"),
    ("SHIPMENT", "RELS"): _("Released by carrier"),
    ("EQUIPMENT", "PICK"): _("Picked up"),
    ("EQUIPMENT", "DROP"): _("Dropped off"),
}


def describe_carrier_event(carrier_event_type: str, event_code: str) -> str:
    """Return a readable label for a carrier event code, or "" when there is none.

    Only codes actually observed in carrier responses are listed; an unrecognised
    code returns "" rather than a guess, and the caller falls back to showing the
    raw carrier type and code.
    """
    key = ((carrier_event_type or "").upper().strip(), (event_code or "").upper().strip())
    label = _CARRIER_EVENT_LABELS.get(key)
    return str(label) if label else ""
