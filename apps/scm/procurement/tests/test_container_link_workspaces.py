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
from apps.scm.procurement.workspace import ContainerPath
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryLine
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


def _delivery(order, reference="IF-1") -> SupplierDelivery:
    return SupplierDelivery.objects.create(
        team=order.team,
        purchase_order=order,
        delivery_reference=reference,
        supplier="Acme",
    )


def _delivery_line(delivery, line, container=None, quantity=Decimal("1"), article="") -> SupplierDeliveryLine:
    return SupplierDeliveryLine.objects.create(
        team=delivery.team,
        delivery=delivery,
        purchase_order_line=line,
        article=article,
        delivery_qty=quantity,
        container=container,
    )


class PurchaseOrderWorkspaceContainerPopulationTest(TestCase):
    """``container_rows`` is the order's physical containers — all of them, each once.

    A container reaches a purchase order three ways: booked onto a supplier delivery
    line, acquired on an order line, or loaded with an order line's goods. The rows
    are one deduplicated population over all three, because ``container_count``, the
    Containers tab and the completeness gaps all read this list and would otherwise
    describe three different orders.
    """

    def setUp(self):
        self.team = Team.objects.create(name="Population", slug="po-population")
        self.user = CustomUser.objects.create_user(username="population@example.com", password="pw")
        Membership.objects.create(team=self.team, user=self.user, role="admin")
        self.order = _order(self.team, "PO-POP")
        self.equipment_line = _line(self.order, "10000", "CONT45G1")
        self.goods_line = _line(self.order, "20000", "DOORS")

    def _rows(self):
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        return workspace, {row.container.pk: row for row in workspace.container_rows}

    # -- one path at a time -------------------------------------------------

    def test_an_acquisition_only_container_is_on_the_order(self):
        container = _container(self.team, "MSC", "300001")
        link_acquired_container(team=self.team, purchase_order_line=self.equipment_line, container=container)

        workspace, rows = self._rows()

        self.assertIn(container.pk, rows)
        self.assertEqual(workspace.container_count, 1)
        self.assertEqual(rows[container.pk].paths, [ContainerPath.ACQUISITION])
        self.assertTrue(rows[container.pk].is_acquired)

    def test_a_load_only_container_is_on_the_order(self):
        container = _container(self.team, "TCL", "300002")
        set_container_load(
            team=self.team, purchase_order_line=self.goods_line, container=container, quantity=Decimal("300")
        )

        workspace, rows = self._rows()

        self.assertIn(container.pk, rows)
        self.assertEqual(workspace.container_count, 1)
        self.assertEqual(rows[container.pk].paths, [ContainerPath.LOAD])
        self.assertTrue(rows[container.pk].carries_load)

    def test_a_supplier_delivery_container_is_still_on_the_order(self):
        """The path that already worked keeps working, delivery reference included."""
        container = _container(self.team, "HLX", "300003")
        _delivery_line(_delivery(self.order, "IF-DEL"), self.goods_line, container=container, article="DOORS")

        workspace, rows = self._rows()

        self.assertEqual(workspace.container_count, 1)
        self.assertEqual(rows[container.pk].paths, [ContainerPath.DELIVERY])
        self.assertEqual(rows[container.pk].delivery_references, ["IF-DEL"])
        self.assertEqual(rows[container.pk].articles, ["DOORS"])

    # -- deduplication ------------------------------------------------------

    def test_a_container_reached_every_way_is_one_row_naming_every_way(self):
        container = _container(self.team, "MSC", "300004")
        _delivery_line(_delivery(self.order, "IF-ALL"), self.goods_line, container=container, article="DOORS")
        link_acquired_container(team=self.team, purchase_order_line=self.equipment_line, container=container)
        set_container_load(team=self.team, purchase_order_line=self.goods_line, container=container)

        workspace, rows = self._rows()

        self.assertEqual(len(workspace.container_rows), 1)
        self.assertEqual(workspace.container_count, 1)
        self.assertCountEqual(
            rows[container.pk].paths,
            [ContainerPath.DELIVERY, ContainerPath.ACQUISITION, ContainerPath.LOAD],
        )

    def test_several_relationships_and_articles_do_not_multiply_the_row(self):
        """Two lines loading one box, one of them twice over two deliveries: one row."""
        container = _container(self.team, "MSC", "300005")
        spares_line = _line(self.order, "30000", "SPARES")
        _delivery_line(_delivery(self.order, "IF-A"), self.goods_line, container=container, article="DOORS")
        _delivery_line(_delivery(self.order, "IF-B"), spares_line, container=container, article="SPARES")
        set_container_load(team=self.team, purchase_order_line=self.goods_line, container=container)
        set_container_load(team=self.team, purchase_order_line=spares_line, container=container)

        workspace, rows = self._rows()

        self.assertEqual(len(workspace.container_rows), 1)
        self.assertCountEqual(rows[container.pk].articles, ["DOORS", "SPARES"])
        self.assertCountEqual(rows[container.pk].delivery_references, ["IF-A", "IF-B"])
        self.assertCountEqual(rows[container.pk].paths, [ContainerPath.DELIVERY, ContainerPath.LOAD])

    def test_each_distinct_box_is_counted_once_across_paths(self):
        acquired = _container(self.team, "MSC", "300006")
        loaded = _container(self.team, "TCL", "300007")
        delivered = _container(self.team, "HLX", "300008")
        link_acquired_container(team=self.team, purchase_order_line=self.equipment_line, container=acquired)
        set_container_load(team=self.team, purchase_order_line=self.goods_line, container=loaded)
        _delivery_line(_delivery(self.order, "IF-MIX"), self.goods_line, container=delivered)

        workspace, _rows = self._rows()

        self.assertEqual(workspace.container_count, 3)
        self.assertCountEqual(
            [row.container.pk for row in workspace.container_rows], [acquired.pk, loaded.pk, delivered.pk]
        )

    # -- a direct link is not a supplier delivery ---------------------------

    def test_a_direct_link_is_not_presented_as_a_supplier_delivery(self):
        container = _container(self.team, "MSC", "300009")
        link_acquired_container(team=self.team, purchase_order_line=self.equipment_line, container=container)

        _workspace, rows = self._rows()

        row = rows[container.pk]
        self.assertEqual(row.delivery_references, [])
        self.assertFalse(row.via_supplier_delivery)
        # The article is the line's own item, read through the link rather than
        # invented from a delivery nobody booked.
        self.assertEqual(row.articles, ["CONT45G1"])

    def test_a_direct_link_adds_no_delivery_row(self):
        container = _container(self.team, "MSC", "300010")
        link_acquired_container(team=self.team, purchase_order_line=self.equipment_line, container=container)

        workspace, _rows = self._rows()

        self.assertEqual(workspace.delivery_rows, [])
        self.assertEqual(workspace.delivery_count, 0)

    # -- what the rest of the workspace derives from the rows ---------------

    def test_completeness_sees_a_directly_linked_container(self):
        """`has_started` and the no-shipment gap both read container_rows."""
        container = _container(self.team, "MSC", "300011")
        link_acquired_container(team=self.team, purchase_order_line=self.equipment_line, container=container)

        workspace, _rows = self._rows()

        self.assertTrue(workspace.has_started)
        self.assertEqual([row.container for row in workspace.containers_needing_shipment], [container])
        self.assertIn("no_shipment", [gap.code for gap in workspace.gaps])

    def test_the_containers_tab_lists_a_directly_linked_box_after_a_full_refresh(self):
        container = _container(self.team, "MSC", "300012")
        link_acquired_container(team=self.team, purchase_order_line=self.equipment_line, container=container)
        self.client.force_login(self.user)
        session = self.client.session
        session["team"] = self.team.pk
        session.save()

        response = self.client.get(
            reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": self.order.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, container.container_id)
        self.assertNotContains(response, "No containers booked yet")

    def test_the_rows_do_not_cost_a_query_per_linked_container(self):
        """Links are loaded in bulk like delivery lines, so the tab stays flat."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries_for(order):
            with CaptureQueriesContext(connection) as captured:
                workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)
                for row in workspace.container_rows:
                    _ = (row.article_label, row.path_label, row.shipment, row.eta, row.filter_buckets)
                for summary in get_purchase_order_line_summaries(workspace):
                    _ = [link.container.container_id for link in summary["links"].acquired]
                    _ = [link.container.container_id for link in summary["links"].loads]
            return len(captured)

        one_order = _order(self.team, "PO-Q1")
        one_line = _line(one_order, "10000", "CONT45G1")
        link_acquired_container(
            team=self.team, purchase_order_line=one_line, container=_container(self.team, "MCU", "400001")
        )

        many_order = _order(self.team, "PO-Q8")
        many_line = _line(many_order, "10000", "CONT45G1")
        for index in range(8):
            link_acquired_container(
                team=self.team,
                purchase_order_line=many_line,
                container=_container(self.team, "MCU", f"{410000 + index:06d}"),
            )

        self.assertEqual(queries_for(many_order), queries_for(one_order))

    def test_another_teams_link_never_reaches_these_rows(self):
        other_team = Team.objects.create(name="Population other", slug="po-population-other")
        other_order = _order(other_team, "OTHER-POP")
        other_line = _line(other_order, "10000", "CONT45G1")
        link_acquired_container(
            team=other_team,
            purchase_order_line=other_line,
            container=_container(other_team, "CMA", "399999"),
        )
        mine = _container(self.team, "MSC", "300013")
        link_acquired_container(team=self.team, purchase_order_line=self.equipment_line, container=mine)

        workspace, _rows = self._rows()

        self.assertEqual([row.container.pk for row in workspace.container_rows], [mine.pk])


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

    def test_another_teams_load_cannot_be_removed_through_the_view(self):
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


class BusinessCentralLinkEndpointTest(TestCase):
    """The four link routes, hit directly against an order Business Central owns.

    The workspace draws no buttons for a BC order, so these posts are what a person
    with a URL, a stale page or a script does. None of them may write: the service
    refuses whatever the view decided, and the view turns the refusal into a sentence
    rather than a stack trace.
    """

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="BC endpoints", slug="bc-link-endpoints")
        cls.user = CustomUser.objects.create_user(username="bc-endpoints@example.com", password="pw")
        Membership.objects.create(team=cls.team, user=cls.user, role="admin")
        cls.order = _order(cls.team, "BC-EP", source=PurchaseOrderSource.BUSINESS_CENTRAL)
        cls.equipment_line = _line(cls.order, "10000", "CONT45G1")
        cls.goods_line = _line(cls.order, "20000", "DOORS")

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["team"] = self.team.pk
        session.save()

    def _acquisition(self, container):
        """A link as the source system would have left it — made without the service."""
        return ContainerAcquisition.objects.create(
            team=self.team, purchase_order_line=self.equipment_line, container=container
        )

    def _load(self, container, quantity=Decimal("300")):
        return ContainerLoad.objects.create(
            team=self.team, purchase_order_line=self.goods_line, container=container, quantity=quantity
        )

    # -- acquisition --------------------------------------------------------

    def test_the_acquisition_form_is_not_even_rendered_for_a_bc_line(self):
        response = self.client.get(
            reverse("procurement:line_link_acquired_container", kwargs={"line_id": self.equipment_line.pk})
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotContains(response, "Link container", status_code=302)

    def test_posting_an_acquisition_to_a_bc_line_writes_nothing(self):
        container = _container(self.team, "MSC", "500001")

        response = self.client.post(
            reverse("procurement:line_link_acquired_container", kwargs={"line_id": self.equipment_line.pk}),
            {"container": container.pk},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ContainerAcquisition.objects.exists())

    def test_an_htmx_acquisition_post_to_a_bc_line_is_forbidden(self):
        """htmx does not swap on a 4xx, so the modal stays put with the reason in it."""
        container = _container(self.team, "MSC", "500002")

        response = self.client.post(
            reverse("procurement:line_link_acquired_container", kwargs={"line_id": self.equipment_line.pk}),
            {"container": container.pk},
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(ContainerAcquisition.objects.exists())

    def test_unlinking_a_bc_acquisition_through_the_view_is_refused(self):
        acquisition = self._acquisition(_container(self.team, "MSC", "500003"))

        response = self.client.post(
            reverse("procurement:acquisition_unlink", kwargs={"acquisition_id": acquisition.pk})
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(ContainerAcquisition.objects.filter(pk=acquisition.pk).exists())

    def test_unlinking_a_bc_acquisition_over_htmx_is_forbidden(self):
        acquisition = self._acquisition(_container(self.team, "MSC", "500004"))

        response = self.client.delete(
            reverse("procurement:acquisition_unlink", kwargs={"acquisition_id": acquisition.pk}),
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(ContainerAcquisition.objects.filter(pk=acquisition.pk).exists())

    # -- load ---------------------------------------------------------------

    def test_posting_a_load_to_a_bc_line_writes_nothing(self):
        container = _container(self.team, "TCL", "500005")

        response = self.client.post(
            reverse("procurement:line_add_container_load", kwargs={"line_id": self.goods_line.pk}),
            {"container": container.pk, "quantity": "300"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ContainerLoad.objects.exists())

    def test_posting_a_load_to_a_bc_line_cannot_update_an_existing_one(self):
        """The upsert path is a write too — the recorded quantity must not move."""
        load = self._load(_container(self.team, "TCL", "500006"))

        response = self.client.post(
            reverse("procurement:line_add_container_load", kwargs={"line_id": self.goods_line.pk}),
            {"container": load.container.pk, "quantity": "999"},
        )

        self.assertEqual(response.status_code, 302)
        load.refresh_from_db()
        self.assertEqual(load.quantity, Decimal("300"))

    def test_removing_a_bc_load_through_the_view_is_refused(self):
        load = self._load(_container(self.team, "TCL", "500007"))

        response = self.client.post(reverse("procurement:container_load_remove", kwargs={"load_id": load.pk}))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(ContainerLoad.objects.filter(pk=load.pk).exists())

    def test_removing_a_bc_load_over_htmx_is_forbidden(self):
        load = self._load(_container(self.team, "TCL", "500008"))

        response = self.client.delete(
            reverse("procurement:container_load_remove", kwargs={"load_id": load.pk}),
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(ContainerLoad.objects.filter(pk=load.pk).exists())

    # -- the same routes on an SCM-owned order still work -------------------

    def test_an_scm_owned_order_is_unaffected_by_the_wall(self):
        manual_order = _order(self.team, "MAN-EP", source=PurchaseOrderSource.MANUAL)
        manual_line = _line(manual_order, "10000", "CONT45G1")
        container = _container(self.team, "HLX", "500009")

        response = self.client.post(
            reverse("procurement:line_link_acquired_container", kwargs={"line_id": manual_line.pk}),
            {"container": container.pk},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ContainerAcquisition.objects.get().container, container)
