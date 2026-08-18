"""Mapping a Traqo container response onto the existing NormalisedTrackingEvent DTO.

Traqo's ``events_table`` is already DCSA-shaped — ``event_type`` is EQUIPMENT or
TRANSPORT and ``event_code`` is GTIN, LOAD, DEPA, ARRI — so the codes go through
unchanged and the tracking layer's own
:func:`~apps.scm.tracking.statuses.normalize_dcsa_event_type` classifies them. There
is no Traqo mapping table here at all: a second one would be a second thing to keep
in step with the carriers.

What one Traqo event actually carries, read from live sandbox responses::

    {"idx": 1, "location": "Mundra", "country": "India",
     "description": "Gate in full", "timestamp": "2026-03-01 00:00:00",
     "event_type": "EQUIPMENT", "event_code": "GTIN", "transport_type": "TRUCK",
     "is_actual": 1, "status": "CGI",
     "status_description": "Container arrival at first POL (Gate in)"}

Four consequences of that shape, each deliberate:

*No stable event ID.* ``idx`` is a position in the list, not an identity — a later
event inserted mid-history shifts every idx after it. Using it as ``raw_event_id``
would make the ingestion layer overwrite one event with another, which is worse than
the duplicate it would prevent. So it is left empty and the existing field-based
fingerprint identifies the event; ``idx`` stays in the raw payload.

*Actual or forecast, and nothing finer.* ``is_actual`` is a boolean, so 1 becomes
ACTUAL and 0 becomes ESTIMATED. An absent flag stays UNKNOWN rather than being read
as either — a forecast promoted to an observation would report a box as arrived
before it was.

*No UN/LOCODE, coordinates or vessel on the event.* Sandbox events carry a place name
only. The extractors below still look for those fields, so a production payload that
does carry them is not silently dropped; on today's payloads they simply yield
nothing, and the shipment-level position and ``vessels_table`` stay in
TrackingRawPayload for later.

*Naive timestamps.* Traqo sends ``"YYYY-MM-DD HH:MM:SS"`` with no offset, so they are
read as UTC and ``event_datetime_timezone`` is left empty rather than claiming a zone
Traqo did not state. Confirming that assumption needs production data — see README.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from apps.scm.integrations.carriers.dcsa.schemas import DcsaEventClassifier, NormalisedTrackingEvent
from apps.scm.integrations.carriers.exceptions import CarrierInvalidResponseError

from . import PROVIDER_CODE

logger = logging.getLogger(__name__)

# Keys a Traqo event may carry for a UN/LOCODE, coordinates, vessel and voyage. None
# appears in a sandbox response; they are read so that a production payload carrying
# them reaches TrackingEvent instead of being lost. An absent key yields "" — nothing
# here infers a value from another field.
_UNLOCODE_KEYS = ("unlocode", "un_locode", "location_unlocode", "location_code", "port_code")
_LATITUDE_KEYS = ("latitude", "lat")
_LONGITUDE_KEYS = ("longitude", "lng", "lon")
_VESSEL_NAME_KEYS = ("vessel", "vessel_name")
_VESSEL_IMO_KEYS = ("imo", "vessel_imo", "imo_number")
_VOYAGE_KEYS = ("voyage", "voyage_number", "voyage_no")
_FACILITY_KEYS = ("facility", "facility_name", "terminal")


def _text(source: dict, keys: tuple[str, ...]) -> str:
    """Return the first non-empty value among ``keys``, as a stripped string."""
    for key in keys:
        value = source.get(key)
        if value not in (None, "", []):
            return str(value).strip()
    return ""


def _parse_timestamp(value) -> datetime | None:
    """Parse a Traqo timestamp into an aware UTC datetime, or None.

    Accepts the space-separated form Traqo sends, with or without fractional seconds,
    and an ISO-8601 string with an offset should Traqo ever send one — in which case
    the offset it states is kept.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError, TypeError:
        logger.debug("Traqo: could not parse timestamp %r", value)
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _classifier(event: dict) -> str:
    """Return the DCSA classifier for a Traqo event's ``is_actual`` flag.

    Absent or unrecognised leaves the classifier empty, which the tracking layer
    normalises to UNKNOWN. That is the honest answer: an event we cannot place as
    observed or forecast must not be counted as either.
    """
    flag = event.get("is_actual")
    if flag in (1, True, "1", "true", "True"):
        return DcsaEventClassifier.ACTUAL
    if flag in (0, False, "0", "false", "False"):
        return DcsaEventClassifier.ESTIMATED
    return ""


def _vessel(event: dict, vessels: dict[str, dict]) -> tuple[str, str, str]:
    """Return (vessel_name, imo, voyage) for an event, or empty strings.

    Two sources, both requiring Traqo to have named the vessel *for this event*: the
    fields on the event itself, and a ``vessel_id`` resolved against ``vessels_table``.

    What it deliberately does not do is attach the shipment's current vessel to every
    event. ``vessels_table`` describes the voyage, not the leg: hanging its entry on a
    truck gate-in would state that a box was aboard a ship when it was on a chassis.
    """
    source = event
    vessel_id = event.get("vessel_id")
    if vessel_id is not None and str(vessel_id) in vessels:
        source = {**vessels[str(vessel_id)], **event}

    return (
        _text(source, _VESSEL_NAME_KEYS),
        _text(source, _VESSEL_IMO_KEYS),
        _text(source, _VOYAGE_KEYS),
    )


def _index_vessels(data: dict) -> dict[str, dict]:
    """Index ``vessels_table`` by ``vessel_id`` so an event can be joined to it."""
    indexed: dict[str, dict] = {}
    for vessel in data.get("vessels_table") or []:
        if isinstance(vessel, dict) and vessel.get("vessel_id") is not None:
            indexed[str(vessel["vessel_id"])] = vessel
    return indexed


def map_traqo_container_payload(payload: dict, *, container_number: str = "") -> list[NormalisedTrackingEvent]:
    """Map a Traqo container response envelope into normalised tracking events.

    ``container_number`` is what was asked about. It is passed in rather than taken
    from the response because ``reference_number`` echoes whichever reference the
    endpoint was called with — a bill of lading on the BL endpoint — and putting a
    document number into an equipment reference would attach events to the wrong
    thing. It falls back to ``reference_number`` when the caller does not say.

    Every event Traqo lists is mapped. One that cannot be classified still arrives
    with its original type, code, description and payload intact and reaches
    TrackingEvent as UNKNOWN — a gap in our understanding must cost detail, not the
    event.

    Raises :class:`CarrierInvalidResponseError` when the envelope has no shipment
    object at all; an empty ``events_table`` is a valid answer and returns [].
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise CarrierInvalidResponseError(
            "Traqo payload has no shipment data object to map.",
            provider_code=PROVIDER_CODE,
        )

    reference = (container_number or str(data.get("reference_number") or "")).strip().upper()
    vessels = _index_vessels(data)

    events: list[NormalisedTrackingEvent] = []
    for raw in data.get("events_table") or []:
        if not isinstance(raw, dict):
            logger.warning("Traqo: skipping non-object event in events_table: %r", raw)
            continue
        events.append(_map_event(raw, reference=reference, vessels=vessels))
    return events


def _map_event(raw: dict, *, reference: str, vessels: dict[str, dict]) -> NormalisedTrackingEvent:
    """Map one Traqo event, keeping everything the provider said about it."""
    vessel_name, vessel_imo, voyage = _vessel(raw, vessels)
    # Traqo's own short wording first; its longer status description stands in when
    # there is none, so an event is never left without a label it can be read by.
    description = str(raw.get("description") or raw.get("status_description") or "").strip()

    return NormalisedTrackingEvent(
        event_type=str(raw.get("event_type") or "").strip(),
        event_classifier=_classifier(raw),
        event_code=str(raw.get("event_code") or "").strip(),
        description=description,
        event_datetime=_parse_timestamp(raw.get("timestamp")),
        # Traqo states no offset, so no zone is claimed here. See the module docstring.
        event_datetime_timezone="",
        location_name=str(raw.get("location") or "").strip(),
        location_unlocode=_text(raw, _UNLOCODE_KEYS),
        facility_name=_text(raw, _FACILITY_KEYS),
        latitude=_text(raw, _LATITUDE_KEYS),
        longitude=_text(raw, _LONGITUDE_KEYS),
        vessel_name=vessel_name,
        vessel_imo=vessel_imo,
        voyage_number=voyage,
        transport_mode=str(raw.get("transport_type") or "").strip(),
        container_number=reference,
        # Empty on purpose: Traqo's ``idx`` is a list position, not an identity. See
        # the module docstring.
        raw_event_id="",
        source_provider=PROVIDER_CODE,
        # The event verbatim — including idx, country, status and status_description,
        # so nothing Traqo said about it is lost and the mapping can be revisited.
        raw_payload=dict(raw),
    )
