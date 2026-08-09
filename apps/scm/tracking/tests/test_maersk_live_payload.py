"""Regression tests against a real Maersk Track & Trace response.

The fixture ``maersk_public_events_response.json`` is a sanitised copy of an actual
response from ``/track-and-trace/public-events``. Booking, transport-document and
event identifiers were replaced with synthetic values; the structure was not touched.

It exists because the invented fixture we had was wrong in a way no test could catch:
it put location, vessel and voyage flat on the event, and Maersk puts them inside
``transportCall``. Every field extracted below therefore came back empty in
production while the whole suite passed. These tests assert against the shape the
carrier actually sends, all the way through to the stored TrackingEvent.

The container number is Maersk's own published test reference, already used as
``test_connection_reference`` in the integration config.
"""

import json
import pathlib

from django.test import TestCase

from apps.scm.integrations.carriers.dcsa.parser import DcsaParser
from apps.scm.integrations.carriers.maersk.parser import MaerskParser
from apps.scm.tracking.ingestion import persist_normalised_events
from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingSubscription
from apps.teams.models import Team

FIXTURE = (
    pathlib.Path(__file__).parents[2]
    / "integrations"
    / "tests"
    / "fixtures"
    / "carriers"
    / "maersk_public_events_response.json"
)

CONTAINER_NUMBER = "TRDU9258963"


def live_payload() -> dict:
    return json.loads(FIXTURE.read_text())


def by_code(events: list, code: str, classifier: str = ""):
    """Return the parsed event with this carrier code (and classifier, if given)."""
    for event in events:
        if event.event_code == code and (not classifier or event.event_classifier == classifier):
            return event
    raise AssertionError(f"No parsed event with code {code!r} {classifier!r}")


class MaerskLivePayloadParsingTest(TestCase):
    """The parser reads the nested structure Maersk actually sends."""

    @classmethod
    def setUpTestData(cls):
        cls.events = MaerskParser().parse_tracking_events(live_payload())

    def test_every_event_in_the_response_is_parsed(self):
        self.assertEqual(len(self.events), 10)

    def test_transport_event_reads_location_from_the_transport_call(self):
        arrival = by_code(self.events, "ARRI")
        self.assertEqual(arrival.location_name, "APM Terminals Gothenburg AB")
        self.assertEqual(arrival.location_unlocode, "SEGOT")

    def test_transport_event_reads_vessel_and_voyage_from_the_transport_call(self):
        arrival = by_code(self.events, "ARRI")
        self.assertEqual(arrival.vessel_name, "JEBEL ALI")
        self.assertEqual(arrival.vessel_imo, "9525936")
        self.assertEqual(arrival.voyage_number, "623W")

    def test_transport_event_reads_coordinates_from_the_nested_location(self):
        arrival = by_code(self.events, "ARRI")
        self.assertEqual(arrival.latitude, "57.697938")
        self.assertEqual(arrival.longitude, "11.856845")

    def test_transport_event_takes_its_container_from_the_references_array(self):
        """A TRANSPORT event has no equipmentReference of its own — only references[EQ]."""
        arrival = by_code(self.events, "ARRI")
        self.assertEqual(arrival.container_number, CONTAINER_NUMBER)

    def test_equipment_event_reads_location_and_mode(self):
        gate_out = by_code(self.events, "GTOT")
        self.assertEqual(gate_out.location_name, "Chuangyuan 6th Depot")
        self.assertEqual(gate_out.location_unlocode, "CNSHA")
        self.assertEqual(gate_out.transport_mode, "TRUCK")

    def test_equipment_event_keeps_its_flat_equipment_reference(self):
        self.assertEqual(by_code(self.events, "GTIN").container_number, CONTAINER_NUMBER)

    def test_document_references_supply_booking_and_bill_of_lading(self):
        gate_out = by_code(self.events, "GTOT")
        self.assertEqual(gate_out.booking_number, "BKG0000001")
        self.assertEqual(gate_out.bill_of_lading_number, "TRD0000002")

    def test_estimated_departure_is_classified_as_a_forecast(self):
        departure = by_code(self.events, "DEPA")
        self.assertEqual(departure.event_classifier, "EST")
        self.assertTrue(departure.is_estimated)
        self.assertFalse(departure.is_actual)

    def test_actual_arrival_is_classified_as_observed(self):
        self.assertTrue(by_code(self.events, "ARRI").is_actual)

    def test_shipment_events_carry_no_place_and_that_is_correct(self):
        """A document milestone has no transportCall, and must not invent one."""
        received = by_code(self.events, "RECE")
        self.assertEqual(received.location_name, "")
        self.assertEqual(received.location_unlocode, "")
        self.assertEqual(received.vessel_name, "")

    def test_every_event_has_a_carrier_event_id(self):
        """Without it, deduplication falls back to field hashing."""
        for event in self.events:
            self.assertTrue(event.raw_event_id, f"{event.event_code} has no event ID")

    def test_every_event_has_a_parsed_datetime(self):
        for event in self.events:
            self.assertIsNotNone(event.event_datetime, f"{event.event_code} has no datetime")

    def test_the_shared_dcsa_parser_handles_it_without_maersk_specific_code(self):
        """Maersk's payload is DCSA-conformant; the deviation was ours, not theirs."""
        shared = DcsaParser("maersk").parse(live_payload())
        self.assertEqual(len(shared), len(self.events))
        self.assertEqual(by_code(shared, "ARRI").location_unlocode, "SEGOT")


class MaerskLivePayloadPersistenceTest(TestCase):
    """Parsed events reach the database with their location and vessel intact."""

    def setUp(self):
        self.team = Team.objects.create(name="maersk-live", slug="maersk-live")
        self.provider = TrackingProvider.objects.create(code="maersk", name="Maersk")
        self.subscription = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.provider,
            tracking_reference=CONTAINER_NUMBER,
        )
        self.result = self._ingest()

    def _ingest(self) -> dict:
        return persist_normalised_events(
            team=self.team,
            provider=self.provider,
            events=MaerskParser().parse_tracking_events(live_payload()),
            subscription=self.subscription,
        )

    def _event(self, code: str) -> TrackingEvent:
        return TrackingEvent.objects.get(team=self.team, event_code=code)

    def test_all_events_are_stored(self):
        self.assertEqual(self.result, {"created": 10, "updated": 0, "failed": 0})

    def test_arrival_is_stored_with_place_vessel_and_voyage(self):
        arrival = self._event("ARRI")
        self.assertEqual(arrival.event_type, TrackingEvent.EventType.VESSEL_ARRIVED)
        self.assertEqual(arrival.location_name, "APM Terminals Gothenburg AB")
        self.assertEqual(arrival.location_unlocode, "SEGOT")
        self.assertEqual(arrival.vessel_name, "JEBEL ALI")
        self.assertEqual(arrival.vessel_imo, "9525936")
        self.assertEqual(arrival.voyage_number, "623W")
        self.assertTrue(arrival.is_actual)

    def test_coordinates_are_stored_as_decimals(self):
        arrival = self._event("ARRI")
        self.assertIsNotNone(arrival.location_latitude)
        self.assertIsNotNone(arrival.location_longitude)
        self.assertAlmostEqual(float(arrival.location_latitude), 57.697938, places=5)

    def test_transport_mode_is_normalised(self):
        self.assertEqual(self._event("GTOT").transport_mode, TrackingEvent.TransportMode.TRUCK)
        self.assertEqual(self._event("ARRI").transport_mode, TrackingEvent.TransportMode.VESSEL)

    def test_equipment_reference_is_stored_for_transport_events_too(self):
        self.assertEqual(self._event("ARRI").equipment_reference, CONTAINER_NUMBER)

    def test_estimated_departure_is_not_stored_as_actual(self):
        departure = self._event("DEPA")
        self.assertEqual(departure.event_type, TrackingEvent.EventType.VESSEL_DEPARTED)
        self.assertEqual(departure.event_time_type, TrackingEvent.EventTimeType.ESTIMATED)
        self.assertFalse(departure.is_actual)

    def test_known_codes_are_mapped_to_internal_event_types(self):
        expected = {
            "GTOT": TrackingEvent.EventType.GATE_OUT,
            "GTIN": TrackingEvent.EventType.GATE_IN,
            "ARRI": TrackingEvent.EventType.VESSEL_ARRIVED,
            "DEPA": TrackingEvent.EventType.VESSEL_DEPARTED,
            "RECE": TrackingEvent.EventType.BOOKING_CREATED,
        }
        for code, event_type in expected.items():
            with self.subTest(code=code):
                self.assertEqual(
                    TrackingEvent.objects.filter(team=self.team, event_code=code).first().event_type,
                    event_type,
                )

    def test_document_milestones_stay_unclassified_rather_than_being_guessed(self):
        """DRFT / ISSU / PENA / RELS describe paperwork, not a movement of the box."""
        for code in ("DRFT", "ISSU", "PENA", "RELS"):
            with self.subTest(code=code):
                event = self._event(code)
                self.assertEqual(event.event_type, TrackingEvent.EventType.UNKNOWN)
                self.assertTrue(event.is_unclassified)

    def test_unclassified_events_keep_what_the_carrier_said(self):
        drafted = self._event("DRFT")
        self.assertEqual(drafted.carrier_event_type, "SHIPMENT")
        self.assertEqual(drafted.event_code, "DRFT")
        self.assertEqual(drafted.carrier_reference, "SHIPMENT / DRFT")
        self.assertEqual(drafted.carrier_label, "Transport document drafted")

    def test_an_unlabelled_code_still_reports_its_carrier_classification(self):
        """A code with no label must fall back to the raw codes, never to nothing."""
        event = TrackingEvent(carrier_event_type="SHIPMENT", event_code="XXXX")
        self.assertEqual(event.carrier_label, "")
        self.assertEqual(event.carrier_reference, "SHIPMENT / XXXX")
        self.assertEqual(event.display_title, "Unknown event")

    def test_display_title_prefers_our_label_for_a_classified_event(self):
        self.assertEqual(self._event("ARRI").display_title, "Vessel Arrived")

    def test_display_title_never_shows_the_bare_word_unknown(self):
        """ "Unknown" reads as though the carrier said nothing — it did not."""
        for code in ("DRFT", "ISSU", "PENA", "RELS"):
            with self.subTest(code=code):
                self.assertNotEqual(self._event(code).display_title, "Unknown")

    def test_re_ingesting_the_same_payload_creates_no_duplicates(self):
        result = self._ingest()
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 10)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 10)
