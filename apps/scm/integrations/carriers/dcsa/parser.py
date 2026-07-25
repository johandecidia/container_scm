# DCSA payload parser — normalises DCSA Track & Trace API responses.
# Carriers that follow the DCSA standard (Maersk, CMA CGM, Hapag-Lloyd, etc.)
# can use or extend this parser.
import logging
from datetime import datetime

from .schemas import NormalisedTrackingEvent

logger = logging.getLogger(__name__)


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


def _location(event: dict) -> dict:
    """Return the location sub-document of a DCSA event."""
    return event.get("location") or event.get("eventLocation") or {}


def _extract_location(event: dict) -> tuple[str, str, str]:
    """Return (location_name, location_unlocode, facility_name) from a DCSA event."""
    location = _location(event)
    name = location.get("locationName") or location.get("facilityName") or ""
    unlocode = location.get("UNLocationCode") or location.get("unLocationCode") or ""
    facility = location.get("facilityName") or ""
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
    """Return (vessel_name, vessel_imo, voyage_number) from a DCSA event dict."""
    vessel = event.get("vessel") or {}
    name = vessel.get("vesselName") or vessel.get("name") or ""
    imo = vessel.get("vesselIMONumber") or vessel.get("imoNumber") or ""
    voyage = event.get("exportVoyageNumber") or event.get("importVoyageNumber") or event.get("voyageNumber") or ""
    return name, str(imo or ""), voyage


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

        location_name, unlocode, facility_name = _extract_location(raw)
        latitude, longitude = _extract_coordinates(raw)
        vessel_name, vessel_imo, voyage = _extract_vessel(raw)
        transport_mode = raw.get("modeOfTransport") or raw.get("transportMode") or ""
        description = raw.get("description") or raw.get("eventDescription") or raw.get("statusName") or ""

        # Equipment / container reference
        equipment = raw.get("equipmentReference") or raw.get("containerNumber") or ""
        booking = raw.get("carrierBookingReference") or raw.get("bookingNumber") or ""
        bl = raw.get("transportDocumentReference") or raw.get("billOfLadingNumber") or ""
        po = raw.get("purchaseOrderReference") or raw.get("purchaseOrderNumber") or ""

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
