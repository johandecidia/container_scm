"""Mapping a Vizion update payload onto the existing NormalisedTrackingEvent DTO.

Vizion publishes a ``journey_event`` object on every milestone, and it is DCSA::

    "journey_event": {"journey_type": "TRANSPORT", "event_classifier": "EST",
                      "event_type": "ARRI", "transport_mode": "VESSEL",
                      "facility_type": "POTE"}

``journey_type`` is the same SHIPMENT / EQUIPMENT / TRANSPORT partition the tracking
layer's :func:`~apps.scm.tracking.statuses.normalize_dcsa_event_type` keys on, and
``event_classifier`` is the same ACT / EST / PLN
:class:`~apps.scm.integrations.carriers.dcsa.schemas.DcsaEventClassifier`. So the codes
go through unchanged and the existing normalisers classify them. There is no Vizion
mapping table in this module at all: a second one would be a second thing to keep in
step with the carriers, and it would be translating DCSA into DCSA.

What one Vizion milestone carries::

    {"id": "…", "timestamp": "2023-01-04T01:45:00.000+00:00",
     "description": "Loaded on vessel at origin port", "raw_description": "Loaded",
     "vessel": "CT ROTTERDAM", "vessel_imo": "9395575", "vessel_mmsi": "209836000",
     "voyage": "EB1230103", "planned": false, "mode": "Vessel", "source": "carrier",
     "journey_event": {…}, "shipment_location": {"type_code": "POL"},
     "location": {"name": "Ensenada, Mexico", "city": "Ensenada", "state": "…",
                  "country": "Mexico", "unlocode": "MXESE", "facility": null,
                  "geolocation": {"latitude": 31.86, "longitude": -116.59}}}

Five consequences of that shape, each deliberate.

*Timestamps are offset-aware, so nothing has to be guessed.* Unlike Traqo's naive local
strings, Vizion sends an offset. The instant is therefore exact, and none of the
timezone-provenance machinery Traqo needed applies. Where the location also publishes an
IANA ``timezone`` it is recorded as the event's zone; otherwise the offset itself is.

*No event identity is claimed.* Milestones carry an ``id``, and it is preserved in
``raw_data``, but it is **not** used as ``raw_event_id``. Vizion documents that the ETA
and the ATA occupy *the same milestone*: it flips ``planned: true → false`` and its
timestamp stops being a forecast. If that flip reuses the id, an id-keyed fingerprint
would silently rewrite the forecast out of existence; if it does not, an id-keyed
fingerprint duplicates the whole history on every poll. Neither is knowable from the
documentation, and the field-based fingerprint is safe under *both*: the flip becomes two
rows — an EST and an ACT — which is what DCSA models anyway, and which the canonical ETA
selector already reads correctly, because it suppresses a forecast once an actual arrival
exists. See ``README.md``.

*The classifier comes from DCSA first and the boolean second.* ``journey_event
.event_classifier`` distinguishes EST from PLN; ``planned`` cannot. The boolean is only
consulted when the DCSA object is absent, and an absent flag stays UNKNOWN rather than
being read as either — a forecast promoted to an observation would report a box as
arrived before it was.

*Provider-only facts stay in the raw payload.* ``vessel_mmsi``, ``source``,
``shipment_location.type_code``, ``raw_description`` and the location's ``city`` /
``state`` / ``country`` have no canonical field. They are preserved verbatim on the
event's ``raw_data`` rather than being forced into one that means something else.

*A place name is never promoted to a code.* An absent ``unlocode`` yields "", never a
guess derived from the name — the same rule the Traqo benchmark holds providers to.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from apps.scm.integrations.carriers.dcsa.schemas import DcsaEventClassifier, NormalisedTrackingEvent
from apps.scm.integrations.carriers.exceptions import CarrierInvalidResponseError

from . import PROVIDER_CODE

logger = logging.getLogger(__name__)

# Where the Vizion-only facts land in a stored event's raw_data. Prefixed so they cannot
# collide with a Vizion field: every key Vizion sends is a plain snake_case name.
PROVIDER_DETAIL_KEY = "_vizion"


def _text(source: dict, *keys: str) -> str:
    """Return the first non-empty value among ``keys``, as a stripped string."""
    for key in keys:
        value = source.get(key)
        if isinstance(value, (dict, list)):
            continue
        if value not in (None, "", [], {}):
            return str(value).strip()
    return ""


def _dict(source: dict, key: str) -> dict:
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def _parse_timestamp(value) -> datetime | None:
    """Parse a Vizion ISO-8601 timestamp into an aware UTC datetime, or None.

    Vizion sends an offset on every milestone timestamp, so there is nothing to infer.
    A value that arrives without one is read as UTC and logged: it is a departure from
    the documented contract, and silently shifting it by this server's zone would put a
    wrong instant on a canonical event.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError, TypeError:
        logger.warning("Vizion: could not parse timestamp %r", value)
        return None
    if parsed.tzinfo is None:
        logger.warning("Vizion: timestamp %r carried no offset; reading it as UTC.", value)
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timezone_name(milestone: dict, location: dict, event_datetime: datetime | None) -> str:
    """Return the zone to record for this event.

    The location's published IANA zone where there is one, because it says where the
    event happened rather than merely how the string was offset. Otherwise the offset
    Vizion sent, which is still an exact statement about the instant.
    """
    published = _text(location, "timezone")
    if published:
        return published
    raw = str(milestone.get("timestamp") or "").strip()
    if not raw or event_datetime is None:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError, TypeError:
        return ""
    return str(parsed.tzinfo or "")


def _classifier(milestone: dict, journey_event: dict) -> str:
    """Return the DCSA classifier for a Vizion milestone.

    ``journey_event.event_classifier`` is already DCSA and is used verbatim, so PLANNED
    survives as PLANNED instead of being flattened into ESTIMATED. Only when it is absent
    does the ``planned`` boolean stand in, and only for the two values it can express.
    """
    stated = _text(journey_event, "event_classifier").upper()
    if stated in (
        DcsaEventClassifier.ACTUAL,
        DcsaEventClassifier.ESTIMATED,
        DcsaEventClassifier.PLANNED,
        DcsaEventClassifier.REQUESTED,
    ):
        return stated
    if stated:
        logger.info("Vizion: unrecognised event_classifier %r — leaving the event unclassified.", stated)
        return ""

    planned = milestone.get("planned")
    if planned is True:
        return DcsaEventClassifier.ESTIMATED
    if planned is False:
        return DcsaEventClassifier.ACTUAL
    return ""


def _coordinate(geolocation: dict, *keys: str) -> str:
    """Return a coordinate as a string, or "" when Vizion did not state one.

    Returned as text because :class:`NormalisedTrackingEvent` carries coordinates as
    strings and the ingestion layer is the one place that converts them to Decimal —
    converting here would mean two places could disagree about a malformed value.
    """
    for key in keys:
        value = geolocation.get(key)
        if value in (None, ""):
            continue
        return str(value).strip()
    return ""


def _transport_mode(milestone: dict, journey_event: dict) -> str:
    """Return the transport mode, preferring the DCSA value over the display one.

    ``journey_event.transport_mode`` is DCSA (VESSEL, RAIL, TRUCK, BARGE) and maps
    exactly. ``mode`` is Vizion's display wording and includes Feeder and Trunk, which
    have no canonical counterpart and normalise to OTHER — the original string stays in
    the raw payload either way.
    """
    return _text(journey_event, "transport_mode") or _text(milestone, "mode")


def _provider_detail(milestone: dict, location: dict, journey_event: dict) -> dict:
    """Return the Vizion facts that have no canonical field, so none of them is lost.

    Every one of these is a real supply-chain fact Container SCM cannot currently
    represent. Keeping them here means the gap is recoverable — a later canonical change
    can re-read stored payloads rather than needing every container refetched.
    """
    shipment_location = _dict(milestone, "shipment_location")
    return {
        "milestone_id": _text(milestone, "id"),
        "source": _text(milestone, "source"),
        "raw_description": _text(milestone, "raw_description"),
        "planned": milestone.get("planned"),
        "mode": _text(milestone, "mode"),
        "vessel_mmsi": _text(milestone, "vessel_mmsi"),
        "shipment_location_type_code": _text(shipment_location, "type_code"),
        "journey_type": _text(journey_event, "journey_type"),
        "event_classifier": _text(journey_event, "event_classifier"),
        "empty_indicator": _text(journey_event, "empty_indicator"),
        "facility_type": _text(journey_event, "facility_type"),
        "document_type": _text(journey_event, "document_type"),
        "location_city": _text(location, "city"),
        "location_state": _text(location, "state"),
        "location_country": _text(location, "country"),
        "location_timezone": _text(location, "timezone"),
    }


def map_vizion_milestone(
    milestone: dict,
    *,
    container_number: str,
    bill_of_lading: str = "",
    booking_number: str = "",
) -> NormalisedTrackingEvent:
    """Map one Vizion milestone, keeping everything the provider said about it."""
    journey_event = _dict(milestone, "journey_event")
    location = _dict(milestone, "location")
    geolocation = _dict(location, "geolocation")
    event_datetime = _parse_timestamp(milestone.get("timestamp"))

    return NormalisedTrackingEvent(
        # DCSA straight through: journey_type is the partition normalize_dcsa_event_type
        # keys on, and event_type is the code it keys on.
        event_type=_text(journey_event, "journey_type"),
        event_classifier=_classifier(milestone, journey_event),
        event_code=_text(journey_event, "event_type"),
        # Vizion's standardised wording, falling back to the carrier's own where it could
        # not normalise it, so an event is never left without a label it can be read by.
        description=_text(milestone, "description", "raw_description"),
        event_datetime=event_datetime,
        event_datetime_timezone=_timezone_name(milestone, location, event_datetime),
        location_name=_text(location, "name"),
        location_unlocode=_text(location, "unlocode"),
        facility_name=_text(location, "facility"),
        latitude=_coordinate(geolocation, "latitude", "lat"),
        longitude=_coordinate(geolocation, "longitude", "lon", "lng"),
        vessel_name=_text(milestone, "vessel"),
        vessel_imo=_text(milestone, "vessel_imo"),
        voyage_number=_text(milestone, "voyage"),
        transport_mode=_transport_mode(milestone, journey_event),
        container_number=container_number,
        bill_of_lading_number=bill_of_lading,
        booking_number=booking_number,
        # Empty on purpose. See the module docstring: the ETA and the ATA share a
        # milestone, so an id-keyed fingerprint is unsafe in both directions until a
        # refetch settles whether the id is stable.
        raw_event_id="",
        source_provider=PROVIDER_CODE,
        # The milestone verbatim, plus the Vizion-only facts gathered under one key so a
        # canonical gap can be closed later without refetching anything.
        raw_payload={**milestone, PROVIDER_DETAIL_KEY: _provider_detail(milestone, location, journey_event)},
    )


def map_vizion_update(update: dict, *, container_number: str = "") -> list[NormalisedTrackingEvent]:
    """Map one Vizion update envelope into normalised tracking events.

    ``container_number`` is what was asked about. It is passed in rather than taken from
    the response so that a bill-of-lading reference — whose payload names whichever
    container Vizion attached — cannot write events against the wrong box. It falls back
    to the payload's own ``container_id`` when the caller does not say.

    Every milestone is mapped. One that cannot be classified still arrives with its
    original type, code, description and payload intact and reaches TrackingEvent as
    UNKNOWN — a gap in our understanding must cost detail, not the event.

    Raises :class:`CarrierInvalidResponseError` when the envelope has no payload object
    at all; an empty ``milestones`` list is a valid answer and returns [].
    """
    if not isinstance(update, dict):
        raise CarrierInvalidResponseError("Vizion update was not a JSON object.", provider_code=PROVIDER_CODE)

    payload = update.get("payload") if isinstance(update.get("payload"), dict) else None
    if payload is None:
        raise CarrierInvalidResponseError("Vizion update has no payload object to map.", provider_code=PROVIDER_CODE)

    reference = (container_number or _text(payload, "container_id")).strip().upper()
    bill_of_lading = _text(payload, "bill_of_lading")
    booking_number = _text(payload, "booking_number")

    events: list[NormalisedTrackingEvent] = []
    for milestone in payload.get("milestones") or []:
        if not isinstance(milestone, dict):
            logger.warning("Vizion: skipping non-object milestone: %r", milestone)
            continue
        events.append(
            map_vizion_milestone(
                milestone,
                container_number=reference,
                bill_of_lading=bill_of_lading,
                booking_number=booking_number,
            )
        )
    return events


def map_vizion_updates(updates: list[dict], *, container_number: str = "") -> list[NormalisedTrackingEvent]:
    """Map every update envelope, oldest first.

    Order matters and is not incidental. Ingestion refreshes an event it already holds
    with the values it is given, so processing the newest update last is what makes the
    newest version of a milestone the one that survives. Feeding them newest-first would
    let a stale envelope overwrite a fresh one.

    Envelopes are sorted by ``created_at`` where Vizion states it, and otherwise left in
    the order Vizion returned them — the API documents newest-first for reference lists,
    but does not promise an order here, and inventing one from an unsorted list would be
    a guess with a silent failure mode.
    """
    ordered = sorted(
        (update for update in updates if isinstance(update, dict)),
        key=lambda update: str(update.get("created_at") or ""),
    )

    events: list[NormalisedTrackingEvent] = []
    for update in ordered:
        try:
            events.extend(map_vizion_update(update, container_number=container_number))
        except CarrierInvalidResponseError:
            # One malformed envelope must not lose the others: an update with no payload
            # is a real Vizion state (a webhook_failed row, for instance) and simply has
            # no milestones to contribute.
            logger.info("Vizion: update %s carries no payload; skipping it.", update.get("id"))
            continue
    return events


def read_stored_payload(payload_json: dict, reference: str) -> list[NormalisedTrackingEvent]:
    """Re-read a stored Vizion payload into normalised events.

    Registered in :mod:`apps.scm.tracking.sources` so the existing re-parse command works
    for Vizion with no Vizion-specific branch in it. It lives in the mapper rather than in
    the service because it needs nothing but mapping: the service imports the tracking
    layer's ETA module, and the tracking layer imports this hook, so putting it there
    would close an import cycle for no gain.
    """
    updates = payload_json.get("updates") if isinstance(payload_json, dict) else None
    return map_vizion_updates(updates or [], container_number=reference)


def read_latest_payload(updates: list[dict]) -> dict:
    """Return the payload object of the newest update, or {} when there is none.

    The shipment-level facts — origin and destination ports, inland origin and
    destination, container ISO, the carrier SCAC — live on the payload rather than on any
    milestone, and only the newest envelope's version of them is current.
    """
    ordered = sorted(
        (update for update in updates if isinstance(update, dict)),
        key=lambda update: str(update.get("created_at") or ""),
    )
    for update in reversed(ordered):
        payload = update.get("payload")
        if isinstance(payload, dict):
            return payload
    return {}
