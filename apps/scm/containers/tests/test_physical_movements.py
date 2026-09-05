"""The physical state model: what moves a container, and what must not.

The property under test throughout is the one LOC-2 exists for — tracking evidence
is not physical state. A carrier's report reaches the database, and only the
precedence rules in ``movements.py`` decide whether it also becomes where the box
is.

The scenarios are written the way they happen operationally, with real clock times,
because the whole point is that ordering follows ``occurred_at`` and not the order
the rows were inserted. A test that wrote its movements in chronological order would
pass against a service that simply took the last write.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.choices import LocationSource, LocationType, MovementType
from apps.scm.containers.models import Container, ContainerLocation, ContainerMovement, EquipmentType
from apps.scm.containers.movements import (
    get_current_state_movement,
    project_container_state,
    record_container_movement,
)
from apps.scm.containers.utils import calculate_check_digit
from apps.teams.models import Team


def _equipment() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team, owner="XIN", serial="123456") -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit(owner, "U", serial),
        equipment_type=_equipment(),
    )


def _location(team, name, location_type=LocationType.DEPOT, parent=None) -> ContainerLocation:
    return ContainerLocation.objects.create(team=team, name=name, location_type=location_type, parent_location=parent)


class MovementSemanticsTest(TestCase):
    """Each movement type's from/to shape, and what it leaves current_location as."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Gate", slug="loc2-gate")
        cls.terminal = _location(cls.team, "Oceanterminalen")
        cls.depot = _location(cls.team, "MCR Depot")

    def setUp(self):
        self.container = _container(self.team)

    def test_gate_in_records_a_movement_and_sets_the_location(self):
        movement = record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            gate_name="John Evans",
        )

        self.assertEqual(movement.movement_type, MovementType.GATE_IN)
        self.assertEqual(movement.to_location, self.terminal)
        self.assertEqual(movement.gate_name, "John Evans")
        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, self.terminal)
        self.assertEqual(self.container.location_source, LocationSource.MANUAL)

    def test_gate_out_without_a_destination_clears_the_location(self):
        """The box left and nobody recorded where it went. That is not "still here"."""
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(hours=3),
        )
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_OUT,
        )

        self.container.refresh_from_db()
        self.assertIsNone(self.container.current_location)

    def test_gate_out_takes_its_origin_from_where_the_container_is(self):
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(hours=3),
        )
        movement = record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_OUT,
        )

        self.assertEqual(movement.from_location, self.terminal)

    def test_gate_out_to_a_known_place_moves_the_container_there(self):
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(hours=3),
        )
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_OUT,
            to_location=self.depot,
        )

        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, self.depot)

    def test_transfer_moves_from_a_to_b(self):
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.RECEIVED,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(hours=2),
        )
        movement = record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.TRANSFER,
            from_location=self.terminal,
            to_location=self.depot,
        )

        self.assertEqual(movement.from_location, self.terminal)
        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, self.depot)

    def test_received_sets_the_receiving_location(self):
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.RECEIVED,
            to_location=self.depot,
        )
        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, self.depot)


class MovementValidationTest(TestCase):
    """Accepted movements are held to a standard carrier evidence is not."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Valid", slug="loc2-valid")
        cls.other_team = Team.objects.create(name="Other", slug="loc2-other")
        cls.terminal = _location(cls.team, "Oceanterminalen")
        cls.foreign = _location(cls.other_team, "Someone Else's Depot")

    def setUp(self):
        self.container = _container(self.team)

    def test_gate_in_needs_a_destination(self):
        with self.assertRaises(ValidationError):
            record_container_movement(
                team=self.team,
                container=self.container,
                movement_type=MovementType.GATE_IN,
            )

    def test_gate_out_needs_an_origin_it_can_find(self):
        """Nothing has ever placed this box, so there is nowhere for it to leave."""
        with self.assertRaises(ValidationError):
            record_container_movement(
                team=self.team,
                container=self.container,
                movement_type=MovementType.GATE_OUT,
            )

    def test_a_transfer_between_the_same_two_places_is_rejected(self):
        with self.assertRaises(ValidationError):
            record_container_movement(
                team=self.team,
                container=self.container,
                movement_type=MovementType.TRANSFER,
                from_location=self.terminal,
                to_location=self.terminal,
            )

    def test_no_movement_may_reference_another_tenants_location(self):
        with self.assertRaises(ValidationError):
            record_container_movement(
                team=self.team,
                container=self.container,
                movement_type=MovementType.GATE_IN,
                to_location=self.foreign,
            )
        self.assertFalse(ContainerMovement.objects.filter(container=self.container).exists())

    def test_no_movement_may_reference_another_tenants_container(self):
        theirs = _container(self.other_team, owner="ABC", serial="999999")
        with self.assertRaises(ValidationError):
            record_container_movement(
                team=self.team,
                container=theirs,
                movement_type=MovementType.GATE_IN,
                to_location=self.terminal,
            )
        self.assertFalse(ContainerMovement.objects.filter(container=theirs).exists())

    def test_a_rejected_movement_leaves_the_container_where_it_was(self):
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
        )
        with self.assertRaises(ValidationError):
            record_container_movement(
                team=self.team,
                container=self.container,
                movement_type=MovementType.TRANSFER,
                from_location=self.terminal,
                to_location=self.terminal,
            )
        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, self.terminal)


class StatePrecedenceTest(TestCase):
    """The rules that stop weak, late evidence from rewriting what we know.

    These are the LOC-2 scenarios verbatim: a carrier discharge in the morning, an
    operator's gate move in the afternoon, and the carrier's version turning up
    hours after the fact.
    """

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Precedence", slug="loc2-prec")
        cls.port = _location(cls.team, "Göteborg", LocationType.PORT)
        cls.terminal = _location(cls.team, "Oceanterminalen", parent=cls.port)

    def setUp(self):
        self.container = _container(self.team)
        self.today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

    def _at(self, hour, minute=0):
        return self.today + timedelta(hours=hour, minutes=minute)

    def test_a_late_carrier_event_does_not_undo_an_afternoon_gate_in(self):
        """10:00 discharge, 14:32 gate in, carrier's version ingested at 15:10.

        The carrier movement is written last and is still the older event, so it
        goes into the history and changes nothing.
        """
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=self._at(14, 32),
            source=LocationSource.MANUAL,
        )
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.port,
            occurred_at=self._at(9, 30),
            source=LocationSource.TRACKING_EVENT,
        )

        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, self.terminal)
        self.assertEqual(self.container.last_location_update, self._at(14, 32))
        # Recorded, though: the history keeps both.
        self.assertEqual(ContainerMovement.objects.filter(container=self.container).count(), 2)

    def test_a_late_carrier_event_does_not_restore_a_location_after_gate_out(self):
        """Gate out at 17:00; a 16:00 carrier event received at 17:30 arrives after."""
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=self._at(14, 32),
        )
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_OUT,
            occurred_at=self._at(17, 0),
        )
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=self._at(16, 0),
            source=LocationSource.TRACKING_EVENT,
        )

        self.container.refresh_from_db()
        self.assertIsNone(self.container.current_location)

    def test_at_the_same_instant_the_operator_outranks_the_carrier(self):
        """Two claims about one moment. Somebody saw the box; the carrier inferred it."""
        moment = self._at(14, 32)
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=moment,
            source=LocationSource.MANUAL,
        )
        # Written second, so insertion order alone would hand it the state.
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.port,
            occurred_at=moment,
            source=LocationSource.TRACKING_EVENT,
        )

        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, self.terminal)

    def test_at_the_same_instant_a_depot_outranks_an_import(self):
        moment = self._at(11, 0)
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.RECEIVED,
            to_location=self.port,
            occurred_at=moment,
            source=LocationSource.IMPORT,
        )
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.RECEIVED,
            to_location=self.terminal,
            occurred_at=moment,
            source=LocationSource.DEPOT,
        )

        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, self.terminal)

    def test_a_genuinely_newer_carrier_movement_does_update_the_state(self):
        """Precedence is not "carriers never count". Time leads, and this is newer.

        Nothing stronger contradicts it: the last observation of the box was six
        hours earlier, and a carrier that has since seen it somewhere else is the
        newer truth.
        """
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=self._at(8, 0),
            source=LocationSource.MANUAL,
        )
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.port,
            occurred_at=self._at(14, 0),
            source=LocationSource.TRACKING_EVENT,
        )

        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, self.port)
        self.assertEqual(self.container.location_source, LocationSource.TRACKING_EVENT)

    def test_identical_time_and_source_falls_back_to_the_later_record(self):
        """The documented tie-breaker of last resort, and it must be deterministic."""
        moment = self._at(12, 0)
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.RECEIVED,
            to_location=self.port,
            occurred_at=moment,
        )
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.RECEIVED,
            to_location=self.terminal,
            occurred_at=moment,
        )

        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, self.terminal)

    def test_a_history_only_movement_is_stored_and_ignored(self):
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=self._at(9, 0),
        )
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.port,
            occurred_at=self._at(18, 0),
            affects_current_state=False,
        )

        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, self.terminal)
        self.assertEqual(ContainerMovement.objects.filter(container=self.container).count(), 2)

    def test_the_winning_movement_explains_the_current_location(self):
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=self._at(14, 32),
        )
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.port,
            occurred_at=self._at(9, 30),
            source=LocationSource.TRACKING_EVENT,
        )

        winner = get_current_state_movement(self.team, self.container)
        self.assertIsNotNone(winner)
        self.assertEqual(winner.to_location, self.terminal)
        self.assertEqual(winner.source, LocationSource.MANUAL)


class ProjectionTest(TestCase):
    """current_location is derived, and deriving it again must change nothing."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Projection", slug="loc2-proj")
        cls.terminal = _location(cls.team, "Oceanterminalen")

    def test_projecting_twice_gives_the_same_answer(self):
        container = _container(self.team)
        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
        )
        container.refresh_from_db()

        project_container_state(team=self.team, container=container)
        project_container_state(team=self.team, container=container)

        container.refresh_from_db()
        self.assertEqual(container.current_location, self.terminal)

    def test_a_location_with_no_movement_history_is_left_alone(self):
        """No reverse inference: a legacy position is not deleted for lacking a movement."""
        container = _container(self.team)
        container.current_location = self.terminal
        container.save(update_fields=["current_location"])

        winner = project_container_state(team=self.team, container=container)

        self.assertIsNone(winner)
        container.refresh_from_db()
        self.assertEqual(container.current_location, self.terminal)
        self.assertFalse(ContainerMovement.objects.filter(container=container).exists())
