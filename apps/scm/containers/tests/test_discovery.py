"""Tests for container discovery workflow (PlannedContainer)."""

from django.test import TestCase

from apps.scm.containers.discovery import (
    add_planned_container,
    cancel_planned_container,
    get_planned_containers,
    get_planned_containers_for_discovery,
    mark_planned_container_arrived,
    mark_planned_container_detected,
    mark_planned_container_in_transit,
    run_discovery_for_team,
)
from apps.scm.containers.models import PlannedContainer, PlannedContainerStatus
from apps.teams.models import Team


def _team(slug):
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


class AddPlannedContainerTest(TestCase):
    def test_creates_planned_container(self):
        team = _team("disc-add")
        pc = add_planned_container(team, "MCUU1000001")
        self.assertEqual(pc.container_number, "MCUU1000001")
        self.assertEqual(pc.status, PlannedContainerStatus.PLANNED)
        self.assertEqual(pc.team, team)

    def test_uppercases_container_number(self):
        team = _team("disc-upper")
        pc = add_planned_container(team, "mcuu1000002")
        self.assertEqual(pc.container_number, "MCUU1000002")

    def test_idempotent_add(self):
        team = _team("disc-idempotent")
        pc1 = add_planned_container(team, "MCUU1000003")
        pc2 = add_planned_container(team, "MCUU1000003")
        self.assertEqual(pc1.pk, pc2.pk)
        self.assertEqual(PlannedContainer.objects.filter(team=team, container_number="MCUU1000003").count(), 1)

    def test_team_isolation(self):
        team1 = _team("disc-iso-1")
        team2 = _team("disc-iso-2")
        add_planned_container(team1, "MCUU1000004")
        add_planned_container(team2, "MCUU1000004")
        self.assertEqual(PlannedContainer.objects.filter(container_number="MCUU1000004").count(), 2)
        self.assertEqual(PlannedContainer.objects.filter(team=team1, container_number="MCUU1000004").count(), 1)


class StatusTransitionTest(TestCase):
    def _planned(self, team, number="MCUU2000001"):
        return add_planned_container(team, number)

    def test_mark_detected(self):
        team = _team("disc-detected")
        pc = self._planned(team)
        updated = mark_planned_container_detected(pc)
        self.assertEqual(updated.status, PlannedContainerStatus.DETECTED)
        self.assertIsNotNone(updated.detected_at)

    def test_mark_in_transit(self):
        team = _team("disc-in-transit")
        pc = self._planned(team, "MCUU2000002")
        mark_planned_container_detected(pc)
        updated = mark_planned_container_in_transit(pc)
        self.assertEqual(updated.status, PlannedContainerStatus.IN_TRANSIT)

    def test_mark_arrived(self):
        team = _team("disc-arrived")
        pc = self._planned(team, "MCUU2000003")
        updated = mark_planned_container_arrived(pc)
        self.assertEqual(updated.status, PlannedContainerStatus.ARRIVED)

    def test_cancel(self):
        team = _team("disc-cancel")
        pc = self._planned(team, "MCUU2000004")
        updated = cancel_planned_container(pc)
        self.assertEqual(updated.status, PlannedContainerStatus.CANCELLED)


class GetPlannedContainersTest(TestCase):
    def test_filters_by_status(self):
        team = _team("disc-filter")
        add_planned_container(team, "MCUU3000001")
        pc2 = add_planned_container(team, "MCUU3000002")
        mark_planned_container_detected(pc2)

        planned = list(get_planned_containers(team, status="planned"))
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].container_number, "MCUU3000001")

    def test_returns_all_without_filter(self):
        team = _team("disc-all")
        add_planned_container(team, "MCUU3000003")
        add_planned_container(team, "MCUU3000004")
        self.assertEqual(get_planned_containers(team).count(), 2)

    def test_team_scoped(self):
        team1 = _team("disc-scope-1")
        team2 = _team("disc-scope-2")
        add_planned_container(team1, "MCUU3000005")
        add_planned_container(team2, "MCUU3000006")
        self.assertEqual(get_planned_containers(team1).count(), 1)

    def test_discovery_queue_only_planned(self):
        team = _team("disc-queue")
        add_planned_container(team, "MCUU4000001")
        pc2 = add_planned_container(team, "MCUU4000002")
        mark_planned_container_detected(pc2)

        queue = list(get_planned_containers_for_discovery(team))
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].container_number, "MCUU4000001")


class RunDiscoveryTest(TestCase):
    def test_run_with_no_providers(self):
        team = _team("disc-run-empty")
        add_planned_container(team, "MCUU5000001")
        add_planned_container(team, "MCUU5000002")

        summary = run_discovery_for_team(team=team, providers=[])
        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["detected"], 0)
        self.assertEqual(summary["errors"], [])

    def test_run_updates_last_checked_at(self):
        team = _team("disc-run-checked")
        pc = add_planned_container(team, "MCUU5000003")
        self.assertIsNone(pc.last_checked_at)

        run_discovery_for_team(team=team, providers=[])

        pc.refresh_from_db()
        self.assertIsNotNone(pc.last_checked_at)

    def test_run_with_mock_provider_detects(self):
        """A provider that returns True causes status → DETECTED."""
        team = _team("disc-run-detect")
        add_planned_container(team, "MCUU5000004")

        class _MockProvider:
            def check_container_exists(self, container_number):
                return True

        summary = run_discovery_for_team(team=team, providers=[_MockProvider()])
        self.assertEqual(summary["detected"], 1)
        pc = PlannedContainer.objects.get(team=team, container_number="MCUU5000004")
        self.assertEqual(pc.status, PlannedContainerStatus.DETECTED)
