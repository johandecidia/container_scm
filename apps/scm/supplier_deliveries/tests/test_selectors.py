"""Tests for supplier delivery selectors."""

from decimal import Decimal

from django.test import TestCase

from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine, PurchaseOrderStatus
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryLine, SupplierDeliveryStatus
from apps.scm.supplier_deliveries.selectors import (
    get_po_delivery_summary,
    get_supplier_deliveries_for_team,
    get_supplier_delivery_dashboard,
    get_supplier_delivery_detail,
)
from apps.teams.models import Team


def _team(slug) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _po(team, po_number="PO-SEL-001", external_id="bc-sel-po-001") -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=external_id,
        po_number=po_number,
        supplier_no="SUP-001",
        supplier_name="Selector Supplier",
        status=PurchaseOrderStatus.OPEN,
    )


def _po_line(team, po, line_no="10000", ordered_qty=1000) -> PurchaseOrderLine:
    return PurchaseOrderLine.objects.create(
        team=team,
        purchase_order=po,
        external_id=f"bc-sel-line-{line_no}",
        line_no=line_no,
        item_no="ITEM-001",
        description="Test Item",
        ordered_qty=ordered_qty,
    )


def _delivery(team, po, reference="DEL-SEL-001", status=SupplierDeliveryStatus.PLANNED) -> SupplierDelivery:
    return SupplierDelivery.objects.create(
        team=team,
        purchase_order=po,
        delivery_reference=reference,
        status=status,
    )


def _delivery_line(team, delivery, po_line, qty=300) -> SupplierDeliveryLine:
    return SupplierDeliveryLine.objects.create(
        team=team,
        delivery=delivery,
        purchase_order_line=po_line,
        delivery_qty=qty,
    )


class GetSupplierDeliveriesForTeamTest(TestCase):
    def test_returns_only_team_deliveries(self):
        team1 = _team("sd-sel-team1")
        team2 = _team("sd-sel-team2")
        po1 = _po(team1, po_number="PO-T1", external_id="bc-t1-po")
        po2 = _po(team2, po_number="PO-T2", external_id="bc-t2-po")
        _delivery(team1, po1, reference="DEL-T1")
        _delivery(team2, po2, reference="DEL-T2")

        result = list(get_supplier_deliveries_for_team(team1))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].delivery_reference, "DEL-T1")


class GetSupplierDeliveryDetailTest(TestCase):
    def test_returns_correct_delivery(self):
        team = _team("sd-sel-detail")
        po = _po(team, po_number="PO-DET", external_id="bc-det-po")
        delivery = _delivery(team, po, reference="DEL-DET")
        result = get_supplier_delivery_detail(team, delivery.pk)
        self.assertEqual(result, delivery)

    def test_raises_for_wrong_team(self):
        team1 = _team("sd-sel-detail-t1")
        team2 = _team("sd-sel-detail-t2")
        po = _po(team1, po_number="PO-DET2", external_id="bc-det2-po")
        delivery = _delivery(team1, po, reference="DEL-DET2")
        with self.assertRaises(SupplierDelivery.DoesNotExist):
            get_supplier_delivery_detail(team2, delivery.pk)


class GetPODeliverySummaryTest(TestCase):
    def test_summary_calculations(self):
        team = _team("sd-sel-summary")
        po = _po(team, po_number="PO-SUM", external_id="bc-sum-po")
        line = _po_line(team, po, ordered_qty=1000)

        # Delivery A: RECEIVED — 300
        del_a = _delivery(team, po, reference="DEL-A", status=SupplierDeliveryStatus.RECEIVED)
        _delivery_line(team, del_a, line, qty=300)

        # Delivery B: SHIPPED — 250
        del_b = _delivery(team, po, reference="DEL-B", status=SupplierDeliveryStatus.SHIPPED)
        _delivery_line(team, del_b, line, qty=250)

        # Delivery C: PLANNED — 450
        del_c = _delivery(team, po, reference="DEL-C", status=SupplierDeliveryStatus.PLANNED)
        _delivery_line(team, del_c, line, qty=450)

        summary = get_po_delivery_summary(team, po)

        self.assertEqual(summary["ordered_qty"], Decimal("1000"))
        self.assertEqual(summary["planned_qty"], Decimal("1000"))  # all non-cancelled
        self.assertEqual(summary["shipped_qty"], Decimal("550"))  # SHIPPED + RECEIVED
        self.assertEqual(summary["received_qty"], Decimal("300"))
        self.assertEqual(summary["remaining_qty"], Decimal("700"))  # 1000 - 300

    def test_summary_empty(self):
        team = _team("sd-sel-summary-empty")
        po = _po(team, po_number="PO-EMP", external_id="bc-emp-po")
        _po_line(team, po, ordered_qty=500)
        summary = get_po_delivery_summary(team, po)
        self.assertEqual(summary["ordered_qty"], Decimal("500"))
        self.assertEqual(summary["shipped_qty"], Decimal("0"))
        self.assertEqual(summary["remaining_qty"], Decimal("500"))


class GetSupplierDeliveryDashboardTest(TestCase):
    def test_dashboard_counts(self):
        team = _team("sd-sel-dashboard")
        po = _po(team, po_number="PO-DASH", external_id="bc-dash-po")

        _delivery(team, po, "DEL-OPEN1", SupplierDeliveryStatus.PLANNED)
        _delivery(team, po, "DEL-OPEN2", SupplierDeliveryStatus.BOOKED)
        _delivery(team, po, "DEL-PART1", SupplierDeliveryStatus.SHIPPED)
        _delivery(team, po, "DEL-PART2", SupplierDeliveryStatus.IN_TRANSIT)
        _delivery(team, po, "DEL-DONE", SupplierDeliveryStatus.RECEIVED)

        dashboard = get_supplier_delivery_dashboard(team)

        self.assertEqual(dashboard["open_count"], 2)
        self.assertEqual(dashboard["partial_count"], 2)
        self.assertEqual(dashboard["completed_count"], 1)
        self.assertEqual(dashboard["in_transit_count"], 1)
