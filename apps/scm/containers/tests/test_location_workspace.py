"""The Location Workspace: what is here, what moved, and what it refuses to guess.

The load-bearing assertions here are the negative ones. Inventory must contain only
containers whose ``current_location`` is this location — not containers that merely
passed through, and never another team's. And Expected arrivals must stay
unavailable: no shipment destination is reliably resolvable to a location record,
and a count matched on place names would be confidently wrong.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.choices import ContainerStatus, LocationSource, LocationType, MovementType
from apps.scm.containers.models import Container, ContainerLocation, ContainerMovement, EquipmentType
from apps.scm.containers.selectors import get_location_workspace
from apps.scm.containers.services import set_container_location
from apps.scm.containers.utils import calculate_check_digit
from apps.teams.models import Team

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _equipment_type(iso_code="45G1", description="40' HC") -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code=iso_code,
        defaults={"category": "GP", "length_ft": 40, "high_cube": True, "description": description},
    )[0]


def _container(team, serial, *, owner="MCU", location=None, status=ContainerStatus.AVAILABLE) -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit(owner, "U", serial),
        equipment_type=_equipment_type(),
        current_location=location,
        status=status,
    )


def _location(team, name="Oceanterminalen", **kwargs) -> ContainerLocation:
    return ContainerLocation.objects.create(
        team=team,
        name=name,
        location_type=kwargs.pop("location_type", LocationType.DEPOT),
        city=kwargs.pop("city", "Gothenburg"),
        country=kwargs.pop("country", "Sweden"),
        **kwargs,
    )


def _movement(team, container, *, to_location=None, from_location=None, when=None, **kwargs) -> ContainerMovement:
    return ContainerMovement.objects.create(
        team=team,
        container=container,
        from_location=from_location,
        to_location=to_location,
        movement_type=kwargs.pop("movement_type", MovementType.POSITION_UPDATE),
        occurred_at=when or timezone.now(),
        source=kwargs.pop("source", LocationSource.MANUAL),
        **kwargs,
    )


class LocationInventoryTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Inv", slug="loc-ws-inv")
        cls.depot = _location(cls.team)
        cls.other_place = _location(cls.team, name="John Evans", city="Rotterdam")

        cls.here = _container(cls.team, "200930", location=cls.depot)
        cls.also_here = _container(cls.team, "200931", location=cls.depot, status=ContainerStatus.BOOKED)
        cls.elsewhere = _container(cls.team, "200932", location=cls.other_place)
        cls.nowhere = _container(cls.team, "200933")

    def setUp(self):
        self.workspace = get_location_workspace(team=self.team, location=self.depot)

    def test_inventory_is_only_what_is_currently_here(self):
        numbers = {container.container_id for container in self.workspace.inventory}
        self.assertEqual(numbers, {self.here.container_id, self.also_here.container_id})

    def test_containers_at_another_location_are_excluded(self):
        self.assertNotIn(self.elsewhere, list(self.workspace.inventory))

    def test_containers_with_no_location_are_excluded(self):
        self.assertNotIn(self.nowhere, list(self.workspace.inventory))

    def test_the_count_matches_the_inventory(self):
        self.assertEqual(self.workspace.container_count, 2)

    def test_the_status_breakdown_only_lists_statuses_that_are_present(self):
        present = {(row.status, row.count) for row in self.workspace.occupied_status_counts}
        self.assertEqual(present, {(ContainerStatus.AVAILABLE, 1), (ContainerStatus.BOOKED, 1)})

    def test_a_container_that_only_passed_through_is_not_inventory(self):
        # It moved out again: the movement is history, the current location is not here.
        passed_through = _container(self.team, "200934", location=self.other_place)
        _movement(self.team, passed_through, to_location=self.depot)
        _movement(self.team, passed_through, from_location=self.depot, to_location=self.other_place)

        workspace = get_location_workspace(team=self.team, location=self.depot)

        self.assertNotIn(passed_through, list(workspace.inventory))
        self.assertEqual(workspace.container_count, 2)

    def test_an_empty_location_reports_itself_as_empty(self):
        empty = _location(self.team, name="Nowhere Depot")
        workspace = get_location_workspace(team=self.team, location=empty)

        self.assertTrue(workspace.is_empty)
        self.assertEqual(workspace.container_count, 0)
        self.assertEqual(workspace.occupied_status_counts, [])


class LocationSinceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Since", slug="loc-ws-since")
        cls.depot = _location(cls.team)

    def test_since_comes_from_the_movement_into_this_location(self):
        container = _container(self.team, "300001")
        arrived = timezone.now() - timedelta(days=2)
        set_container_location(container, self.depot, occurred_at=arrived)

        workspace = get_location_workspace(team=self.team, location=self.depot)
        row = next(iter(workspace.inventory))

        self.assertEqual(row.at_location_since, arrived)

    def test_since_is_the_latest_arrival_when_a_box_came_back(self):
        container = _container(self.team, "300002")
        elsewhere = _location(self.team, name="Elsewhere")
        first = timezone.now() - timedelta(days=10)
        latest = timezone.now() - timedelta(days=1)
        _movement(self.team, container, to_location=self.depot, when=first)
        _movement(self.team, container, from_location=self.depot, to_location=elsewhere, when=first)
        set_container_location(container, self.depot, occurred_at=latest)

        workspace = get_location_workspace(team=self.team, location=self.depot)
        row = next(iter(workspace.inventory))

        self.assertEqual(row.at_location_since, latest)

    def test_since_is_none_when_nothing_recorded_how_the_box_got_here(self):
        # Location set directly on the row, with no movement and no location stamp:
        # nothing recorded when it arrived, so nothing is reported.
        _container(self.team, "300003", location=self.depot)

        workspace = get_location_workspace(team=self.team, location=self.depot)
        row = next(iter(workspace.inventory))

        self.assertIsNone(row.at_location_since)

    def test_containers_with_no_arrival_time_sort_last(self):
        undated = _container(self.team, "300004", location=self.depot)
        dated = _container(self.team, "300005")
        set_container_location(dated, self.depot, occurred_at=timezone.now() - timedelta(days=5))

        workspace = get_location_workspace(team=self.team, location=self.depot)
        order = [container.pk for container in workspace.inventory]

        self.assertEqual(order, [dated.pk, undated.pk])


class LocationMovementTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Move", slug="loc-ws-move")
        cls.depot = _location(cls.team)
        cls.elsewhere = _location(cls.team, name="John Evans", city="Rotterdam")
        cls.container = _container(cls.team, "400001", location=cls.depot)

    def test_a_movement_into_the_location_is_shown(self):
        movement = _movement(self.team, self.container, from_location=self.elsewhere, to_location=self.depot)
        workspace = get_location_workspace(team=self.team, location=self.depot)
        self.assertIn(movement, workspace.recent_movements)

    def test_a_movement_out_of_the_location_is_shown(self):
        movement = _movement(self.team, self.container, from_location=self.depot, to_location=self.elsewhere)
        workspace = get_location_workspace(team=self.team, location=self.depot)
        self.assertIn(movement, workspace.recent_movements)

    def test_a_movement_between_two_other_places_is_not_shown(self):
        third = _location(self.team, name="Somewhere else")
        movement = _movement(self.team, self.container, from_location=self.elsewhere, to_location=third)
        workspace = get_location_workspace(team=self.team, location=self.depot)
        self.assertNotIn(movement, workspace.recent_movements)

    def test_movements_are_newest_first(self):
        old = _movement(self.team, self.container, to_location=self.depot, when=timezone.now() - timedelta(days=5))
        new = _movement(self.team, self.container, to_location=self.depot, when=timezone.now() - timedelta(hours=1))
        workspace = get_location_workspace(team=self.team, location=self.depot)
        self.assertEqual(workspace.recent_movements[:2], [new, old])

    def test_recent_arrivals_are_counted_over_the_last_week(self):
        _movement(self.team, self.container, to_location=self.depot, when=timezone.now() - timedelta(days=2))
        _movement(self.team, self.container, to_location=self.depot, when=timezone.now() - timedelta(days=30))
        _movement(self.team, self.container, from_location=self.depot, when=timezone.now() - timedelta(days=1))

        workspace = get_location_workspace(team=self.team, location=self.depot)

        self.assertEqual(workspace.moved_in_last_week, 1)

    def test_a_location_with_no_movements_says_so(self):
        quiet = _location(self.team, name="Quiet Depot")
        workspace = get_location_workspace(team=self.team, location=quiet)
        self.assertFalse(workspace.has_movement_history)


class LocationExpectedArrivalsTest(TestCase):
    """Expected arrivals must stay unavailable until the domain can answer them."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Exp", slug="loc-ws-exp")
        cls.depot = _location(cls.team)

    def test_expected_arrivals_are_not_claimed(self):
        workspace = get_location_workspace(team=self.team, location=self.depot)

        self.assertFalse(workspace.expected.is_available)
        self.assertEqual(workspace.expected.objects, [])
        self.assertEqual(workspace.expected.count, 0)

    def test_the_reason_is_stated_rather_than_left_blank(self):
        workspace = get_location_workspace(team=self.team, location=self.depot)
        self.assertTrue(workspace.expected.reason)

    def test_a_shipment_named_after_the_city_does_not_become_an_expected_arrival(self):
        from apps.scm.shipments.models import Shipment, ShipmentContainer

        container = _container(self.team, "500001")
        shipment = Shipment.objects.create(
            team=self.team,
            shipment_number="SH-CITY",
            destination_port="Gothenburg",
            eta=timezone.localdate() + timedelta(days=3),
        )
        ShipmentContainer.objects.create(shipment=shipment, container=container)

        workspace = get_location_workspace(team=self.team, location=self.depot)

        self.assertEqual(workspace.expected.count, 0)


class LocationTeamIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Mine", slug="loc-ws-mine")
        cls.other = Team.objects.create(name="Theirs", slug="loc-ws-theirs")
        cls.depot = _location(cls.team)

    def test_another_teams_container_pointing_here_is_not_inventory(self):
        foreign = _container(self.other, "600001", location=self.depot)

        workspace = get_location_workspace(team=self.team, location=self.depot)

        self.assertNotIn(foreign, list(workspace.inventory))
        self.assertEqual(workspace.container_count, 0)

    def test_another_teams_movement_here_is_not_activity(self):
        foreign = _container(self.other, "600002", location=self.depot)
        movement = _movement(self.other, foreign, to_location=self.depot)

        workspace = get_location_workspace(team=self.team, location=self.depot)

        self.assertNotIn(movement, workspace.recent_movements)
