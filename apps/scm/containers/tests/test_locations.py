"""Tests for ContainerLocation and ContainerMovement models, services, selectors, and views."""

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.choices import LocationSource, LocationType
from apps.scm.containers.models import Container, ContainerLocation, ContainerMovement, EquipmentType
from apps.scm.containers.selectors import filter_containers, get_team_locations, get_team_locations_with_counts
from apps.scm.containers.services import create_location, set_container_location, update_location
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


def _make_location(team, name="Test Depot", location_type=LocationType.DEPOT) -> ContainerLocation:
    return ContainerLocation.objects.create(team=team, name=name, location_type=location_type)


# ---------------------------------------------------------------------------
# ContainerLocation model tests
# ---------------------------------------------------------------------------


class ContainerLocationModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Loc Team", slug="loc-team")

    def test_str_with_city_and_country(self):
        loc = ContainerLocation.objects.create(team=self.team, name="Port of Rotterdam", city="Rotterdam", country="NL")
        self.assertIn("Rotterdam", str(loc))

    def test_str_name_only(self):
        loc = ContainerLocation.objects.create(team=self.team, name="Unknown Depot")
        self.assertEqual(str(loc), "Unknown Depot")

    def test_default_location_type_is_unknown(self):
        loc = ContainerLocation.objects.create(team=self.team, name="X")
        self.assertEqual(loc.location_type, LocationType.UNKNOWN)

    def test_is_active_default_true(self):
        loc = ContainerLocation.objects.create(team=self.team, name="Y")
        self.assertTrue(loc.is_active)


# ---------------------------------------------------------------------------
# ContainerLocation service tests
# ---------------------------------------------------------------------------


class ContainerLocationServiceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Svc Loc Team", slug="svc-loc-team")

    def test_create_location(self):
        loc = create_location(
            team=self.team,
            data={"name": "Hamburg Port", "location_type": LocationType.PORT, "country": "DE", "city": "Hamburg"},
        )
        self.assertIsNotNone(loc.pk)
        self.assertEqual(loc.team, self.team)
        self.assertEqual(loc.location_type, LocationType.PORT)

    def test_update_location(self):
        loc = _make_location(self.team, name="Old Name")
        updated = update_location(
            loc, {"name": "New Name", "city": "Oslo", "country": "NO", "location_type": LocationType.DEPOT}
        )
        self.assertEqual(updated.name, "New Name")
        loc.refresh_from_db()
        self.assertEqual(loc.name, "New Name")


# ---------------------------------------------------------------------------
# set_container_location service tests
# ---------------------------------------------------------------------------


class SetContainerLocationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Move Team", slug="move-team")
        cls.user = CustomUser.objects.create_user(username="move@example.com", password="pass")

    def test_creates_movement(self):
        container = _make_container(self.team)
        loc = _make_location(self.team)
        movement = set_container_location(container, loc)
        self.assertIsNotNone(movement.pk)
        self.assertEqual(movement.to_location, loc)
        self.assertEqual(movement.container, container)

    def test_updates_container_location(self):
        container = _make_container(self.team)
        loc = _make_location(self.team)
        set_container_location(container, loc)
        container.refresh_from_db()
        self.assertEqual(container.current_location, loc)

    def test_movement_records_from_location(self):
        container = _make_container(self.team)
        loc1 = _make_location(self.team, name="Depot A")
        loc2 = _make_location(self.team, name="Depot B")
        set_container_location(container, loc1)
        movement = set_container_location(container, loc2)
        self.assertEqual(movement.from_location, loc1)
        self.assertEqual(movement.to_location, loc2)

    def test_sets_last_location_update(self):
        container = _make_container(self.team)
        loc = _make_location(self.team)
        before = timezone.now()
        set_container_location(container, loc)
        container.refresh_from_db()
        self.assertIsNotNone(container.last_location_update)
        self.assertGreaterEqual(container.last_location_update, before)

    def test_sets_location_source(self):
        container = _make_container(self.team)
        loc = _make_location(self.team)
        set_container_location(container, loc, source=LocationSource.IMPORT)
        container.refresh_from_db()
        self.assertEqual(container.location_source, LocationSource.IMPORT)


# ---------------------------------------------------------------------------
# Selector tests
# ---------------------------------------------------------------------------


class GetTeamLocationsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Sel Loc Team", slug="sel-loc-team")
        cls.other_team = Team.objects.create(name="Other Team", slug="other-loc-team")

    def test_returns_only_team_locations(self):
        _make_location(self.team, name="Mine")
        _make_location(self.other_team, name="Other")
        qs = get_team_locations(self.team)
        names = list(qs.values_list("name", flat=True))
        self.assertIn("Mine", names)
        self.assertNotIn("Other", names)

    def test_active_only_filter(self):
        _make_location(self.team, name="Active")
        inactive = _make_location(self.team, name="Inactive")
        inactive.is_active = False
        inactive.save()
        qs = get_team_locations(self.team, active_only=True)
        names = list(qs.values_list("name", flat=True))
        self.assertIn("Active", names)
        self.assertNotIn("Inactive", names)

    def test_locations_with_counts(self):
        loc = _make_location(self.team, name="With Container")
        container = _make_container(self.team)
        container.current_location = loc
        container.save(update_fields=["current_location"])
        qs = get_team_locations_with_counts(self.team)
        loc_data = qs.get(pk=loc.pk)
        self.assertEqual(loc_data.container_count, 1)


class FilterContainersByLocationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Filter Loc Team", slug="filter-loc-team")
        cls.depot = ContainerLocation.objects.create(team=cls.team, name="My Depot", location_type=LocationType.DEPOT)
        cls.port = ContainerLocation.objects.create(
            team=cls.team, name="Rotterdam Port", location_type=LocationType.PORT
        )
        cls.c1 = _make_container(cls.team, owner="AAA", serial="000001")
        cls.c1.current_location = cls.depot
        cls.c1.save(update_fields=["current_location"])

        cls.c2 = _make_container(cls.team, owner="BBB", serial="000002")
        cls.c2.current_location = cls.port
        cls.c2.save(update_fields=["current_location"])

        cls.c3 = _make_container(cls.team, owner="CCC", serial="000003")  # no location

    def test_filter_by_location_type(self):
        qs = filter_containers(self.team, location_type=LocationType.DEPOT)
        self.assertIn(self.c1, qs)
        self.assertNotIn(self.c2, qs)
        self.assertNotIn(self.c3, qs)

    def test_filter_by_location_id(self):
        qs = filter_containers(self.team, location_id=str(self.port.pk))
        self.assertIn(self.c2, qs)
        self.assertNotIn(self.c1, qs)

    def test_filter_missing_location(self):
        qs = filter_containers(self.team, missing_location=True)
        self.assertIn(self.c3, qs)
        self.assertNotIn(self.c1, qs)
        self.assertNotIn(self.c2, qs)

    def test_search_by_location_name(self):
        qs = filter_containers(self.team, search="Rotterdam")
        self.assertIn(self.c2, qs)
        self.assertNotIn(self.c1, qs)


# ---------------------------------------------------------------------------
# Team isolation tests
# ---------------------------------------------------------------------------


class TeamIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team_a = Team.objects.create(name="Team A", slug="team-a-loc")
        cls.team_b = Team.objects.create(name="Team B", slug="team-b-loc")

    def test_location_not_visible_across_teams(self):
        _make_location(self.team_a, name="A Depot")
        qs = get_team_locations(self.team_b)
        self.assertEqual(qs.count(), 0)

    def test_movements_not_visible_across_teams(self):
        container = _make_container(self.team_a)
        loc = _make_location(self.team_a)
        set_container_location(container, loc)
        movements = ContainerMovement.objects.filter(team=self.team_b)
        self.assertEqual(movements.count(), 0)


# ---------------------------------------------------------------------------
# View tests
# ---------------------------------------------------------------------------


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerLocationViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="View Loc Team", slug="view-loc-team")
        cls.user = CustomUser.objects.create_user(username="viewloc@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_location_list_loads(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:location_list"))
        self.assertEqual(response.status_code, 200)

    def test_location_create_htmx(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:location_create"), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)

    def test_location_create_post(self):
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("containers:location_create"),
            data={"name": "Test Port", "location_type": LocationType.PORT, "is_active": True},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ContainerLocation.objects.filter(team=self.team, name="Test Port").exists())

    def test_location_update_post(self):
        loc = _make_location(self.team, name="Before")
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("containers:location_update", kwargs={"location_id": loc.pk}),
            data={"name": "After", "location_type": LocationType.DEPOT, "is_active": True},
        )
        self.assertEqual(response.status_code, 302)
        loc.refresh_from_db()
        self.assertEqual(loc.name, "After")

    def test_location_deactivate_toggle(self):
        loc = _make_location(self.team)
        self.assertTrue(loc.is_active)
        client = Client()
        client.force_login(self.user)
        client.post(reverse("containers:location_deactivate", kwargs={"location_id": loc.pk}))
        loc.refresh_from_db()
        self.assertFalse(loc.is_active)

    def test_location_not_accessible_by_other_team(self):
        other_team = Team.objects.create(name="Other Team VL", slug="other-view-loc")
        other_user = CustomUser.objects.create_user(username="other_vl@example.com", password="pass")
        other_team.members.add(other_user, through_defaults={"role": ROLE_MEMBER})
        loc = _make_location(self.team)
        client = Client()
        client.force_login(other_user)
        response = client.post(reverse("containers:location_deactivate", kwargs={"location_id": loc.pk}))
        self.assertEqual(response.status_code, 404)
