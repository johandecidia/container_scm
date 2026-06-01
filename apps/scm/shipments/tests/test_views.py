"""Tests for shipment views and team isolation."""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment
from apps.scm.shipments.services import create_shipment
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team: Team, owner: str = "MSC", serial: str = "100001") -> Container:
    check = calculate_check_digit(owner, "U", serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_et(),
    )


def _make_user_and_team(username: str, team_slug: str):
    team = Team.objects.create(name=team_slug, slug=team_slug)
    user = CustomUser.objects.create_user(username=username, password="pass")
    team.members.add(user, through_defaults={"role": ROLE_MEMBER})
    return user, team


def _post_data(**kwargs) -> dict:
    return {"shipment_number": "SHP-TEST-1", **kwargs}


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentListPermissionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("list-perm@example.com", "list-perm-team")

    def test_list_requires_login(self):
        client = Client()
        response = client.get(reverse("shipments:list"))
        self.assertIn(response.status_code, [302, 403])

    def test_logged_in_user_can_see_list(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("shipments:list"))
        self.assertEqual(response.status_code, 200)

    def test_user_without_team_gets_404(self):
        teamless = CustomUser.objects.create_user(username="teamless-ship@example.com", password="pass")
        client = Client()
        client.force_login(teamless)
        response = client.get(reverse("shipments:list"))
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentTeamIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("iso-ship@example.com", "iso-ship-team")
        cls.other_user, cls.other_team = _make_user_and_team("other-iso-ship@example.com", "other-iso-ship-team")
        cls.own_shipment = create_shipment(cls.team, cls.user, {"shipment_number": "OWN-SHIP"})
        cls.other_shipment = create_shipment(cls.other_team, cls.other_user, {"shipment_number": "OTHER-SHIP"})

    def test_list_only_shows_own_team_shipments(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("shipments:list"))
        self.assertEqual(response.status_code, 200)
        shipments = list(response.context["shipments"])
        self.assertIn(self.own_shipment, shipments)
        self.assertNotIn(self.other_shipment, shipments)

    def test_detail_other_team_gives_404(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:detail", kwargs={"pk": self.other_shipment.pk})
        response = client.get(url)
        self.assertIn(response.status_code, [404, 403])

    def test_update_other_team_gives_404(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:update", kwargs={"pk": self.other_shipment.pk})
        response = client.get(url)
        self.assertIn(response.status_code, [404, 403])

    def test_cancel_other_team_gives_404(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:cancel", kwargs={"pk": self.other_shipment.pk})
        response = client.post(url)
        self.assertIn(response.status_code, [404, 403])
        self.other_shipment.refresh_from_db()
        self.assertNotEqual(self.other_shipment.status, Shipment.Status.CANCELLED)


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentCreateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("create-ship@example.com", "create-ship-team")

    def test_create_shipment_post(self):
        client = Client()
        client.force_login(self.user)
        response = client.post(reverse("shipments:create"), data=_post_data())
        self.assertIn(response.status_code, [200, 302])
        self.assertTrue(Shipment.objects.filter(shipment_number="SHP-TEST-1", team=self.team).exists())

    def test_create_get_returns_form(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("shipments:create"))
        self.assertEqual(response.status_code, 200)


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentUpdateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("update-ship@example.com", "update-ship-team")
        cls.shipment = create_shipment(cls.team, cls.user, {"shipment_number": "UPD-SHP"})

    def test_update_shipment_post(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:update", kwargs={"pk": self.shipment.pk})
        response = client.post(url, data={"carrier": "Maersk"})
        self.assertIn(response.status_code, [200, 302])


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentStatusUpdateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("status-ship@example.com", "status-ship-team")
        cls.shipment = create_shipment(cls.team, cls.user, {"shipment_number": "STS-SHP"})

    def test_status_update_changes_status(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:status_update", kwargs={"pk": self.shipment.pk})
        client.post(url, data={"status": Shipment.Status.BOOKED})
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.BOOKED)

    def test_status_update_htmx_returns_partial(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:status_update", kwargs={"pk": self.shipment.pk})
        response = client.post(url, data={"status": Shipment.Status.BOOKED}, HTTP_HX_REQUEST="true")
        self.assertIn(response.status_code, [200])
        self.assertContains(response, "shipment-row-")


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentDetailTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("detail-ship@example.com", "detail-ship-team")
        cls.shipment = create_shipment(cls.team, cls.user, {"shipment_number": "DET-SHP"})

    def test_detail_page_loads(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:detail", kwargs={"pk": self.shipment.pk})
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DET-SHP")


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentTimelinePartialTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("timeline-ship@example.com", "timeline-ship-team")
        cls.shipment = create_shipment(cls.team, cls.user, {"shipment_number": "TL-SHP"})

    def test_timeline_partial_returns_200(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:timeline", kwargs={"pk": self.shipment.pk})
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
