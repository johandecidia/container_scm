"""Tests for container views and team isolation."""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

OWNER = "CSQ"
CAT = "U"
SERIAL = "305418"
CHECK = calculate_check_digit(OWNER, CAT, SERIAL)
VALID_ID = f"{OWNER}{CAT}{SERIAL}{CHECK}"


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _make_container(team, owner=OWNER, serial=SERIAL) -> Container:
    check = calculate_check_digit(owner, CAT, serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id=CAT,
        serial_number=serial,
        check_digit=check,
        equipment_type=_et(),
    )


def _post_data(container_id=None) -> dict:
    if container_id is None:
        container_id = VALID_ID
    et = _et()
    return {
        "container_id_input": container_id,
        "equipment_type": et.pk,
        "status": "AVAILABLE",
        "condition": "GOOD",
        "color_system": "UNKNOWN",
    }


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerListPermissionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="View Team", slug="view-team")
        cls.user = CustomUser.objects.create_user(username="view@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_list_requires_login(self):
        client = Client()
        response = client.get(reverse("containers:list"))
        self.assertIn(response.status_code, [302, 403])
        self.assertIn("/login/", response.get("Location", ""))

    def test_logged_in_user_can_see_list(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:list"))
        self.assertEqual(response.status_code, 200)

    def test_user_without_team_gets_404(self):
        teamless = CustomUser.objects.create_user(username="noteam@example.com", password="pass")
        client = Client()
        client.force_login(teamless)
        response = client.get(reverse("containers:list"))
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerTeamIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = CustomUser.objects.create_user(username="iso@example.com", password="pass")
        cls.team = Team.objects.create(name="My Team", slug="my-team")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

        cls.other_team = Team.objects.create(name="Other Team", slug="other-team")
        cls.other_container = _make_container(cls.other_team, owner="MSC", serial="999999")

    def test_list_only_returns_own_team_containers(self):
        own = _make_container(self.team)
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:list"))
        self.assertEqual(response.status_code, 200)
        containers = list(response.context["containers"])
        self.assertIn(own, containers)
        self.assertNotIn(self.other_container, containers)

    def test_detail_other_team_gives_404(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("containers:detail", kwargs={"container_id": self.other_container.pk})
        response = client.get(url)
        self.assertIn(response.status_code, [404, 403])

    def test_update_other_team_gives_404(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("containers:update", kwargs={"container_id": self.other_container.pk})
        response = client.get(url)
        self.assertIn(response.status_code, [404, 403])

    def test_delete_other_team_gives_404_and_does_not_delete(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("containers:delete", kwargs={"container_id": self.other_container.pk})
        response = client.post(url)
        self.assertIn(response.status_code, [404, 403])
        self.assertTrue(Container.objects.filter(pk=self.other_container.pk).exists())


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerCreateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Create Team", slug="create-team")
        cls.user = CustomUser.objects.create_user(username="create@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_create_container_post(self):
        _et()  # ensure equipment type exists
        client = Client()
        client.force_login(self.user)
        response = client.post(reverse("containers:create"), data=_post_data())
        self.assertIn(response.status_code, [200, 302])
        self.assertTrue(Container.objects.filter(owner_code=OWNER, serial_number=SERIAL, team=self.team).exists())

    def test_invalid_post_shows_form_errors(self):
        client = Client()
        client.force_login(self.user)
        response = client.post(reverse("containers:create"), data={"container_id_input": "INVALID"})
        self.assertEqual(response.status_code, 200)
