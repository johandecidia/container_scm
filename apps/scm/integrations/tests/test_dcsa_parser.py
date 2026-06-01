"""Tests for the DcsaParser — normalised event parsing from DCSA-standard payloads."""

import unittest

from apps.scm.integrations.carriers.dcsa.parser import DcsaParser
from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent

SAMPLE_PAYLOAD = {
    "events": [
        {
            "eventID": "evt-001",
            "eventType": "EQUIPMENT",
            "eventClassifierCode": "ACT",
            "equipmentEventTypeCode": "LOAD",
            "eventDateTime": "2024-03-15T14:30:00Z",
            "equipmentReference": "MSCU1234567",
            "carrierBookingReference": "BKG123456",
            "transportDocumentReference": "MSCBL123456",
            "location": {
                "locationName": "Port of Rotterdam",
                "UNLocationCode": "NLRTM",
            },
            "vessel": {
                "vesselName": "MSC ALLEGRA",
                "vesselIMONumber": "9123456",
            },
            "exportVoyageNumber": "VOY001W",
            "modeOfTransport": "VESSEL",
        }
    ]
}


class DcsaParserParseTest(unittest.TestCase):
    def setUp(self):
        self.parser = DcsaParser("msc")

    def test_parse_returns_list_of_length_one(self):
        events = self.parser.parse(SAMPLE_PAYLOAD)
        self.assertEqual(len(events), 1)

    def test_first_event_is_normalised_tracking_event(self):
        events = self.parser.parse(SAMPLE_PAYLOAD)
        self.assertIsInstance(events[0], NormalisedTrackingEvent)

    def test_event_type_is_equipment(self):
        events = self.parser.parse(SAMPLE_PAYLOAD)
        self.assertEqual(events[0].event_type, "EQUIPMENT")

    def test_event_datetime_is_not_none(self):
        events = self.parser.parse(SAMPLE_PAYLOAD)
        self.assertIsNotNone(events[0].event_datetime)

    def test_location_unlocode_is_nlrtm(self):
        events = self.parser.parse(SAMPLE_PAYLOAD)
        self.assertEqual(events[0].location_unlocode, "NLRTM")

    def test_container_number_is_mscu1234567(self):
        events = self.parser.parse(SAMPLE_PAYLOAD)
        self.assertEqual(events[0].container_number, "MSCU1234567")

    def test_source_provider_is_msc(self):
        events = self.parser.parse(SAMPLE_PAYLOAD)
        self.assertEqual(events[0].source_provider, "msc")

    def test_raw_payload_is_not_empty(self):
        events = self.parser.parse(SAMPLE_PAYLOAD)
        self.assertTrue(events[0].raw_payload)
        self.assertEqual(events[0].raw_payload, SAMPLE_PAYLOAD["events"][0])

    def test_is_actual_true_for_act_classifier(self):
        events = self.parser.parse(SAMPLE_PAYLOAD)
        self.assertTrue(events[0].is_actual)

    def test_is_estimated_false_for_act_classifier(self):
        events = self.parser.parse(SAMPLE_PAYLOAD)
        self.assertFalse(events[0].is_estimated)


class DcsaParserEmptyPayloadTest(unittest.TestCase):
    def setUp(self):
        self.parser = DcsaParser("msc")

    def test_empty_dict_returns_empty_list(self):
        events = self.parser.parse({})
        self.assertEqual(events, [])

    def test_none_like_empty_list_events_key_returns_empty_list(self):
        events = self.parser.parse({"events": []})
        self.assertEqual(events, [])

    def test_empty_list_returns_empty_list(self):
        events = self.parser.parse([])
        self.assertEqual(events, [])
