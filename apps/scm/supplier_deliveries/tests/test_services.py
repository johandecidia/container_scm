"""Tests for supplier delivery services."""

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine, PurchaseOrderStatus
from apps.scm.supplier_deliveries.models import SupplierDeliveryStatus
from apps.scm.supplier_deliveries.services import (
    create_supplier_delivery,
    create_supplier_delivery_line,
    mark_supplier_delivery_received,
    mark_supplier_delivery_shipped,
)
from apps.teams.models import Team


def _team(slug) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _po(team, po_number="PO-SVC-001", external_id="bc-svc-po-001") -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=external_id,
        po_number=po_number,
        supplier_no="SUP-001",
        supplier_name="Service Supplier",
        status=PurchaseOrderStatus.OPEN,
    )


def _po_line(team, po, line_no="10000", ordered_qty=1000) -> PurchaseOrderLine:
    return PurchaseOrderLine.objects.create(
        team=team,
        purchase_order=po,
        external_id=f"bc-svc-line-{line_no}",
        line_no=line_no,
        item_no="ITEM-SVC",
        description="Service Item",
        ordered_qty=ordered_qty,
    )


class CreateSupplierDeliveryTest(TestCase):
    def test_creates_correctly(self):
        team = _team("sd-svc-create")
        po = _po(team)
        delivery = create_supplier_delivery(
            team=team,
            purchase_order=po,
            delivery_reference="DEL-NEW",
            supplier="My Supplier",
            planned_arrival_date=datetime.date(2026, 8, 1),
        )
        self.assertEqual(delivery.delivery_reference, "DEL-NEW")
        self.assertEqual(delivery.team, team)
        self.assertEqual(delivery.purchase_order, po)
        self.assertEqual(delivery.supplier, "My Supplier")
        self.assertEqual(delivery.status, SupplierDeliveryStatus.PLANNED)
        self.assertEqual(delivery.planned_arrival_date, datetime.date(2026, 8, 1))

    def test_default_status_is_planned(self):
        team = _team("sd-svc-default-status")
        po = _po(team, po_number="PO-DS", external_id="bc-ds-po")
        delivery = create_supplier_delivery(team=team, purchase_order=po, delivery_reference="DEL-DS")
        self.assertEqual(delivery.status, SupplierDeliveryStatus.PLANNED)


class CreateSupplierDeliveryLineTest(TestCase):
    def test_creates_line_within_limit(self):
        team = _team("sd-svc-line-ok")
        po = _po(team, po_number="PO-LO", external_id="bc-lo-po")
        line = _po_line(team, po, ordered_qty=1000)
        delivery = create_supplier_delivery(team=team, purchase_order=po, delivery_reference="DEL-LO")

        dl = create_supplier_delivery_line(
            team=team,
            delivery=delivery,
            purchase_order_line=line,
            delivery_qty=Decimal("300"),
            article="ITEM-SVC",
            unit="PCS",
        )
        self.assertEqual(dl.delivery_qty, Decimal("300"))
        self.assertEqual(dl.delivery, delivery)

    def test_partial_deliveries_can_sum_to_ordered_qty(self):
        team = _team("sd-svc-partial-ok")
        po = _po(team, po_number="PO-PA", external_id="bc-pa-po")
        line = _po_line(team, po, ordered_qty=1000)
        del_a = create_supplier_delivery(team=team, purchase_order=po, delivery_reference="DEL-PA-A")
        del_b = create_supplier_delivery(team=team, purchase_order=po, delivery_reference="DEL-PA-B")
        del_c = create_supplier_delivery(team=team, purchase_order=po, delivery_reference="DEL-PA-C")

        create_supplier_delivery_line(team=team, delivery=del_a, purchase_order_line=line, delivery_qty=300)
        create_supplier_delivery_line(team=team, delivery=del_b, purchase_order_line=line, delivery_qty=250)
        create_supplier_delivery_line(team=team, delivery=del_c, purchase_order_line=line, delivery_qty=450)

        # Total = 1000, exactly at limit — should not raise
        self.assertEqual(line.delivery_lines.count(), 3)

    def test_raises_when_qty_exceeds_ordered(self):
        team = _team("sd-svc-line-over")
        po = _po(team, po_number="PO-OV", external_id="bc-ov-po")
        line = _po_line(team, po, ordered_qty=1000)
        del_a = create_supplier_delivery(team=team, purchase_order=po, delivery_reference="DEL-OV-A")
        del_b = create_supplier_delivery(team=team, purchase_order=po, delivery_reference="DEL-OV-B")
        del_c = create_supplier_delivery(team=team, purchase_order=po, delivery_reference="DEL-OV-C")

        create_supplier_delivery_line(team=team, delivery=del_a, purchase_order_line=line, delivery_qty=300)
        create_supplier_delivery_line(team=team, delivery=del_b, purchase_order_line=line, delivery_qty=250)
        with self.assertRaises(ValidationError):
            create_supplier_delivery_line(team=team, delivery=del_c, purchase_order_line=line, delivery_qty=600)


class MarkSupplierDeliveryShippedTest(TestCase):
    def test_changes_status_and_date(self):
        team = _team("sd-svc-shipped")
        po = _po(team, po_number="PO-SHP", external_id="bc-shp-po")
        delivery = create_supplier_delivery(team=team, purchase_order=po, delivery_reference="DEL-SHP")

        ship_date = datetime.date(2026, 7, 1)
        result = mark_supplier_delivery_shipped(delivery, actual_ship_date=ship_date)

        self.assertEqual(result.status, SupplierDeliveryStatus.SHIPPED)
        self.assertEqual(result.actual_ship_date, ship_date)

    def test_defaults_to_today(self):
        team = _team("sd-svc-shipped-today")
        po = _po(team, po_number="PO-SHPT", external_id="bc-shpt-po")
        delivery = create_supplier_delivery(team=team, purchase_order=po, delivery_reference="DEL-SHPT")
        result = mark_supplier_delivery_shipped(delivery)
        self.assertEqual(result.actual_ship_date, datetime.date.today())


class MarkSupplierDeliveryReceivedTest(TestCase):
    def test_changes_status_and_date(self):
        team = _team("sd-svc-received")
        po = _po(team, po_number="PO-RCV", external_id="bc-rcv-po")
        delivery = create_supplier_delivery(team=team, purchase_order=po, delivery_reference="DEL-RCV")

        arrival_date = datetime.date(2026, 8, 15)
        result = mark_supplier_delivery_received(delivery, actual_arrival_date=arrival_date)

        self.assertEqual(result.status, SupplierDeliveryStatus.RECEIVED)
        self.assertEqual(result.actual_arrival_date, arrival_date)

    def test_defaults_to_today(self):
        team = _team("sd-svc-received-today")
        po = _po(team, po_number="PO-RCVT", external_id="bc-rcvt-po")
        delivery = create_supplier_delivery(team=team, purchase_order=po, delivery_reference="DEL-RCVT")
        result = mark_supplier_delivery_received(delivery)
        self.assertEqual(result.actual_arrival_date, datetime.date.today())
