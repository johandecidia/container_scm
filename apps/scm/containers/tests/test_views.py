"""
Kort 5 — View-test och team-isolering.
Acceptanskriterier:
  - Oautentiserade användare omdirigeras till login.
  - Autentiserade team-members kan nå listan (200).
  - Användare kan inte nå en annan teams container (404).
"""
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.scm.containers.models import Container
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
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

    def test_logged_in_user_can_see_container_list(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:list"))
        self.assertEqual(response.status_code, 200)

    def test_user_without_team_gets_404(self):
        teamless_user = CustomUser.objects.create_user(
            username="noteam@example.com", password="pass"
        )
        client = Client()
        client.force_login(teamless_user)
        response = client.get(reverse("containers:list"))
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerTeamIsolationTest(TestCase):
    """Team-isolering — viktigaste testerna för SaaS-säkerhet."""

    @classmethod
    def setUpTestData(cls):
        cls.user = CustomUser.objects.create_user(username="iso@example.com", password="pass")
        cls.team = Team.objects.create(name="My Team", slug="my-team")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

        cls.other_team = Team.objects.create(name="Other Team", slug="other-team")
        cls.other_container = Container.objects.create(
            team=cls.other_team, container_number="OTHER000001"
        )

    def test_user_cannot_access_other_team_container_update(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("containers:update", kwargs={"container_id": self.other_container.pk})
        response = client.get(url)
        self.assertIn(response.status_code, [404, 403])

    def test_user_cannot_access_other_team_container_detail(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("containers:detail", kwargs={"container_id": self.other_container.pk})
        response = client.get(url)
        self.assertIn(response.status_code, [404, 403])

    def test_user_cannot_delete_other_team_container(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("containers:delete", kwargs={"container_id": self.other_container.pk})
        response = client.post(url)
        self.assertIn(response.status_code, [404, 403])
        # Verify the container was NOT deleted
        self.assertTrue(Container.objects.filter(pk=self.other_container.pk).exists())

    def test_list_only_returns_own_team_containers(self):
        own_container = Container.objects.create(team=self.team, container_number="OWN000001A")
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:list"))
        self.assertEqual(response.status_code, 200)
        containers = list(response.context["containers"])
        self.assertIn(own_container, containers)
        self.assertNotIn(self.other_container, containers)


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerCreateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Create Team", slug="create-team")
        cls.user = CustomUser.objects.create_user(username="create@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_create_container_post(self):
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("containers:create"),
            data={"container_number": "NEW000001", "carrier": "MSC", "status": "planned"},
        )
        # Should redirect after successful creation (non-HTMX)
        self.assertIn(response.status_code, [200, 302])
        self.assertTrue(
            Container.objects.filter(container_number="NEW000001", team=self.team).exists()
        )
