"""Tests for ContainerPool and ContainerDiscoveryEvent models."""

from django.db import IntegrityError
from django.test import TestCase

from apps.scm.container_discovery.models import (
    ContainerDiscoveryEvent,
    ContainerPool,
    ContainerPoolStatus,
)
from apps.teams.models import Team


def _team(slug="cd-model-team") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _pool_entry(team, container_number="MCUU1234567", status=ContainerPoolStatus.PLANNED) -> ContainerPool:
    return ContainerPool.objects.create(team=team, container_number=container_number, status=status)


class ContainerPoolModelTest(TestCase):
    def test_create_planned_container(self):
        team = _team()
        entry = _pool_entry(team)
        self.assertEqual(entry.container_number, "MCUU1234567")
        self.assertEqual(entry.team, team)

    def test_default_status_is_planned(self):
        team = _team()
        entry = ContainerPool.objects.create(team=team, container_number="MCUU0000001")
        self.assertEqual(entry.status, ContainerPoolStatus.PLANNED)

    def test_unique_container_number_per_team(self):
        team = _team()
        _pool_entry(team, container_number="MCUU9999999")
        with self.assertRaises(IntegrityError):
            ContainerPool.objects.create(team=team, container_number="MCUU9999999")

    def test_same_container_number_allowed_for_different_teams(self):
        team_a = _team(slug="cd-team-a")
        team_b = _team(slug="cd-team-b")
        entry_a = ContainerPool.objects.create(team=team_a, container_number="MCUU1111111")
        entry_b = ContainerPool.objects.create(team=team_b, container_number="MCUU1111111")
        self.assertNotEqual(entry_a.pk, entry_b.pk)

    def test_str(self):
        team = _team()
        entry = _pool_entry(team)
        self.assertIn("MCUU1234567", str(entry))

    def test_timestamps_set(self):
        team = _team()
        entry = _pool_entry(team)
        self.assertIsNotNone(entry.created_at)
        self.assertIsNotNone(entry.updated_at)


class ContainerDiscoveryEventModelTest(TestCase):
    def test_create_detection_event(self):
        team = _team()
        pool_entry = _pool_entry(team)
        event = ContainerDiscoveryEvent.objects.create(
            team=team,
            container_pool=pool_entry,
            container_number="MCUU1234567",
            carrier_code="dummy",
            carrier_name="Dummy Carrier",
            event_type=ContainerDiscoveryEvent.EventType.CONTAINER_DETECTED,
            payload={"source": "dummy"},
        )
        self.assertEqual(event.container_number, "MCUU1234567")
        self.assertEqual(event.event_type, ContainerDiscoveryEvent.EventType.CONTAINER_DETECTED)

    def test_payload_saved_as_json(self):
        team = _team()
        pool_entry = _pool_entry(team)
        payload = {"carrier": "dummy", "status": "IN_TRANSIT", "extra": [1, 2, 3]}
        event = ContainerDiscoveryEvent.objects.create(
            team=team,
            container_pool=pool_entry,
            container_number="MCUU1234567",
            event_type=ContainerDiscoveryEvent.EventType.CONTAINER_DETECTED,
            payload=payload,
        )
        event.refresh_from_db()
        self.assertEqual(event.payload["carrier"], "dummy")
        self.assertEqual(event.payload["extra"], [1, 2, 3])

    def test_str(self):
        team = _team()
        event = ContainerDiscoveryEvent.objects.create(
            team=team,
            container_number="MCUU1234567",
            event_type=ContainerDiscoveryEvent.EventType.SEARCH_FAILED,
            payload={},
        )
        self.assertIn("MCUU1234567", str(event))
