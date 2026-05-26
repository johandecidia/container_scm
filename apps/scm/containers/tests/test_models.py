"""
Kort 2 & 5 — BaseTeamModel-arv och Container-model.
Acceptanskriterier: Container ärver BaseTeamModel och har rätt fält.
"""
import datetime

from django.test import TestCase

from apps.teams.models import BaseTeamModel, Team
from apps.scm.containers.models import Container
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser


class ContainerModelInheritanceTest(TestCase):
    def test_container_extends_base_team_model(self):
        self.assertTrue(issubclass(Container, BaseTeamModel))

    def test_container_has_team_and_timestamps(self):
        field_names = [f.name for f in Container._meta.fields]
        self.assertIn("team", field_names)
        self.assertIn("created_at", field_names)
        self.assertIn("updated_at", field_names)

    def test_container_has_required_fields(self):
        field_names = [f.name for f in Container._meta.fields]
        self.assertIn("container_number", field_names)
        self.assertIn("carrier", field_names)
        self.assertIn("status", field_names)
        self.assertIn("etd", field_names)
        self.assertIn("eta", field_names)

    def test_container_ordering(self):
        self.assertEqual(Container._meta.ordering, ["-created_at"])


class ContainerModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Test Team", slug="test-team")
        cls.user = CustomUser.objects.create_user(username="test@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_create_container(self):
        container = Container.objects.create(
            team=self.team,
            container_number="MSCU1234567",
            carrier="MSC",
            status="in_transit",
        )
        self.assertEqual(container.team, self.team)
        self.assertEqual(container.container_number, "MSCU1234567")
        self.assertEqual(container.carrier, "MSC")
        self.assertEqual(container.status, "in_transit")

    def test_container_str(self):
        container = Container.objects.create(
            team=self.team,
            container_number="TEST9999999",
        )
        self.assertEqual(str(container), "TEST9999999")

    def test_container_date_fields_optional(self):
        container = Container.objects.create(
            team=self.team,
            container_number="NODATE00001",
        )
        self.assertIsNone(container.etd)
        self.assertIsNone(container.eta)

    def test_container_date_fields_set(self):
        container = Container.objects.create(
            team=self.team,
            container_number="DATED000001",
            etd=datetime.date(2026, 6, 1),
            eta=datetime.date(2026, 7, 1),
        )
        self.assertEqual(container.etd, datetime.date(2026, 6, 1))
        self.assertEqual(container.eta, datetime.date(2026, 7, 1))
