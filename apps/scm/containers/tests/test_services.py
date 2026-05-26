"""
Kort 5 — Service-test.
Acceptanskriterier: Services skapar och uppdaterar containers med korrekt team-tillhörighet.
"""
from django.test import TestCase

from apps.teams.models import Team
from apps.scm.containers.models import Container
from apps.scm.containers.services import create_container, delete_container, update_container


class CreateContainerTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Service Team", slug="service-team")

    def test_create_container_sets_team(self):
        container = create_container(
            team=self.team,
            container_number="MSCU1234567",
            carrier="MSC",
            status="planned",
        )
        self.assertEqual(container.team, self.team)
        self.assertEqual(container.container_number, "MSCU1234567")
        self.assertEqual(container.carrier, "MSC")

    def test_create_container_persists_to_db(self):
        container = create_container(team=self.team, container_number="SAVE0000001")
        self.assertIsNotNone(container.pk)
        self.assertTrue(Container.objects.filter(pk=container.pk).exists())


class UpdateContainerTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Update Team", slug="update-team")

    def test_update_container_changes_fields(self):
        container = Container.objects.create(
            team=self.team, container_number="UPD00000001", status="planned"
        )
        updated = update_container(container, status="in_transit", carrier="MSC")
        self.assertEqual(updated.status, "in_transit")
        self.assertEqual(updated.carrier, "MSC")

    def test_update_container_persists_to_db(self):
        container = Container.objects.create(
            team=self.team, container_number="PERSIST0001", status="planned"
        )
        update_container(container, status="delivered")
        container.refresh_from_db()
        self.assertEqual(container.status, "delivered")


class DeleteContainerTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Delete Team", slug="delete-team")

    def test_delete_container_removes_from_db(self):
        container = Container.objects.create(
            team=self.team, container_number="DEL00000001"
        )
        pk = container.pk
        delete_container(container)
        self.assertFalse(Container.objects.filter(pk=pk).exists())
