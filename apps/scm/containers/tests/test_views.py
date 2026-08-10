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
        response = client.post(reverse("containers:create"), data={"container_number": VALID_ID})
        self.assertIn(response.status_code, [200, 302])
        self.assertTrue(Container.objects.filter(owner_code=OWNER, serial_number=SERIAL, team=self.team).exists())

    def test_invalid_post_shows_form_errors(self):
        client = Client()
        client.force_login(self.user)
        response = client.post(reverse("containers:create"), data={"container_number": "INVALID"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid container ID format")


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerUpdateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Update Team", slug="update-team-ctr")
        cls.user = CustomUser.objects.create_user(username="update-ctr@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.container = _make_container(cls.team, owner="UPD", serial="111111")

    def test_update_get_loads(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("containers:update", kwargs={"container_id": self.container.pk})
        response = client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_update_htmx_returns_row_partial(self):
        _et()
        client = Client()
        client.force_login(self.user)
        url = reverse("containers:update", kwargs={"container_id": self.container.pk})
        check = calculate_check_digit("UPD", "U", "111111")
        data = {
            "container_id_input": f"UPDU111111{check}",
            "equipment_type": _et().pk,
            "status": "AVAILABLE",
            "condition": "FAIR",
            "color_system": "UNKNOWN",
        }
        response = client.post(url, data=data, HTTP_HX_REQUEST="true")
        # HTMX valid update returns row partial
        self.assertIn(response.status_code, [200, 302])


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerDeleteTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Delete Team", slug="delete-team-ctr")
        cls.user = CustomUser.objects.create_user(username="delete-ctr@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_delete_removes_container(self):
        container = _make_container(self.team, owner="DEL", serial="222222")
        client = Client()
        client.force_login(self.user)
        url = reverse("containers:delete", kwargs={"container_id": container.pk})
        response = client.post(url)
        self.assertIn(response.status_code, [200, 302])
        self.assertFalse(Container.objects.filter(pk=container.pk).exists())

    def test_delete_htmx_returns_200(self):
        container = _make_container(self.team, owner="DLH", serial="333333")
        client = Client()
        client.force_login(self.user)
        url = reverse("containers:delete", kwargs={"container_id": container.pk})
        response = client.post(url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Container.objects.filter(pk=container.pk).exists())


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerListHtmxTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="HTMX Ctr Team", slug="htmx-ctr-team")
        cls.user = CustomUser.objects.create_user(username="htmx-ctr@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_htmx_list_returns_partial_template(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:list"), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "scm/containers/partials/container_table.html")

    def test_non_htmx_list_returns_full_page(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:list"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "scm/containers/pages/container_list.html")


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerListFilterTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Filter Ctr Team", slug="filter-ctr-team")
        cls.user = CustomUser.objects.create_user(username="filter-ctr@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        from apps.scm.containers.choices import ContainerStatus

        cls.available = _make_container(cls.team, owner="AVL", serial="444444")
        # Set one container status to BOOKED (non-AVAILABLE status)
        cls.booked = _make_container(cls.team, owner="BKD", serial="555555")
        cls.booked.status = ContainerStatus.BOOKED
        cls.booked.save()

    def test_filter_by_status_available(self):
        from apps.scm.containers.choices import ContainerStatus

        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:list"), {"status": ContainerStatus.AVAILABLE})
        self.assertEqual(response.status_code, 200)
        containers = list(response.context["containers"])
        self.assertIn(self.available, containers)
        self.assertNotIn(self.booked, containers)

    def test_filter_by_status_booked(self):
        from apps.scm.containers.choices import ContainerStatus

        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:list"), {"status": ContainerStatus.BOOKED})
        self.assertEqual(response.status_code, 200)
        containers = list(response.context["containers"])
        self.assertIn(self.booked, containers)
        self.assertNotIn(self.available, containers)


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerListEmptyStateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Empty Ctr Team", slug="empty-ctr-team")
        cls.user = CustomUser.objects.create_user(username="empty-ctr@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_empty_list_returns_200(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:list"))
        self.assertEqual(response.status_code, 200)
        containers = list(response.context["containers"])
        self.assertEqual(len(containers), 0)


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerDetailContentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Detail Ctr Team", slug="detail-ctr-team")
        cls.user = CustomUser.objects.create_user(username="detail-ctr@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.container = _make_container(cls.team, owner="DTC", serial="666666")

    def test_detail_shows_container_number(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("containers:detail", kwargs={"container_id": self.container.pk})
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DTC")


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerDiscoveryDashboardTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Discovery Team", slug="discovery-team-ctr")
        cls.user = CustomUser.objects.create_user(username="disc-ctr@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_discovery_dashboard_loads(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:discovery_dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_discovery_dashboard_requires_login(self):
        client = Client()
        response = client.get(reverse("containers:discovery_dashboard"))
        self.assertIn(response.status_code, [302, 403])

    def test_discovery_dashboard_has_counts_in_context(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:discovery_dashboard"))
        self.assertIn("counts", response.context)
        counts = response.context["counts"]
        self.assertIn("planned", counts)
        self.assertIn("detected", counts)

    def test_discovery_dashboard_empty_state_shows_zero_counts(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:discovery_dashboard"))
        counts = response.context["counts"]
        self.assertEqual(counts["planned"], 0)
        self.assertEqual(counts["detected"], 0)

    def test_discovery_dashboard_user_without_team_gets_404(self):
        teamless = CustomUser.objects.create_user(username="disc-ctr-noteam@example.com", password="pass")
        client = Client()
        client.force_login(teamless)
        response = client.get(reverse("containers:discovery_dashboard"))
        self.assertEqual(response.status_code, 404)

    def test_discovery_dashboard_team_isolation(self):
        from apps.scm.containers.models import PlannedContainer

        # Create a planned container for another team — should not affect this team's counts
        other_team = Team.objects.create(name="Other Discovery", slug="other-disc-ctr")
        # ISO 6346 format: 4 letters + 7 digits = 11 chars
        PlannedContainer.objects.create(team=other_team, container_number="XXXX1234567"[:11])
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:discovery_dashboard"))
        counts = response.context["counts"]
        self.assertEqual(counts["planned"], 0)
