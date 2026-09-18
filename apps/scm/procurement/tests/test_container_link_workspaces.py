"""The container links as the two workspaces present them, and the views that write them.

The read models are tested for the distinction they are built to preserve: the
purchase order page must show acquired and loaded containers as two separate groups,
and the container page must say "acquired through" and "contents" separately. A test
that only counted links would pass while the pages had merged them.
"""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.containers.workspace import get_container_workspace, get_container_workspaces
from apps.scm.procurement.container_links import link_acquired_container, set_container_load
from apps.scm.procurement.models import (
    ContainerAcquisition,
    ContainerLoad,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderSource,
)
from apps.scm.procurement.selectors import get_purchase_order_line_summaries, get_purchase_order_workspace
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="45G1",
        defaults={"category": "GP", "length_ft": 45, "high_cube": True, "description": "45' HC"},
    )[0]


def _container(team, owner="MSC", serial="123456") -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit(owner, "U", serial),
        equipment_type=_equipment_type(),
    )


def _order(team, po_number="IF117064", source=PurchaseOrderSource.MANUAL) -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=f"{team.slug}-{po_number}",
        po_number=po_number,
        supplier_name="Acme",
        source_system=source,
    )


def _line(order, line_no="10000", item_no="CONT45G1") -> PurchaseOrderLine:
    return PurchaseOrderLine.objects.create(
        team=order.team,
        purchase_order=order,
        external_id=f"{order.external_id}-{line_no}",
        line_no=line_no,
        item_no=item_no,
        description=item_no,
        ordered_qty=Decimal("20"),
    )


class PurchaseOrderWorkspaceLinksTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="PO WS", slug="po-ws-links")
        cls.order = _order(cls.team)
        cls.equipment_line = _line(cls.order, "10000", "CONT45G1")
        cls.goods_line = _line(cls.order, "20000", "DOORS")
        cls.acquired = _container(cls.team, "MSC", "123456")
        cls.also_acquired = _container(cls.team, "MSC", "234567")
        cls.loaded = _container(cls.team, "TCL", "123456")
        link_acquired_container(team=cls.team, purchase_order_line=cls.equipment_line, container=cls.acquired)
        link_acquired_container(team=cls.team, purchase_order_line=cls.equipment_line, container=cls.also_acquired)
        set_container_load(
            team=cls.team, purchase_order_line=cls.goods_line, container=cls.loaded, quantity=Decimal("300")
        )

    def _summaries(self):
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        return {summary["line"].pk: summary for summary in get_purchase_order_line_summaries(workspace)}

    def test_each_line_summary_carries_its_links(self):
        summaries = self._summaries()

        equipment = summaries[self.equipment_line.pk]["links"]
        self.assertEqual(
            [link.container.container_id for link in equipment.acquired],
            [self.acquired.container_id, self.also_acquired.container_id],
        )

    def test_acquired_and_loaded_are_separate_groups_on_the_summary(self):
        summaries = self._summaries()

        self.assertEqual(summaries[self.equipment_line.pk]["links"].loads, [])
        self.assertEqual(summaries[self.goods_line.pk]["links"].acquired, [])
        self.assertEqual(
            [link.container for link in summaries[self.goods_line.pk]["links"].loads],
            [self.loaded],
        )

    def test_another_teams_links_never_appear(self):
        other_team = Team.objects.create(name="PO WS other", slug="po-ws-links-other")
        other_line = _line(_order(other_team, "OTHER"))
        link_acquired_container(
            team=other_team,
            purchase_order_line=other_line,
            container=_container(other_team, "CMA", "999999"),
        )

        summaries = self._summaries()

        self.assertEqual(len(summaries), 2)
        self.assertEqual(len(summaries[self.equipment_line.pk]["links"].acquired), 2)


class PurchaseOrderWorkspacePageTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="PO page", slug="po-page-links")
        cls.user = CustomUser.objects.create_user(username="po-page@example.com", password="pw")
        Membership.objects.create(team=cls.team, user=cls.user, role="admin")
        cls.order = _order(cls.team, "PO-PAGE")
        cls.equipment_line = _line(cls.order, "10000", "CONT45G1")
        cls.goods_line = _line(cls.order, "20000", "DOORS")
        cls.acquired = _container(cls.team, "MSC", "123456")
        cls.loaded = _container(cls.team, "TCL", "123456")
        link_acquired_container(team=cls.team, purchase_order_line=cls.equipment_line, container=cls.acquired)
        set_container_load(
            team=cls.team, purchase_order_line=cls.goods_line, container=cls.loaded, quantity=Decimal("300")
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["team"] = self.team.pk
        session.save()

    def test_the_workspace_names_both_groups_and_both_boxes(self):
        response = self.client.get(
            reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": self.order.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Acquired containers")
        self.assertContains(response, "Loaded in")
        self.assertContains(response, self.acquired.container_id)
        self.assertContains(response, self.loaded.container_id)

    def test_the_workspace_offers_both_link_actions(self):
        response = self.client.get(
            reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": self.order.pk})
        )

        self.assertContains(
            response,
            reverse("procurement:line_link_acquired_container", kwargs={"line_id": self.equipment_line.pk}),
        )
        self.assertContains(
            response,
            reverse("procurement:line_add_container_load", kwargs={"line_id": self.goods_line.pk}),
        )


class ContainerWorkspaceProcurementTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Box WS", slug="box-ws-links")
        cls.user = CustomUser.objects.create_user(username="box-ws@example.com", password="pw")
        Membership.objects.create(team=cls.team, user=cls.user, role="admin")
        cls.equipment_order = _order(cls.team, "IF117064")
        cls.equipment_line = _line(cls.equipment_order, "10000", "CONT45G1")
        cls.goods_order = _order(cls.team, "PO-123")
        cls.goods_line = _line(cls.goods_order, "20000", "DOORS")
        cls.container = _container(cls.team, "MSC", "123456")
        link_acquired_container(team=cls.team, purchase_order_line=cls.equipment_line, container=cls.container)
        set_container_load(
            team=cls.team, purchase_order_line=cls.goods_line, container=cls.container, quantity=Decimal("300")
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["team"] = self.team.pk
        session.save()

    def test_the_workspace_carries_both_procurement_answers(self):
        workspace = get_container_workspace(team=self.team, container=self.container)

        self.assertIsNotNone(workspace.procurement)
        self.assertEqual(workspace.procurement.acquired_line, self.equipment_line)
        self.assertEqual(workspace.procurement.acquired_order, self.equipment_order)
        self.assertEqual([load.purchase_order_line for load in workspace.procurement.loads], [self.goods_line])

    def test_a_bulk_built_workspace_reports_no_procurement_rather_than_none(self):
        """It never loaded the links, so it must not claim there are none."""
        workspaces = get_container_workspaces(self.team, [self.container])

        self.assertIsNone(workspaces[self.container.pk].procurement)
        self.assertTrue(workspaces[self.container.pk].tracking_only)

    def test_the_container_page_shows_the_procurement_section(self):
        response = self.client.get(reverse("containers:detail", kwargs={"container_id": self.container.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Acquired through")
        self.assertContains(response, "Contents")
        self.assertContains(response, "IF117064")
        self.assertContains(response, "PO-123")
        self.assertContains(response, "300")

    def test_an_unlinked_container_gets_no_procurement_section(self):
        stray = _container(self.team, "HLX", "999999")

        response = self.client.get(reverse("containers:detail", kwargs={"container_id": stray.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Acquired through")


class LinkViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Link views", slug="link-views")
        cls.user = CustomUser.objects.create_user(username="link-views@example.com", password="pw")
        Membership.objects.create(team=cls.team, user=cls.user, role="admin")
        cls.order = _order(cls.team, "PO-LINKV")
        cls.line = _line(cls.order, "10000", "CONT45G1")
        cls.other_line = _line(cls.order, "20000", "DOORS")

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["team"] = self.team.pk
        session.save()

    def test_acquisition_form_renders(self):
        response = self.client.get(
            reverse("procurement:line_link_acquired_container", kwargs={"line_id": self.line.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Link acquired container")

    def test_posting_the_acquisition_form_links_the_container(self):
        container = _container(self.team)

        response = self.client.post(
            reverse("procurement:line_link_acquired_container", kwargs={"line_id": self.line.pk}),
            {"container": container.pk},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ContainerAcquisition.objects.get().container, container)

    def test_an_already_acquired_container_is_not_even_offered(self):
        """The picker excludes boxes with an origin, so an impossible choice never appears."""
        container = _container(self.team)
        link_acquired_container(team=self.team, purchase_order_line=self.line, container=container)

        response = self.client.post(
            reverse("procurement:line_link_acquired_container", kwargs={"line_id": self.other_line.pk}),
            {"container": container.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContainerAcquisition.objects.get().purchase_order_line, self.line)

    def test_unlinking_an_acquisition_through_the_view(self):
        container = _container(self.team)
        acquisition = link_acquired_container(team=self.team, purchase_order_line=self.line, container=container)

        response = self.client.post(
            reverse("procurement:acquisition_unlink", kwargs={"acquisition_id": acquisition.pk})
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ContainerAcquisition.objects.exists())
        self.assertTrue(Container.objects.filter(pk=container.pk).exists())

    def test_posting_the_load_form_records_the_quantity(self):
        container = _container(self.team, "TCL", "123456")

        response = self.client.post(
            reverse("procurement:line_add_container_load", kwargs={"line_id": self.other_line.pk}),
            {"container": container.pk, "quantity": "300"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ContainerLoad.objects.get().quantity, Decimal("300"))

    def test_the_load_form_accepts_a_blank_quantity(self):
        container = _container(self.team, "TCL", "123456")

        response = self.client.post(
            reverse("procurement:line_add_container_load", kwargs={"line_id": self.other_line.pk}),
            {"container": container.pk, "quantity": ""},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(ContainerLoad.objects.get().quantity)

    def test_posting_the_load_form_twice_updates_rather_than_duplicates(self):
        container = _container(self.team, "TCL", "123456")
        url = reverse("procurement:line_add_container_load", kwargs={"line_id": self.other_line.pk})

        self.client.post(url, {"container": container.pk, "quantity": "300"})
        self.client.post(url, {"container": container.pk, "quantity": "250"})

        self.assertEqual(ContainerLoad.objects.count(), 1)
        self.assertEqual(ContainerLoad.objects.get().quantity, Decimal("250"))

    def test_removing_a_load_through_the_view(self):
        container = _container(self.team, "TCL", "123456")
        load = set_container_load(team=self.team, purchase_order_line=self.other_line, container=container)

        response = self.client.post(reverse("procurement:container_load_remove", kwargs={"load_id": load.pk}))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ContainerLoad.objects.exists())
        self.assertTrue(Container.objects.filter(pk=container.pk).exists())

    def test_an_htmx_write_asks_for_a_page_refresh(self):
        """A link changes the line, the Containers tab and Completeness together."""
        container = _container(self.team)

        response = self.client.post(
            reverse("procurement:line_link_acquired_container", kwargs={"line_id": self.line.pk}),
            {"container": container.pk},
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Refresh"], "true")

    def test_another_teams_container_is_not_offered_by_the_picker(self):
        other_team = Team.objects.create(name="Link views other", slug="link-views-other")
        foreign = _container(other_team, "CMA", "999999")

        response = self.client.post(
            reverse("procurement:line_link_acquired_container", kwargs={"line_id": self.line.pk}),
            {"container": foreign.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ContainerAcquisition.objects.exists())

    def test_another_teams_line_is_not_reachable(self):
        other_team = Team.objects.create(name="Link views alt", slug="link-views-alt")
        foreign_line = _line(_order(other_team, "ALT"))

        response = self.client.get(
            reverse("procurement:line_link_acquired_container", kwargs={"line_id": foreign_line.pk})
        )

        self.assertEqual(response.status_code, 404)

    def test_another_teams_load_cannot_be_removed(self):
        other_team = Team.objects.create(name="Link views third", slug="link-views-third")
        foreign_line = _line(_order(other_team, "THIRD"))
        foreign_load = set_container_load(
            team=other_team,
            purchase_order_line=foreign_line,
            container=_container(other_team, "ONE", "888888"),
        )

        response = self.client.post(reverse("procurement:container_load_remove", kwargs={"load_id": foreign_load.pk}))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(ContainerLoad.objects.filter(pk=foreign_load.pk).exists())
