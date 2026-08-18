"""Mapping tests: a Traqo event becomes a NormalisedTrackingEvent without loss.

The base payload is the captured sandbox response. Where a test needs a field the
sandbox does not carry — a UN/LOCODE, coordinates, a vessel — it adds it to a copy, so
it is visible exactly which parts of the mapping today's Traqo payloads exercise and
which are there so a richer production payload is not silently dropped.
"""

import copy
import json
import pathlib
from datetime import UTC, datetime

from django.test import SimpleTestCase

from apps.scm.integrations.carriers.dcsa.schemas import DcsaEventClassifier
from apps.scm.integrations.carriers.exceptions import CarrierInvalidResponseError
from apps.scm.integrations.traqo.mapper import map_traqo_container_payload

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "traqo"
CONTAINER_NUMBER = "MRSU6859427"


def sandbox_payload() -> dict:
    return json.loads((FIXTURES / "sandbox_container_MRSU6859427.json").read_text())


def payload_with_events(*events) -> dict:
    payload = sandbox_payload()
    payload["data"]["events_table"] = list(events)
    return payload


class TraqoSandboxMappingTest(SimpleTestCase):
    """The real sandbox response maps event for event."""

    def setUp(self):
        self.events = map_traqo_container_payload(sandbox_payload(), container_number=CONTAINER_NUMBER)

    def test_every_traqo_event_is_mapped(self):
        self.assertEqual(len(self.events), 3)

    def test_a_gate_in_event_maps_field_for_field(self):
        event = self.events[0]

        self.assertEqual(event.event_type, "EQUIPMENT")
        self.assertEqual(event.event_code, "GTIN")
        self.assertEqual(event.event_classifier, DcsaEventClassifier.ACTUAL)
        self.assertEqual(event.description, "Gate in full")
        self.assertEqual(event.location_name, "Mundra")
        self.assertEqual(event.transport_mode, "TRUCK")
        self.assertEqual(event.container_number, CONTAINER_NUMBER)
        self.assertEqual(event.event_datetime, datetime(2026, 3, 1, tzinfo=UTC))
        self.assertEqual(event.source_provider, "traqo")

    def test_an_actual_event_stays_actual(self):
        loaded = self.events[1]

        self.assertEqual(loaded.event_code, "LOAD")
        self.assertTrue(loaded.is_actual)
        self.assertFalse(loaded.is_estimated)

    def test_a_forecast_arrival_is_not_treated_as_an_observation(self):
        arrival = self.events[2]

        self.assertEqual(arrival.event_code, "ARRI")
        self.assertEqual(arrival.event_classifier, DcsaEventClassifier.ESTIMATED)
        self.assertFalse(arrival.is_actual)
        self.assertTrue(arrival.is_estimated)

    def test_naive_traqo_timestamps_become_aware(self):
        for event in self.events:
            self.assertIsNotNone(event.event_datetime.tzinfo)

    def test_no_timezone_is_claimed_that_traqo_did_not_state(self):
        for event in self.events:
            self.assertEqual(event.event_datetime_timezone, "")

    def test_the_original_event_payload_is_kept_verbatim(self):
        raw = self.events[0].raw_payload

        # Including the fields the DTO has no home for, so nothing Traqo said is lost.
        self.assertEqual(raw["idx"], 1)
        self.assertEqual(raw["country"], "India")
        self.assertEqual(raw["status"], "CGI")
        self.assertEqual(raw["status_description"], "Container arrival at first POL (Gate in)")

    def test_no_source_event_id_is_invented_from_the_list_position(self):
        # ``idx`` is a position, not an identity — using it would let a later inserted
        # event overwrite an earlier one.
        for event in self.events:
            self.assertEqual(event.raw_event_id, "")

    def test_sandbox_events_carry_no_unlocode_vessel_or_coordinates(self):
        # Documents the gap rather than hiding it: Traqo gives a place name only.
        for event in self.events:
            self.assertEqual(event.location_unlocode, "")
            self.assertEqual(event.vessel_name, "")
            self.assertEqual(event.vessel_imo, "")
            self.assertEqual(event.voyage_number, "")
            self.assertEqual(event.latitude, "")
            self.assertEqual(event.longitude, "")


class TraqoRicherEventMappingTest(SimpleTestCase):
    """Fields Traqo may carry in production reach the DTO instead of being dropped."""

    def test_a_unlocode_on_the_event_is_mapped(self):
        payload = payload_with_events(
            {
                "idx": 1,
                "location": "Rotterdam",
                "unlocode": "NLRTM",
                "description": "Discharged",
                "timestamp": "2026-04-02 08:15:00",
                "event_type": "EQUIPMENT",
                "event_code": "DISC",
                "transport_type": "VESSEL",
                "is_actual": 1,
            }
        )

        event = map_traqo_container_payload(payload, container_number=CONTAINER_NUMBER)[0]

        self.assertEqual(event.location_unlocode, "NLRTM")
        self.assertEqual(event.location_name, "Rotterdam")

    def test_coordinates_on_the_event_are_mapped_as_strings(self):
        payload = payload_with_events(
            {
                "idx": 1,
                "location": "Singapore",
                "latitude": "1.264",
                "longitude": "103.84",
                "timestamp": "2026-04-05 11:00:00",
                "event_type": "TRANSPORT",
                "event_code": "ARRI",
                "is_actual": 1,
            }
        )

        event = map_traqo_container_payload(payload, container_number=CONTAINER_NUMBER)[0]

        self.assertEqual(event.latitude, "1.264")
        self.assertEqual(event.longitude, "103.84")

    def test_a_vessel_named_on_the_event_is_mapped(self):
        payload = payload_with_events(
            {
                "idx": 1,
                "location": "Mundra",
                "timestamp": "2026-03-03 00:00:00",
                "event_type": "TRANSPORT",
                "event_code": "DEPA",
                "transport_type": "VESSEL",
                "is_actual": 1,
                "vessel": "MAERSK KOWLOON",
                "imo": "9784271",
                "voyage": "512W",
            }
        )

        event = map_traqo_container_payload(payload, container_number=CONTAINER_NUMBER)[0]

        self.assertEqual(event.vessel_name, "MAERSK KOWLOON")
        self.assertEqual(event.vessel_imo, "9784271")
        self.assertEqual(event.voyage_number, "512W")

    def test_a_vessel_id_is_resolved_against_the_vessels_table(self):
        payload = payload_with_events(
            {
                "idx": 1,
                "location": "Mundra",
                "timestamp": "2026-03-03 00:00:00",
                "event_type": "TRANSPORT",
                "event_code": "DEPA",
                "transport_type": "VESSEL",
                "is_actual": 1,
                "vessel_id": 1,
            }
        )

        event = map_traqo_container_payload(payload, container_number=CONTAINER_NUMBER)[0]

        # vessels_table in the sandbox fixture: ASTRID MAERSK, IMO 9948750.
        self.assertEqual(event.vessel_name, "ASTRID MAERSK")
        self.assertEqual(event.vessel_imo, "9948750")

    def test_the_current_vessel_is_not_attached_to_events_that_do_not_name_one(self):
        # vessels_table describes the voyage, not the leg: hanging it on a truck
        # movement would claim the box was aboard a ship.
        events = map_traqo_container_payload(sandbox_payload(), container_number=CONTAINER_NUMBER)

        self.assertTrue(all(not event.vessel_name for event in events))


class TraqoUnknownAndMissingDataTest(SimpleTestCase):
    """An event we cannot interpret survives; missing optional data is tolerated."""

    def test_an_unknown_event_keeps_the_providers_own_code_and_wording(self):
        payload = payload_with_events(
            {
                "idx": 1,
                "location": "Antwerp",
                "description": "Customs inspection scheduled",
                "timestamp": "2026-04-10 09:00:00",
                "event_type": "SOMETHING_NEW",
                "event_code": "ZZZZ",
                "transport_type": "MULE",
                "is_actual": 1,
            }
        )

        event = map_traqo_container_payload(payload, container_number=CONTAINER_NUMBER)[0]

        self.assertEqual(event.event_type, "SOMETHING_NEW")
        self.assertEqual(event.event_code, "ZZZZ")
        self.assertEqual(event.description, "Customs inspection scheduled")
        self.assertEqual(event.raw_payload["event_code"], "ZZZZ")

    def test_an_event_without_an_is_actual_flag_is_neither_observed_nor_forecast(self):
        payload = payload_with_events(
            {"idx": 1, "location": "Mundra", "timestamp": "2026-03-01 00:00:00", "event_code": "GTIN"}
        )

        event = map_traqo_container_payload(payload, container_number=CONTAINER_NUMBER)[0]

        self.assertEqual(event.event_classifier, "")
        self.assertFalse(event.is_actual)
        self.assertFalse(event.is_estimated)

    def test_an_event_with_almost_nothing_still_maps(self):
        payload = payload_with_events({"idx": 1})

        events = map_traqo_container_payload(payload, container_number=CONTAINER_NUMBER)

        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].event_datetime)
        self.assertEqual(events[0].container_number, CONTAINER_NUMBER)

    def test_an_unparseable_timestamp_does_not_lose_the_event(self):
        payload = payload_with_events(
            {"idx": 1, "timestamp": "next tuesday", "event_type": "EQUIPMENT", "event_code": "GTIN"}
        )

        event = map_traqo_container_payload(payload, container_number=CONTAINER_NUMBER)[0]

        self.assertIsNone(event.event_datetime)
        self.assertEqual(event.raw_payload["timestamp"], "next tuesday")

    def test_a_timestamp_with_an_offset_keeps_the_offset_traqo_stated(self):
        payload = payload_with_events({"idx": 1, "timestamp": "2026-03-01T06:00:00+02:00", "event_code": "GTIN"})

        event = map_traqo_container_payload(payload, container_number=CONTAINER_NUMBER)[0]

        self.assertEqual(event.event_datetime, datetime(2026, 3, 1, 4, 0, tzinfo=UTC))

    def test_the_status_description_stands_in_when_there_is_no_description(self):
        payload = payload_with_events(
            {"idx": 1, "event_code": "GTIN", "status_description": "Container arrival at first POL (Gate in)"}
        )

        event = map_traqo_container_payload(payload, container_number=CONTAINER_NUMBER)[0]

        self.assertEqual(event.description, "Container arrival at first POL (Gate in)")

    def test_an_empty_events_table_is_a_valid_answer(self):
        self.assertEqual(map_traqo_container_payload(payload_with_events()), [])

    def test_a_non_object_event_is_skipped_without_losing_the_rest(self):
        payload = sandbox_payload()
        payload["data"]["events_table"].insert(1, "not an event")

        events = map_traqo_container_payload(payload, container_number=CONTAINER_NUMBER)

        self.assertEqual(len(events), 3)

    def test_a_payload_without_a_shipment_object_is_rejected(self):
        for payload in ({}, {"success": True}, {"data": []}):
            with self.assertRaises(CarrierInvalidResponseError):
                map_traqo_container_payload(payload)


class TraqoReferenceIdentityTest(SimpleTestCase):
    """Which box the events belong to is the caller's question, not the payload's."""

    def test_the_reference_number_is_used_when_the_caller_names_nothing(self):
        events = map_traqo_container_payload(sandbox_payload())

        self.assertEqual(events[0].container_number, CONTAINER_NUMBER)

    def test_the_requested_container_wins_over_the_echoed_reference(self):
        # The BL endpoint echoes a document number in reference_number; letting that
        # become an equipment reference would attach events to the wrong thing.
        payload = copy.deepcopy(sandbox_payload())
        payload["data"]["reference_number"] = "MAEU123456789"

        events = map_traqo_container_payload(payload, container_number=CONTAINER_NUMBER)

        self.assertEqual(events[0].container_number, CONTAINER_NUMBER)
