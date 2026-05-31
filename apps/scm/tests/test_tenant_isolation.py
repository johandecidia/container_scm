"""Tests that verify SCM data is isolated between teams (tenant isolation).

Each test asserts that Team A cannot access Team B's data via selectors or views.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.selectors import get_team_containers
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.imports.models import ImportJob
from apps.scm.shipments.models import Shipment
from apps.scm.shipments.selectors import get_team_shipments
from apps.scm.tracking.models import TrackingProvider, TrackingSubscription
from apps.teams.models import Team
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _team(name: str, slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": name})[0]


def _user(email: str) -> CustomUser:
    return CustomUser.objects.get_or_create(username=email, defaults={"email": email})[0]


def _equipment_type(iso_code: str = "22G1") -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code=iso_code,
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20GP test"},
    )[0]


def _container(team: Team, owner: str = "CSQ", serial: str = "305418") -> Container:
    check = calculate_check_digit(owner, "U", serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_equipment_type(),
    )


def _shipment(team: Team) -> Shipment:
    return Shipment.objects.create(team=team, shipment_number="SHP-001")


def _import_job(team: Team, user: CustomUser) -> ImportJob:
    f = SimpleUploadedFile("test.csv", b"Container No\nCSQU3054187", content_type="text/csv")
    return ImportJob.objects.create(
        team=team,
        created_by=user,
        file=f,
        original_filename="test.csv",
        import_type=ImportJob.ImportType.CONTAINERS,
        status=ImportJob.Status.UPLOADED,
    )


def _tracking_subscription(team: Team) -> TrackingSubscription:
    provider, _ = TrackingProvider.objects.get_or_create(
        code="test-provider",
        defaults={"name": "Test Provider"},
    )
    return TrackingSubscription.objects.create(
        team=team,
        provider=provider,
        tracking_reference="CSQU3054187",
        reference_type=TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
    )


def _client_for(user: CustomUser, team: Team) -> Client:
    c = Client()
    c.force_login(user)
    session = c.session
    session["team_id"] = team.pk
    session.save()
    return c


# ---------------------------------------------------------------------------
# Selector-level isolation
# ---------------------------------------------------------------------------


class ContainerSelectorIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team_a = _team("Team A", "iso-team-a")
        cls.team_b = _team("Team B", "iso-team-b")
        _equipment_type()
        cls.container_a = _container(cls.team_a, owner="CSQ", serial="305418")
        cls.container_b = _container(cls.team_b, owner="CAM", serial="123456")

    def test_team_a_sees_only_own_containers(self):
        qs = get_team_containers(self.team_a)
        self.assertIn(self.container_a, qs)
        self.assertNotIn(self.container_b, qs)

    def test_team_b_sees_only_own_containers(self):
        qs = get_team_containers(self.team_b)
        self.assertIn(self.container_b, qs)
        self.assertNotIn(self.container_a, qs)


class ShipmentSelectorIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team_a = _team("Team A", "siso-team-a")
        cls.team_b = _team("Team B", "siso-team-b")
        cls.shipment_a = _shipment(cls.team_a)
        cls.shipment_b = _shipment(cls.team_b)

    def test_team_a_sees_only_own_shipments(self):
        qs = get_team_shipments(self.team_a)
        self.assertIn(self.shipment_a, qs)
        self.assertNotIn(self.shipment_b, qs)

    def test_team_b_sees_only_own_shipments(self):
        qs = get_team_shipments(self.team_b)
        self.assertIn(self.shipment_b, qs)
        self.assertNotIn(self.shipment_a, qs)


class TrackingSubscriptionIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team_a = _team("Team A", "tiso-team-a")
        cls.team_b = _team("Team B", "tiso-team-b")
        cls.sub_a = _tracking_subscription(cls.team_a)
        cls.sub_b = _tracking_subscription(cls.team_b)

    def test_team_a_queryset_excludes_team_b(self):
        qs = TrackingSubscription.objects.filter(team=self.team_a)
        self.assertIn(self.sub_a, qs)
        self.assertNotIn(self.sub_b, qs)

    def test_team_b_queryset_excludes_team_a(self):
        qs = TrackingSubscription.objects.filter(team=self.team_b)
        self.assertIn(self.sub_b, qs)
        self.assertNotIn(self.sub_a, qs)


# ---------------------------------------------------------------------------
# View-level isolation
# ---------------------------------------------------------------------------


@override_settings(STORAGES=_TEST_STORAGES)
class ImportJobViewIsolationTest(TestCase):
    """Team A user must receive 404 when accessing Team B's import job."""

    @classmethod
    def setUpTestData(cls):
        cls.team_a = _team("Team A", "viso-team-a")
        cls.team_b = _team("Team B", "viso-team-b")
        cls.user_a = _user("viso-a@example.com")
        cls.user_b = _user("viso-b@example.com")
        cls.team_a.members.add(cls.user_a)
        cls.team_b.members.add(cls.user_b)
        cls.job_b = _import_job(cls.team_b, cls.user_b)

    def test_team_a_user_cannot_access_team_b_import(self):
        c = _client_for(self.user_a, self.team_a)
        resp = c.get(reverse("imports:detail", kwargs={"pk": self.job_b.pk}))
        self.assertEqual(resp.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerViewIsolationTest(TestCase):
    """Team A user must receive 404 when accessing Team B's container."""

    @classmethod
    def setUpTestData(cls):
        cls.team_a = _team("Team A", "cviso-team-a")
        cls.team_b = _team("Team B", "cviso-team-b")
        cls.user_a = _user("cviso-a@example.com")
        cls.user_b = _user("cviso-b@example.com")
        cls.team_a.members.add(cls.user_a)
        cls.team_b.members.add(cls.user_b)
        _equipment_type()
        cls.container_b = _container(cls.team_b, owner="CSQ", serial="305418")

    def test_team_a_user_cannot_access_team_b_container(self):
        c = _client_for(self.user_a, self.team_a)
        resp = c.get(reverse("containers:detail", kwargs={"container_id": self.container_b.pk}))
        self.assertEqual(resp.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentViewIsolationTest(TestCase):
    """Team A user must receive 404 when accessing Team B's shipment."""

    @classmethod
    def setUpTestData(cls):
        cls.team_a = _team("Team A", "sviso-team-a")
        cls.team_b = _team("Team B", "sviso-team-b")
        cls.user_a = _user("sviso-a@example.com")
        cls.user_b = _user("sviso-b@example.com")
        cls.team_a.members.add(cls.user_a)
        cls.team_b.members.add(cls.user_b)
        cls.shipment_b = _shipment(cls.team_b)

    def test_team_a_user_cannot_access_team_b_shipment(self):
        c = _client_for(self.user_a, self.team_a)
        resp = c.get(reverse("shipments:detail", kwargs={"pk": self.shipment_b.pk}))
        self.assertEqual(resp.status_code, 404)
