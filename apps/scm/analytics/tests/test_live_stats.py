"""Tests for analytics live dashboard stats selector."""

from django.test import TestCase

from apps.scm.analytics.selectors import get_live_dashboard_stats
from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment
from apps.teams.models import Team


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team: Team, owner: str = "MSC", serial: str = "111111", status: str = "AVAILABLE") -> Container:
    check = calculate_check_digit(owner, "U", serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check,
        status=status,
        equipment_type=_et(),
    )


def _shipment(team: Team, number: str, **kwargs) -> Shipment:
    return Shipment.objects.create(team=team, shipment_number=number, **kwargs)


class LiveDashboardStatsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Live Stats Team", slug="live-stats-team")
        cls.other_team = Team.objects.create(name="Other Live Stats Team", slug="other-live-stats-team")

    def test_returns_dict(self):
        stats = get_live_dashboard_stats(self.team)
        self.assertIsInstance(stats, dict)
        self.assertIn("active_shipments", stats)
        self.assertIn("delayed_shipments", stats)
        self.assertIn("containers_in_transit", stats)
        self.assertIn("containers_available", stats)
        self.assertIn("tracking_issues", stats)

    def test_counts_active_shipments(self):
        _shipment(self.team, "LS-ACTIVE", status=Shipment.Status.IN_TRANSIT)
        _shipment(self.team, "LS-DRAFT", status=Shipment.Status.DRAFT)
        stats = get_live_dashboard_stats(self.team)
        self.assertGreaterEqual(stats["active_shipments"], 1)

    def test_does_not_count_other_team_shipments(self):
        _shipment(self.other_team, "LS-OTHER-ACTIVE", status=Shipment.Status.IN_TRANSIT)
        stats = get_live_dashboard_stats(self.team)
        # Should not include other team's shipment
        other_stats = get_live_dashboard_stats(self.other_team)
        self.assertNotEqual(stats["active_shipments"], other_stats["active_shipments"])

    def test_counts_containers_in_transit(self):
        _container(self.team, owner="CTR", serial="555555", status="IN_TRANSIT")
        stats = get_live_dashboard_stats(self.team)
        self.assertGreaterEqual(stats["containers_in_transit"], 1)

    def test_counts_available_containers(self):
        _container(self.team, owner="AVL", serial="444444", status="AVAILABLE")
        stats = get_live_dashboard_stats(self.team)
        self.assertGreaterEqual(stats["containers_available"], 1)

    def test_other_team_containers_not_counted(self):
        _container(self.other_team, owner="OTH", serial="777777", status="IN_TRANSIT")
        stats = get_live_dashboard_stats(self.team)
        other_stats = get_live_dashboard_stats(self.other_team)
        self.assertNotEqual(stats["containers_in_transit"], other_stats["containers_in_transit"])
