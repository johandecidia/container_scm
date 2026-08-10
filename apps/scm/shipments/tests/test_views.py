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

    def test_timeline_partial_htmx_returns_200(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:timeline", kwargs={"pk": self.shipment.pk})
        response = client.get(url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)

    def test_timeline_cross_team_returns_404(self):
        other_user, other_team = _make_user_and_team("tl-other-ship@example.com", "tl-other-ship-team")
        other_shipment = create_shipment(other_team, other_user, {"shipment_number": "TL-OTHER-SHP"})
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:timeline", kwargs={"pk": other_shipment.pk})
        response = client.get(url)
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentListFilterTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("filter-ship@example.com", "filter-ship-team")
        cls.booked = create_shipment(cls.team, cls.user, {"shipment_number": "FILTER-BOOKED"})
        cls.booked.status = Shipment.Status.BOOKED
        cls.booked.save()
        cls.draft = create_shipment(cls.team, cls.user, {"shipment_number": "FILTER-DRAFT"})

    def test_list_filter_by_status_booked(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("shipments:list"), {"status": Shipment.Status.BOOKED})
        self.assertEqual(response.status_code, 200)
        shipments = list(response.context["shipments"])
        self.assertIn(self.booked, shipments)
        self.assertNotIn(self.draft, shipments)

    def test_list_filter_by_status_draft(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("shipments:list"), {"status": Shipment.Status.DRAFT})
        self.assertEqual(response.status_code, 200)
        shipments = list(response.context["shipments"])
        self.assertIn(self.draft, shipments)
        self.assertNotIn(self.booked, shipments)

    def test_list_search_by_shipment_number(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("shipments:list"), {"search": "FILTER-BOOKED"})
        self.assertEqual(response.status_code, 200)
        shipments = list(response.context["shipments"])
        self.assertIn(self.booked, shipments)
        self.assertNotIn(self.draft, shipments)


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentListHtmxTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("htmx-list-ship@example.com", "htmx-list-ship-team")
        create_shipment(cls.team, cls.user, {"shipment_number": "HTMX-LIST-SHP"})

    def test_htmx_list_returns_partial_template(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("shipments:list"), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        # HTMX requests return partial template (no full page wrapper)
        self.assertTemplateUsed(response, "scm/shipments/partials/shipment_table.html")

    def test_non_htmx_list_returns_full_page_template(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("shipments:list"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "scm/shipments/pages/shipment_list.html")


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentListEmptyStateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("empty-ship@example.com", "empty-ship-team")

    def test_empty_list_returns_200(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("shipments:list"))
        self.assertEqual(response.status_code, 200)
        shipments = list(response.context["shipments"])
        self.assertEqual(len(shipments), 0)


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentCancelHtmxTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("cancel-htmx-ship@example.com", "cancel-htmx-ship-team")

    def setUp(self):
        # Use _testMethodName (short) to keep shipment_number under 100 chars
        self.shipment = create_shipment(self.team, self.user, {"shipment_number": f"CH-{self._testMethodName[:30]}"})

    def test_cancel_htmx_returns_row_partial(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:cancel", kwargs={"pk": self.shipment.pk})
        response = client.post(url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "scm/shipments/partials/shipment_row.html")

    def test_cancel_sets_status(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:cancel", kwargs={"pk": self.shipment.pk})
        client.post(url)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.CANCELLED)


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentContainerAddTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        _et()
        cls.user, cls.team = _make_user_and_team("ctr-add-ship@example.com", "ctr-add-ship-team")
        cls.shipment = create_shipment(cls.team, cls.user, {"shipment_number": "CTR-ADD-SHP"})
        cls.container = _container(cls.team, owner="CTR", serial="111111")

    def test_container_add_get_returns_form(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:container_add", kwargs={"pk": self.shipment.pk})
        response = client.get(url)
        self.assertIn(response.status_code, [200])

    def test_container_add_cross_team_shipment_gives_404(self):
        other_user, other_team = _make_user_and_team("ctr-add-other@example.com", "ctr-add-other-team")
        other_shipment = create_shipment(other_team, other_user, {"shipment_number": "CTR-ADD-OTHER"})
        client = Client()
        client.force_login(self.user)
        url = reverse("shipments:container_add", kwargs={"pk": other_shipment.pk})
        response = client.get(url)
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentAnonymousAccessTest(TestCase):
    def test_anonymous_user_is_redirected_from_list(self):
        client = Client()
        response = client.get(reverse("shipments:list"))
        self.assertIn(response.status_code, [302, 403])
        if response.status_code == 302:
            self.assertIn("/login/", response.get("Location", ""))

    def test_anonymous_user_is_redirected_from_create(self):
        client = Client()
        response = client.get(reverse("shipments:create"))
        self.assertIn(response.status_code, [302, 403])
