"""Tests for procurement services: import, fulfillment, and event creation."""

from decimal import Decimal

from django.test import TestCase

from apps.scm.procurement.models import (
    PurchaseOrder,
    PurchaseOrderEvent,
    PurchaseOrderEventType,
    PurchaseOrderLine,
)
from apps.scm.procurement.services import (
    calculate_purchase_order_fulfillment,
    create_purchase_order_event,
    import_purchase_orders_from_bc,
)
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryLine, SupplierDeliveryStatus
from apps.teams.models import Team


def _team(slug="svc-team") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


_BC_DATA = [
    {
        "external_id": "bc-po-123",
        "po_number": "PO-123",
        "supplier_no": "SUP-001",
        "supplier_name": "Example Supplier",
        "status": "open",
        "order_date": "2026-06-01",
        "expected_receipt_date": "2026-07-01",
        "currency": "EUR",
        "lines": [
            {
                "external_id": "bc-line-1",
                "line_no": "10000",
                "item_no": "ITEM-001",
                "description": "Yoga mat",
                "ordered_qty": 100,
                "expected_receipt_date": "2026-07-01",
            }
        ],
    }
]


class ImportPurchaseOrdersTest(TestCase):
    def test_creates_purchase_order(self):
        team = _team()
        import_purchase_orders_from_bc(team, _BC_DATA)
        self.assertEqual(PurchaseOrder.objects.filter(team=team).count(), 1)
        po = PurchaseOrder.objects.get(team=team, external_id="bc-po-123")
        self.assertEqual(po.po_number, "PO-123")
        self.assertEqual(po.supplier_name, "Example Supplier")

    def test_creates_purchase_order_lines(self):
        team = _team()
        import_purchase_orders_from_bc(team, _BC_DATA)
        po = PurchaseOrder.objects.get(team=team, external_id="bc-po-123")
        self.assertEqual(po.lines.count(), 1)
        line = po.lines.first()
        self.assertEqual(line.item_no, "ITEM-001")
        self.assertEqual(line.ordered_qty, Decimal("100"))

    def test_import_is_idempotent_for_order(self):
        team = _team()
        import_purchase_orders_from_bc(team, _BC_DATA)
        import_purchase_orders_from_bc(team, _BC_DATA)
        self.assertEqual(PurchaseOrder.objects.filter(team=team).count(), 1)

    def test_import_is_idempotent_for_lines(self):
        team = _team()
        import_purchase_orders_from_bc(team, _BC_DATA)
        import_purchase_orders_from_bc(team, _BC_DATA)
        po = PurchaseOrder.objects.get(team=team, external_id="bc-po-123")
        self.assertEqual(po.lines.count(), 1)

    def test_import_updates_existing_order(self):
        team = _team()
        import_purchase_orders_from_bc(team, _BC_DATA)
        updated_data = [{**_BC_DATA[0], "supplier_name": "Updated Supplier", "lines": []}]
        import_purchase_orders_from_bc(team, updated_data)
        po = PurchaseOrder.objects.get(team=team, external_id="bc-po-123")
        self.assertEqual(po.supplier_name, "Updated Supplier")

    def test_import_updates_existing_line(self):
        team = _team()
        import_purchase_orders_from_bc(team, _BC_DATA)
        original_line = _BC_DATA[0]["lines"][0]
        updated_line = {**original_line, "ordered_qty": 200}
        updated_data = [{**_BC_DATA[0], "lines": [updated_line]}]
        import_purchase_orders_from_bc(team, updated_data)
        po = PurchaseOrder.objects.get(team=team, external_id="bc-po-123")
        line = po.lines.get(external_id="bc-line-1")
        self.assertEqual(line.ordered_qty, Decimal("200"))


class FulfillmentEngineTest(TestCase):
    def _make_po_with_lines(self, team, ordered, shipped, received):
        po = PurchaseOrder.objects.create(
            team=team,
            external_id="bc-po-fulfil",
            po_number="PO-F",
            supplier_no="SUP",
            supplier_name="Supplier",
            status="open",
        )
        PurchaseOrderLine.objects.create(
            team=team,
            purchase_order=po,
            external_id="bc-line-f",
            line_no="10000",
            item_no="ITEM-F",
            ordered_qty=ordered,
            shipped_qty=shipped,
            received_qty=received,
        )
        return po

    def test_fulfillment_example_from_spec(self):
        """Spec example: ordered=100, shipped=40, received=10 — no arrived deliveries."""
        team = _team(slug="fulfil-team")
        po = self._make_po_with_lines(team, ordered=100, shipped=40, received=10)
        result = calculate_purchase_order_fulfillment(po)
        self.assertEqual(result["ordered_qty"], Decimal("100"))
        self.assertEqual(result["shipped_qty"], Decimal("40"))
        self.assertEqual(result["in_transit_qty"], Decimal("30"))
        self.assertEqual(result["received_qty"], Decimal("10"))
        self.assertEqual(result["remaining_qty"], Decimal("90"))
        self.assertEqual(result["arrived_qty"], Decimal("0"))

    def test_fulfillment_arrived_qty_from_supplier_deliveries(self):
        """arrived_qty is calculated from supplier deliveries with ARRIVED status."""
        team = _team(slug="arrived-team")
        po = self._make_po_with_lines(team, ordered=100, shipped=60, received=10)
        po_line = po.lines.first()

        delivery = SupplierDelivery.objects.create(
            team=team,
            purchase_order=po,
            delivery_reference="DEL-001",
            status=SupplierDeliveryStatus.ARRIVED,
        )
        SupplierDeliveryLine.objects.create(
            team=team,
            delivery=delivery,
            purchase_order_line=po_line,
            delivery_qty=Decimal("25"),
        )

        result = calculate_purchase_order_fulfillment(po)
        self.assertEqual(result["arrived_qty"], Decimal("25"))
        # in_transit = shipped - received - arrived = 60 - 10 - 25 = 25
        self.assertEqual(result["in_transit_qty"], Decimal("25"))

    def test_fulfillment_empty_order(self):
        team = _team(slug="empty-team")
        po = PurchaseOrder.objects.create(
            team=team,
            external_id="bc-po-empty",
            po_number="PO-E",
            supplier_no="SUP",
            supplier_name="Supplier",
            status="open",
        )
        result = calculate_purchase_order_fulfillment(po)
        self.assertEqual(result["ordered_qty"], Decimal("0"))
        self.assertEqual(result["remaining_qty"], Decimal("0"))


class CreatePurchaseOrderEventTest(TestCase):
    def _make_po(self):
        team = _team(slug="event-team")
        return PurchaseOrder.objects.create(
            team=team,
            external_id="bc-po-evt",
            po_number="PO-EVT",
            supplier_no="SUP",
            supplier_name="Supplier",
            status="open",
        )

    def test_create_event(self):
        po = self._make_po()
        event = create_purchase_order_event(po, PurchaseOrderEventType.CREATED, description="Imported")
        self.assertEqual(event.purchase_order, po)
        self.assertEqual(event.event_type, PurchaseOrderEventType.CREATED)
        self.assertEqual(event.description, "Imported")

    def test_invalid_event_type_raises(self):
        po = self._make_po()
        with self.assertRaises(ValueError):
            create_purchase_order_event(po, "INVALID_TYPE")

    def test_metadata_stored(self):
        po = self._make_po()
        event = create_purchase_order_event(po, PurchaseOrderEventType.LOADED, metadata={"vessel": "MSC Maya"})
        self.assertEqual(PurchaseOrderEvent.objects.get(pk=event.pk).metadata["vessel"], "MSC Maya")
