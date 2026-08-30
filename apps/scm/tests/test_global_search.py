"""Global SCM search: what it finds, in what order, and whose data it never shows.

The container-number cases are the point of this file. A container's ISO number is
composed on read from four columns, so the number printed on the box exists in no
column and a substring search cannot find it — which is exactly what someone typing
it expects to work.
"""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.models import Container, ContainerLocation, EquipmentType
from apps.scm.containers.utils import calculate_check_digit, container_number_query
from apps.scm.procurement.models import PurchaseOrder
from apps.scm.search import group_search_results, search_scm
from apps.scm.shipments.models import Shipment
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

OWNER = "MCU"
CAT = "U"
SERIAL = "200930"
CHECK = calculate_check_digit(OWNER, CAT, SERIAL)
FULL_NUMBER = f"{OWNER}{CAT}{SERIAL}{CHECK}"


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="45HC",
        defaults={"category": "HC", "length_ft": 40, "high_cube": True, "description": "40' High Cube"},
    )[0]


def _container(team, owner=OWNER, serial=SERIAL) -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id=CAT,
        serial_number=serial,
        check_digit=calculate_check_digit(owner, CAT, serial),
        equipment_type=_equipment_type(),
    )


class ContainerNumberQueryTest(TestCase):
    """Decomposing a typed number, before any database is involved."""

    def test_a_whole_number_is_recognised_as_whole(self):
        query = container_number_query(FULL_NUMBER)
        self.assertIsNotNone(query)
        self.assertTrue(query.is_whole_number)

    def test_a_partial_number_is_not_whole(self):
        for text in ("MCUU", "MCUU2009", "MCUU200930"):
            with self.subTest(text=text):
                query = container_number_query(text)
                self.assertIsNotNone(query)
                self.assertFalse(query.is_whole_number)

    def test_spacing_and_hyphens_are_not_part_of_the_identity(self):
        self.assertTrue(container_number_query("mcuu 2009 300").is_whole_number)
        self.assertTrue(container_number_query("MCUU-200930-0").is_whole_number)

    def test_something_that_is_not_a_container_number_is_left_alone(self):
        """The caller's substring matching answers instead — this returns nothing."""
        for text in ("Gothenburg", "117064", "MC", "MCUX200930", ""):
            with self.subTest(text=text):
                self.assertIsNone(container_number_query(text))


@override_settings(STORAGES=_TEST_STORAGES)
class SearchTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Search Team", slug="search-team")
        cls.user = CustomUser.objects.create_user(username="search@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

        cls.other_team = Team.objects.create(name="Other Search Team", slug="other-search-team")
        cls.other_user = CustomUser.objects.create_user(username="other-search@example.com", password="pass")
        cls.other_team.members.add(cls.other_user, through_defaults={"role": ROLE_MEMBER})

        cls.container = _container(cls.team)
        cls.other_container = _container(cls.other_team)

    def _search(self, query, team=None):
        return search_scm(team=team or self.team, query=query)

    def _kinds(self, results):
        return [result.kind for result in results]


class ContainerNumberSearchTest(SearchTestBase):
    """The number on the box finds the box."""

    def test_a_full_container_number_finds_the_container(self):
        results = self._search(FULL_NUMBER)

        containers = [result for result in results if result.kind == "container"]
        self.assertEqual([result.title for result in containers], [FULL_NUMBER])

    def test_a_full_container_number_is_reported_as_an_exact_match(self):
        results = self._search(FULL_NUMBER)

        self.assertTrue(results[0].is_exact)
        self.assertEqual(results[0].kind, "container")

    def test_the_exact_match_leads_the_results(self):
        """Even with other containers sharing the prefix."""
        for serial in ("200931", "200932", "200933"):
            _container(self.team, serial=serial)

        results = self._search(FULL_NUMBER)

        self.assertEqual(results[0].title, FULL_NUMBER)
        self.assertTrue(results[0].is_exact)

    def test_a_partial_number_finds_the_container_without_claiming_exactness(self):
        results = self._search("MCUU2009")

        containers = [result for result in results if result.kind == "container"]
        self.assertIn(FULL_NUMBER, [result.title for result in containers])
        self.assertFalse(any(result.is_exact for result in containers))

    def test_a_container_result_links_to_the_container_workspace(self):
        results = self._search(FULL_NUMBER)

        self.assertEqual(
            results[0].url,
            reverse("containers:detail", kwargs={"container_id": self.container.pk}),
        )

    def test_a_number_belonging_to_another_team_is_not_found(self):
        """Both teams have a container with this number; only ours comes back."""
        results = self._search(FULL_NUMBER)

        urls = [result.url for result in results]
        self.assertNotIn(
            reverse("containers:detail", kwargs={"container_id": self.other_container.pk}),
            urls,
        )

    def test_an_unknown_number_returns_nothing_rather_than_a_near_miss(self):
        self.assertEqual(self._search("ZZZU9999999"), [])


class SearchScopeTest(SearchTestBase):
    """Which kinds are searched, and in which order they are offered."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.order = PurchaseOrder.objects.create(
            team=cls.team,
            external_id="ext-117064",
            po_number="117064",
            supplier_no="S-1",
            supplier_name="CPI",
        )
        cls.shipment = Shipment.objects.create(
            team=cls.team,
            shipment_number="SH-2026-00124",
            origin_port="Shanghai",
            destination_port="Gothenburg",
        )
        cls.location = ContainerLocation.objects.create(
            team=cls.team,
            name="Oceanterminalen",
            city="Gothenburg",
            country="Sweden",
        )

    def test_a_purchase_order_number_finds_the_purchase_order(self):
        results = self._search("117064")

        orders = [result for result in results if result.kind == "purchase_order"]
        self.assertEqual([result.title for result in orders], ["117064"])
        self.assertEqual(
            orders[0].url,
            reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": self.order.pk}),
        )

    def test_a_shipment_number_finds_the_shipment(self):
        results = self._search("SH-2026-00124")

        shipments = [result for result in results if result.kind == "shipment"]
        self.assertEqual([result.title for result in shipments], ["SH-2026-00124"])
        self.assertEqual(shipments[0].subtitle, "Shanghai → Gothenburg")

    def test_a_location_finds_the_containers_recorded_there(self):
        results = self._search("Oceanterminalen")

        locations = [result for result in results if result.kind == "location"]
        self.assertEqual([result.title for result in locations], ["Oceanterminalen"])
        self.assertIn(f"location_id={self.location.pk}", locations[0].url)

    def test_containers_are_offered_before_the_supporting_kinds(self):
        """A term that hits several kinds still leads with the container."""
        Shipment.objects.create(team=self.team, shipment_number="MCU-SHIP", origin_port="Gothenburg")
        ContainerLocation.objects.create(team=self.team, name="MCU Depot")

        kinds = self._kinds(self._search("MCU"))

        self.assertEqual(kinds[0], "container")
        self.assertLess(kinds.index("container"), kinds.index("shipment"))
        self.assertLess(kinds.index("shipment"), kinds.index("location"))

    def test_an_empty_query_searches_nothing(self):
        for text in ("", "   "):
            with self.subTest(text=text):
                self.assertEqual(self._search(text), [])

    def test_another_teams_objects_never_appear(self):
        PurchaseOrder.objects.create(
            team=self.other_team,
            external_id="ext-other",
            po_number="117064",
            supplier_no="S-9",
            supplier_name="Other Supplier",
        )
        Shipment.objects.create(team=self.other_team, shipment_number="SH-2026-00124")
        ContainerLocation.objects.create(team=self.other_team, name="Oceanterminalen")

        for term in ("117064", "SH-2026-00124", "Oceanterminalen", FULL_NUMBER):
            with self.subTest(term=term):
                results = self._search(term)
                self.assertTrue(results, f"expected our own team's {term} to still be found")

        # And the other team sees only its own.
        theirs = self._search("Other Supplier", team=self.other_team)
        self.assertEqual([result.title for result in theirs], ["117064"])
        ours = self._search("Other Supplier")
        self.assertEqual(ours, [])


class SearchGroupingTest(SearchTestBase):
    def test_results_are_grouped_in_kind_order_with_labels(self):
        PurchaseOrder.objects.create(
            team=self.team,
            external_id="ext-mcu",
            po_number="MCU-1",
            supplier_no="S-1",
            supplier_name="CPI",
        )

        groups = group_search_results(self._search("MCU"))

        self.assertEqual([group.kind for group in groups], ["container", "purchase_order"])
        self.assertEqual(groups[0].label, "Containers")
        self.assertTrue(all(group.results for group in groups))

    def test_nothing_found_produces_no_groups(self):
        self.assertEqual(group_search_results(self._search("nothing-matches-this")), [])


@override_settings(STORAGES=_TEST_STORAGES)
class SearchEndpointTest(SearchTestBase):
    """The HTMX endpoint behind the search box in the SCM shell."""

    def setUp(self):
        self.client_ = Client()
        self.client_.force_login(self.user)

    def _get(self, query):
        return self.client_.get(reverse("analytics:search"), {"q": query}, HTTP_HX_REQUEST="true")

    def test_it_requires_a_login(self):
        response = Client().get(reverse("analytics:search"), {"q": FULL_NUMBER})
        self.assertIn(response.status_code, [302, 403])

    def test_it_renders_the_results_partial(self):
        response = self._get(FULL_NUMBER)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "scm/partials/search_results.html")

    def test_the_partial_keeps_the_swap_target_in_every_state(self):
        for query in ("", FULL_NUMBER, "nothing-matches-this"):
            with self.subTest(query=query):
                self.assertContains(self._get(query), 'id="scm-search-results"')

    def test_a_container_result_links_to_its_workspace(self):
        response = self._get(FULL_NUMBER)

        self.assertContains(response, reverse("containers:detail", kwargs={"container_id": self.container.pk}))
        self.assertContains(response, FULL_NUMBER)

    def test_results_are_grouped_under_a_heading(self):
        self.assertContains(self._get(FULL_NUMBER), "Containers")

    def test_an_exact_container_number_is_marked(self):
        self.assertContains(self._get(FULL_NUMBER), "Exact match")

    def test_an_empty_query_returns_no_results_and_no_error_text(self):
        response = self._get("")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Exact match")
        self.assertNotContains(response, "Nothing found")

    def test_a_query_with_no_matches_says_so(self):
        self.assertContains(self._get("nothing-matches-this"), "Nothing found")

    def test_another_teams_container_is_never_rendered(self):
        response = self._get(FULL_NUMBER)

        self.assertNotContains(
            response,
            reverse("containers:detail", kwargs={"container_id": self.other_container.pk}),
        )
