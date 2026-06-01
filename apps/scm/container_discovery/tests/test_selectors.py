"""Tests for container discovery selectors (dashboard metrics)."""

from django.test import TestCase

from apps.scm.container_discovery.models import ContainerPool, ContainerPoolStatus
from apps.scm.container_discovery.selectors import get_container_discovery_dashboard
from apps.teams.models import Team


def _team(slug="cd-sel-team") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


class DiscoveryDashboardMetricsTest(TestCase):
    def test_planned_count(self):
        team = _team()
        ContainerPool.objects.create(team=team, container_number="MCUU0001", status=ContainerPoolStatus.PLANNED)
        ContainerPool.objects.create(team=team, container_number="MCUU0002", status=ContainerPoolStatus.PLANNED)
        dashboard = get_container_discovery_dashboard(team=team)
        self.assertEqual(dashboard["planned_count"], 2)

    def test_detected_count(self):
        team = _team()
        ContainerPool.objects.create(team=team, container_number="MCUU0010", status=ContainerPoolStatus.DETECTED)
        dashboard = get_container_discovery_dashboard(team=team)
        self.assertEqual(dashboard["detected_count"], 1)

    def test_planned_and_detected_counts_are_independent(self):
        team = _team()
        ContainerPool.objects.create(team=team, container_number="MCUU0020", status=ContainerPoolStatus.PLANNED)
        ContainerPool.objects.create(team=team, container_number="MCUU0021", status=ContainerPoolStatus.DETECTED)
        ContainerPool.objects.create(team=team, container_number="MCUU0022", status=ContainerPoolStatus.RETIRED)
        dashboard = get_container_discovery_dashboard(team=team)
        self.assertEqual(dashboard["planned_count"], 1)
        self.assertEqual(dashboard["detected_count"], 1)

    def test_in_transit_and_arrived_default_zero(self):
        team = _team()
        dashboard = get_container_discovery_dashboard(team=team)
        self.assertEqual(dashboard["in_transit_count"], 0)
        self.assertEqual(dashboard["arrived_count"], 0)
