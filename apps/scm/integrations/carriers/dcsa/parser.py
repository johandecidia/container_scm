"""DCSA payload parser — normalises DCSA Track & Trace API responses.

Carriers that follow the DCSA standard (Maersk, CMA CGM, Hapag-Lloyd, etc.) can use
or extend this parser.

DCSA carries the interesting detail one level down, not on the event itself:

``transportCall``
    Where the event happened and what was carrying the box — ``location``,
    ``UNLocationCode``, ``vessel``, the voyage numbers and ``modeOfTransport``. Only
    SHIPMENT events lack it, because a document milestone has no place or vessel.

``references``
    The equipment, booking and document numbers an event relates to, as
    ``{referenceType, referenceValue}`` pairs. TRANSPORT events name their container
    here; EQUIPMENT events also carry it flat as ``equipmentReference``.

``documentReferences``
    Booking (``BKG``) and transport document (``TRD``) numbers, as
    ``{documentReferenceType, documentReferenceValue}`` pairs.

Every extractor still accepts the flat spelling first. Some carriers put location and
vessel directly on the event, and dropping that path would break them; reading the
nested form is an addition, not a replacement.
"""

import logging
from datetime import datetime

from .schemas import NormalisedTrackingEvent

logger = logging.getLogger(__name__)

# DCSA reference types (``references[].referenceType``).
_REFERENCE_EQUIPMENT = "EQ"
_REFERENCE_BOOKING = "BKG"
_REFERENCE_TRANSPORT_DOCUMENT = "TRD"
_REFERENCE_PURCHASE_ORDER = "PO"

# DCSA document reference types (``documentReferences[].documentReferenceType``).
_DOCUMENT_BOOKING = "BKG"
_DOCUMENT_TRANSPORT_DOCUMENT = "TRD"


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string to an aware datetime, or return None."""
    if not value:
        return None
    try:
        # Handle trailing Z (UTC) which Python < 3.11 doesn't parse natively.
        normalised = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalised)
    except ValueError, TypeError:
        logger.debug("Could not parse datetime value: %r", value)
        return None


def _sub_document(event: dict, key: str) -> dict:
    """Return ``event[key]`` when it is an object, else an empty dict."""
    value = event.get(key)
    return value if isinstance(value, dict) else {}


def _transport_call(event: dict) -> dict:
    """Return the event's transportCall, which holds place and carriage."""
    return _sub_document(event, "transportCall")


def _location(event: dict) -> dict:
    """Return the location sub-document of a DCSA event.

    Flat first, then inside transportCall — that is where every real DCSA transport
    and equipment event puts it.
    """
    flat = _sub_document(event, "location") or _sub_document(event, "eventLocation")
    return flat or _sub_document(_transport_call(event), "location")


def _extract_location(event: dict) -> tuple[str, str, str]:
    """Return (location_name, location_unlocode, facility_name) from a DCSA event.

    The UN/LOCODE sits on the transportCall rather than on the location object it
    describes, so it is looked up in both places.
    """
    location = _location(event)
    call = _transport_call(event)
    name = location.get("locationName") or location.get("facilityName") or ""
    unlocode = (
        location.get("UNLocationCode")
        or location.get("unLocationCode")
        or call.get("UNLocationCode")
        or call.get("unLocationCode")
        or ""
    )
    facility = location.get("facilityName") or call.get("otherFacility") or ""
    return name, unlocode, facility


def _extract_coordinates(event: dict) -> tuple[str, str]:
    """Return (latitude, longitude) as raw strings from a DCSA event, if present.

    DCSA carries coordinates as strings; they are kept as strings here and only
    converted when persisted, so an unparseable value never loses the original.
    """
    location = _location(event)
    latitude = location.get("latitude") or event.get("latitude") or ""
    longitude = location.get("longitude") or event.get("longitude") or ""
    return str(latitude or ""), str(longitude or "")


def _extract_vessel(event: dict) -> tuple[str, str, str]:
    """Return (vessel_name, vessel_imo, voyage_number) from a DCSA event dict.

    ``carrierVoyageNumber`` is the last resort: export and import voyage numbers say
    which leg the event belongs to, and a carrier that sends only the generic one is
    still telling us the voyage.
    """
    call = _transport_call(event)
    vessel = _sub_document(event, "vessel") or _sub_document(call, "vessel")
    name = vessel.get("vesselName") or vessel.get("name") or ""
    imo = vessel.get("vesselIMONumber") or vessel.get("imoNumber") or ""
    voyage = _first_present(
        event,
        call,
        keys=("exportVoyageNumber", "importVoyageNumber", "voyageNumber", "carrierVoyageNumber"),
    )
    return name, str(imo or ""), voyage


def _first_present(*sources: dict, keys: tuple[str, ...]) -> str:
    """Return the first non-empty value for any of ``keys``, source by source."""
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value:
                return str(value)
    return ""


def _reference_value(event: dict, reference_type: str) -> str:
    """Return the first ``references`` entry of ``reference_type``, or "".

    TRANSPORT events name their container only here — an event with no
    ``equipmentReference`` of its own is still about a specific box.
    """
    for entry in event.get("references") or []:
        if isinstance(entry, dict) and entry.get("referenceType") == reference_type:
            value = entry.get("referenceValue")
            if value:
                return str(value)
    return ""


def _document_reference_value(event: dict, document_type: str) -> str:
    """Return the first ``documentReferences`` entry of ``document_type``, or ""."""
    for entry in event.get("documentReferences") or []:
        if isinstance(entry, dict) and entry.get("documentReferenceType") == document_type:
            value = entry.get("documentReferenceValue")
            if value:
                return str(value)
    return ""


class DcsaParser:
    """Parses a DCSA-standard tracking payload into NormalisedTrackingEvent objects.

    Usage:
        parser = DcsaParser(source_provider="maersk")
        events = parser.parse(payload)
    """

    def __init__(self, source_provider: str = "") -> None:
        self.source_provider = source_provider

    def parse(self, payload: dict | list) -> list[NormalisedTrackingEvent]:
        """Parse a raw DCSA payload and return a list of NormalisedTrackingEvent.

        Accepts either a dict (with an "events" key) or a list of event dicts.
        Returns an empty list if the payload is empty or malformed.
        """
        if not payload:
            return []

        # DCSA responses may be {"events": [...]} or directly a list.
        if isinstance(payload, dict):
            events_raw = payload.get("events") or payload.get("trackingData") or []
        else:
            events_raw = payload

        results: list[NormalisedTrackingEvent] = []
        for raw_event in events_raw:
            try:
                event = self._parse_event(raw_event)
                if event:
                    results.append(event)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "DcsaParser: failed to parse event for provider=%s: %r",
                    self.source_provider,
                    raw_event,
                    exc_info=True,
                )
        return results

    def _parse_event(self, raw: dict) -> NormalisedTrackingEvent | None:
        if not isinstance(raw, dict):
            return None

        event_type = raw.get("eventType") or raw.get("eventClassifierCode") or ""
        classifier = raw.get("eventClassifierCode") or ""
        event_code = (
            raw.get("shipmentEventTypeCode")
            or raw.get("equipmentEventTypeCode")
            or raw.get("transportEventTypeCode")
            or raw.get("eventTypeCode")
            or ""
        )

        dt_str = raw.get("eventDateTime") or raw.get("eventCreatedDateTime") or ""
        tz_str = raw.get("eventDateTimeTimezone") or ""
        event_dt = _parse_datetime(dt_str)

        call = _transport_call(raw)
        location_name, unlocode, facility_name = _extract_location(raw)
        latitude, longitude = _extract_coordinates(raw)
        vessel_name, vessel_imo, voyage = _extract_vessel(raw)
        transport_mode = _first_present(raw, call, keys=("modeOfTransport", "transportMode"))
        description = raw.get("description") or raw.get("eventDescription") or raw.get("statusName") or ""

        # References. Each is looked for flat, then in the DCSA reference arrays, so
        # an event that only names its container in ``references`` is still tied to it.
        equipment = _first_present(raw, keys=("equipmentReference", "containerNumber")) or _reference_value(
            raw, _REFERENCE_EQUIPMENT
        )
        booking = (
            _first_present(raw, keys=("carrierBookingReference", "bookingNumber"))
            or _document_reference_value(raw, _DOCUMENT_BOOKING)
            or _reference_value(raw, _REFERENCE_BOOKING)
        )
        bl = (
            _first_present(raw, keys=("transportDocumentReference", "billOfLadingNumber"))
            or _document_reference_value(raw, _DOCUMENT_TRANSPORT_DOCUMENT)
            or _reference_value(raw, _REFERENCE_TRANSPORT_DOCUMENT)
        )
        po = _first_present(raw, keys=("purchaseOrderReference", "purchaseOrderNumber")) or _reference_value(
            raw, _REFERENCE_PURCHASE_ORDER
        )

        raw_event_id = raw.get("eventID") or raw.get("eventId") or raw.get("trackingEventID") or ""

        return NormalisedTrackingEvent(
            event_type=event_type,
            event_classifier=classifier,
            event_code=event_code,
            description=description,
            event_datetime=event_dt,
            event_datetime_timezone=tz_str,
            location_name=location_name,
            location_unlocode=unlocode,
            facility_name=facility_name,
            latitude=latitude,
            longitude=longitude,
            vessel_name=vessel_name,
            vessel_imo=vessel_imo,
            voyage_number=voyage,
            transport_mode=transport_mode,
            container_number=equipment,
            booking_number=booking,
            bill_of_lading_number=bl,
            purchase_order_number=po,
            raw_event_id=raw_event_id,
            source_provider=self.source_provider,
            raw_payload=raw,
        )
