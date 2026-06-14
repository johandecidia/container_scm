"""Unit tests for the PO fulfillment engine.

Tests calculate_purchase_order_fulfillment() with scenarios that correspond
to conceptual fulfillment states: open, partial, shipped, and completed.

Note: the fulfillment engine returns qty aggregates (not a status string).
The conceptual state is derived from the combination of those aggregates.
"""

from decimal import Decimal

from django.test import TestCase

from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine
from apps.scm.procurement.services import calculate_purchase_order_fulfillment
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryLine, SupplierDeliveryStatus
from apps.teams.models import Team


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _po(team: Team, external_id: str = "po-001") -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=external_id,
        po_number=external_id,
        supplier_no="SUP",
        supplier_name="Test Supplier",
        status="open",
    )


def _line(
    team: Team,
    po: PurchaseOrder,
    external_id: str = "line-001",
    ordered: str = "100",
    shipped: str = "0",
    received: str = "0",
) -> PurchaseOrderLine:
    return PurchaseOrderLine.objects.create(
        team=team,
        purchase_order=po,
        external_id=external_id,
        line_no="10000",
        item_no="ITEM-001",
        ordered_qty=Decimal(ordered),
        shipped_qty=Decimal(shipped),
        received_qty=Decimal(received),
    )


class PoFulfillmentStatusScenariosTest(TestCase):
    """Verify conceptual fulfillment states via qty aggregates."""

    def test_po_fulfillment_status_is_open_when_nothing_shipped(self):
        """A PO with ordered qty but zero shipped: in_transit=0, remaining=ordered (open state)."""
        team = _team("fe-open")
        po = _po(team, "po-fe-open")
        _line(team, po, ordered="100", shipped="0", received="0")

        result = calculate_purchase_order_fulfillment(po)

        self.assertEqual(result["ordered_qty"], Decimal("100"))
        self.assertEqual(result["shipped_qty"], Decimal("0"))
        self.assertEqual(result["in_transit_qty"], Decimal("0"))
        self.assertEqual(result["received_qty"], Decimal("0"))
        self.assertEqual(result["remaining_qty"], Decimal("100"))

    def test_po_fulfillment_status_is_partial_when_some_quantity_shipped(self):
        """Some but not all qty shipped: shipped > 0, remaining > 0 (partial state)."""
        team = _team("fe-partial")
        po = _po(team, "po-fe-partial")
        _line(team, po, ordered="100", shipped="40", received="0")

        result = calculate_purchase_order_fulfillment(po)

        self.assertEqual(result["shipped_qty"], Decimal("40"))
        self.assertEqual(result["in_transit_qty"], Decimal("40"))
        self.assertGreater(result["remaining_qty"], Decimal("0"))

    def test_po_fulfillment_status_is_shipped_when_all_quantity_shipped(self):
        """All ordered qty shipped but nothing received yet: in_transit = ordered (shipped state)."""
        team = _team("fe-shipped")
        po = _po(team, "po-fe-shipped")
        _line(team, po, ordered="100", shipped="100", received="0")

        result = calculate_purchase_order_fulfillment(po)

        self.assertEqual(result["shipped_qty"], Decimal("100"))
        self.assertEqual(result["ordered_qty"], Decimal("100"))
        self.assertEqual(result["in_transit_qty"], Decimal("100"))
        # remaining = ordered - received = 100 - 0
        self.assertEqual(result["remaining_qty"], Decimal("100"))

    def test_po_fulfillment_status_is_completed_when_all_quantity_received(self):
        """All ordered qty received: remaining = 0 (completed state)."""
        team = _team("fe-complete")
        po = _po(team, "po-fe-complete")
        _line(team, po, ordered="100", shipped="100", received="100")

        result = calculate_purchase_order_fulfillment(po)

        self.assertEqual(result["received_qty"], Decimal("100"))
        self.assertEqual(result["remaining_qty"], Decimal("0"))
        self.assertEqual(result["in_transit_qty"], Decimal("0"))

    def test_po_fulfillment_totals_are_calculated_from_lines(self):
        """Multi-line PO: fulfillment engine aggregates across all lines."""
        team = _team("fe-multiline")
        po = _po(team, "po-fe-multi")
        _line(team, po, external_id="l1", ordered="60", shipped="20", received="10")
        _line(team, po, external_id="l2", ordered="40", shipped="30", received="5")

        result = calculate_purchase_order_fulfillment(po)

        self.assertEqual(result["ordered_qty"], Decimal("100"))  # 60 + 40
        self.assertEqual(result["shipped_qty"], Decimal("50"))  # 20 + 30
        self.assertEqual(result["received_qty"], Decimal("15"))  # 10 + 5
        self.assertEqual(result["remaining_qty"], Decimal("85"))  # 100 - 15
        self.assertEqual(result["in_transit_qty"], Decimal("35"))  # 50 - 15 - 0


class PoFulfillmentQuantityTest(TestCase):
    """Verify specific qty fields are computed correctly."""

    def test_po_fulfillment_remaining_quantity_is_ordered_minus_received(self):
        """remaining_qty = max(ordered_qty - received_qty, 0)."""
        team = _team("fe-rem")
        po = _po(team, "po-fe-rem")
        _line(team, po, ordered="200", shipped="150", received="50")

        result = calculate_purchase_order_fulfillment(po)

        self.assertEqual(result["remaining_qty"], Decimal("150"))  # 200 - 50

    def test_po_fulfillment_shipped_quantity_correct(self):
        """shipped_qty sums shipped_qty across all lines."""
        team = _team("fe-sq")
        po = _po(team, "po-fe-sq")
        _line(team, po, external_id="l1", ordered="50", shipped="30")
        _line(team, po, external_id="l2", ordered="50", shipped="20")

        result = calculate_purchase_order_fulfillment(po)

        self.assertEqual(result["shipped_qty"], Decimal("50"))

    def test_po_fulfillment_received_quantity_correct(self):
        """received_qty sums received_qty across all lines."""
        team = _team("fe-rq")
        po = _po(team, "po-fe-rq")
        _line(team, po, external_id="l1", ordered="50", received="10")
        _line(team, po, external_id="l2", ordered="50", received="25")

        result = calculate_purchase_order_fulfillment(po)

        self.assertEqual(result["received_qty"], Decimal("35"))

    def test_po_fulfillment_in_transit_deducts_arrived_deliveries(self):
        """arrived_qty (from SupplierDelivery.ARRIVED lines) is deducted from in_transit."""
        team = _team("fe-arr")
        po = _po(team, "po-fe-arr")
        line = _line(team, po, ordered="100", shipped="80", received="10")

        delivery = SupplierDelivery.objects.create(
            team=team,
            purchase_order=po,
            delivery_reference="DEL-ARR",
            status=SupplierDeliveryStatus.ARRIVED,
        )
        SupplierDeliveryLine.objects.create(
            team=team,
            delivery=delivery,
            purchase_order_line=line,
            delivery_qty=Decimal("30"),
        )

        result = calculate_purchase_order_fulfillment(po)

        self.assertEqual(result["arrived_qty"], Decimal("30"))
        self.assertEqual(result["in_transit_qty"], Decimal("40"))  # 80 - 10 - 30

    def test_po_fulfillment_in_transit_never_negative(self):
        """in_transit is clamped to 0 when received + arrived > shipped."""
        team = _team("fe-neg")
        po = _po(team, "po-fe-neg")
        _line(team, po, ordered="100", shipped="10", received="80")

        result = calculate_purchase_order_fulfillment(po)

        self.assertEqual(result["in_transit_qty"], Decimal("0"))

    def test_po_fulfillment_empty_po_returns_zero_aggregates(self):
        """A PO with no lines returns all zeros."""
        team = _team("fe-empty")
        po = _po(team, "po-fe-empty")

        result = calculate_purchase_order_fulfillment(po)

        for key in ["ordered_qty", "shipped_qty", "in_transit_qty", "arrived_qty", "received_qty", "remaining_qty"]:
            self.assertEqual(result[key], Decimal("0"), f"{key} should be 0 for empty PO")
