"""End-to-end flow tests for the SCM system.

Covers two layers:

Layer 1 — BC XML import round-trip:
  1. Parse the anonymized BC XML fixture with ``parse_bc_po_xml``.
  2. Pass the parsed data to ``import_purchase_orders_from_bc``.
  3. Assert that ``PurchaseOrder`` and ``PurchaseOrderLine`` records are
     created in the database and scoped to the correct team.

Layer 2 — Full SCM lifecycle (PO → deliveries → shipments → containers → tracking):
  PO-TEST-E2E-001 with 1 000 units across 3 lines (300 + 250 + 450).
  Three supplier deliveries (DEL-E2E-A/B/C) cover each line via containers
  MCUU100001, MCUU100002, MCUU100003.
  Three shipments each own one container.
  Tracking events are attached and appear in the merged timeline.
  Fulfillment totals are verified via ``calculate_purchase_order_fulfillment``.
  The shipment detail context selector returns PO, delivery, container and
  tracking data for a given shipment.
"""

import datetime as _dt
import io
from decimal import Decimal
from pathlib import Path

from django.test import TestCase, override_settings

from apps.scm.imports.bc_xml_parser import parse_bc_po_xml
from apps.scm.procurement.models import PurchaseOrder
from apps.scm.procurement.selectors import get_team_purchase_orders
from apps.scm.procurement.services import calculate_purchase_order_fulfillment, import_purchase_orders_from_bc
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser

FIXTURE_PATH = (
    Path(__file__).parent.parent / "imports" / "tests" / "fixtures" / "business_central_purchase_order_sample.xml"
)


def _make_team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _make_user(email: str) -> CustomUser:
    return CustomUser.objects.get_or_create(username=email, defaults={"email": email})[0]


class BCXmlEndToEndImportTests(TestCase):
    """Full round-trip: fixture XML → parse → import → DB assertions."""

    def setUp(self):
        self.team = _make_team("e2e-team-alpha")
        self.user = _make_user("e2e@example.com")

    def _parse_fixture(self):
        return parse_bc_po_xml(io.BytesIO(FIXTURE_PATH.read_bytes()))

    def test_fixture_parses_without_error(self):
        pos = self._parse_fixture()
        self.assertEqual(len(pos), 1)

    def test_import_creates_purchase_order(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        self.assertEqual(PurchaseOrder.objects.filter(team=self.team).count(), 1)

    def test_import_creates_correct_po_number(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.po_number, "PO-2026-001")

    def test_import_creates_correct_supplier(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.supplier_no, "SUPP-0001")
        self.assertEqual(po.supplier_name, "Anon Supplier Ltd")

    def test_import_creates_correct_order_date(self):
        from datetime import date

        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.order_date, date(2026, 3, 15))

    def test_import_creates_correct_expected_receipt_date(self):
        from datetime import date

        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.expected_receipt_date, date(2026, 6, 1))

    def test_import_creates_correct_currency(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.currency, "USD")

    def test_import_creates_three_lines(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.lines.count(), 3)

    def test_lines_have_correct_item_nos(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        item_nos = list(po.lines.values_list("item_no", flat=True))
        self.assertIn("ITM-4210", item_nos)
        self.assertIn("ITM-7830", item_nos)
        self.assertIn("ITM-9010", item_nos)

    def test_lines_have_correct_quantities(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        qtys = {line.item_no: line.ordered_qty for line in po.lines.all()}
        self.assertEqual(qtys["ITM-4210"], Decimal("50"))
        self.assertEqual(qtys["ITM-7830"], Decimal("100"))
        self.assertEqual(qtys["ITM-9010"], Decimal("15.000"))

    def test_idempotent_import_does_not_duplicate(self):
        """Importing the same XML twice must not create duplicate records."""
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        import_purchase_orders_from_bc(self.team, pos)
        self.assertEqual(PurchaseOrder.objects.filter(team=self.team).count(), 1)
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.lines.count(), 3)

    def test_lines_scoped_to_team(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team, pos)
        po = PurchaseOrder.objects.get(team=self.team)
        for line in po.lines.all():
            self.assertEqual(line.team, self.team)


class BCXmlMultiTenancyTests(TestCase):
    """Confirm that PO records are isolated between teams."""

    def setUp(self):
        self.team_a = _make_team("e2e-team-beta-a")
        self.team_b = _make_team("e2e-team-beta-b")

    def _parse_fixture(self):
        return parse_bc_po_xml(io.BytesIO(FIXTURE_PATH.read_bytes()))

    def test_team_b_cannot_see_team_a_orders(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team_a, pos)

        team_b_orders = get_team_purchase_orders(self.team_b)
        self.assertEqual(team_b_orders.count(), 0)

    def test_team_a_selector_returns_its_own_orders(self):
        pos = self._parse_fixture()
        import_purchase_orders_from_bc(self.team_a, pos)

        team_a_orders = get_team_purchase_orders(self.team_a)
        self.assertEqual(team_a_orders.count(), 1)


# ---------------------------------------------------------------------------
# Layer 2 — Full SCM lifecycle
# ---------------------------------------------------------------------------

from apps.scm.containers.models import Container, EquipmentType  # noqa: E402
from apps.scm.containers.utils import calculate_check_digit  # noqa: E402
from apps.scm.shipments.models import ShipmentContainer  # noqa: E402
from apps.scm.shipments.selectors import (  # noqa: E402
    get_merged_shipment_timeline,
    get_shipment_detail_context,
    get_shipment_supplier_deliveries,
)
from apps.scm.shipments.services import add_container_to_shipment, create_shipment  # noqa: E402
from apps.scm.supplier_deliveries.models import (  # noqa: E402
    SupplierDelivery,
    SupplierDeliveryLine,
    SupplierDeliveryStatus,
)
from apps.scm.supplier_deliveries.selectors import get_supplier_deliveries_for_team  # noqa: E402
from apps.scm.tracking.models import TrackingEvent, TrackingProvider  # noqa: E402

_E2E_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Synthetic PO: 1 000 units across 3 lines (300 + 250 + 450).
_SYNTHETIC_PO_DATA = [
    {
        "external_id": "E2E-EXT-PO-001",
        "po_number": "PO-TEST-E2E-001",
        "supplier_no": "SUPP-E2E-01",
        "supplier_name": "E2E Test Supplier Ltd",
        "status": "open",
        "order_date": _dt.date(2026, 1, 15),
        "expected_receipt_date": _dt.date(2026, 7, 30),
        "currency": "USD",
        "lines": [
            {
                "external_id": "E2E-LINE-A",
                "line_no": "10000",
                "item_no": "ITM-E2E-A",
                "description": "Item Alpha",
                "ordered_qty": Decimal("300"),
                "shipped_qty": Decimal("0"),
                "received_qty": Decimal("0"),
                "expected_receipt_date": None,
            },
            {
                "external_id": "E2E-LINE-B",
                "line_no": "20000",
                "item_no": "ITM-E2E-B",
                "description": "Item Beta",
                "ordered_qty": Decimal("250"),
                "shipped_qty": Decimal("0"),
                "received_qty": Decimal("0"),
                "expected_receipt_date": None,
            },
            {
                "external_id": "E2E-LINE-C",
                "line_no": "30000",
                "item_no": "ITM-E2E-C",
                "description": "Item Gamma",
                "ordered_qty": Decimal("450"),
                "shipped_qty": Decimal("0"),
                "received_qty": Decimal("0"),
                "expected_receipt_date": None,
            },
        ],
    }
]


def _eq_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20GP"},
    )[0]


def _make_container(team: Team, serial: str) -> Container:
    check = calculate_check_digit("MCU", "U", serial)
    return Container.objects.create(
        team=team,
        owner_code="MCU",
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_eq_type(),
    )


@override_settings(STORAGES=_E2E_STORAGES)
class SCMEndToEndFlowTests(TestCase):
    """Full SCM lifecycle: BC import → Supplier Deliveries → Shipments → Containers → Tracking.

    Scenario
    --------
    PO-TEST-E2E-001 (1 000 units):
      Line A — 300 units  → Delivery A (SHIPPED)  → Container MCUU100001 → Shipment A
      Line B — 250 units  → Delivery B (IN_TRANSIT) → Container MCUU100002 → Shipment B
      Line C — 450 units  → Delivery C (ARRIVED)  → Container MCUU100003 → Shipment C

    Tracking events are attached to each shipment.
    """

    @classmethod
    def setUpTestData(cls):
        cls.team = _make_team("e2e-full-flow-team")
        cls.user = _make_user("e2e-full@example.com")
        cls.user.set_password("testpass")
        cls.user.save()
        Membership.objects.get_or_create(team=cls.team, user=cls.user, defaults={"role": "member"})

        # --- PO import ---
        orders = import_purchase_orders_from_bc(cls.team, _SYNTHETIC_PO_DATA)
        cls.po = orders[0]
        cls.line_a = cls.po.lines.get(external_id="E2E-LINE-A")
        cls.line_b = cls.po.lines.get(external_id="E2E-LINE-B")
        cls.line_c = cls.po.lines.get(external_id="E2E-LINE-C")

        # --- Containers (MCU U 100001/2/3) ---
        cls.container_a = _make_container(cls.team, "100001")
        cls.container_b = _make_container(cls.team, "100002")
        cls.container_c = _make_container(cls.team, "100003")

        # --- Supplier deliveries ---
        cls.delivery_a = SupplierDelivery.objects.create(
            team=cls.team,
            purchase_order=cls.po,
            supplier="E2E Test Supplier Ltd",
            delivery_reference="DEL-E2E-A",
            status=SupplierDeliveryStatus.SHIPPED,
        )
        cls.delivery_b = SupplierDelivery.objects.create(
            team=cls.team,
            purchase_order=cls.po,
            supplier="E2E Test Supplier Ltd",
            delivery_reference="DEL-E2E-B",
            status=SupplierDeliveryStatus.IN_TRANSIT,
        )
        cls.delivery_c = SupplierDelivery.objects.create(
            team=cls.team,
            purchase_order=cls.po,
            supplier="E2E Test Supplier Ltd",
            delivery_reference="DEL-E2E-C",
            status=SupplierDeliveryStatus.ARRIVED,
        )

        # --- Delivery lines (container links delivery → PO line) ---
        cls.del_line_a = SupplierDeliveryLine.objects.create(
            team=cls.team,
            delivery=cls.delivery_a,
            purchase_order_line=cls.line_a,
            delivery_qty=Decimal("300"),
            container=cls.container_a,
        )
        cls.del_line_b = SupplierDeliveryLine.objects.create(
            team=cls.team,
            delivery=cls.delivery_b,
            purchase_order_line=cls.line_b,
            delivery_qty=Decimal("250"),
            container=cls.container_b,
        )
        cls.del_line_c = SupplierDeliveryLine.objects.create(
            team=cls.team,
            delivery=cls.delivery_c,
            purchase_order_line=cls.line_c,
            delivery_qty=Decimal("450"),
            container=cls.container_c,
        )

        # Simulate BC syncing shipped_qty back to PO lines
        for line, qty in [(cls.line_a, "300"), (cls.line_b, "250"), (cls.line_c, "450")]:
            line.shipped_qty = Decimal(qty)
            line.save(update_fields=["shipped_qty", "updated_at"])

        # --- Shipments ---
        _shipment_defaults = {"carrier": "MSC", "origin_port": "CNSHA", "destination_port": "DEHAM"}
        cls.shipment_a = create_shipment(cls.team, cls.user, {"shipment_number": "SHP-E2E-A", **_shipment_defaults})
        cls.shipment_b = create_shipment(cls.team, cls.user, {"shipment_number": "SHP-E2E-B", **_shipment_defaults})
        cls.shipment_c = create_shipment(cls.team, cls.user, {"shipment_number": "SHP-E2E-C", **_shipment_defaults})

        # --- Link containers to shipments ---
        add_container_to_shipment(cls.team, cls.shipment_a, cls.container_a, cls.user)
        add_container_to_shipment(cls.team, cls.shipment_b, cls.container_b, cls.user)
        add_container_to_shipment(cls.team, cls.shipment_c, cls.container_c, cls.user)

        # --- Tracking provider and events ---
        cls.provider = TrackingProvider.objects.create(
            code="e2e-manual-provider",
            name="E2E Manual Provider",
            provider_type=TrackingProvider.ProviderType.MANUAL,
        )
        cls.event_a = TrackingEvent.objects.create(
            team=cls.team,
            shipment=cls.shipment_a,
            container=cls.container_a,
            provider=cls.provider,
            event_type=TrackingEvent.EventType.VESSEL_DEPARTED,
            description="Vessel departed from Shanghai",
            location_name="Shanghai",
            location_unlocode="CNSHA",
            event_datetime=_dt.datetime(2026, 6, 1, 10, 0, tzinfo=_dt.UTC),
        )
        cls.event_b = TrackingEvent.objects.create(
            team=cls.team,
            shipment=cls.shipment_b,
            container=cls.container_b,
            provider=cls.provider,
            event_type=TrackingEvent.EventType.LOADED_ON_VESSEL,
            description="Container loaded on vessel",
            location_name="Shanghai",
            location_unlocode="CNSHA",
            event_datetime=_dt.datetime(2026, 5, 28, 8, 0, tzinfo=_dt.UTC),
        )
        cls.event_c = TrackingEvent.objects.create(
            team=cls.team,
            shipment=cls.shipment_c,
            container=cls.container_c,
            provider=cls.provider,
            event_type=TrackingEvent.EventType.VESSEL_ARRIVED,
            description="Vessel arrived at Hamburg",
            location_name="Hamburg",
            location_unlocode="DEHAM",
            event_datetime=_dt.datetime(2026, 7, 15, 6, 0, tzinfo=_dt.UTC),
        )

    # ------------------------------------------------------------------
    # Test 1: PO import
    # ------------------------------------------------------------------

    def test_business_central_po_import_creates_po_and_lines(self):
        """Synthetic BC data creates a PurchaseOrder and all three lines."""
        self.assertEqual(self.po.po_number, "PO-TEST-E2E-001")
        self.assertEqual(self.po.supplier_no, "SUPP-E2E-01")
        self.assertEqual(self.po.lines.count(), 3)
        item_nos = set(self.po.lines.values_list("item_no", flat=True))
        self.assertEqual(item_nos, {"ITM-E2E-A", "ITM-E2E-B", "ITM-E2E-C"})
        for line in self.po.lines.all():
            self.assertEqual(line.team, self.team)

    # ------------------------------------------------------------------
    # Test 2: Partial supplier deliveries
    # ------------------------------------------------------------------

    def test_supplier_deliveries_can_partially_fulfill_po(self):
        """Three deliveries partially cover the PO; each has correct qty and status."""
        deliveries = SupplierDelivery.objects.filter(team=self.team, purchase_order=self.po)
        self.assertEqual(deliveries.count(), 3)

        self.assertEqual(self.del_line_a.delivery_qty, Decimal("300"))
        self.assertEqual(self.del_line_b.delivery_qty, Decimal("250"))
        self.assertEqual(self.del_line_c.delivery_qty, Decimal("450"))

        self.assertEqual(
            SupplierDelivery.objects.get(pk=self.delivery_a.pk).status,
            SupplierDeliveryStatus.SHIPPED,
        )
        self.assertEqual(
            SupplierDelivery.objects.get(pk=self.delivery_b.pk).status,
            SupplierDeliveryStatus.IN_TRANSIT,
        )
        self.assertEqual(
            SupplierDelivery.objects.get(pk=self.delivery_c.pk).status,
            SupplierDeliveryStatus.ARRIVED,
        )

    # ------------------------------------------------------------------
    # Test 3: Shipments linked to supplier deliveries via containers
    # ------------------------------------------------------------------

    def test_shipments_can_be_linked_to_supplier_deliveries(self):
        """The selector returns the correct delivery for each shipment via container."""
        deliveries_a = get_shipment_supplier_deliveries(self.team, self.shipment_a)
        self.assertEqual(deliveries_a.count(), 1)
        self.assertEqual(deliveries_a.first().delivery_reference, "DEL-E2E-A")

        deliveries_b = get_shipment_supplier_deliveries(self.team, self.shipment_b)
        self.assertEqual(deliveries_b.count(), 1)
        self.assertEqual(deliveries_b.first().delivery_reference, "DEL-E2E-B")

        deliveries_c = get_shipment_supplier_deliveries(self.team, self.shipment_c)
        self.assertEqual(deliveries_c.count(), 1)
        self.assertEqual(deliveries_c.first().delivery_reference, "DEL-E2E-C")

    # ------------------------------------------------------------------
    # Test 4: Containers linked to shipments
    # ------------------------------------------------------------------

    def test_containers_can_be_linked_to_shipments(self):
        """Each shipment has exactly one container and maps to the right physical unit."""
        sc_a = ShipmentContainer.objects.filter(shipment=self.shipment_a)
        self.assertEqual(sc_a.count(), 1)
        self.assertEqual(sc_a.first().container_id, self.container_a.pk)

        sc_b = ShipmentContainer.objects.filter(shipment=self.shipment_b)
        self.assertEqual(sc_b.count(), 1)
        self.assertEqual(sc_b.first().container_id, self.container_b.pk)

        sc_c = ShipmentContainer.objects.filter(shipment=self.shipment_c)
        self.assertEqual(sc_c.count(), 1)
        self.assertEqual(sc_c.first().container_id, self.container_c.pk)

    # ------------------------------------------------------------------
    # Test 5: Tracking events update shipment visibility
    # ------------------------------------------------------------------

    def test_tracking_events_update_shipment_visibility(self):
        """Tracking events are persisted and appear in the merged shipment timeline."""
        self.assertTrue(TrackingEvent.objects.filter(shipment=self.shipment_a).exists())
        self.assertTrue(TrackingEvent.objects.filter(shipment=self.shipment_b).exists())
        self.assertTrue(TrackingEvent.objects.filter(shipment=self.shipment_c).exists())

        timeline_a = get_merged_shipment_timeline(self.team, self.shipment_a)
        tracking_types_a = {item.event_type for item in timeline_a if item.source == "tracking"}
        self.assertIn(TrackingEvent.EventType.VESSEL_DEPARTED, tracking_types_a)

        timeline_c = get_merged_shipment_timeline(self.team, self.shipment_c)
        tracking_types_c = {item.event_type for item in timeline_c if item.source == "tracking"}
        self.assertIn(TrackingEvent.EventType.VESSEL_ARRIVED, tracking_types_c)

        # Each timeline also contains internal shipment events (e.g. CREATED, CONTAINER_ADDED)
        shipment_sources_a = {item.source for item in timeline_a}
        self.assertIn("shipment", shipment_sources_a)

    # ------------------------------------------------------------------
    # Test 6: Fulfillment totals
    # ------------------------------------------------------------------

    def test_po_fulfillment_totals_are_correct(self):
        """Fulfillment engine aggregates ordered / shipped / arrived / remaining correctly."""
        totals = calculate_purchase_order_fulfillment(self.po)

        self.assertEqual(totals["ordered_qty"], Decimal("1000"))
        self.assertEqual(totals["shipped_qty"], Decimal("1000"))
        # delivery_c has status ARRIVED → its delivery_qty counts as arrived
        self.assertEqual(totals["arrived_qty"], Decimal("450"))
        # received_qty = 0 in the base fixture → remaining = 1000
        self.assertEqual(totals["received_qty"], Decimal("0"))
        self.assertEqual(totals["remaining_qty"], Decimal("1000"))

        # Simulate BC confirming receipt on all lines
        for line, qty in [(self.line_a, "300"), (self.line_b, "250"), (self.line_c, "450")]:
            type(line).objects.filter(pk=line.pk).update(received_qty=Decimal(qty))

        totals_after = calculate_purchase_order_fulfillment(self.po)
        self.assertEqual(totals_after["received_qty"], Decimal("1000"))
        self.assertEqual(totals_after["remaining_qty"], Decimal("0"))

    # ------------------------------------------------------------------
    # Test 7: Shipment detail view shows full visibility context
    # ------------------------------------------------------------------

    def test_shipment_detail_view_shows_full_visibility_context(self):
        """get_shipment_detail_context returns PO, delivery, container and tracking data."""
        context = get_shipment_detail_context(self.team, self.shipment_a.pk)

        self.assertEqual(context["shipment"], self.shipment_a)

        container_pks = [sc.container_id for sc in context["containers"]]
        self.assertIn(self.container_a.pk, container_pks)

        po_numbers = [po.po_number for po in context["purchase_orders"]]
        self.assertIn("PO-TEST-E2E-001", po_numbers)

        delivery_refs = [d.delivery_reference for d in context["supplier_deliveries"]]
        self.assertIn("DEL-E2E-A", delivery_refs)

        self.assertIsNotNone(context["latest_tracking_event"])
        self.assertEqual(
            context["latest_tracking_event"].event_type,
            TrackingEvent.EventType.VESSEL_DEPARTED,
        )

        sources = {item.source for item in context["timeline_events"]}
        self.assertIn("shipment", sources)
        self.assertIn("tracking", sources)

    # ------------------------------------------------------------------
    # Test 8: Team isolation
    # ------------------------------------------------------------------

    def test_team_isolation_prevents_cross_team_data_access(self):
        """A different team cannot see this team's POs or deliveries."""
        other_team = _make_team("e2e-isolation-other-team")

        self.assertEqual(get_team_purchase_orders(other_team).count(), 0)
        self.assertEqual(get_supplier_deliveries_for_team(other_team).count(), 0)
