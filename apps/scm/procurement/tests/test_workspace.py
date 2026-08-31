"""The Purchase Order Workspace: what it derives, and what it refuses to invent.

Two things are asserted repeatedly here because they are the failure modes this
work exists to prevent:

* The Business Central *document* status must never be presented as the operational
  status. They are different facts about different things, and the old page
  conflated them.
* The container tab must not cost a query per container. It is the one place on the
  page whose cost scales with the data, so it has a query-count regression test.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.procurement.activity import get_purchase_order_activity
from apps.scm.procurement.models import (
    PurchaseOrder,
    PurchaseOrderEventType,
    PurchaseOrderLine,
    PurchaseOrderLogisticsStatus,
    PurchaseOrderSource,
    PurchaseOrderStatus,
)
from apps.scm.procurement.selectors import get_purchase_order_workspace
from apps.scm.procurement.services import create_purchase_order_event
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.supplier_deliveries.models import (
    SupplierDelivery,
    SupplierDeliveryLine,
    SupplierDeliveryStatus,
)
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team: Team, owner: str = "MCU", serial: str = "200930") -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit(owner, "U", serial),
        equipment_type=_equipment_type(),
    )


def _purchase_order(team: Team, **kwargs) -> PurchaseOrder:
    defaults = {
        "external_id": kwargs.pop("external_id", "bc-po-1"),
        "po_number": "117064",
        "supplier_no": "CPI",
        "supplier_name": "CPI",
        "status": PurchaseOrderStatus.OPEN,
        "source_system": PurchaseOrderSource.BUSINESS_CENTRAL,
        "order_date": date(2026, 8, 3),
    }
    defaults.update(kwargs)
    return PurchaseOrder.objects.create(team=team, **defaults)


def _line(order: PurchaseOrder, line_no="10000", item_no="CONT22G1", **quantities) -> PurchaseOrderLine:
    return PurchaseOrderLine.objects.create(
        team=order.team,
        purchase_order=order,
        external_id=f"{order.external_id}-{line_no}",
        line_no=line_no,
        item_no=item_no,
        ordered_qty=quantities.get("ordered", Decimal("0")),
        shipped_qty=quantities.get("shipped", Decimal("0")),
        received_qty=quantities.get("received", Decimal("0")),
    )


def _delivery(
    order: PurchaseOrder, reference: str, status=SupplierDeliveryStatus.PLANNED, **kwargs
) -> SupplierDelivery:
    return SupplierDelivery.objects.create(
        team=order.team,
        purchase_order=order,
        delivery_reference=reference,
        status=status,
        **kwargs,
    )


def _delivery_line(delivery, po_line, qty, container=None, article="") -> SupplierDeliveryLine:
    return SupplierDeliveryLine.objects.create(
        team=delivery.team,
        delivery=delivery,
        purchase_order_line=po_line,
        delivery_qty=Decimal(str(qty)),
        container=container,
        article=article,
    )


class PurchaseOrderStatusSeparationTest(TestCase):
    """The document status and the logistics status are never the same field."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Sep", slug="po-ws-sep")

    def test_logistics_status_is_derived_not_the_document_status(self):
        # Business Central says the paperwork is closed. Nothing has shipped.
        order = _purchase_order(self.team, status=PurchaseOrderStatus.CLOSED)
        _line(order, ordered=Decimal("42"))

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)

        self.assertEqual(workspace.logistics_status, PurchaseOrderLogisticsStatus.NOT_STARTED)
        self.assertEqual(order.status, PurchaseOrderStatus.CLOSED)

    def test_partially_shipped_order_reports_partially_shipped(self):
        order = _purchase_order(self.team, status=PurchaseOrderStatus.OPEN)
        _line(order, ordered=Decimal("42"), shipped=Decimal("24"))

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)

        self.assertEqual(workspace.logistics_status, PurchaseOrderLogisticsStatus.PARTIALLY_SHIPPED)
        self.assertEqual(str(workspace.logistics_status_label), "Partially shipped")

    def test_completed_order_reports_completed(self):
        order = _purchase_order(self.team)
        _line(order, ordered=Decimal("10"), shipped=Decimal("10"), received=Decimal("10"))

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)

        self.assertEqual(workspace.logistics_status, PurchaseOrderLogisticsStatus.COMPLETED)


class PurchaseOrderProgressTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Prog", slug="po-ws-prog")
        cls.order = _purchase_order(cls.team)
        cls.line = _line(cls.order, ordered=Decimal("42"), shipped=Decimal("24"), received=Decimal("8"))
        delivery = _delivery(cls.order, "IF117064-A", status=SupplierDeliveryStatus.ARRIVED)
        _delivery_line(delivery, cls.line, 12, container=_container(cls.team))
        _delivery_line(delivery, cls.line, 24)

    def setUp(self):
        self.workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)

    def test_fulfillment_figures_come_from_the_engine(self):
        self.assertEqual(self.workspace.ordered_qty, Decimal("42"))
        self.assertEqual(self.workspace.shipped_qty, Decimal("24"))
        self.assertEqual(self.workspace.received_qty, Decimal("8"))
        self.assertEqual(self.workspace.arrived_qty, Decimal("36"))
        self.assertEqual(self.workspace.remaining_qty, Decimal("34"))

    def test_assigned_is_what_is_booked_onto_deliveries(self):
        self.assertEqual(self.workspace.assigned_qty, Decimal("36"))

    def test_unshipped_is_ordered_minus_shipped(self):
        self.assertEqual(self.workspace.unshipped_qty, Decimal("18"))

    def test_unbooked_is_ordered_minus_assigned(self):
        self.assertEqual(self.workspace.unbooked_qty, Decimal("6"))

    def test_quantity_on_delivery_lines_without_a_container_is_reported(self):
        self.assertEqual(self.workspace.unassigned_container_qty, Decimal("24"))

    def test_percentages_never_exceed_one_hundred(self):
        order = _purchase_order(self.team, external_id="over", po_number="OVER")
        _line(order, ordered=Decimal("5"), shipped=Decimal("9"))
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)
        self.assertEqual(workspace.shipped_percent, 100.0)

    def test_an_order_with_no_lines_has_no_progress_to_show(self):
        order = _purchase_order(self.team, external_id="empty", po_number="EMPTY")
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)
        self.assertFalse(workspace.has_progress)
        self.assertEqual(workspace.shipped_percent, 0.0)


class PurchaseOrderGapsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Gaps", slug="po-ws-gaps")

    def test_an_order_that_has_not_started_reports_no_gaps(self):
        order = _purchase_order(self.team, external_id="fresh", po_number="FRESH")
        _line(order, ordered=Decimal("42"))

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)

        self.assertFalse(workspace.has_gaps)

    def test_a_started_order_reports_what_is_missing(self):
        order = _purchase_order(self.team, external_id="started", po_number="STARTED")
        line = _line(order, ordered=Decimal("42"), shipped=Decimal("24"))
        delivery = _delivery(order, "IF-A")
        _delivery_line(delivery, line, 12, container=_container(self.team, serial="200930"))
        _delivery_line(delivery, line, 6)

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)
        codes = {gap.code for gap in workspace.gaps}

        self.assertIn("unbooked", codes)  # 42 ordered, 18 booked
        self.assertIn("no_container", codes)  # 6 booked with no container number
        self.assertIn("no_shipment", codes)  # the one container is on no shipment
        self.assertIn("unshipped", codes)  # 42 ordered, 24 shipped

    def test_a_fully_handled_order_reports_nothing_missing(self):
        order = _purchase_order(self.team, external_id="clean", po_number="CLEAN")
        line = _line(order, ordered=Decimal("10"), shipped=Decimal("10"))
        delivery = _delivery(order, "IF-CLEAN")
        container = _container(self.team, owner="TEM", serial="123456")
        _delivery_line(delivery, line, 10, container=container)
        shipment = Shipment.objects.create(team=self.team, shipment_number="SH-1")
        ShipmentContainer.objects.create(shipment=shipment, container=container)

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)

        self.assertFalse(workspace.has_gaps)

    def test_gap_quantities_are_rendered_without_trailing_zeros(self):
        order = _purchase_order(self.team, external_id="fmt", po_number="FMT")
        line = _line(order, ordered=Decimal("10"), shipped=Decimal("4"))
        delivery = _delivery(order, "IF-FMT")
        _delivery_line(delivery, line, 4, container=_container(self.team, owner="ABC", serial="111111"))

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)
        messages = " ".join(str(gap.message) for gap in workspace.gaps)

        self.assertIn("6 units", messages)
        self.assertNotIn("6.000", messages)


class PurchaseOrderContainerRowsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Rows", slug="po-ws-rows")
        cls.order = _purchase_order(cls.team)
        cls.line = _line(cls.order, ordered=Decimal("20"), item_no="CONT22G1")
        cls.delivery = _delivery(cls.order, "IF-ROWS")
        cls.container = _container(cls.team, owner="MCU", serial="200930")
        _delivery_line(cls.delivery, cls.line, 10, container=cls.container, article="CONT22G1")

    def test_a_container_row_keeps_the_article_it_was_booked_for(self):
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        row = workspace.container_rows[0]
        self.assertEqual(row.articles, ["CONT22G1"])

    def test_the_article_falls_back_to_the_order_line_item(self):
        order = _purchase_order(self.team, external_id="fallback", po_number="FB")
        line = _line(order, item_no="CONT45G1", ordered=Decimal("5"))
        delivery = _delivery(order, "IF-FB")
        _delivery_line(delivery, line, 5, container=_container(self.team, owner="TEM", serial="765432"), article="")

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)

        self.assertEqual(workspace.container_rows[0].articles, ["CONT45G1"])

    def test_one_container_on_two_delivery_lines_is_one_row_naming_both_articles(self):
        second = _line(self.order, line_no="20000", item_no="CONT40G1", ordered=Decimal("5"))
        _delivery_line(self.delivery, second, 5, container=self.container, article="CONT40G1")

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)

        self.assertEqual(len(workspace.container_rows), 1)
        self.assertEqual(sorted(workspace.container_rows[0].articles), ["CONT22G1", "CONT40G1"])

    def test_delivery_lines_without_a_container_produce_no_row(self):
        _delivery_line(self.delivery, self.line, 10)
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        self.assertEqual(len(workspace.container_rows), 1)

    def test_an_untracked_container_reports_no_state_rather_than_a_guess(self):
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        row = workspace.container_rows[0]
        self.assertEqual(row.current_status, "")
        self.assertEqual(row.journey_state, "unknown")
        self.assertIsNone(row.eta)

    def test_a_container_with_no_shipment_is_bucketed_as_needing_one(self):
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        row = workspace.container_rows[0]
        self.assertTrue(row.needs_shipment)
        self.assertIn("needs-shipment", row.filter_buckets)

    def test_a_container_on_a_shipment_carries_its_eta(self):
        eta = timezone.localdate() + timedelta(days=4)
        shipment = Shipment.objects.create(
            team=self.team, shipment_number="SH-260081", destination_port="Gothenburg", eta=eta
        )
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        row = workspace.container_rows[0]

        self.assertEqual(row.shipment, shipment)
        self.assertEqual(row.eta, eta)
        self.assertFalse(row.needs_shipment)

    def test_next_arrival_is_the_soonest_eta_and_how_many_share_it(self):
        soon = timezone.localdate() + timedelta(days=4)
        later = timezone.localdate() + timedelta(days=9)
        near = Shipment.objects.create(team=self.team, shipment_number="SH-A", destination_port="Gothenburg", eta=soon)
        far = Shipment.objects.create(team=self.team, shipment_number="SH-B", eta=later)
        second = _container(self.team, owner="TEM", serial="222222")
        third = _container(self.team, owner="TEM", serial="333333")
        _delivery_line(self.delivery, self.line, 1, container=second)
        _delivery_line(self.delivery, self.line, 1, container=third)
        ShipmentContainer.objects.create(shipment=near, container=self.container)
        ShipmentContainer.objects.create(shipment=near, container=second)
        ShipmentContainer.objects.create(shipment=far, container=third)

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)

        self.assertEqual(workspace.next_arrival, (soon, 2))
        self.assertEqual(workspace.next_arrival_destinations, ["Gothenburg"])

    def test_next_arrival_is_none_when_no_container_has_an_eta(self):
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        self.assertIsNone(workspace.next_arrival)


class PurchaseOrderDeliveryRowsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Del", slug="po-ws-del")
        cls.order = _purchase_order(cls.team)
        cls.line = _line(cls.order, ordered=Decimal("20"))

    def test_delivery_rows_carry_their_quantity_and_container_count(self):
        delivery = _delivery(self.order, "IF117064-A", status=SupplierDeliveryStatus.SHIPPED)
        _delivery_line(delivery, self.line, 6, container=_container(self.team, owner="MCU", serial="200930"))
        _delivery_line(delivery, self.line, 6, container=_container(self.team, owner="TEM", serial="123456"))

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        row = workspace.delivery_rows[0]

        self.assertEqual(row.quantity, Decimal("12"))
        self.assertEqual(row.container_count, 2)
        self.assertEqual(row.line_count, 2)

    def test_a_delivery_with_no_lines_still_appears(self):
        _delivery(self.order, "IF-EMPTY")
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        self.assertEqual(workspace.delivery_count, 1)
        self.assertEqual(workspace.delivery_rows[0].quantity, Decimal("0"))

    def test_actual_dates_win_over_planned_ones(self):
        delivery = _delivery(
            self.order,
            "IF-DATES",
            planned_ship_date=date(2026, 8, 10),
            actual_ship_date=date(2026, 8, 14),
            planned_arrival_date=date(2026, 9, 4),
        )
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        row = workspace.delivery_rows[0]

        self.assertEqual(row.ship_date, date(2026, 8, 14))
        self.assertTrue(row.ship_date_is_actual)
        self.assertEqual(row.arrival_date, date(2026, 9, 4))
        self.assertFalse(row.arrival_date_is_actual)
        self.assertEqual(delivery.delivery_reference, "IF-DATES")


class PurchaseOrderActivityTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Act", slug="po-ws-act")
        cls.order = _purchase_order(cls.team)

    def test_real_purchase_order_events_appear(self):
        create_purchase_order_event(self.order, PurchaseOrderEventType.LOADED, description="Loaded at Ningbo")

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        titles = [str(entry.title) for entry in get_purchase_order_activity(workspace)]

        self.assertIn("Loaded", titles)

    def test_deliveries_appear_and_link_to_themselves(self):
        delivery = _delivery(self.order, "IF-ACT")

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        entries = [entry for entry in get_purchase_order_activity(workspace) if entry.kind == "delivery"]

        self.assertEqual(len(entries), 1)
        self.assertIn(str(delivery.pk), entries[0].url)

    def test_the_record_being_created_is_always_the_oldest_entry(self):
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)
        entries = get_purchase_order_activity(workspace)

        self.assertEqual(entries[-1].kind, "created")

    def test_a_manual_order_gets_no_source_sync_lines(self):
        order = _purchase_order(
            self.team,
            external_id="manual-1",
            po_number="MAN",
            source_system=PurchaseOrderSource.MANUAL,
            last_synced_at=timezone.now(),
        )
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)
        kinds = {entry.kind for entry in get_purchase_order_activity(workspace)}

        self.assertNotIn("refresh", kinds)
        self.assertNotIn("source", kinds)

    def test_a_synced_order_reports_when_it_was_last_read(self):
        order = _purchase_order(
            self.team,
            external_id="synced-1",
            po_number="SYNC",
            last_synced_at=timezone.now(),
        )
        workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)
        kinds = {entry.kind for entry in get_purchase_order_activity(workspace)}

        self.assertIn("refresh", kinds)


class PurchaseOrderTeamIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Mine", slug="po-ws-mine")
        cls.other = Team.objects.create(name="Theirs", slug="po-ws-theirs")
        cls.order = _purchase_order(cls.team)
        cls.line = _line(cls.order, ordered=Decimal("10"))

    def test_another_teams_delivery_lines_never_reach_the_workspace(self):
        # A delivery line owned by the other team, pointing at this team's PO line.
        # Nothing should create this, but the workspace must not read it if it exists.
        foreign_container = _container(self.other, owner="XXX", serial="999999")
        foreign_delivery = SupplierDelivery.objects.create(
            team=self.other, purchase_order=self.order, delivery_reference="FOREIGN"
        )
        SupplierDeliveryLine.objects.create(
            team=self.other,
            delivery=foreign_delivery,
            purchase_order_line=self.line,
            delivery_qty=Decimal("10"),
            container=foreign_container,
        )

        workspace = get_purchase_order_workspace(team=self.team, purchase_order=self.order)

        self.assertEqual(workspace.container_rows, [])
        self.assertEqual(workspace.delivery_rows, [])
        self.assertEqual(workspace.assigned_qty, Decimal("0"))


@override_settings(STORAGES=_TEST_STORAGES)
class PurchaseOrderWorkspacePageTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Page", slug="po-ws-page")
        cls.user = CustomUser.objects.create_user(username="po-ws@example.com", password="pw")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

        cls.order = _purchase_order(cls.team, status=PurchaseOrderStatus.CLOSED)
        cls.line = _line(cls.order, ordered=Decimal("42"), shipped=Decimal("24"))
        cls.delivery = _delivery(cls.order, "IF117064-A", status=SupplierDeliveryStatus.SHIPPED)
        cls.container = _container(cls.team, owner="MCU", serial="200930")
        _delivery_line(cls.delivery, cls.line, 12, container=cls.container, article="CONT22G1")

        cls.manual_order = _purchase_order(
            cls.team, external_id="manual-page", po_number="MAN-1", source_system=PurchaseOrderSource.MANUAL
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("procurement:purchase_order_detail", args=[self.order.pk])

    def test_the_existing_detail_url_still_resolves(self):
        self.assertEqual(self.url, f"/scm/procurement/purchase-orders/{self.order.pk}/")
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_page_renders_all_four_tabs(self):
        response = self.client.get(self.url)
        content = response.content.decode()
        for label in ("Overview", "Containers", "Deliveries", "Activity"):
            self.assertIn(f">{label}</button>", content)

    def test_overview_is_the_default_tab(self):
        content = self.client.get(self.url).content.decode()
        self.assertIn("|| 'overview'", content)

    def test_the_headline_status_is_the_logistics_status(self):
        self.assertContains(self.client.get(self.url), "Partially shipped")

    def test_the_document_status_is_labelled_as_such_and_kept_separate(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Document status")
        # The BC document status still appears, but only as source metadata.
        self.assertContains(response, "Closed")

    def test_the_progress_figures_are_on_the_page(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Order progress")
        self.assertContains(response, "Assigned")
        self.assertContains(response, "Remaining")

    def test_containers_link_to_the_container_workspace(self):
        response = self.client.get(self.url)
        self.assertContains(response, reverse("containers:detail", args=[self.container.pk]))
        self.assertContains(response, self.container.container_id)

    def test_the_article_is_shown_beside_the_container(self):
        self.assertContains(self.client.get(self.url), "CONT22G1")

    def test_deliveries_are_shown_and_link_to_themselves(self):
        response = self.client.get(self.url)
        self.assertContains(response, "IF117064-A")
        self.assertContains(response, reverse("supplier_deliveries:detail", args=[self.delivery.pk]))

    def test_add_containers_still_opens_the_intake_modal_for_this_order(self):
        self.assertContains(
            self.client.get(self.url),
            f"{reverse('containers:create')}?purchase_order={self.order.pk}",
        )

    def test_a_business_central_order_is_marked_as_read_only(self):
        self.assertContains(self.client.get(self.url), "Managed by Business Central")

    def test_a_manual_order_is_not_marked_as_business_central(self):
        response = self.client.get(reverse("procurement:purchase_order_detail", args=[self.manual_order.pk]))
        self.assertNotContains(response, "Managed by Business Central")

    def test_no_edit_action_is_offered_for_a_business_central_order(self):
        # There is no PO edit view at all, so the workspace must not imply one.
        self.assertNotContains(self.client.get(self.url), "Edit purchase order")

    def test_an_order_with_nothing_linked_renders_cleanly(self):
        bare = _purchase_order(self.team, external_id="bare", po_number="BARE")
        response = self.client.get(reverse("procurement:purchase_order_detail", args=[bare.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No containers booked yet")
        self.assertContains(response, "No deliveries against this order yet")

    def test_another_teams_order_is_not_reachable(self):
        other_team = Team.objects.create(name="Other", slug="po-ws-page-other")
        other_order = _purchase_order(other_team, external_id="other-1", po_number="OTHER")
        response = self.client.get(reverse("procurement:purchase_order_detail", args=[other_order.pk]))
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class PurchaseOrderWorkspaceQueryCountTest(TestCase):
    """The containers tab must not cost a query per container.

    The assertion is that ten containers cost the same as one, not that some exact
    number of queries is correct — an exact budget would break on any unrelated
    change and teach people to bump the number.
    """

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="N1", slug="po-ws-n1")
        cls.user = CustomUser.objects.create_user(username="po-ws-n1@example.com", password="pw")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    # Container numbers are unique per team, so each order's boxes get their own
    # serial block rather than colliding with the previous order's.
    _SERIAL_BLOCKS = {"one": 100_000, "ten": 200_000, "pgsmall": 300_000, "pglarge": 400_000}

    def _order_with(self, container_count: int, *, external_id: str) -> PurchaseOrder:
        order = _purchase_order(self.team, external_id=external_id, po_number=external_id.upper())
        line = _line(order, ordered=Decimal(container_count))
        delivery = _delivery(order, f"IF-{external_id}")
        shipment = Shipment.objects.create(
            team=self.team, shipment_number=f"SH-{external_id}", eta=timezone.localdate()
        )
        base = self._SERIAL_BLOCKS[external_id]
        for index in range(container_count):
            container = _container(self.team, owner="MCU", serial=f"{base + index:06d}")
            _delivery_line(delivery, line, 1, container=container)
            ShipmentContainer.objects.create(shipment=shipment, container=container)
        return order

    def _queries_for(self, order: PurchaseOrder) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            workspace = get_purchase_order_workspace(team=self.team, purchase_order=order)
            # Touch every derivation the containers tab renders.
            for row in workspace.container_rows:
                _ = (row.article_label, row.shipment, row.eta, row.current_status, row.filter_buckets)
            for row in workspace.delivery_rows:
                _ = (row.quantity, row.container_count)
        return len(captured)

    def test_ten_containers_cost_the_same_as_one(self):
        one = self._order_with(1, external_id="one")
        ten = self._order_with(10, external_id="ten")

        self.assertEqual(self._queries_for(ten), self._queries_for(one))

    def test_the_page_itself_does_not_scale_with_container_count(self):
        self.client.force_login(self.user)
        small = self._order_with(1, external_id="pgsmall")
        large = self._order_with(12, external_id="pglarge")

        with self.assertNumQueries(self._page_queries(small)):
            self.client.get(reverse("procurement:purchase_order_detail", args=[large.pk]))

    def _page_queries(self, order: PurchaseOrder) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            self.client.get(reverse("procurement:purchase_order_detail", args=[order.pk]))
        return len(captured)
