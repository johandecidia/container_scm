"""The Container Workspace as a page: header, tabs, and what each section may claim.

The tracking pipeline is covered in apps.scm.tracking and the panel component in
test_tracking_panel; what is tested here is the workspace around them — that the
route did not move, that all four sections arrive in one response, that Related
shows only relationships that exist, and that Activity does not invent history.
"""

from datetime import UTC, datetime, timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.activity import get_container_activity
from apps.scm.containers.models import Container, ContainerLocation, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.containers.workspace import get_container_workspace
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryLine
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

OWNER = "WSP"
CAT = "U"
SERIAL = "410238"


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="45HC",
        defaults={"category": "HC", "length_ft": 40, "high_cube": True, "description": "40' High Cube"},
    )[0]


def _container(team, serial=SERIAL) -> Container:
    return Container.objects.create(
        team=team,
        owner_code=OWNER,
        category_id=CAT,
        serial_number=serial,
        check_digit=calculate_check_digit(OWNER, CAT, serial),
        equipment_type=_equipment_type(),
    )


@override_settings(STORAGES=_TEST_STORAGES)
class WorkspacePageTestBase(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Workspace Page", slug="workspace-page")
        self.user = CustomUser.objects.create_user(username="workspace-page@example.com", password="pass")
        self.team.members.add(self.user, through_defaults={"role": ROLE_MEMBER})
        self.container = _container(self.team)
        self.client_ = Client()
        self.client_.force_login(self.user)

    def _url(self, container=None) -> str:
        return reverse("containers:detail", kwargs={"container_id": (container or self.container).pk})

    def _get(self, container=None, **params):
        return self.client_.get(self._url(container), params)

    def _panel(self, name, response=None):
        """Return the markup of one tab panel, by position.

        Split on the panel role rather than on the Alpine expression: the tab
        buttons mention every tab's name before any panel begins.
        """
        body = (response or self._get()).content.decode()
        panels = body.split('role="tabpanel"')
        order = ["overview", "journey", "activity", "related"]
        self.assertEqual(len(panels), len(order) + 1, "expected exactly four tab panels")
        return panels[order.index(name) + 1]


class WorkspaceRouteTest(WorkspacePageTestBase):
    """The route and the template a workspace is reached through did not change."""

    def test_the_container_detail_url_still_answers(self):
        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "scm/containers/pages/container_detail.html")

    def test_the_url_is_unchanged(self):
        self.assertEqual(self._url(), f"/scm/containers/{self.container.pk}/")

    def test_it_renders_the_container_that_was_asked_for(self):
        other = _container(self.team, serial="410239")

        response = self._get()

        self.assertEqual(response.context["container"], self.container)
        self.assertContains(response, self.container.container_id)
        self.assertNotContains(response, other.container_id)

    def test_another_teams_container_is_not_reachable(self):
        other_team = Team.objects.create(name="Workspace Other", slug="workspace-other")
        theirs = _container(other_team, serial="410240")

        self.assertEqual(self.client_.get(self._url(theirs)).status_code, 404)

    def test_it_requires_a_login(self):
        response = Client().get(self._url())
        self.assertIn(response.status_code, [302, 403])


class WorkspaceHeaderTest(WorkspacePageTestBase):
    def test_the_container_number_is_the_identity(self):
        self.assertContains(self._get(), self.container.container_id)

    def test_the_equipment_type_is_named(self):
        self.assertContains(self._get(), "High Cube")

    def test_actions_are_gathered_into_a_menu(self):
        response = self._get()

        self.assertContains(response, "Actions")
        self.assertContains(response, "Edit container")
        self.assertContains(response, "Delete container")

    def test_the_actions_menu_refreshes_through_the_existing_endpoint(self):
        """No second refresh implementation: the same URL and the same target."""
        response = self._get()

        self.assertContains(
            response,
            reverse("containers:refresh_tracking", args=[self.container.pk]),
        )
        self.assertContains(response, 'hx-target="#container-tracking-panel"')

    def test_an_untracked_container_says_so_in_the_header(self):
        self.assertContains(self._get(), "Not tracked")

    def test_a_container_with_no_route_renders_no_empty_route(self):
        """The absence of a route is a shorter header, not an arrow between dashes."""
        workspace = get_container_workspace(self.team, self.container)

        self.assertFalse(workspace.has_route)
        self.assertEqual(workspace.route_origin, "")
        self.assertEqual(workspace.route_destination, "")


class WorkspaceTabsTest(WorkspacePageTestBase):
    """All four sections arrive in one response; Overview is the one on show."""

    def test_all_four_sections_are_present(self):
        response = self._get()

        for label in ("Overview", "Journey", "Activity", "Related"):
            with self.subTest(section=label):
                self.assertContains(response, f">{label}<")

    def test_overview_is_the_default_tab(self):
        self.assertContains(self._get(), "'overview'")

    def test_a_tab_can_be_deep_linked(self):
        """The tab is client state, so the page answers identically and Alpine reads it."""
        response = self._get(tab="journey")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "tab === 'journey'")

    def test_the_map_is_on_the_default_tab(self):
        """A map that started hidden would fit its bounds to a zero-height canvas."""
        self.assertIn("data-scm-map", self._panel("overview"))

    def test_there_is_only_one_map_on_the_page(self):
        self.assertEqual(self._get().content.decode().count("data-scm-map"), 1)

    def test_the_tracking_panel_and_the_journey_are_separately_addressable(self):
        response = self._get()

        self.assertContains(response, 'id="container-tracking-panel"')
        self.assertContains(response, 'id="container-journey"')

    def test_the_page_does_not_ship_out_of_band_swap_markers(self):
        """hx-swap-oob belongs to the refresh response, never to a full page."""
        self.assertNotContains(self._get(), "hx-swap-oob")


class WorkspaceRefreshReachesBothTabsTest(WorkspacePageTestBase):
    """One refresh, one carrier call, both tabs current."""

    def test_the_refresh_response_carries_the_summary_and_the_timeline(self):
        response = self.client_.post(
            reverse("containers:refresh_tracking", args=[self.container.pk]),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="container-tracking-panel"')
        self.assertContains(response, 'id="container-journey"')

    def test_the_timeline_comes_back_out_of_band(self):
        response = self.client_.post(
            reverse("containers:refresh_tracking", args=[self.container.pk]),
            HTTP_HX_REQUEST="true",
        )

        body = response.content.decode()
        journey = body.split('id="container-journey"', 1)[1]
        self.assertIn('hx-swap-oob="true"', journey.split(">", 1)[0])


class WorkspaceRelatedTest(WorkspacePageTestBase):
    """Related shows relationships that exist, and nothing where none do."""

    def _link_shipment(self) -> Shipment:
        shipment = Shipment.objects.create(
            team=self.team,
            shipment_number="SH-2026-00124",
            origin_port="Shanghai",
            destination_port="Gothenburg",
        )
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)
        return shipment

    def _link_purchase_order(self) -> PurchaseOrder:
        order = PurchaseOrder.objects.create(
            team=self.team,
            external_id="ext-117064",
            po_number="117064",
            supplier_no="S-1",
            supplier_name="CPI",
        )
        line = PurchaseOrderLine.objects.create(
            team=self.team,
            purchase_order=order,
            external_id="ext-line-1",
            line_no="10000",
            item_no="ART-1",
            description="Widgets",
        )
        delivery = SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=order,
            supplier="CPI",
            delivery_reference="DEL-1",
        )
        SupplierDeliveryLine.objects.create(
            team=self.team,
            delivery=delivery,
            purchase_order_line=line,
            container=self.container,
        )
        return order

    def test_a_linked_shipment_is_shown_and_linked(self):
        shipment = self._link_shipment()

        response = self._get()

        self.assertContains(response, "SH-2026-00124")
        self.assertContains(response, reverse("shipments:detail", kwargs={"pk": shipment.pk}))
        self.assertContains(response, "Shanghai → Gothenburg")

    def test_a_linked_purchase_order_is_shown_and_linked(self):
        order = self._link_purchase_order()

        response = self._get()

        self.assertContains(response, "117064")
        self.assertContains(
            response,
            reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": order.pk}),
        )
        self.assertContains(response, "CPI")

    def test_a_recorded_location_links_to_the_containers_there(self):
        location = ContainerLocation.objects.create(
            team=self.team, name="Oceanterminalen", city="Gothenburg", country="Sweden"
        )
        self.container.current_location = location
        self.container.save()

        response = self._get()

        self.assertContains(response, "Oceanterminalen")
        self.assertContains(response, f"{reverse('containers:list')}?location_id={location.pk}")

    def test_a_container_with_no_relations_says_so_once(self):
        response = self._get()

        self.assertContains(response, "Nothing linked to this container yet")

    def test_a_container_with_relations_does_not_say_it_has_none(self):
        self._link_shipment()

        self.assertNotContains(self._get(), "Nothing linked to this container yet")

    def test_another_teams_shipment_is_not_related_in(self):
        """The link row is team-scoped through the shipment, not through itself."""
        other_team = Team.objects.create(name="Related Other", slug="related-other")
        theirs = Shipment.objects.create(team=other_team, shipment_number="SH-OTHER")
        ShipmentContainer.objects.create(shipment=theirs, container=self.container)

        response = self._get()

        self.assertNotContains(response, "SH-OTHER")
        self.assertEqual(response.context["workspace"].shipment_containers, [])


class WorkspaceActivityTest(WorkspacePageTestBase):
    """Activity reports what is recorded, and admits what is not."""

    def _activity(self):
        workspace = get_container_workspace(self.team, self.container)
        return get_container_activity(team=self.team, container=self.container, workspace=workspace)

    def test_a_new_container_has_exactly_its_creation_recorded(self):
        entries = self._activity()

        self.assertEqual([entry.kind for entry in entries], ["created"])

    def test_an_untouched_container_is_not_reported_as_edited(self):
        """Django stamps updated_at on creation, which is not an edit."""
        self.assertNotIn("edited", [entry.kind for entry in self._activity()])

    def test_an_edited_container_is_reported_as_edited_without_naming_fields(self):
        self.container.notes = "Seal replaced"
        self.container.updated_by = self.user
        self.container.save()
        # auto_now would stamp this edit microseconds after creation, which is not
        # what a later edit looks like. Written directly so the timestamps differ
        # the way they do in production.
        Container.objects.filter(pk=self.container.pk).update(updated_at=self.container.created_at + timedelta(days=1))
        self.container.refresh_from_db()

        edits = [entry for entry in self._activity() if entry.kind == "edited"]

        self.assertEqual(len(edits), 1)
        self.assertEqual(str(edits[0].title), "Container record updated")
        # No field-level history exists, so none is claimed.
        self.assertEqual(edits[0].detail, "")

    def test_being_added_to_a_shipment_is_activity(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SH-ACT-1")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        entries = self._activity()

        shipment_entries = [entry for entry in entries if entry.kind == "shipment"]
        self.assertEqual(len(shipment_entries), 1)
        self.assertIn("SH-ACT-1", shipment_entries[0].detail)
        self.assertEqual(shipment_entries[0].url, reverse("shipments:detail", kwargs={"pk": shipment.pk}))

    def test_entries_are_newest_first(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SH-ACT-2")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        entries = self._activity()

        self.assertEqual(
            [entry.occurred_at for entry in entries], sorted((e.occurred_at for e in entries), reverse=True)
        )

    def test_a_movement_is_activity_with_its_source(self):
        from apps.scm.containers.models import ContainerMovement

        origin = ContainerLocation.objects.create(team=self.team, name="Depot A")
        destination = ContainerLocation.objects.create(team=self.team, name="Depot B")
        ContainerMovement.objects.create(
            team=self.team,
            container=self.container,
            from_location=origin,
            to_location=destination,
            occurred_at=datetime(2026, 8, 12, 14, 22, tzinfo=UTC),
        )

        movements = [entry for entry in self._activity() if entry.kind == "movement"]

        self.assertEqual(len(movements), 1)
        self.assertEqual(movements[0].detail, "Depot A → Depot B")

    def test_the_page_states_that_edits_are_not_itemised(self):
        """The limit is part of the feature: this view must not read as an audit log."""
        self.assertContains(self._get(), "not recorded yet")

    def test_a_container_with_no_history_beyond_creation_shows_no_invented_rows(self):
        activity = self._panel("activity")

        self.assertIn("Container created", activity)
        for invented in ("Gate in", "Gate out", "ETA changed", "Tracking refreshed"):
            with self.subTest(invented=invented):
                self.assertNotIn(invented, activity)

    def test_activity_is_team_scoped(self):
        other_team = Team.objects.create(name="Activity Other", slug="activity-other")
        theirs = Shipment.objects.create(team=other_team, shipment_number="SH-OTHER-ACT")
        ShipmentContainer.objects.create(shipment=theirs, container=self.container)

        details = " ".join(entry.detail for entry in self._activity())

        self.assertNotIn("SH-OTHER-ACT", details)
