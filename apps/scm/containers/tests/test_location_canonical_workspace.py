"""The Location Workspace once it can answer what is expected, and its alias UI.

Before LOC-1 the Expected tab reported *why* it could not answer. It can now, and
the important part is what the answer is made of: canonical destinations and the
containment MCR recorded, never a comparison between a carrier's destination text
and this location's name.

The empty state is asserted too. "Nothing is expected here" and "we cannot tell you
what is expected" are different claims; the page must now make the first one.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.choices import LocationType
from apps.scm.containers.location_workspace import get_location_workspace
from apps.scm.containers.models import Container, ContainerLocation, EquipmentType, LocationAlias
from apps.scm.containers.selectors import get_location_subtree_ids
from apps.scm.containers.services import create_location, create_location_alias
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.models import TrackingEvent, TrackingProvider
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _equipment() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team, serial: str) -> Container:
    return Container.objects.create(
        team=team,
        owner_code="MSK",
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit("MSK", "U", serial),
        equipment_type=_equipment(),
    )


def _shipment(team, number, destination_location, *, days=3, destination_port="Gothenburg"):
    today = timezone.localdate()
    shipment = Shipment.objects.create(
        team=team,
        shipment_number=number,
        carrier="Maersk",
        status=Shipment.Status.IN_TRANSIT,
        destination_port=destination_port,
        destination_location=destination_location,
        eta=today + timedelta(days=days),
        original_eta=today + timedelta(days=days),
    )
    ShipmentContainer.objects.create(shipment=shipment, container=_container(team, number[-6:]))
    return shipment


class SubtreeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Subtree", slug="loc-subtree")
        cls.other_team = Team.objects.create(name="Theirs", slug="loc-subtree-theirs")

    def test_a_leaf_is_its_own_subtree(self):
        depot = create_location(self.team, {"name": "Depot"})
        self.assertEqual(get_location_subtree_ids(team=self.team, location=depot), [depot.pk])

    def test_a_port_includes_its_terminals(self):
        port = create_location(self.team, {"name": "Göteborg", "location_type": LocationType.PORT})
        first = create_location(self.team, {"name": "Oceanterminalen", "parent_location": port})
        second = create_location(self.team, {"name": "APM", "parent_location": port})
        self.assertCountEqual(get_location_subtree_ids(team=self.team, location=port), [port.pk, first.pk, second.pk])

    def test_the_subtree_reaches_two_levels_down(self):
        port = create_location(self.team, {"name": "Göteborg", "location_type": LocationType.PORT})
        terminal = create_location(self.team, {"name": "Oceanterminalen", "parent_location": port})
        berth = create_location(self.team, {"name": "Berth 12", "parent_location": terminal})
        self.assertIn(berth.pk, get_location_subtree_ids(team=self.team, location=port))

    def test_a_terminal_does_not_include_the_port_above_it(self):
        port = create_location(self.team, {"name": "Göteborg", "location_type": LocationType.PORT})
        terminal = create_location(self.team, {"name": "Oceanterminalen", "parent_location": port})
        self.assertEqual(get_location_subtree_ids(team=self.team, location=terminal), [terminal.pk])


class ExpectedArrivalsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Expected", slug="loc-expected")

    def setUp(self):
        self.port = create_location(
            self.team, {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT"}
        )
        self.terminal = create_location(
            self.team,
            {"name": "Oceanterminalen", "location_type": LocationType.DEPOT, "parent_location": self.port},
        )
        self.sibling = create_location(self.team, {"name": "APM Terminals Gothenburg", "parent_location": self.port})

    def expected(self, location):
        return sorted(obj.label for obj in get_location_workspace(self.team, location).expected.objects)

    def test_a_canonical_destination_puts_a_shipment_on_the_terminals_tab(self):
        """The headline capability: no text matching anywhere in this path."""
        _shipment(self.team, "SHP-100001", self.terminal)
        self.assertEqual(self.expected(self.terminal), ["SHP-100001"])

    def test_the_answer_is_now_available_rather_than_explained_away(self):
        workspace = get_location_workspace(self.team, self.terminal)
        self.assertTrue(workspace.expected.is_available)
        self.assertEqual(workspace.expected.reason, "")

    def test_a_sibling_terminals_traffic_does_not_appear(self):
        _shipment(self.team, "SHP-200002", self.sibling)
        self.assertEqual(self.expected(self.terminal), [])

    def test_the_port_sees_what_is_bound_for_its_terminals(self):
        _shipment(self.team, "SHP-100001", self.terminal)
        _shipment(self.team, "SHP-200002", self.sibling)
        _shipment(self.team, "SHP-300003", self.port)
        self.assertEqual(self.expected(self.port), ["SHP-100001", "SHP-200002", "SHP-300003"])

    def test_a_shipment_whose_destination_is_only_text_is_not_claimed(self):
        """It says "Gothenburg". That is not a decision about which place."""
        _shipment(self.team, "SHP-400004", None)
        self.assertEqual(self.expected(self.port), [])
        self.assertEqual(self.expected(self.terminal), [])

    def test_a_terminal_named_in_the_destination_text_is_still_not_matched(self):
        """Even where the text happens to be the location's exact name."""
        _shipment(self.team, "SHP-500005", None, destination_port="Oceanterminalen")
        self.assertEqual(self.expected(self.terminal), [])

    def test_the_container_count_comes_from_the_shipments_boxes(self):
        _shipment(self.team, "SHP-100001", self.terminal)
        self.assertEqual(get_location_workspace(self.team, self.terminal).expected.container_count, 1)

    def test_something_arriving_beyond_the_window_is_not_expected_yet(self):
        _shipment(self.team, "SHP-600006", self.terminal, days=90)
        self.assertEqual(self.expected(self.terminal), [])


class ExpectedArrivalsIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Mine", slug="loc-exp-mine")
        cls.other_team = Team.objects.create(name="Theirs", slug="loc-exp-theirs")

    def test_another_teams_shipment_never_appears_on_my_locations_tab(self):
        mine = create_location(self.team, {"name": "Oceanterminalen", "unlocode": "SEGOT"})
        theirs = create_location(self.other_team, {"name": "Oceanterminalen", "unlocode": "SEGOT"})
        _shipment(self.other_team, "SHP-THEIRS", theirs)
        workspace = get_location_workspace(self.team, mine)
        self.assertEqual(workspace.expected.objects, [])


# The aggregation of unmatched carrier evidence used to be tested here, against
# `get_unresolved_external_locations`. LOC-5 moved it to
# `apps.scm.visibility.location_quality`, which groups the same evidence, counts the
# containers behind it and can act on it; the tests moved with it, to
# `apps/scm/visibility/tests/test_location_quality.py`.


@override_settings(STORAGES=_TEST_STORAGES)
class LocationUiTest(TestCase):
    """Only as much UI as makes LOC-1 operational, over the existing patterns."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="UI", slug="loc-ui")
        cls.user = CustomUser.objects.create_user(username="locui@example.com", password="pw")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        self.port = create_location(
            self.team, {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT"}
        )
        self.terminal = create_location(
            self.team,
            {
                "name": "Oceanterminalen",
                "location_type": LocationType.DEPOT,
                # The terminal carries the code too — the case the schema must allow
                # and the header has to render without implying it is unique.
                "unlocode": "SEGOT",
                "parent_location": self.port,
            },
        )

    # -- creating and editing ------------------------------------------------

    def test_a_location_can_be_created_with_its_canonical_identifiers(self):
        response = self.client.post(
            reverse("containers:location_create"),
            data={
                "name": "Rotterdam",
                "location_type": LocationType.PORT,
                "unlocode": "nl rtm",
                "country_code": "nl",
                "is_active": True,
            },
        )
        self.assertEqual(response.status_code, 302)
        created = ContainerLocation.objects.get(team=self.team, name="Rotterdam")
        self.assertEqual(created.unlocode, "NLRTM")
        self.assertEqual(created.country_code, "NL")

    def test_a_code_typed_with_a_space_is_accepted_rather_than_refused_on_length(self):
        """The column holds five characters; normalisation is what makes it five."""
        response = self.client.post(
            reverse("containers:location_create"),
            data={"name": "Felixstowe", "location_type": LocationType.PORT, "unlocode": "GB FXT", "is_active": True},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ContainerLocation.objects.get(team=self.team, name="Felixstowe").unlocode, "GBFXT")

    def test_something_that_is_not_a_code_is_an_error_and_not_silently_dropped(self):
        response = self.client.post(
            reverse("containers:location_create"),
            data={"name": "Nowhere", "location_type": LocationType.OTHER, "unlocode": "Gothenburg", "is_active": True},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "is not a UN/LOCODE")
        self.assertFalse(ContainerLocation.objects.filter(team=self.team, name="Nowhere").exists())

    def test_a_parent_can_be_set_through_the_form(self):
        response = self.client.post(
            reverse("containers:location_update", kwargs={"location_id": self.terminal.pk}),
            data={
                "name": "Oceanterminalen",
                "location_type": LocationType.DEPOT,
                "parent_location": str(self.port.pk),
                "is_active": True,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.terminal.refresh_from_db()
        self.assertEqual(self.terminal.parent_location, self.port)

    def test_the_form_does_not_offer_the_location_as_its_own_parent(self):
        response = self.client.get(
            reverse("containers:location_update", kwargs={"location_id": self.port.pk}), HTTP_HX_REQUEST="true"
        )
        parents = response.context["form"].fields["parent_location"].queryset
        self.assertNotIn(self.port, parents)
        self.assertIn(self.terminal, parents)

    def test_the_form_does_not_offer_another_teams_location_as_a_parent(self):
        other_team = Team.objects.create(name="Theirs", slug="loc-ui-theirs")
        theirs = create_location(other_team, {"name": "Their Port"})
        response = self.client.get(
            reverse("containers:location_update", kwargs={"location_id": self.terminal.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertNotIn(theirs, response.context["form"].fields["parent_location"].queryset)

    # -- reading -------------------------------------------------------------

    def test_the_list_shows_the_code_and_the_containment(self):
        response = self.client.get(reverse("containers:location_list"))
        self.assertContains(response, "SEGOT")
        self.assertContains(response, "Oceanterminalen")

    def test_the_workspace_shows_the_parent_and_the_code(self):
        response = self.client.get(reverse("containers:location_detail", kwargs={"location_id": self.terminal.pk}))
        self.assertContains(response, "SEGOT")
        self.assertContains(response, reverse("containers:location_detail", args=[self.port.pk]))

    def test_the_workspace_no_longer_says_the_question_is_unanswerable(self):
        response = self.client.get(reverse("containers:location_detail", kwargs={"location_id": self.terminal.pk}))
        self.assertNotContains(response, "nothing in the data reliably connects")
        self.assertContains(response, "Nothing is expected here")

    def test_the_workspace_lists_what_is_expected(self):
        _shipment(self.team, "SHP-100001", self.terminal)
        response = self.client.get(reverse("containers:location_detail", kwargs={"location_id": self.terminal.pk}))
        self.assertContains(response, "SHP-100001")

    def test_the_list_points_at_the_data_quality_queue_when_there_is_work(self):
        """The rows themselves live on the queue now. This is the pointer to it."""
        provider = TrackingProvider.objects.create(code="traqo", name="Traqo")
        TrackingEvent.objects.create(
            team=self.team,
            provider=provider,
            event_fingerprint="fp-ui",
            location_name="GOTHENBURG",
            location_resolution_status="unresolved",
            event_datetime=timezone.now(),
        )
        response = self.client.get(reverse("containers:location_list"))
        self.assertContains(response, "Fix location coverage")
        self.assertContains(response, reverse("visibility:location_quality"))

    def test_the_list_does_not_nag_when_the_location_data_is_complete(self):
        """Both locations here carry coordinates, and nothing is unmatched."""
        ContainerLocation.objects.filter(team=self.team).update(latitude="57.7", longitude="11.9")
        response = self.client.get(reverse("containers:location_list"))
        self.assertNotContains(response, "Fix location coverage")

    # -- aliases -------------------------------------------------------------

    def test_the_alias_form_loads(self):
        response = self.client.get(
            reverse("containers:location_alias_create", kwargs={"location_id": self.port.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add alias")

    def test_an_alias_can_be_added(self):
        response = self.client.post(
            reverse("containers:location_alias_create", kwargs={"location_id": self.port.pk}),
            data={"source": "Traqo", "external_name": "GOTHENBURG"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        alias = LocationAlias.objects.get(team=self.team, location=self.port)
        self.assertEqual(alias.source, "traqo")
        self.assertEqual(alias.normalized_name, "gothenburg")

    def test_an_alias_with_neither_identifier_is_refused_with_a_message(self):
        response = self.client.post(
            reverse("containers:location_alias_create", kwargs={"location_id": self.port.pk}),
            data={"source": "traqo", "external_name": "", "external_code": ""},
            HTTP_HX_REQUEST="true",
        )
        self.assertContains(response, "Give the external code, the external name, or both.")
        self.assertFalse(LocationAlias.objects.filter(team=self.team).exists())

    def test_a_duplicate_alias_is_a_message_rather_than_a_second_row(self):
        create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG"})
        response = self.client.post(
            reverse("containers:location_alias_create", kwargs={"location_id": self.terminal.pk}),
            data={"source": "traqo", "external_name": "gothenburg"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(LocationAlias.objects.filter(team=self.team).count(), 1)

    def test_an_alias_can_be_removed(self):
        alias = create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG"})
        response = self.client.post(
            reverse(
                "containers:location_alias_delete",
                kwargs={"location_id": self.port.pk, "alias_id": alias.pk},
            ),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(LocationAlias.objects.filter(pk=alias.pk).exists())

    def test_the_workspace_lists_the_aliases(self):
        create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG"})
        response = self.client.get(reverse("containers:location_detail", kwargs={"location_id": self.port.pk}))
        self.assertContains(response, "External names")
        self.assertContains(response, "GOTHENBURG")

    def test_another_teams_location_cannot_be_given_an_alias(self):
        other_team = Team.objects.create(name="Theirs", slug="loc-ui-alias-theirs")
        theirs = create_location(other_team, {"name": "Their Port"})
        response = self.client.post(
            reverse("containers:location_alias_create", kwargs={"location_id": theirs.pk}),
            data={"source": "traqo", "external_name": "THEIRS"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(LocationAlias.objects.exists())

    def test_another_teams_alias_cannot_be_removed(self):
        other_team = Team.objects.create(name="Theirs", slug="loc-ui-del-theirs")
        theirs = create_location(other_team, {"name": "Their Port"})
        alias = create_location_alias(other_team, theirs, {"source": "traqo", "external_name": "THEIRS"})
        response = self.client.post(
            reverse(
                "containers:location_alias_delete",
                kwargs={"location_id": theirs.pk, "alias_id": alias.pk},
            )
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(LocationAlias.objects.filter(pk=alias.pk).exists())


class SeedCommandTest(TestCase):
    """The development example, and the coordinates it deliberately does not invent."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Seed", slug="loc-seed")

    def run_command(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("seed_locations", "--team", self.team.slug, stdout=out)
        return out.getvalue()

    def test_it_establishes_the_port_with_the_terminal_inside_it(self):
        self.run_command()
        port = ContainerLocation.objects.get(team=self.team, name="Göteborg")
        terminal = ContainerLocation.objects.get(team=self.team, name="Oceanterminalen")
        self.assertEqual(port.location_type, LocationType.PORT)
        self.assertEqual(port.unlocode, "SEGOT")
        self.assertEqual(terminal.location_type, LocationType.DEPOT)
        self.assertEqual(terminal.unlocode, "SEGOT")
        self.assertEqual(terminal.parent_location, port)

    def test_it_leaves_coordinates_unset_rather_than_guessing_them(self):
        self.run_command()
        for name in ("Göteborg", "Oceanterminalen"):
            with self.subTest(name=name):
                location = ContainerLocation.objects.get(team=self.team, name=name)
                self.assertIsNone(location.latitude)
                self.assertIsNone(location.longitude)

    def test_the_provider_names_resolve_to_the_port_and_not_a_terminal(self):
        """ "GOTHENBURG" from a carrier names the city, not a berth."""
        from apps.scm.containers.location_resolver import LocationQuery, resolve_location

        self.run_command()
        port = ContainerLocation.objects.get(team=self.team, name="Göteborg")
        for source, name in (("traqo", "GOTHENBURG"), ("maersk", "GOTEBORG"), ("cma-cgm", "GOTHENBURG, SE")):
            with self.subTest(source=source):
                result = resolve_location(self.team, LocationQuery(source=source, name=name))
                self.assertEqual(result.location, port)

    def test_running_it_twice_does_not_duplicate_anything(self):
        self.run_command()
        self.run_command()
        self.assertEqual(ContainerLocation.objects.filter(team=self.team).count(), 2)
        self.assertEqual(LocationAlias.objects.filter(team=self.team).count(), 5)

    def test_an_unknown_team_is_an_error_rather_than_a_silent_no_op(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("seed_locations", "--team", "no-such-team")
