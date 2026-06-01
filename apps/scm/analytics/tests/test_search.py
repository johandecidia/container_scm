"""Tests for SCM global search — team isolation and result correctness."""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.search import search_scm
from apps.scm.shipments.models import Shipment
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


def _container(team: Team, owner: str = "CSQ", serial: str = "305418") -> Container:
    check = calculate_check_digit(owner, "U", serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_et(),
    )


def _shipment(team: Team, number: str = "SHP-001", **kwargs) -> Shipment:
    return Shipment.objects.create(team=team, shipment_number=number, **kwargs)


def _make_user_and_team(username: str, slug: str):
    team = Team.objects.create(name=slug, slug=slug)
    user = CustomUser.objects.create_user(username=username, password="pass")
    team.members.add(user, through_defaults={"role": ROLE_MEMBER})
    return user, team


class SearchContainersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Search Team", slug="search-team")
        cls.other_team = Team.objects.create(name="Other Search Team", slug="other-search-team")
        cls.container = _container(cls.team, owner="MSC", serial="123456")
        cls.other_container = _container(cls.other_team, owner="CMA", serial="654321")

    def test_finds_container_by_owner_code(self):
        results = search_scm(self.team, "MSC")
        urls = [r.url for r in results]
        self.assertTrue(any(str(self.container.pk) in u for u in urls))

    def test_does_not_return_other_team_container(self):
        results = search_scm(self.team, "CMA")
        urls = [r.url for r in results]
        self.assertFalse(any(str(self.other_container.pk) in u for u in urls))

    def test_empty_query_returns_no_results(self):
        results = search_scm(self.team, "")
        self.assertEqual(results, [])

    def test_result_kind_is_container(self):
        results = search_scm(self.team, "MSC")
        container_results = [r for r in results if r.kind == "container"]
        self.assertTrue(len(container_results) >= 1)


class SearchShipmentsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Ship Search", slug="ship-search")
        cls.other_team = Team.objects.create(name="Other Ship Search", slug="other-ship-search")
        cls.shipment = _shipment(
            cls.team,
            "SHPS-001",
            carrier="Maersk",
            carrier_booking_reference="BKG-999",
            bill_of_lading_number="BOLX123",
        )
        cls.other_shipment = _shipment(cls.other_team, "SHPS-OTHER", carrier="COSCO")

    def test_finds_shipment_by_number(self):
        results = search_scm(self.team, "SHPS-001")
        self.assertTrue(any(r.kind == "shipment" for r in results))

    def test_finds_shipment_by_carrier(self):
        results = search_scm(self.team, "Maersk")
        self.assertTrue(any(r.kind == "shipment" for r in results))

    def test_finds_shipment_by_bill_of_lading(self):
        results = search_scm(self.team, "BOLX123")
        self.assertTrue(any(r.kind == "shipment" for r in results))

    def test_does_not_return_other_team_shipment(self):
        results = search_scm(self.team, "COSCO")
        self.assertEqual(results, [])

    def test_no_match_returns_empty(self):
        results = search_scm(self.team, "ZZZNOMATCH999")
        self.assertEqual(results, [])


@override_settings(STORAGES=_TEST_STORAGES)
class SearchViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("search-view@example.com", "search-view-team")

    def test_search_view_requires_login(self):
        client = Client()
        response = client.get(reverse("analytics:search"), {"q": "test"})
        self.assertIn(response.status_code, [302, 403])

    def test_search_view_returns_200_for_logged_in_user(self):
        client = Client()
        client.login(username="search-view@example.com", password="pass")
        response = client.get(reverse("analytics:search"), {"q": "test"})
        self.assertEqual(response.status_code, 200)

    def test_search_view_renders_without_query(self):
        client = Client()
        client.login(username="search-view@example.com", password="pass")
        response = client.get(reverse("analytics:search"))
        self.assertEqual(response.status_code, 200)
