"""Tests for normalised event persistence: field mapping, fingerprinting and
idempotency (including concurrent writers).
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.carriers.dcsa.parser import DcsaParser
from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
from apps.scm.tracking.ingestion import (
    build_event_fingerprint,
    persist_normalised_event,
    persist_normalised_events,
)
from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingSubscription
from apps.teams.models import Team

_EVENT_TIME = datetime(2024, 3, 10, 8, 0, tzinfo=UTC)


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _provider(code: str = "maersk") -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(code=code, defaults={"name": code})[0]


def _normalised(**kwargs) -> NormalisedTrackingEvent:
    defaults = {
        "event_type": "EQUIPMENT",
        "event_classifier": "ACT",
        "event_code": "LOAD",
        "description": "Loaded on vessel",
        "event_datetime": _EVENT_TIME,
        "location_name": "Port of Felixstowe",
        "location_unlocode": "GBFXT",
        "latitude": "51.955000",
        "longitude": "1.351000",
        "vessel_name": "MAERSK EINDHOVEN",
        "vessel_imo": "9778791",
        "voyage_number": "213E",
        "transport_mode": "VESSEL",
        "container_number": "MRKU1234567",
        "raw_event_id": "MAERSK-EVT-001",
        "source_provider": "maersk",
        "raw_payload": {"eventID": "MAERSK-EVT-001"},
    }
    defaults.update(kwargs)
    return NormalisedTrackingEvent(**defaults)


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------


class NormalisedEventPersistenceTest(TestCase):
    """Every field the DCSA schema carries survives the trip to the database."""

    def setUp(self):
        self.team = _team("ingest-fields-team")
        self.provider = _provider("maersk")
        self.event, self.created = persist_normalised_event(
            team=self.team,
            provider=self.provider,
            normalised=_normalised(),
        )

    def test_event_is_created(self):
        self.assertTrue(self.created)

    def test_internal_event_type_is_mapped_from_dcsa_code(self):
        self.assertEqual(self.event.event_type, TrackingEvent.EventType.LOADED_ON_VESSEL)

    def test_carrier_event_type_and_code_are_preserved(self):
        self.assertEqual(self.event.carrier_event_type, "EQUIPMENT")
        self.assertEqual(self.event.event_code, "LOAD")

    def test_carrier_description_is_preserved_verbatim(self):
        self.assertEqual(self.event.carrier_description, "Loaded on vessel")

    def test_event_time_type_is_actual(self):
        self.assertEqual(self.event.event_time_type, TrackingEvent.EventTimeType.ACTUAL)
        self.assertTrue(self.event.is_actual)
        self.assertFalse(self.event.is_estimated)

    def test_event_datetime_is_stored(self):
        self.assertEqual(self.event.event_datetime, _EVENT_TIME)

    def test_location_is_stored(self):
        self.assertEqual(self.event.location_name, "Port of Felixstowe")
        self.assertEqual(self.event.location_unlocode, "GBFXT")

    def test_coordinates_are_stored_as_decimals(self):
        self.assertEqual(self.event.location_latitude, Decimal("51.955000"))
        self.assertEqual(self.event.location_longitude, Decimal("1.351000"))

    def test_vessel_and_voyage_are_stored(self):
        self.assertEqual(self.event.vessel_name, "MAERSK EINDHOVEN")
        self.assertEqual(self.event.vessel_imo, "9778791")
        self.assertEqual(self.event.voyage_number, "213E")

    def test_transport_mode_is_normalised(self):
        self.assertEqual(self.event.transport_mode, TrackingEvent.TransportMode.VESSEL)

    def test_equipment_reference_is_stored(self):
        self.assertEqual(self.event.equipment_reference, "MRKU1234567")

    def test_carrier_event_id_is_stored(self):
        self.assertEqual(self.event.source_event_id, "MAERSK-EVT-001")

    def test_raw_event_payload_is_stored(self):
        self.assertEqual(self.event.raw_data, {"eventID": "MAERSK-EVT-001"})

    def test_received_at_is_recorded(self):
        self.assertIsNotNone(self.event.received_at)

    def test_fingerprint_is_set(self):
        self.assertTrue(self.event.event_fingerprint)


class EstimatedEventPersistenceTest(TestCase):
    """An estimated event is stored as a forecast, never as an observation."""

    def setUp(self):
        self.team = _team("ingest-est-team")
        self.provider = _provider("maersk")

    def test_estimated_classifier_maps_to_estimated(self):
        event, _ = persist_normalised_event(
            team=self.team,
            provider=self.provider,
            normalised=_normalised(event_classifier="EST", raw_event_id="EST-1"),
        )
        self.assertEqual(event.event_time_type, TrackingEvent.EventTimeType.ESTIMATED)
        self.assertFalse(event.is_actual)
        self.assertTrue(event.is_estimated)

    def test_planned_classifier_maps_to_planned(self):
        event, _ = persist_normalised_event(
            team=self.team,
            provider=self.provider,
            normalised=_normalised(event_classifier="PLN", raw_event_id="PLN-1"),
        )
        self.assertEqual(event.event_time_type, TrackingEvent.EventTimeType.PLANNED)
        self.assertTrue(event.is_estimated)

    def test_requested_classifier_maps_to_requested(self):
        event, _ = persist_normalised_event(
            team=self.team,
            provider=self.provider,
            normalised=_normalised(event_classifier="REQ", raw_event_id="REQ-1"),
        )
        self.assertEqual(event.event_time_type, TrackingEvent.EventTimeType.REQUESTED)
        self.assertFalse(event.is_actual)

    def test_missing_classifier_is_unknown_not_actual(self):
        event, _ = persist_normalised_event(
            team=self.team,
            provider=self.provider,
            normalised=_normalised(event_classifier="", raw_event_id="UNK-1"),
        )
        self.assertEqual(event.event_time_type, TrackingEvent.EventTimeType.UNKNOWN)
        self.assertFalse(event.is_actual)

    def test_unparseable_coordinate_is_dropped_not_guessed(self):
        event, _ = persist_normalised_event(
            team=self.team,
            provider=self.provider,
            normalised=_normalised(latitude="not-a-number", longitude="", raw_event_id="BADCOORD-1"),
        )
        self.assertIsNone(event.location_latitude)
        self.assertIsNone(event.location_longitude)
        # The original value is still available for inspection.
        self.assertIn("eventID", event.raw_data)

    def test_unknown_transport_mode_becomes_other(self):
        event, _ = persist_normalised_event(
            team=self.team,
            provider=self.provider,
            normalised=_normalised(transport_mode="HOVERCRAFT", raw_event_id="MODE-1"),
        )
        self.assertEqual(event.transport_mode, TrackingEvent.TransportMode.OTHER)

    def test_unmapped_event_code_stays_unknown_but_keeps_carrier_code(self):
        event, _ = persist_normalised_event(
            team=self.team,
            provider=self.provider,
            normalised=_normalised(event_code="ZZZZ", description="", raw_event_id="ZZZ-1"),
        )
        self.assertEqual(event.event_type, TrackingEvent.EventType.UNKNOWN)
        self.assertEqual(event.event_code, "ZZZZ")


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


class EventFingerprintTest(TestCase):
    """The fingerprint is stable for the same event and distinct for different ones."""

    def _fp(self, **kwargs) -> str:
        defaults = {
            "team_id": 1,
            "provider_code": "maersk",
            "reference": "MRKU1234567",
            "carrier_event_type": "EQUIPMENT",
            "event_code": "LOAD",
            "event_time_type": "actual",
            "event_datetime": _EVENT_TIME,
            "location_unlocode": "GBFXT",
            "vessel_imo": "9778791",
            "voyage_number": "213E",
        }
        defaults.update(kwargs)
        return build_event_fingerprint(**defaults)

    def test_same_fields_give_same_fingerprint(self):
        self.assertEqual(self._fp(), self._fp())

    def test_different_team_gives_different_fingerprint(self):
        self.assertNotEqual(self._fp(), self._fp(team_id=2))

    def test_different_provider_gives_different_fingerprint(self):
        self.assertNotEqual(self._fp(), self._fp(provider_code="hapag_lloyd"))

    def test_different_event_time_gives_different_fingerprint(self):
        other = datetime(2024, 3, 11, 8, 0, tzinfo=UTC)
        self.assertNotEqual(self._fp(), self._fp(event_datetime=other))

    def test_different_location_gives_different_fingerprint(self):
        self.assertNotEqual(self._fp(), self._fp(location_unlocode="NLRTM"))

    def test_different_event_code_gives_different_fingerprint(self):
        self.assertNotEqual(self._fp(), self._fp(event_code="DISC"))

    def test_actual_and_estimated_of_same_event_are_distinct(self):
        """An estimated arrival and the actual arrival are two different events."""
        self.assertNotEqual(self._fp(), self._fp(event_time_type="estimated"))

    def test_different_voyage_gives_different_fingerprint(self):
        self.assertNotEqual(self._fp(), self._fp(voyage_number="214E"))

    def test_carrier_event_id_takes_precedence_over_fields(self):
        """With an event ID, a corrected time still points at the same event."""
        with_id = self._fp(source_event_id="EVT-1")
        with_id_changed_time = self._fp(source_event_id="EVT-1", event_datetime=None)
        self.assertEqual(with_id, with_id_changed_time)

    def test_different_carrier_event_ids_are_distinct(self):
        self.assertNotEqual(self._fp(source_event_id="EVT-1"), self._fp(source_event_id="EVT-2"))


class EventIdempotencyTest(TestCase):
    """Re-processing the same carrier payload never creates duplicates."""

    def setUp(self):
        self.team = _team("ingest-idem-team")
        self.provider = _provider("maersk")

    def test_same_event_twice_creates_one_row(self):
        first, created_first = persist_normalised_event(
            team=self.team, provider=self.provider, normalised=_normalised()
        )
        second, created_second = persist_normalised_event(
            team=self.team, provider=self.provider, normalised=_normalised()
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 1)

    def test_event_without_carrier_id_is_still_deduplicated(self):
        normalised = _normalised(raw_event_id="")
        persist_normalised_event(team=self.team, provider=self.provider, normalised=normalised)
        persist_normalised_event(team=self.team, provider=self.provider, normalised=normalised)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 1)

    def test_corrected_event_updates_in_place(self):
        persist_normalised_event(team=self.team, provider=self.provider, normalised=_normalised())
        corrected = datetime(2024, 3, 10, 9, 30, tzinfo=UTC)
        event, created = persist_normalised_event(
            team=self.team,
            provider=self.provider,
            normalised=_normalised(event_datetime=corrected, description="Loaded on vessel (corrected)"),
        )
        self.assertFalse(created)
        self.assertEqual(event.event_datetime, corrected)
        self.assertEqual(event.carrier_description, "Loaded on vessel (corrected)")
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 1)

    def test_different_events_create_separate_rows(self):
        persist_normalised_event(team=self.team, provider=self.provider, normalised=_normalised())
        persist_normalised_event(
            team=self.team,
            provider=self.provider,
            normalised=_normalised(raw_event_id="MAERSK-EVT-002", event_code="DISC"),
        )
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 2)

    def test_same_event_for_two_teams_is_not_deduplicated(self):
        """Deduplication is per team — one team's data never suppresses another's."""
        other_team = _team("ingest-idem-other-team")
        persist_normalised_event(team=self.team, provider=self.provider, normalised=_normalised())
        persist_normalised_event(team=other_team, provider=self.provider, normalised=_normalised())
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 1)
        self.assertEqual(TrackingEvent.objects.filter(team=other_team).count(), 1)

    def test_links_are_filled_in_but_never_cleared(self):
        equipment_type = EquipmentType.objects.get_or_create(
            iso_code="22G1",
            defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
        )[0]
        container = Container.objects.create(
            team=self.team,
            owner_code="MRK",
            category_id="U",
            serial_number="123456",
            check_digit=3,
            equipment_type=equipment_type,
        )
        subscription = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.provider,
            tracking_reference=container.container_id,
            container=container,
        )
        persist_normalised_event(team=self.team, provider=self.provider, normalised=_normalised())
        event, _ = persist_normalised_event(
            team=self.team,
            provider=self.provider,
            normalised=_normalised(),
            container=container,
            subscription=subscription,
        )
        self.assertEqual(event.container_id, container.pk)
        # A later payload without the link must not orphan the event.
        event, _ = persist_normalised_event(team=self.team, provider=self.provider, normalised=_normalised())
        self.assertEqual(event.container_id, container.pk)


class PersistBatchTest(TestCase):
    """Batch persistence reports counts and survives a single bad event."""

    def setUp(self):
        self.team = _team("ingest-batch-team")
        self.provider = _provider("maersk")

    def test_batch_counts_created_and_updated(self):
        events = [_normalised(), _normalised(raw_event_id="EVT-2", event_code="DISC")]
        first = persist_normalised_events(team=self.team, provider=self.provider, events=events)
        self.assertEqual(first, {"created": 2, "updated": 0, "failed": 0})
        second = persist_normalised_events(team=self.team, provider=self.provider, events=events)
        self.assertEqual(second, {"created": 0, "updated": 2, "failed": 0})

    def test_empty_batch_is_not_an_error(self):
        self.assertEqual(
            persist_normalised_events(team=self.team, provider=self.provider, events=[]),
            {"created": 0, "updated": 0, "failed": 0},
        )

    def test_dcsa_fixture_events_persist_end_to_end(self):
        import json
        import pathlib

        fixture = (
            pathlib.Path(__file__).parents[2]
            / "integrations"
            / "tests"
            / "fixtures"
            / "carriers"
            / "maersk_tracking_response.json"
        )
        payload = json.loads(fixture.read_text())
        parsed = DcsaParser(source_provider="maersk").parse(payload)
        result = persist_normalised_events(team=self.team, provider=self.provider, events=parsed)

        self.assertEqual(result["created"], 2)
        actual = TrackingEvent.objects.get(team=self.team, source_event_id="MAERSK-EVT-001")
        estimated = TrackingEvent.objects.get(team=self.team, source_event_id="MAERSK-EVT-002")
        self.assertEqual(actual.event_type, TrackingEvent.EventType.LOADED_ON_VESSEL)
        self.assertTrue(actual.is_actual)
        self.assertEqual(estimated.event_type, TrackingEvent.EventType.VESSEL_ARRIVED)
        self.assertTrue(estimated.is_estimated)
        self.assertEqual(estimated.location_unlocode, "NLRTM")


class ConcurrentEventIngestionTest(TestCase):
    """Two workers processing the same payload end up with one row, not two."""

    def setUp(self):
        self.team = _team("ingest-concurrent-team")
        self.provider = _provider("maersk")

    def test_losing_worker_in_a_race_returns_the_existing_event(self):
        """Simulates the real race: another worker committed the row before our write.

        Our INSERT then hits the unique constraint, and the loser must return the
        winner's row rather than raising or creating a duplicate.
        """
        normalised = _normalised()
        fingerprint = build_event_fingerprint(
            team_id=self.team.pk,
            provider_code=self.provider.code,
            source_event_id=normalised.raw_event_id,
        )
        # The competing worker got there first.
        winner = TrackingEvent.objects.create(
            team=self.team,
            provider=self.provider,
            event_fingerprint=fingerprint,
            event_type=TrackingEvent.EventType.LOADED_ON_VESSEL,
        )

        integrity_error = IntegrityError("duplicate key value violates unique constraint")
        with mock.patch.object(TrackingEvent.objects, "get_or_create", side_effect=integrity_error):
            event, created = persist_normalised_event(team=self.team, provider=self.provider, normalised=normalised)

        self.assertFalse(created)
        self.assertEqual(event.pk, winner.pk)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 1)

    def test_unique_constraint_rejects_a_duplicate_fingerprint(self):
        """The database — not just application code — enforces deduplication."""
        event, _ = persist_normalised_event(team=self.team, provider=self.provider, normalised=_normalised())
        with self.assertRaises(IntegrityError), transaction.atomic():
            TrackingEvent.objects.create(
                team=self.team,
                provider=self.provider,
                event_fingerprint=event.event_fingerprint,
                event_type=TrackingEvent.EventType.UNKNOWN,
            )

    def test_blank_fingerprints_are_allowed_to_coexist(self):
        """Legacy rows without a fingerprint must not collide with each other."""
        for _ in range(2):
            TrackingEvent.objects.create(
                team=self.team,
                provider=self.provider,
                event_fingerprint="",
                event_type=TrackingEvent.EventType.UNKNOWN,
            )
        self.assertEqual(TrackingEvent.objects.filter(team=self.team, event_fingerprint="").count(), 2)
