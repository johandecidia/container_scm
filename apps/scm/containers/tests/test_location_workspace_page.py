"""The Location Workspace page: its route, its tabs and what it must not offer.

The read model has its own tests next door. This file is about the page — that the
route exists and does not shadow the create route, that every object on it links
somewhere real, that an inactive or empty location still renders, and that no gate
action is offered for a workflow the backend does not have.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.choices import ContainerStatus, LocationSource, LocationType, MovementType
from apps.scm.containers.models import Container, ContainerLocation, ContainerMovement, EquipmentType
from apps.scm.containers.services import set_container_location
from apps.scm.containers.utils import calculate_check_digit
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _equipment_type(iso_code="45G1", description="40' HC") -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code=iso_code,
        defaults={"category": "GP", "length_ft": 40, "high_cube": True, "description": description},
    )[0]


def _container(team, serial, *, owner="MCU", location=None, status=ContainerStatus.AVAILABLE) -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit(owner, "U", serial),
        equipment_type=_equipment_type(),
        current_location=location,
        status=status,
    )


def _location(team, name="Oceanterminalen", **kwargs) -> ContainerLocation:
    return ContainerLocation.objects.create(
        team=team,
        name=name,
        location_type=kwargs.pop("location_type", LocationType.DEPOT),
        city=kwargs.pop("city", "Gothenburg"),
        country=kwargs.pop("country", "Sweden"),
        **kwargs,
    )


def _movement(team, container, *, to_location=None, from_location=None, when=None, **kwargs) -> ContainerMovement:
    return ContainerMovement.objects.create(
        team=team,
        container=container,
        from_location=from_location,
        to_location=to_location,
        movement_type=kwargs.pop("movement_type", MovementType.POSITION_UPDATE),
        occurred_at=when or timezone.now(),
        source=kwargs.pop("source", LocationSource.MANUAL),
        **kwargs,
    )


@override_settings(STORAGES=_TEST_STORAGES)
class LocationWorkspacePageTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Page", slug="loc-ws-page")
        cls.user = CustomUser.objects.create_user(username="loc-ws@example.com", password="pw")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

        cls.depot = _location(cls.team)
        cls.container = _container(cls.team, "200930", location=cls.depot)
        set_container_location(cls.container, cls.depot, occurred_at=timezone.now() - timedelta(days=2))

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("containers:location_detail", args=[self.depot.pk])

    def test_the_workspace_route_exists(self):
        self.assertEqual(self.url, f"/scm/containers/locations/{self.depot.pk}/")
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_detail_route_does_not_shadow_the_create_route(self):
        response = self.client.get(reverse("containers:location_create"))
        self.assertEqual(response.status_code, 200)

    def test_the_page_renders_all_four_tabs(self):
        content = self.client.get(self.url).content.decode()
        for label in ("Overview", "Inventory", "Expected", "Activity"):
            self.assertIn(f">{label}</button>", content)

    def test_overview_is_the_default_tab(self):
        self.assertIn("|| 'overview'", self.client.get(self.url).content.decode())

    def test_the_header_names_the_place_and_counts_what_is_here(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Oceanterminalen")
        self.assertContains(response, "Gothenburg")
        self.assertContains(response, "1 container currently here")

    def test_inventory_links_to_the_container_workspace(self):
        response = self.client.get(self.url)
        self.assertContains(response, reverse("containers:detail", args=[self.container.pk]))
        self.assertContains(response, self.container.container_id)

    def test_movements_link_to_the_container_workspace(self):
        _movement(self.team, self.container, from_location=self.depot)
        response = self.client.get(self.url)
        self.assertContains(response, reverse("containers:detail", args=[self.container.pk]))

    def test_no_expected_arrival_count_is_shown(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Expected arrivals")
        self.assertNotContains(response, "containers expected here")

    def test_no_gate_actions_are_offered(self):
        content = self.client.get(self.url).content.decode()
        for absent in ("Gate in", "Gate out", "Move container"):
            self.assertNotIn(absent, content)

    def test_an_empty_location_renders_cleanly(self):
        empty = _location(self.team, name="Empty Depot")
        response = self.client.get(reverse("containers:location_detail", args=[empty.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nothing is here")
        self.assertContains(response, "No movements recorded")

    def test_an_inactive_location_still_opens_and_shows_its_containers(self):
        self.depot.is_active = False
        self.depot.save(update_fields=["is_active"])

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Inactive")
        self.assertContains(response, self.container.container_id)

    def test_another_teams_location_is_not_reachable(self):
        other_team = Team.objects.create(name="Other", slug="loc-ws-page-other")
        other_location = _location(other_team, name="Their Depot")
        response = self.client.get(reverse("containers:location_detail", args=[other_location.pk]))
        self.assertEqual(response.status_code, 404)

    def test_an_htmx_request_returns_the_inventory_table_alone(self):
        response = self.client.get(self.url, headers={"hx-request": "true"})
        content = response.content.decode()
        self.assertIn("location-inventory", content)
        self.assertNotIn('role="tablist"', content)

    def test_the_inventory_filter_narrows_the_table(self):
        _container(self.team, "200999", owner="TEM", location=self.depot, status=ContainerStatus.BOOKED)

        response = self.client.get(self.url, {"status": ContainerStatus.BOOKED}, headers={"hx-request": "true"})

        self.assertContains(response, "TEMU200999")
        self.assertNotContains(response, self.container.container_id)

    def test_a_filter_matching_nothing_says_so_rather_than_looking_empty(self):
        response = self.client.get(self.url, {"search": "ZZZZ9999999"}, headers={"hx-request": "true"})
        self.assertContains(response, "No containers match these filters")


@override_settings(STORAGES=_TEST_STORAGES)
class LocationsAreReachableTest(TestCase):
    """Every place a location is named leads to its workspace, not to a workaround."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Reach", slug="loc-ws-reach")
        cls.user = CustomUser.objects.create_user(username="loc-ws-reach@example.com", password="pw")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.depot = _location(cls.team)
        cls.container = _container(cls.team, "900001", location=cls.depot)

    def setUp(self):
        self.client.force_login(self.user)
        self.workspace_url = reverse("containers:location_detail", args=[self.depot.pk])

    def test_the_list_links_each_location_to_its_workspace(self):
        response = self.client.get(reverse("containers:location_list"))
        self.assertContains(response, self.workspace_url)

    def test_the_list_still_shows_the_container_count(self):
        response = self.client.get(reverse("containers:location_list"))
        self.assertContains(response, "Oceanterminalen")
        self.assertContains(response, "Containers")

    def test_the_container_workspace_links_to_the_location_workspace(self):
        response = self.client.get(reverse("containers:detail", args=[self.container.pk]))
        self.assertContains(response, self.workspace_url)

    def test_global_search_links_to_the_location_workspace(self):
        from apps.scm.search import search_scm

        results = [result for result in search_scm(self.team, "Oceanterminalen") if result.kind == "location"]

        self.assertEqual([result.url for result in results], [self.workspace_url])

    def test_the_old_filtered_container_list_url_still_works(self):
        response = self.client.get(reverse("containers:list"), {"location_id": self.depot.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.container.container_id)

    def test_search_does_not_reach_another_teams_location(self):
        from apps.scm.search import search_scm

        other = Team.objects.create(name="Other", slug="loc-ws-reach-other")
        _location(other, name="Oceanterminalen")

        results = [result for result in search_scm(self.team, "Oceanterminalen") if result.kind == "location"]

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, self.workspace_url)


@override_settings(STORAGES=_TEST_STORAGES)
class LocationWorkspaceQueryCountTest(TestCase):
    """A busy depot must not cost more queries than a quiet one."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="N1", slug="loc-ws-n1")
        cls.user = CustomUser.objects.create_user(username="loc-ws-n1@example.com", password="pw")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.quiet = cls._populate(_location(cls.team, name="Quiet"), 1, base=700_000)
        cls.busy = cls._populate(_location(cls.team, name="Busy"), 20, base=800_000)

    @classmethod
    def _populate(cls, location, count, *, base):
        for index in range(count):
            container = _container(cls.team, f"{base + index:06d}", location=location)
            _movement(cls.team, container, to_location=location)
        return location

    def _page_queries(self, location) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            self.client.get(reverse("containers:location_detail", args=[location.pk]))
        return len(captured)

    def test_twenty_containers_cost_the_same_as_one(self):
        self.client.force_login(self.user)
        self.assertEqual(self._page_queries(self.busy), self._page_queries(self.quiet))
