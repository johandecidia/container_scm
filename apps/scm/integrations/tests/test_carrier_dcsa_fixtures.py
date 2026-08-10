"""DCSA parser tests with per-carrier fixture data.

Tests the DcsaParser against realistic response fixtures for each DCSA-compliant
carrier (Maersk, CMA CGM, Hapag-Lloyd).  Also verifies that raw payload data is
preserved unchanged and that non-DCSA fixtures are stored as raw payloads without
parsing errors.
"""

import json
import pathlib
import unittest

from apps.scm.integrations.carriers.dcsa.parser import DcsaParser
from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "carriers"


def load_fixture(filename: str) -> dict:
    return json.loads((FIXTURES_DIR / filename).read_text())


# ---------------------------------------------------------------------------
# Maersk (DCSA)
# ---------------------------------------------------------------------------


class MaerskDcsaParserTest(unittest.TestCase):
    """DcsaParser correctly parses a Maersk-format DCSA payload."""

    def setUp(self):
        self.payload = load_fixture("maersk_tracking_response.json")
        self.parser = DcsaParser(source_provider="maersk")
        self.events = self.parser.parse(self.payload)

    def test_maersk_adapter_returns_normalized_tracking_structure(self):
        self.assertEqual(len(self.events), 2)
        for event in self.events:
            self.assertIsInstance(event, NormalisedTrackingEvent)

    def test_maersk_first_event_is_load(self):
        event = self.events[0]
        self.assertEqual(event.event_code, "LOAD")
        self.assertEqual(event.container_number, "MRKU1234567")

    def test_maersk_first_event_has_location(self):
        event = self.events[0]
        self.assertEqual(event.location_unlocode, "GBFXT")
        self.assertIn("Felixstowe", event.location_name)

    def test_maersk_first_event_has_vessel(self):
        event = self.events[0]
        self.assertEqual(event.vessel_name, "MAERSK EINDHOVEN")
        self.assertEqual(event.vessel_imo, "9778791")
        self.assertEqual(event.voyage_number, "213E")

    def test_maersk_first_event_is_actual(self):
        event = self.events[0]
        self.assertTrue(event.is_actual)
        self.assertFalse(event.is_estimated)

    def test_maersk_second_event_is_estimated(self):
        event = self.events[1]
        self.assertFalse(event.is_actual)
        self.assertTrue(event.is_estimated)

    def test_maersk_source_provider_set(self):
        for event in self.events:
            self.assertEqual(event.source_provider, "maersk")

    def test_maersk_raw_event_id_preserved(self):
        self.assertEqual(self.events[0].raw_event_id, "MAERSK-EVT-001")
        self.assertEqual(self.events[1].raw_event_id, "MAERSK-EVT-002")

    def test_maersk_raw_payload_is_original_event_dict(self):
        """raw_payload must equal the original event dict from the fixture."""
        self.assertEqual(self.events[0].raw_payload, self.payload["events"][0])
        self.assertEqual(self.events[1].raw_payload, self.payload["events"][1])

    def test_maersk_raw_payload_is_not_mutated(self):
        """Parsing must not alter the original fixture data."""
        original = load_fixture("maersk_tracking_response.json")
        self.assertEqual(self.payload, original)

    def test_maersk_booking_reference_extracted(self):
        self.assertEqual(self.events[0].booking_number, "MAEU123456789")

    def test_maersk_bl_reference_extracted(self):
        self.assertEqual(self.events[0].bill_of_lading_number, "MAEU-BL-123456789")

    def test_maersk_event_datetime_is_not_none(self):
        for event in self.events:
            self.assertIsNotNone(event.event_datetime)


# ---------------------------------------------------------------------------
# CMA CGM (DCSA)
# ---------------------------------------------------------------------------


class CmaCgmDcsaParserTest(unittest.TestCase):
    """DcsaParser correctly parses a CMA CGM-format DCSA payload."""

    def setUp(self):
        self.payload = load_fixture("cma_cgm_tracking_response.json")
        self.parser = DcsaParser(source_provider="cma_cgm")
        self.events = self.parser.parse(self.payload)

    def test_cma_adapter_returns_normalized_tracking_structure(self):
        self.assertEqual(len(self.events), 2)
        for event in self.events:
            self.assertIsInstance(event, NormalisedTrackingEvent)

    def test_cma_first_event_is_load(self):
        event = self.events[0]
        self.assertEqual(event.event_code, "LOAD")
        self.assertEqual(event.container_number, "CMAU1234567")

    def test_cma_second_event_is_discharge(self):
        event = self.events[1]
        self.assertEqual(event.event_code, "DISC")

    def test_cma_first_event_location_is_shanghai(self):
        self.assertEqual(self.events[0].location_unlocode, "CNSHA")

    def test_cma_second_event_location_is_le_havre(self):
        self.assertEqual(self.events[1].location_unlocode, "FRLEH")

    def test_cma_source_provider_is_cma_cgm(self):
        for event in self.events:
            self.assertEqual(event.source_provider, "cma_cgm")

    def test_cma_raw_payload_is_original_event_dict(self):
        self.assertEqual(self.events[0].raw_payload, self.payload["events"][0])

    def test_cma_all_events_have_datetime(self):
        for event in self.events:
            self.assertIsNotNone(event.event_datetime)


# ---------------------------------------------------------------------------
# Hapag-Lloyd (DCSA)
# ---------------------------------------------------------------------------


class HapagLloydDcsaParserTest(unittest.TestCase):
    """DcsaParser correctly parses a Hapag-Lloyd-format DCSA payload."""

    def setUp(self):
        self.payload = load_fixture("hapag_lloyd_tracking_response.json")
        self.parser = DcsaParser(source_provider="hapag_lloyd")
        self.events = self.parser.parse(self.payload)

    def test_hapag_adapter_returns_normalized_tracking_structure(self):
        self.assertEqual(len(self.events), 3)
        for event in self.events:
            self.assertIsInstance(event, NormalisedTrackingEvent)

    def test_hapag_first_event_is_load(self):
        event = self.events[0]
        self.assertEqual(event.event_code, "LOAD")
        self.assertEqual(event.container_number, "HLXU1234567")

    def test_hapag_second_event_is_departure(self):
        event = self.events[1]
        self.assertEqual(event.event_code, "DEPA")

    def test_hapag_third_event_is_estimated_arrival(self):
        event = self.events[2]
        self.assertEqual(event.event_code, "ARRI")
        self.assertTrue(event.is_estimated)

    def test_hapag_source_provider_set(self):
        for event in self.events:
            self.assertEqual(event.source_provider, "hapag_lloyd")

    def test_hapag_raw_event_ids_are_unique(self):
        raw_ids = [e.raw_event_id for e in self.events]
        self.assertEqual(len(raw_ids), len(set(raw_ids)), "Raw event IDs must be unique within a response")

    def test_hapag_raw_payload_preserved_for_all_events(self):
        for i, event in enumerate(self.events):
            self.assertEqual(event.raw_payload, self.payload["events"][i])


# ---------------------------------------------------------------------------
# Non-DCSA raw fixture storage (MSC, ONE, COSCO)
# These carriers do not use the DCSA format.  We verify that the fixture data
# is valid JSON and can be stored as-is (raw payload), without DcsaParser.
# ---------------------------------------------------------------------------


class NonDcsaFixtureValidityTest(unittest.TestCase):
    """Non-DCSA carrier fixtures must be valid, non-empty JSON dicts."""

    def _assert_fixture_is_valid_dict(self, filename: str, expected_container_key: str) -> None:
        data = load_fixture(filename)
        self.assertIsInstance(data, dict, f"{filename} must be a JSON object")
        self.assertIn(
            expected_container_key,
            data,
            f"{filename} must contain '{expected_container_key}' key",
        )

    def test_msc_fixture_is_valid(self):
        self._assert_fixture_is_valid_dict("msc_tracking_response.json", "containerNumber")

    def test_one_fixture_is_valid(self):
        self._assert_fixture_is_valid_dict("one_tracking_response.json", "containerNo")

    def test_cosco_fixture_is_valid(self):
        self._assert_fixture_is_valid_dict("cosco_tracking_response.json", "containerNo")

    def test_msc_fixture_has_movements(self):
        data = load_fixture("msc_tracking_response.json")
        self.assertIn("movements", data)
        self.assertGreater(len(data["movements"]), 0)

    def test_one_fixture_has_tracking_events(self):
        data = load_fixture("one_tracking_response.json")
        self.assertIn("trackingEvents", data)
        self.assertGreater(len(data["trackingEvents"]), 0)

    def test_cosco_fixture_has_tracking_details(self):
        data = load_fixture("cosco_tracking_response.json")
        self.assertIn("trackingDetails", data)
        self.assertGreater(len(data["trackingDetails"]), 0)


class MscFixtureStatusStringsTest(unittest.TestCase):
    """MSC fixture contains raw status strings that can be mapped via normalize_event_type."""

    def setUp(self):
        from apps.scm.tracking.statuses import TrackingEventType, normalize_event_type

        self.normalize = normalize_event_type
        self.TrackingEventType = TrackingEventType
        self.data = load_fixture("msc_tracking_response.json")

    def test_msc_status_gate_in_maps_to_normalized_gate_in(self):
        status = self.data["movements"][0]["status"]
        self.assertEqual(self.normalize(status), self.TrackingEventType.GATE_IN)

    def test_msc_status_loaded_on_board_maps_to_normalized_loaded(self):
        status = self.data["movements"][1]["status"]
        self.assertEqual(self.normalize(status), self.TrackingEventType.LOADED_ON_VESSEL)

    def test_msc_status_vessel_departed_maps_to_normalized_vessel_departed(self):
        status = self.data["movements"][2]["status"]
        self.assertEqual(self.normalize(status), self.TrackingEventType.VESSEL_DEPARTED)

    def test_msc_status_delivered_maps_to_normalized_delivered(self):
        status = self.data["movements"][3]["status"]
        self.assertEqual(self.normalize(status), self.TrackingEventType.DELIVERED)


class OneFixtureStatusStringsTest(unittest.TestCase):
    """ONE fixture status strings can be normalized via normalize_event_type."""

    def setUp(self):
        from apps.scm.tracking.statuses import TrackingEventType, normalize_event_type

        self.normalize = normalize_event_type
        self.TrackingEventType = TrackingEventType
        self.data = load_fixture("one_tracking_response.json")

    def test_one_status_gate_in_maps_to_normalized_gate_in(self):
        status = self.data["trackingEvents"][0]["status"]
        self.assertEqual(self.normalize(status), self.TrackingEventType.GATE_IN)

    def test_one_status_loaded_on_board_maps_to_normalized_loaded(self):
        status = self.data["trackingEvents"][1]["status"]
        self.assertEqual(self.normalize(status), self.TrackingEventType.LOADED_ON_VESSEL)

    def test_one_status_vessel_departed_maps_to_normalized_vessel_departed(self):
        status = self.data["trackingEvents"][2]["status"]
        self.assertEqual(self.normalize(status), self.TrackingEventType.VESSEL_DEPARTED)

    def test_one_status_vessel_arrived_maps_to_normalized_vessel_arrived(self):
        status = self.data["trackingEvents"][3]["status"]
        self.assertEqual(self.normalize(status), self.TrackingEventType.VESSEL_ARRIVED)


class CoscoFixtureStatusStringsTest(unittest.TestCase):
    """COSCO fixture status strings can be normalized or produce UNKNOWN for proprietary codes."""

    def setUp(self):
        from apps.scm.tracking.statuses import TrackingEventType, normalize_event_type

        self.normalize = normalize_event_type
        self.TrackingEventType = TrackingEventType
        self.data = load_fixture("cosco_tracking_response.json")

    def test_cosco_description_gate_in_maps_to_normalized_gate_in(self):
        description = self.data["trackingDetails"][0]["description"]
        self.assertEqual(self.normalize(description), self.TrackingEventType.GATE_IN)

    def test_cosco_description_vessel_departed_maps_to_normalized_vessel_departed(self):
        description = self.data["trackingDetails"][1]["description"]
        self.assertEqual(self.normalize(description), self.TrackingEventType.VESSEL_DEPARTED)

    def test_cosco_description_vessel_arrived_maps_to_normalized_vessel_arrived(self):
        description = self.data["trackingDetails"][2]["description"]
        self.assertEqual(self.normalize(description), self.TrackingEventType.VESSEL_ARRIVED)

    def test_cosco_description_delivered_maps_to_normalized_delivered(self):
        description = self.data["trackingDetails"][3]["description"]
        self.assertEqual(self.normalize(description), self.TrackingEventType.DELIVERED)

    def test_cosco_unknown_actcode_maps_to_unknown_or_known_type(self):
        """Proprietary actCode values that don't match normalized descriptions return UNKNOWN."""
        result = self.normalize("TOTALLY_UNKNOWN_COSCO_CODE")
        self.assertEqual(result, self.TrackingEventType.UNKNOWN)
