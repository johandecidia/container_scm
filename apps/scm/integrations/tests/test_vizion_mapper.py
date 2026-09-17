"""Mapping tests: a Vizion milestone becomes a NormalisedTrackingEvent without loss.

The base payload is the synthetic transshipment journey described in
``fixtures/vizion/README.md`` — built from Vizion's published schema, not recorded from a
live account. So these tests prove the mapper reads the documented contract correctly and
loses nothing; only the live acceptance runs can prove the contract matches reality.

Where a test needs a shape the base payload does not carry — a milestone with no
``journey_event``, a timestamp with no offset, a location with no coordinates — it edits a
copy, so it is visible which parts of the mapping the ordinary payload exercises and which
are there so a stranger payload is not silently dropped.
"""

import copy
import json
import pathlib
from datetime import UTC, datetime

from django.test import SimpleTestCase

from apps.scm.integrations.carriers.dcsa.schemas import DcsaEventClassifier
from apps.scm.integrations.carriers.exceptions import CarrierInvalidResponseError
from apps.scm.integrations.vizion.mapper import (
    PROVIDER_DETAIL_KEY,
    map_vizion_milestone,
    map_vizion_update,
    map_vizion_updates,
    read_latest_payload,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "vizion"
CONTAINER_NUMBER = "BBCU3273070"


def updates() -> list[dict]:
    return json.loads((FIXTURES / "updates_transshipment.json").read_text())


def first_update() -> dict:
    return updates()[0]


def milestone_by_id(update: dict, milestone_id: str) -> dict:
    for milestone in update["payload"]["milestones"]:
        if milestone["id"] == milestone_id:
            return milestone
    raise AssertionError(f"No milestone {milestone_id} in the fixture.")


class VizionEventMappingTest(SimpleTestCase):
    """The documented milestone shape maps field for field."""

    def setUp(self):
        self.events = map_vizion_update(first_update(), container_number=CONTAINER_NUMBER)

    def test_every_milestone_is_mapped(self):
        self.assertEqual(len(self.events), 8)

    def test_a_gate_in_maps_dcsa_straight_through(self):
        event = self.events[0]

        # journey_type and event_type are already DCSA, so no Vizion mapping table exists
        # and the tracking layer's own normaliser classifies them.
        self.assertEqual(event.event_type, "EQUIPMENT")
        self.assertEqual(event.event_code, "GTIN")
        self.assertEqual(event.event_classifier, DcsaEventClassifier.ACTUAL)
        self.assertEqual(event.description, "Gate in at origin terminal")
        self.assertEqual(event.transport_mode, "TRUCK")
        self.assertEqual(event.container_number, CONTAINER_NUMBER)
        self.assertEqual(event.source_provider, "vizion")

    def test_an_offset_timestamp_becomes_an_exact_utc_instant(self):
        # 08:15 at +07:00 is 01:15Z. Nothing is guessed: Vizion states the offset.
        self.assertEqual(self.events[0].event_datetime, datetime(2026, 8, 1, 1, 15, tzinfo=UTC))

    def test_the_published_iana_zone_is_preferred_over_the_bare_offset(self):
        self.assertEqual(self.events[0].event_datetime_timezone, "Asia/Ho_Chi_Minh")

    def test_planned_and_estimated_are_kept_apart(self):
        eta = milestone_by_id(first_update(), "m-0007")
        discharge = milestone_by_id(first_update(), "m-0008")

        estimated = map_vizion_milestone(eta, container_number=CONTAINER_NUMBER)
        planned = map_vizion_milestone(discharge, container_number=CONTAINER_NUMBER)

        # Both have planned: true, so the boolean alone would flatten them into one
        # classifier. The DCSA field distinguishes them and is what the mapper reads.
        self.assertEqual(estimated.event_classifier, DcsaEventClassifier.ESTIMATED)
        self.assertEqual(planned.event_classifier, DcsaEventClassifier.PLANNED)

    def test_an_actual_event_is_actual_and_a_forecast_is_not(self):
        self.assertTrue(self.events[0].is_actual)
        self.assertFalse(self.events[0].is_estimated)

        eta = map_vizion_milestone(milestone_by_id(first_update(), "m-0007"), container_number=CONTAINER_NUMBER)
        self.assertTrue(eta.is_estimated)
        self.assertFalse(eta.is_actual)

    def test_the_container_number_asked_about_wins_over_the_payload(self):
        events = map_vizion_update(first_update(), container_number="TEMU1234567")

        self.assertTrue(all(event.container_number == "TEMU1234567" for event in events))

    def test_an_envelope_without_a_payload_is_refused(self):
        with self.assertRaises(CarrierInvalidResponseError):
            map_vizion_update({"id": "x", "status": "webhook_failed"}, container_number=CONTAINER_NUMBER)

    def test_an_empty_milestone_list_is_a_valid_answer(self):
        update = first_update()
        update["payload"]["milestones"] = []

        self.assertEqual(map_vizion_update(update, container_number=CONTAINER_NUMBER), [])


class VizionLocationMappingTest(SimpleTestCase):
    """UN/LOCODE and coordinates survive; a place name is never promoted to a code."""

    def setUp(self):
        self.events = map_vizion_update(first_update(), container_number=CONTAINER_NUMBER)

    def test_unlocode_and_coordinates_are_mapped(self):
        event = self.events[0]

        self.assertEqual(event.location_name, "Ho Chi Minh City, Vietnam")
        self.assertEqual(event.location_unlocode, "VNSGN")
        self.assertEqual(event.latitude, "10.7626")
        self.assertEqual(event.longitude, "106.6602")
        self.assertEqual(event.facility_name, "Cat Lai Terminal")

    def test_a_location_without_coordinates_yields_no_coordinates(self):
        # m-0006 has geolocation: null. Nothing is derived from the name.
        customs = next(event for event in self.events if event.description == "Customs released")

        self.assertEqual(customs.location_unlocode, "SGSIN")
        self.assertEqual(customs.latitude, "")
        self.assertEqual(customs.longitude, "")

    def test_a_missing_unlocode_is_not_invented_from_the_name(self):
        milestone = copy.deepcopy(milestone_by_id(first_update(), "m-0001"))
        milestone["location"].pop("unlocode")

        event = map_vizion_milestone(milestone, container_number=CONTAINER_NUMBER)

        self.assertEqual(event.location_name, "Ho Chi Minh City, Vietnam")
        self.assertEqual(event.location_unlocode, "")

    def test_city_state_and_country_are_preserved_where_canonical_has_no_field(self):
        detail = self.events[0].raw_payload[PROVIDER_DETAIL_KEY]

        self.assertEqual(detail["location_city"], "Ho Chi Minh City")
        self.assertEqual(detail["location_country"], "Vietnam")


class VizionVesselMappingTest(SimpleTestCase):
    """Vessel, IMO and voyage reach the canonical fields; MMSI is preserved elsewhere."""

    def setUp(self):
        self.events = map_vizion_update(first_update(), container_number=CONTAINER_NUMBER)
        self.loaded = self.events[1]

    def test_vessel_imo_and_voyage_are_mapped(self):
        self.assertEqual(self.loaded.vessel_name, "ONE OLYMPUS")
        self.assertEqual(self.loaded.vessel_imo, "9868284")
        self.assertEqual(self.loaded.voyage_number, "047E")

    def test_mmsi_has_no_canonical_field_and_is_kept_in_the_raw_payload(self):
        self.assertEqual(self.loaded.raw_payload[PROVIDER_DETAIL_KEY]["vessel_mmsi"], "636020947")

    def test_a_truck_movement_is_not_given_the_voyages_vessel(self):
        gate_in = self.events[0]

        self.assertEqual(gate_in.vessel_name, "")
        self.assertEqual(gate_in.vessel_imo, "")
        self.assertEqual(gate_in.voyage_number, "")


class VizionTransshipmentTest(SimpleTestCase):
    """Two legs stay two legs: nothing is collapsed into one voyage."""

    def setUp(self):
        self.events = map_vizion_update(first_update(), container_number=CONTAINER_NUMBER)

    def test_both_voyages_survive(self):
        voyages = {event.voyage_number for event in self.events if event.voyage_number}

        self.assertEqual(voyages, {"047E", "112W"})

    def test_both_vessels_survive_with_their_own_imos(self):
        pairs = {(event.vessel_name, event.vessel_imo) for event in self.events if event.vessel_name}

        self.assertEqual(pairs, {("ONE OLYMPUS", "9868284"), ("ONE APUS", "9806079")})

    def test_the_transshipment_leg_keeps_its_own_vessel_on_each_side(self):
        arrival = next(event for event in self.events if event.description.endswith("transshipment port"))
        departure = next(event for event in self.events if event.description.startswith("Vessel departed from trans"))

        # Arrived on the first vessel, left on the second. Attributing one vessel to the
        # whole call would erase the transshipment.
        self.assertEqual(arrival.vessel_name, "ONE OLYMPUS")
        self.assertEqual(departure.vessel_name, "ONE APUS")

    def test_the_leg_label_is_preserved_even_though_canonical_cannot_express_it(self):
        arrival = next(event for event in self.events if event.description.endswith("transshipment port"))

        self.assertEqual(arrival.raw_payload[PROVIDER_DETAIL_KEY]["shipment_location_type_code"], "RTP")


class VizionUnusualPayloadTest(SimpleTestCase):
    """A milestone that departs from the documented shape still reaches canonical form."""

    def test_a_milestone_without_a_journey_event_falls_back_to_the_planned_boolean(self):
        # m-0006 carries no journey_event at all.
        customs = map_vizion_milestone(milestone_by_id(first_update(), "m-0006"), container_number=CONTAINER_NUMBER)

        self.assertEqual(customs.event_classifier, DcsaEventClassifier.ACTUAL)
        # No DCSA codes to classify by, so the event is unclassified — and keeps its
        # description and payload rather than being dropped.
        self.assertEqual(customs.event_type, "")
        self.assertEqual(customs.event_code, "")
        self.assertEqual(customs.description, "Customs released")

    def test_an_absent_planned_flag_leaves_the_event_unclassified(self):
        milestone = copy.deepcopy(milestone_by_id(first_update(), "m-0006"))
        milestone.pop("planned")

        event = map_vizion_milestone(milestone, container_number=CONTAINER_NUMBER)

        # Neither observed nor forecast — reading silence as either would report a box
        # as arrived before it was.
        self.assertEqual(event.event_classifier, "")

    def test_an_unparseable_timestamp_drops_the_instant_and_keeps_the_event(self):
        milestone = copy.deepcopy(milestone_by_id(first_update(), "m-0001"))
        milestone["timestamp"] = "not a date"

        event = map_vizion_milestone(milestone, container_number=CONTAINER_NUMBER)

        self.assertIsNone(event.event_datetime)
        self.assertEqual(event.event_code, "GTIN")

    def test_a_timestamp_without_an_offset_is_read_as_utc_not_as_local(self):
        milestone = copy.deepcopy(milestone_by_id(first_update(), "m-0001"))
        milestone["timestamp"] = "2026-08-01T08:15:00"

        event = map_vizion_milestone(milestone, container_number=CONTAINER_NUMBER)

        self.assertEqual(event.event_datetime, datetime(2026, 8, 1, 8, 15, tzinfo=UTC))

    def test_a_display_mode_with_no_dcsa_counterpart_is_kept_verbatim(self):
        milestone = copy.deepcopy(milestone_by_id(first_update(), "m-0004"))
        milestone["mode"] = "Feeder"
        milestone["journey_event"].pop("transport_mode")

        event = map_vizion_milestone(milestone, container_number=CONTAINER_NUMBER)

        self.assertEqual(event.transport_mode, "Feeder")

    def test_a_non_object_milestone_is_skipped_rather_than_fatal(self):
        update = first_update()
        update["payload"]["milestones"].append("nonsense")

        events = map_vizion_update(update, container_number=CONTAINER_NUMBER)

        self.assertEqual(len(events), 8)


class VizionMultiUpdateTest(SimpleTestCase):
    """Several update envelopes are read oldest-first so the newest version wins."""

    def test_updates_are_ordered_oldest_first(self):
        # Fed newest-first on purpose: the mapper must reorder them, because ingestion
        # refreshes an event with whatever it is given last.
        events = map_vizion_updates(list(reversed(updates())), container_number=CONTAINER_NUMBER)

        etas = [event for event in events if event.event_classifier == DcsaEventClassifier.ESTIMATED]
        self.assertEqual(etas[0].event_datetime, datetime(2026, 9, 10, 5, 0, tzinfo=UTC))
        self.assertEqual(etas[-1].event_datetime, datetime(2026, 9, 12, 7, 30, tzinfo=UTC))

    def test_every_update_contributes_its_milestones(self):
        events = map_vizion_updates(updates(), container_number=CONTAINER_NUMBER)

        self.assertEqual(len(events), 10)

    def test_an_update_with_no_payload_does_not_lose_the_others(self):
        payloads = [*updates(), {"id": "z", "status": "webhook_failed", "created_at": "2026-08-29T00:00:00.000Z"}]

        events = map_vizion_updates(payloads, container_number=CONTAINER_NUMBER)

        self.assertEqual(len(events), 10)

    def test_the_latest_payload_is_the_newest_envelopes(self):
        payload = read_latest_payload(list(reversed(updates())))

        self.assertEqual(len(payload["milestones"]), 2)

    def test_the_latest_payload_of_nothing_is_empty(self):
        self.assertEqual(read_latest_payload([]), {})
