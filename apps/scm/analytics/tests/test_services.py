"""Tests for analytics KPI service functions and snapshot generation."""

import datetime

from django.test import TestCase
from django.utils import timezone

from apps.scm.analytics.models import AnalyticsSnapshot
from apps.scm.analytics.services import (
    create_or_update_snapshot,
    get_active_shipments,
    get_average_transit_days,
    get_completed_shipments,
    get_containers_delivered,
    get_containers_in_transit,
    get_total_shipments,
)
from apps.scm.containers.choices import ContainerStatus
from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.teams.models import Team


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team: Team, owner: str = "CSQ", serial: str = "305418", **kwargs) -> Container:
    check = calculate_check_digit(owner, "U", serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_et(),
        **kwargs,
    )


def _shipment(team: Team, status=Shipment.Status.DRAFT, **kwargs) -> Shipment:
    return Shipment.objects.create(team=team, status=status, **kwargs)


class GetTotalShipmentsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="KPI Team", slug="kpi-team")
        cls.other_team = Team.objects.create(name="Other Team", slug="other-kpi-team")

    def test_counts_all_shipments(self):
        _shipment(self.team, status=Shipment.Status.DRAFT)
        _shipment(self.team, status=Shipment.Status.DELIVERED)
        self.assertEqual(get_total_shipments(self.team), 2)

    def test_filters_by_team(self):
        _shipment(self.team)
        _shipment(self.other_team)
        self.assertEqual(get_total_shipments(self.team), 1)

    def test_returns_zero_when_empty(self):
        self.assertEqual(get_total_shipments(self.team), 0)


class GetActiveShipmentsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Active Team", slug="active-team")

    def test_counts_active_statuses(self):
        _shipment(self.team, status=Shipment.Status.BOOKED)
        _shipment(self.team, status=Shipment.Status.IN_TRANSIT)
        _shipment(self.team, status=Shipment.Status.ARRIVED)
        _shipment(self.team, status=Shipment.Status.DRAFT)
        _shipment(self.team, status=Shipment.Status.DELIVERED)
        self.assertEqual(get_active_shipments(self.team), 3)

    def test_filters_by_team(self):
        other = Team.objects.create(name="Other Active", slug="other-active")
        _shipment(self.team, status=Shipment.Status.IN_TRANSIT)
        _shipment(other, status=Shipment.Status.IN_TRANSIT)
        self.assertEqual(get_active_shipments(self.team), 1)


class GetCompletedShipmentsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Completed Team", slug="completed-team")

    def test_counts_delivered(self):
        _shipment(self.team, status=Shipment.Status.DELIVERED)
        _shipment(self.team, status=Shipment.Status.IN_TRANSIT)
        self.assertEqual(get_completed_shipments(self.team), 1)

    def test_filters_by_team(self):
        other = Team.objects.create(name="Other Completed", slug="other-completed")
        _shipment(self.team, status=Shipment.Status.DELIVERED)
        _shipment(other, status=Shipment.Status.DELIVERED)
        self.assertEqual(get_completed_shipments(self.team), 1)


class GetContainersInTransitTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Transit Team", slug="transit-team")

    def test_counts_in_transit_containers(self):
        _container(self.team, owner="AAA", serial="000001", status=ContainerStatus.IN_TRANSIT)
        _container(self.team, owner="AAA", serial="000002", status=ContainerStatus.AVAILABLE)
        self.assertEqual(get_containers_in_transit(self.team), 1)

    def test_filters_by_team(self):
        other = Team.objects.create(name="Other Transit", slug="other-transit")
        _container(self.team, owner="AAA", serial="111001", status=ContainerStatus.IN_TRANSIT)
        _container(other, owner="BBB", serial="222001", status=ContainerStatus.IN_TRANSIT)
        self.assertEqual(get_containers_in_transit(self.team), 1)


class GetContainersDeliveredTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Delivered Team", slug="delivered-team")

    def test_counts_containers_on_delivered_shipments(self):
        c = _container(self.team, owner="DDD", serial="300001")
        shipment = _shipment(self.team, status=Shipment.Status.DELIVERED)
        ShipmentContainer.objects.create(shipment=shipment, container=c, sequence=1)
        self.assertEqual(get_containers_delivered(self.team), 1)

    def test_does_not_count_containers_on_active_shipments(self):
        c = _container(self.team, owner="DDD", serial="400001")
        shipment = _shipment(self.team, status=Shipment.Status.IN_TRANSIT)
        ShipmentContainer.objects.create(shipment=shipment, container=c, sequence=1)
        self.assertEqual(get_containers_delivered(self.team), 0)


class GetAverageTransitDaysTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Avg Team", slug="avg-team")

    def test_returns_none_when_no_delivered_shipments(self):
        self.assertIsNone(get_average_transit_days(self.team))

    def test_calculates_average(self):
        now = timezone.now()
        _shipment(
            self.team,
            status=Shipment.Status.DELIVERED,
            actual_departure_at=now - timezone.timedelta(days=10),
            actual_arrival_at=now,
        )
        result = get_average_transit_days(self.team)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(float(result), 10.0, places=1)


class CreateOrUpdateSnapshotTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Snapshot Team", slug="snapshot-team")
        cls.today = datetime.date(2026, 1, 20)

    def test_creates_snapshot(self):
        snapshot = create_or_update_snapshot(self.team, date=self.today)
        self.assertIsNotNone(snapshot.pk)
        self.assertEqual(snapshot.team, self.team)
        self.assertEqual(snapshot.date, self.today)

    def test_updates_existing_snapshot_instead_of_duplicating(self):
        _shipment(self.team, status=Shipment.Status.IN_TRANSIT)
        create_or_update_snapshot(self.team, date=self.today)
        _shipment(self.team, status=Shipment.Status.IN_TRANSIT)
        create_or_update_snapshot(self.team, date=self.today)
        self.assertEqual(AnalyticsSnapshot.objects.filter(team=self.team, date=self.today).count(), 1)
        snapshot = AnalyticsSnapshot.objects.get(team=self.team, date=self.today)
        self.assertEqual(snapshot.total_shipments, 2)

    def test_uses_today_when_date_is_none(self):
        from django.utils import timezone

        snapshot = create_or_update_snapshot(self.team)
        self.assertEqual(snapshot.date, timezone.localdate())
