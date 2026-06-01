"""Tests for container services."""

from django.test import TestCase

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.services import create_container, delete_container, update_container
from apps.scm.containers.utils import calculate_check_digit
from apps.teams.models import Team
from apps.users.models import CustomUser

OWNER = "CSQ"
CAT = "U"
SERIAL = "305418"


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _valid_data(owner=OWNER, serial=SERIAL) -> dict:
    check = calculate_check_digit(owner, CAT, serial)
    return {
        "owner_code": owner,
        "category_id": CAT,
        "serial_number": serial,
        "check_digit": check,
        "equipment_type": _et(),
    }


class CreateContainerTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Svc Team", slug="svc-team")
        cls.user = CustomUser.objects.create_user(username="svc@example.com", password="pass")

    def test_sets_team(self):
        c = create_container(team=self.team, user=self.user, data=_valid_data())
        self.assertEqual(c.team, self.team)

    def test_sets_created_by(self):
        c = create_container(team=self.team, user=self.user, data=_valid_data())
        self.assertEqual(c.created_by, self.user)

    def test_sets_updated_by_on_create(self):
        c = create_container(team=self.team, user=self.user, data=_valid_data())
        self.assertEqual(c.updated_by, self.user)

    def test_persists_to_db(self):
        c = create_container(team=self.team, user=self.user, data=_valid_data())
        self.assertIsNotNone(c.pk)
        self.assertTrue(Container.objects.filter(pk=c.pk).exists())


class UpdateContainerTest(TestCase):
    team: Team
    user: CustomUser

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Upd Team", slug="upd-team")
        cls.user = CustomUser.objects.create_user(username="upd@example.com", password="pass")
        cls.other_user = CustomUser.objects.create_user(username="other@example.com", password="pass")

    def _make(self) -> Container:
        return create_container(team=self.team, user=self.user, data=_valid_data())

    def test_updates_fields(self):
        c = self._make()
        updated = update_container(container=c, user=self.other_user, data={"current_location": "Oslo"})
        self.assertEqual(updated.current_location, "Oslo")

    def test_sets_updated_by(self):
        c = self._make()
        updated = update_container(container=c, user=self.other_user, data={"current_location": "Oslo"})
        self.assertEqual(updated.updated_by, self.other_user)

    def test_persists_to_db(self):
        c = self._make()
        update_container(container=c, user=self.other_user, data={"current_location": "Oslo"})
        c.refresh_from_db()
        self.assertEqual(c.current_location, "Oslo")


class DeleteContainerTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Del Team", slug="del-team")
        cls.user = CustomUser.objects.create_user(username="del@example.com", password="pass")

    def test_removes_from_db(self):
        c = create_container(team=self.team, user=self.user, data=_valid_data())
        pk = c.pk
        delete_container(container=c, user=self.user)
        self.assertFalse(Container.objects.filter(pk=pk).exists())
