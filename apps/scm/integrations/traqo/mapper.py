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

A production event carries more — including the ``location_id`` that resolves its
timezone, see :func:`_event_time` — but nothing fewer.

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

*Local naive timestamps.* Traqo sends ``"YYYY-MM-DD HH:MM:SS"`` with no offset, and the
production benchmark proved those are **local** times at the event's place, not UTC:
reading them as UTC put every Yantian event 8 h and every Gothenburg event 2 h away from
what Maersk reported for the same movement. They are converted through the timezone Traqo
publishes for the event's location — see :func:`_event_time`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

# Where the timestamp audit trail lands in a stored event's raw_data. Prefixed so it
# cannot collide with a Traqo field: every key Traqo sends is a plain snake_case name.
TIMESTAMP_AUDIT_KEY = "_timestamp_normalisation"


def _text(source: dict, keys: tuple[str, ...]) -> str:
    """Return the first non-empty value among ``keys``, as a stripped string."""
    for key in keys:
        value = source.get(key)
        if value not in (None, "", []):
            return str(value).strip()
    return ""


# Why an event's instant is what it is. Recorded on the row so a timestamp can be
# audited later without re-reading the whole response.
TZ_OFFSET_SUPPLIED = "offset_supplied"  # Traqo stated an offset; it is kept verbatim
TZ_FROM_LOCATION = "location_timezone"  # converted through locations_table
TZ_NO_LOCATION = "no_location_id"  # the event names no location
TZ_LOCATION_NOT_FOUND = "location_not_found"  # location_id absent from locations_table
TZ_NOT_PUBLISHED = "timezone_not_published"  # the location row states no timezone
TZ_UNKNOWN_ZONE = "timezone_not_recognised"  # the stated zone is not an IANA name
TZ_UNPARSEABLE = "timestamp_unparseable"  # the timestamp itself could not be read

# Statuses where the instant on the row is trustworthy.
_TZ_CONVERTED = (TZ_OFFSET_SUPPLIED, TZ_FROM_LOCATION)


@dataclass(frozen=True)
class _EventTime:
    """One event's instant, and the provenance of the zone used to reach it."""

    value: datetime | None
    timezone_name: str
    status: str
    raw: str

    @property
    def is_converted(self) -> bool:
        return self.status in _TZ_CONVERTED


def _parse_timestamp(value) -> tuple[datetime | None, bool]:
    """Parse a Traqo timestamp, returning (datetime, whether it stated an offset).

    Accepts the space-separated form Traqo sends, with or without fractional seconds,
    and an ISO-8601 string with an offset should Traqo ever send one.
    """
    if not value:
        return None, False
    text = str(value).strip()
    if not text:
        return None, False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError, TypeError:
        logger.debug("Traqo: could not parse timestamp %r", value)
        return None, False
    return parsed, parsed.tzinfo is not None


def _location_zone(event: dict, locations: dict[str, dict]) -> tuple[ZoneInfo | None, str, str]:
    """Return (zone, zone name, status) for the place Traqo puts this event at.

    Traqo publishes an IANA timezone per row of ``locations_table`` and each event
    points at one through ``location_id``. That is the only statement Traqo makes about
    what its naive timestamps mean, so it is the only thing used here.

    Nothing is inferred when it is missing: not from the country, not from the
    coordinates, not from another provider's event, not from this server's zone. A
    guessed zone is a wrong timestamp that looks authoritative.
    """
    location_id = event.get("location_id")
    if location_id is None:
        return None, "", TZ_NO_LOCATION

    location = locations.get(str(location_id))
    if location is None:
        logger.warning("Traqo: event names location_id %r, which locations_table does not list.", location_id)
        return None, "", TZ_LOCATION_NOT_FOUND

    name = str(location.get("timezone") or "").strip()
    if not name:
        return None, "", TZ_NOT_PUBLISHED

    try:
        return ZoneInfo(name), name, TZ_FROM_LOCATION
    except ZoneInfoNotFoundError, ValueError:
        logger.warning("Traqo: %r is not an IANA timezone (location_id %r).", name, location_id)
        return None, "", TZ_UNKNOWN_ZONE


def _event_time(event: dict, locations: dict[str, dict]) -> _EventTime:
    """Resolve one event's instant from a local timestamp and a published timezone.

    ``timestamp`` → ``location_id`` → ``locations_table.timezone`` → aware local time →
    UTC. Only ``locations_table`` is consulted: ``last_synced_at``, ``last_updated_at``
    and ``closed_at`` are Traqo's own infrastructure clock (they arrive at UTC+05:30)
    and say nothing about where the box was.

    **When the zone cannot be established** — a location row with ``timezone: null``,
    which the production benchmark hit for BORAAS — the instant is kept exactly as Traqo
    sent it, ``event_datetime_timezone`` is left empty, and the reason is recorded in the
    raw event. Three alternatives were rejected: guessing the zone from the country or
    the coordinates invents an offset; borrowing another provider's is enrichment; and
    dropping the timestamp hides the event from every timeline. So the row stays
    orderable and readable, claims no zone, and is findable precisely *because* its
    ``event_timezone`` is blank while every converted row names its zone.
    """
    raw = str(event.get("timestamp") or "").strip()
    parsed, has_offset = _parse_timestamp(event.get("timestamp"))
    if parsed is None:
        return _EventTime(None, "", TZ_UNPARSEABLE, raw)
    if has_offset:
        return _EventTime(parsed.astimezone(UTC), str(parsed.tzinfo or ""), TZ_OFFSET_SUPPLIED, raw)

    zone, zone_name, status = _location_zone(event, locations)
    if zone is None:
        # No zone claimed, and the instant is left as sent rather than shifted by a guess.
        return _EventTime(parsed.replace(tzinfo=UTC), "", status, raw)
    return _EventTime(parsed.replace(tzinfo=zone).astimezone(UTC), zone_name, status, raw)


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


def _index_locations(data: dict) -> dict[str, dict]:
    """Index ``locations_table`` by ``location_id`` so an event can be joined to it."""
    indexed: dict[str, dict] = {}
    for location in data.get("locations_table") or []:
        if isinstance(location, dict) and location.get("location_id") is not None:
            indexed[str(location["location_id"])] = location
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
    locations = _index_locations(data)

    events: list[NormalisedTrackingEvent] = []
    for raw in data.get("events_table") or []:
        if not isinstance(raw, dict):
            logger.warning("Traqo: skipping non-object event in events_table: %r", raw)
            continue
        events.append(_map_event(raw, reference=reference, vessels=vessels, locations=locations))
    return events


def _map_event(
    raw: dict, *, reference: str, vessels: dict[str, dict], locations: dict[str, dict]
) -> NormalisedTrackingEvent:
    """Map one Traqo event, keeping everything the provider said about it."""
    vessel_name, vessel_imo, voyage = _vessel(raw, vessels)
    event_time = _event_time(raw, locations)
    # Traqo's own short wording first; its longer status description stands in when
    # there is none, so an event is never left without a label it can be read by.
    description = str(raw.get("description") or raw.get("status_description") or "").strip()

    return NormalisedTrackingEvent(
        event_type=str(raw.get("event_type") or "").strip(),
        event_classifier=_classifier(raw),
        event_code=str(raw.get("event_code") or "").strip(),
        description=description,
        event_datetime=event_time.value,
        # The zone actually used, or empty when Traqo published none — never a guess.
        event_datetime_timezone=event_time.timezone_name,
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
        # so nothing Traqo said about it is lost and the mapping can be revisited —
        # plus how its local timestamp was read, which is not recoverable from the
        # event alone once the instant has been converted.
        raw_payload={**raw, TIMESTAMP_AUDIT_KEY: _timestamp_audit(event_time)},
    )


def _timestamp_audit(event_time: _EventTime) -> dict:
    """Return what was done to this event's timestamp, and on whose authority."""
    return {
        "provider_timestamp": event_time.raw,
        "timezone": event_time.timezone_name,
        "timezone_status": event_time.status,
        "converted": event_time.is_converted,
        "event_datetime_utc": event_time.value.isoformat() if event_time.value else "",
    }
