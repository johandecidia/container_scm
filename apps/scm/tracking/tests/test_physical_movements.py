"""Turning carrier events into movements — and, mostly, not.

The load-bearing assertions here are the negative ones. A resolved location on a
tracking event is evidence, and the failure this module exists to prevent is a
container being recorded at Oceanterminalen because a vessel docked at SEGOT. So
most of these tests check that a perfectly well-formed, fully resolved event
produces no movement at all.

Events are built through ``persist_normalised_event`` where the seam is what is
under test, and directly where the rule is — the DCSA mapping tables are somebody
else's tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from django.test import TestCase

from apps.scm.containers.choices import LocationResolutionStatus, LocationSource, LocationType, MovementType
from apps.scm.containers.models import Container, ContainerLocation, ContainerMovement, EquipmentType
from apps.scm.containers.movements import record_container_movement
from apps.scm.containers.services import create_location, create_location_alias
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
from apps.scm.tracking.ingestion import persist_normalised_event
from apps.scm.tracking.models import TrackingEvent, TrackingProvider
from apps.scm.tracking.physical_movements import interpret_tracking_event, is_eligible
from apps.teams.models import Team

_CONTAINER_NUMBER = "MRKU1234567"
_EVENT_TIME = datetime(2024, 3, 10, 9, 30, tzinfo=UTC)


def _equipment() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _provider(code: str = "traqo") -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(code=code, defaults={"name": code})[0]


def _normalised(**kwargs) -> NormalisedTrackingEvent:
    defaults: dict[str, Any] = {
        "event_type": "EQUIPMENT",
        "event_classifier": "ACT",
        "event_code": "GTIN",
        "description": "Gate in",
        "event_datetime": _EVENT_TIME,
        "container_number": _CONTAINER_NUMBER,
        "location_name": "GOTHENBURG",
        "raw_event_id": "EVT-1",
    }
    defaults.update(kwargs)
    return NormalisedTrackingEvent(**defaults)


class InterpretationRulesTest(TestCase):
    """What may become a movement, and — mostly — what may not."""

    team: Team
    port: ContainerLocation

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Interpret", slug="loc2-interpret")
        cls.port = create_location(
            cls.team, {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT"}
        )

    def setUp(self):
        self.container = Container.objects.create(
            team=self.team,
            owner_code="MRK",
            category_id="U",
            serial_number="123456",
            check_digit=calculate_check_digit("MRK", "U", "123456"),
            equipment_type=_equipment(),
        )

    def _event(self, **kwargs) -> TrackingEvent:
        defaults = {
            "team": self.team,
            "provider": _provider(),
            "container": self.container,
            "event_type": TrackingEvent.EventType.GATE_IN,
            "event_time_type": TrackingEvent.EventTimeType.ACTUAL,
            "event_datetime": _EVENT_TIME,
            "location": self.port,
            "location_resolution_status": LocationResolutionStatus.RESOLVED,
            "event_fingerprint": kwargs.pop("fingerprint", "fp-1"),
        }
        defaults.update(kwargs)
        return TrackingEvent.objects.create(**defaults)

    def test_a_resolved_gate_in_becomes_a_movement(self):
        movement = interpret_tracking_event(self.team, self._event())

        self.assertIsNotNone(movement)
        self.assertEqual(movement.movement_type, MovementType.GATE_IN)
        self.assertEqual(movement.to_location, self.port)
        self.assertEqual(movement.source, LocationSource.TRACKING_EVENT)
        self.assertEqual(movement.occurred_at, _EVENT_TIME)

    def test_the_movement_keeps_the_event_it_came_from(self):
        event = self._event()
        movement = interpret_tracking_event(self.team, event)
        self.assertEqual(movement.related_tracking_event, event)

    def test_a_vessel_arrival_is_not_a_physical_movement(self):
        """The ship reached the port. The box is still on the ship."""
        event = self._event(event_type=TrackingEvent.EventType.VESSEL_ARRIVED)
        self.assertFalse(is_eligible(event))
        self.assertIsNone(interpret_tracking_event(self.team, event))

    def test_a_discharge_is_not_a_physical_movement(self):
        """It says the box came off. It does not say onto which terminal."""
        event = self._event(event_type=TrackingEvent.EventType.DISCHARGED)
        self.assertIsNone(interpret_tracking_event(self.team, event))

    def test_a_gate_out_is_not_acted_on(self):
        """A departure with no destination would replace what we know with nothing."""
        event = self._event(event_type=TrackingEvent.EventType.GATE_OUT)
        self.assertIsNone(interpret_tracking_event(self.team, event))

    def test_an_eta_update_is_not_a_physical_movement(self):
        event = self._event(event_type=TrackingEvent.EventType.ETA_UPDATED)
        self.assertIsNone(interpret_tracking_event(self.team, event))

    def test_a_forecast_gate_in_is_not_acted_on(self):
        event = self._event(event_time_type=TrackingEvent.EventTimeType.ESTIMATED)
        self.assertIsNone(interpret_tracking_event(self.team, event))

    def test_an_unresolved_location_produces_no_movement(self):
        event = self._event(location=None, location_resolution_status=LocationResolutionStatus.UNRESOLVED)
        self.assertIsNone(interpret_tracking_event(self.team, event))

    def test_an_ambiguous_location_produces_no_movement(self):
        """The evidence fitted several places. That is not a position either."""
        event = self._event(location_resolution_status=LocationResolutionStatus.AMBIGUOUS)
        self.assertIsNone(interpret_tracking_event(self.team, event))

    def test_an_undated_event_produces_no_movement(self):
        event = self._event(event_datetime=None)
        self.assertIsNone(interpret_tracking_event(self.team, event))

    def test_an_event_with_no_container_produces_no_movement(self):
        event = self._event(container=None)
        self.assertIsNone(interpret_tracking_event(self.team, event))

    def test_interpreting_the_same_event_twice_creates_one_movement(self):
        event = self._event()
        interpret_tracking_event(self.team, event)
        interpret_tracking_event(self.team, event)
        self.assertEqual(ContainerMovement.objects.filter(related_tracking_event=event).count(), 1)

    def test_a_tracking_movement_does_not_displace_a_newer_manual_one(self):
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=create_location(self.team, {"name": "MCR Depot", "location_type": LocationType.DEPOT}),
            occurred_at=_EVENT_TIME + timedelta(hours=5),
            source=LocationSource.MANUAL,
        )
        interpret_tracking_event(self.team, self._event())

        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location.name, "MCR Depot")


class IngestionSeamTest(TestCase):
    """One wiring in ``persist_normalised_event`` covers every provider.

    Interpretation must never be able to cost an event. These check the happy path
    and, more importantly, that ingestion behaves identically where no movement
    results.
    """

    team: Team
    port: ContainerLocation

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Seam", slug="loc2-seam")
        cls.port = create_location(
            cls.team, {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT"}
        )
        create_location_alias(cls.team, cls.port, {"source": "traqo", "external_name": "GOTHENBURG"})

    def setUp(self):
        self.container = Container.objects.create(
            team=self.team,
            owner_code="MRK",
            category_id="U",
            serial_number="123456",
            check_digit=calculate_check_digit("MRK", "U", "123456"),
            equipment_type=_equipment(),
        )

    def _persist(self, **kwargs) -> TrackingEvent:
        event, _created = persist_normalised_event(
            team=self.team,
            provider=_provider(),
            normalised=_normalised(**kwargs),
            container=self.container,
        )
        return event

    def test_ingesting_a_gate_in_records_the_movement_and_the_location(self):
        self._persist()

        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, self.port)
        self.assertEqual(self.container.location_source, LocationSource.TRACKING_EVENT)

    def test_re_ingesting_the_same_payload_creates_no_second_movement(self):
        self._persist()
        self._persist()
        self._persist()

        self.assertEqual(ContainerMovement.objects.filter(team=self.team).count(), 1)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 1)

    def test_ingesting_a_non_physical_event_stores_it_and_moves_nothing(self):
        event = self._persist(event_code="ARRI", description="Vessel arrived", raw_event_id="EVT-2")

        self.assertEqual(event.event_type, TrackingEvent.EventType.VESSEL_ARRIVED)
        self.assertEqual(event.location, self.port)
        self.assertFalse(ContainerMovement.objects.filter(team=self.team).exists())
        self.container.refresh_from_db()
        self.assertIsNone(self.container.current_location)

    def test_an_event_whose_place_is_unknown_is_stored_and_moves_nothing(self):
        event = self._persist(location_name="SOMEWHERE NOBODY RECORDED", raw_event_id="EVT-3")

        self.assertEqual(event.location_name, "SOMEWHERE NOBODY RECORDED")
        self.assertIsNone(event.location_id)
        self.assertFalse(ContainerMovement.objects.filter(team=self.team).exists())


class TenantIsolationTest(TestCase):
    """A movement cannot be created for another tenant's container."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Ours", slug="loc2-ours")
        cls.other_team = Team.objects.create(name="Theirs", slug="loc2-theirs")
        cls.our_port = ContainerLocation.objects.create(team=cls.team, name="Göteborg", location_type=LocationType.PORT)

    def test_an_event_naming_another_teams_container_moves_nothing(self):
        theirs = Container.objects.create(
            team=self.other_team,
            owner_code="ABC",
            category_id="U",
            serial_number="654321",
            check_digit=calculate_check_digit("ABC", "U", "654321"),
            equipment_type=_equipment(),
        )
        event = TrackingEvent.objects.create(
            team=self.team,
            provider=_provider(),
            container=theirs,
            event_type=TrackingEvent.EventType.GATE_IN,
            event_time_type=TrackingEvent.EventTimeType.ACTUAL,
            event_datetime=_EVENT_TIME,
            location=self.our_port,
            location_resolution_status=LocationResolutionStatus.RESOLVED,
            event_fingerprint="fp-cross",
        )

        self.assertIsNone(interpret_tracking_event(self.team, event))
        self.assertFalse(ContainerMovement.objects.exists())
