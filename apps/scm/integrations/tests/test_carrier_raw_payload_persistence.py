"""Raw payload persistence tests.

Verifies that:
- Raw carrier payloads are stored separately from normalized tracking events.
- The raw payload is stored verbatim (unchanged JSON).
- Normalizing events does not mutate the raw payload dict.
- A normalized tracking event can reference its raw payload via provider/subscription.
- Duplicate raw payloads can be detected via payload_hash.
"""

import copy
import json
import pathlib

from django.test import TestCase
from django.utils import timezone

from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingRawPayload, TrackingSubscription
from apps.scm.tracking.services import store_raw_payload, upsert_tracking_event
from apps.teams.models import Team

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "carriers"


def load_fixture(filename: str) -> dict:
    return json.loads((FIXTURES_DIR / filename).read_text())


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _provider(code: str) -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(
        code=code,
        defaults={"name": f"Provider {code}", "provider_type": TrackingProvider.ProviderType.API},
    )[0]


def _subscription(team: Team, provider: TrackingProvider, ref: str) -> TrackingSubscription:
    return TrackingSubscription.objects.create(team=team, provider=provider, tracking_reference=ref)


# ---------------------------------------------------------------------------
# Raw payload stored separately from normalized event
# ---------------------------------------------------------------------------


class RawPayloadStoredSeparatelyTest(TestCase):
    """Raw payloads live in TrackingRawPayload; normalized events live in TrackingEvent."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("raw-sep-team")
        cls.provider = _provider("RAW_SEP_PROV")
        cls.sub = _subscription(cls.team, cls.provider, "RAW1234567")

    def test_raw_carrier_payload_is_saved_separately(self):
        """Storing a raw payload creates a TrackingRawPayload record, not a TrackingEvent."""
        raw = {"events": [{"eventID": "EVT-001", "eventType": "EQUIPMENT"}]}
        store_raw_payload(self.team, self.provider, raw, subscription=self.sub)

        self.assertEqual(TrackingRawPayload.objects.filter(team=self.team, provider=self.provider).count(), 1)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team, provider=self.provider).count(), 0)

    def test_normalized_tracking_event_does_not_replace_raw_payload(self):
        """Creating a TrackingEvent does not delete the associated raw payload record."""
        raw = {"events": [{"eventID": "EVT-NORM-001"}]}
        store_raw_payload(self.team, self.provider, raw, subscription=self.sub)

        upsert_tracking_event(
            team=self.team,
            provider=self.provider,
            event_type=TrackingEvent.EventType.GATE_IN,
            event_datetime=timezone.now(),
            subscription=self.sub,
            source_event_id="EVT-NORM-001",
        )

        self.assertEqual(TrackingRawPayload.objects.filter(team=self.team, provider=self.provider).count(), 1)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team, provider=self.provider).count(), 1)

    def test_normalized_tracking_event_references_raw_payload_via_subscription(self):
        """A normalized event and its raw payload share the same subscription FK."""
        raw = {"events": [{"eventID": "EVT-REF-001"}]}
        raw_record = store_raw_payload(self.team, self.provider, raw, subscription=self.sub)

        event, _ = upsert_tracking_event(
            team=self.team,
            provider=self.provider,
            event_type=TrackingEvent.EventType.LOADED_ON_VESSEL,
            event_datetime=timezone.now(),
            subscription=self.sub,
            source_event_id="EVT-REF-001",
        )

        self.assertEqual(raw_record.subscription_id, self.sub.pk)
        self.assertEqual(event.subscription_id, self.sub.pk)


# ---------------------------------------------------------------------------
# Raw payload verbatim storage
# ---------------------------------------------------------------------------


class RawPayloadVerbatimTest(TestCase):
    """The raw payload JSON is stored exactly as received from the carrier."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("raw-verbatim-team")
        cls.provider = _provider("RAW_VERBATIM_PROV")
        cls.sub = _subscription(cls.team, cls.provider, "VERBATIM1234567")

    def test_maersk_raw_payload_stored_verbatim(self):
        payload = load_fixture("maersk_tracking_response.json")
        original_copy = copy.deepcopy(payload)

        record = store_raw_payload(self.team, self.provider, payload, subscription=self.sub)
        record.refresh_from_db()

        self.assertEqual(record.payload_json, original_copy)

    def test_msc_raw_payload_stored_verbatim(self):
        payload = load_fixture("msc_tracking_response.json")
        original_copy = copy.deepcopy(payload)

        record = store_raw_payload(self.team, self.provider, payload, subscription=self.sub)
        record.refresh_from_db()

        self.assertEqual(record.payload_json, original_copy)

    def test_hapag_raw_payload_stored_verbatim(self):
        payload = load_fixture("hapag_lloyd_tracking_response.json")
        original_copy = copy.deepcopy(payload)

        record = store_raw_payload(self.team, self.provider, payload, subscription=self.sub)
        record.refresh_from_db()

        self.assertEqual(record.payload_json, original_copy)

    def test_raw_payload_is_not_normalized_on_store(self):
        """store_raw_payload must save the payload as-is, with no event normalization."""
        payload = {"events": [{"eventID": "RAW-X", "eventType": "EQUIPMENT", "some_carrier_field": "vendor_value"}]}
        record = store_raw_payload(self.team, self.provider, payload, subscription=self.sub)

        self.assertIn("some_carrier_field", record.payload_json["events"][0])
        self.assertEqual(record.payload_json["events"][0]["some_carrier_field"], "vendor_value")


# ---------------------------------------------------------------------------
# Normalization does not mutate raw payload
# ---------------------------------------------------------------------------


class NormalizationDoesNotMutateRawPayloadTest(TestCase):
    """Parsing a raw DCSA payload must not alter the original dict."""

    def test_normalization_does_not_mutate_raw_payload(self):
        from apps.scm.integrations.carriers.dcsa.parser import DcsaParser

        payload = load_fixture("maersk_tracking_response.json")
        snapshot_before = json.dumps(payload, sort_keys=True)

        parser = DcsaParser(source_provider="maersk")
        parser.parse(payload)

        snapshot_after = json.dumps(payload, sort_keys=True)
        self.assertEqual(
            snapshot_before,
            snapshot_after,
            "DcsaParser.parse() must not mutate the input payload dict",
        )

    def test_dcsa_event_raw_payload_field_equals_original_event_dict(self):
        """Each NormalisedTrackingEvent.raw_payload is the original event dict, not a copy."""
        from apps.scm.integrations.carriers.dcsa.parser import DcsaParser

        payload = load_fixture("hapag_lloyd_tracking_response.json")
        parser = DcsaParser(source_provider="hapag_lloyd")
        events = parser.parse(payload)

        for i, event in enumerate(events):
            self.assertEqual(
                event.raw_payload,
                payload["events"][i],
                f"Event {i}: raw_payload must equal the original event dict from the fixture",
            )


# ---------------------------------------------------------------------------
# Payload hash deduplication
# ---------------------------------------------------------------------------


class RawPayloadHashTest(TestCase):
    """Payload hash enables detection of duplicate carrier responses."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("raw-hash-team")
        cls.provider = _provider("RAW_HASH_PROV")
        cls.sub = _subscription(cls.team, cls.provider, "HASH1234567")

    def test_payload_hash_is_deterministic(self):
        """Identical payloads always produce the same hash."""
        payload = {"events": [{"eventID": "H-001"}]}
        r1 = store_raw_payload(self.team, self.provider, payload, subscription=self.sub)
        r2 = store_raw_payload(self.team, self.provider, payload, subscription=self.sub)
        self.assertEqual(r1.payload_hash, r2.payload_hash)

    def test_different_payloads_produce_different_hashes(self):
        r1 = store_raw_payload(self.team, self.provider, {"events": [{"eventID": "H-A"}]}, subscription=self.sub)
        r2 = store_raw_payload(self.team, self.provider, {"events": [{"eventID": "H-B"}]}, subscription=self.sub)
        self.assertNotEqual(r1.payload_hash, r2.payload_hash)

    def test_payload_hash_is_sha256_length(self):
        record = store_raw_payload(self.team, self.provider, {"x": 1}, subscription=self.sub)
        self.assertEqual(len(record.payload_hash), 64)


# ---------------------------------------------------------------------------
# Raw payload type classification
# ---------------------------------------------------------------------------


class RawPayloadTypeTest(TestCase):
    """Payload type is correctly set based on the source of the data."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("raw-type-team")
        cls.provider = _provider("RAW_TYPE_PROV")
        cls.sub = _subscription(cls.team, cls.provider, "TYPE1234567")

    def test_api_response_payload_type_is_api_response(self):
        record = store_raw_payload(
            self.team,
            self.provider,
            {"events": []},
            payload_type=TrackingRawPayload.PayloadType.API_RESPONSE,
            subscription=self.sub,
        )
        self.assertEqual(record.payload_type, TrackingRawPayload.PayloadType.API_RESPONSE)

    def test_error_response_payload_type_is_error_response(self):
        record = store_raw_payload(
            self.team,
            self.provider,
            {"error": "timeout"},
            payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE,
            parsed_successfully=False,
            error_message="timeout after 30s",
            subscription=self.sub,
        )
        self.assertEqual(record.payload_type, TrackingRawPayload.PayloadType.ERROR_RESPONSE)
        self.assertFalse(record.parsed_successfully)
        self.assertEqual(record.error_message, "timeout after 30s")

    def test_parsed_successfully_false_for_error_payload(self):
        record = store_raw_payload(
            self.team,
            self.provider,
            {"error": "auth"},
            payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE,
            parsed_successfully=False,
            error_message="401 Unauthorized",
        )
        self.assertFalse(record.parsed_successfully)

    def test_parsed_successfully_true_for_successful_payload(self):
        record = store_raw_payload(
            self.team,
            self.provider,
            {"events": [{"eventID": "OK-001"}]},
            payload_type=TrackingRawPayload.PayloadType.API_RESPONSE,
            parsed_successfully=True,
        )
        self.assertTrue(record.parsed_successfully)
