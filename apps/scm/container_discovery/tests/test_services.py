"""Tests for container pool services (create, mark detected, retire)."""

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.scm.container_discovery.models import ContainerPool, ContainerPoolStatus
from apps.scm.container_discovery.services import (
    create_planned_container,
    mark_container_detected,
    retire_planned_container,
)
from apps.teams.models import Team


def _team(slug="cd-svc-team") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


class CreatePlannedContainerTest(TestCase):
    def test_creates_planned_entry(self):
        team = _team()
        entry = create_planned_container(team=team, container_number="MCUU0000001")
        self.assertEqual(entry.status, ContainerPoolStatus.PLANNED)
        self.assertEqual(entry.container_number, "MCUU0000001")

    def test_normalises_to_uppercase(self):
        team = _team()
        entry = create_planned_container(team=team, container_number="mcuu0000002")
        self.assertEqual(entry.container_number, "MCUU0000002")

    def test_raises_on_duplicate(self):
        team = _team()
        create_planned_container(team=team, container_number="MCUU0000003")
        with self.assertRaises(ValidationError):
            create_planned_container(team=team, container_number="MCUU0000003")


class MarkContainerDetectedTest(TestCase):
    def test_transitions_planned_to_detected(self):
        team = _team()
        entry = create_planned_container(team=team, container_number="MCUU0000010")
        updated = mark_container_detected(entry)
        self.assertEqual(updated.status, ContainerPoolStatus.DETECTED)

    def test_does_not_change_already_detected(self):
        team = _team()
        entry = ContainerPool.objects.create(
            team=team, container_number="MCUU0000011", status=ContainerPoolStatus.DETECTED
        )
        result = mark_container_detected(entry)
        self.assertEqual(result.status, ContainerPoolStatus.DETECTED)


class RetirePlannedContainerTest(TestCase):
    def test_retires_container(self):
        team = _team()
        entry = create_planned_container(team=team, container_number="MCUU0000020")
        updated = retire_planned_container(entry)
        self.assertEqual(updated.status, ContainerPoolStatus.RETIRED)


class GetPlannedContainersTest(TestCase):
    def test_returns_only_planned(self):
        from apps.scm.container_discovery.selectors import get_planned_containers

        team = _team()
        ContainerPool.objects.create(team=team, container_number="MCUU0000030", status=ContainerPoolStatus.PLANNED)
        ContainerPool.objects.create(team=team, container_number="MCUU0000031", status=ContainerPoolStatus.DETECTED)
        ContainerPool.objects.create(team=team, container_number="MCUU0000032", status=ContainerPoolStatus.RETIRED)

        planned = list(get_planned_containers(team=team))
        numbers = [e.container_number for e in planned]
        self.assertIn("MCUU0000030", numbers)
        self.assertNotIn("MCUU0000031", numbers)
        self.assertNotIn("MCUU0000032", numbers)
