"""Tests for the computed SCM logistics status (selectors.get_purchase_order_logistics_status).

Logistics status is COMPUTED by SCM from fulfillment quantities and is distinct
from PurchaseOrder.status (the Business Central document status).
"""

from decimal import Decimal

from django.test import TestCase

from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine, PurchaseOrderLogisticsStatus
from apps.scm.procurement.selectors import get_purchase_order_logistics_status
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryLine, SupplierDeliveryStatus
from apps.teams.models import Team


class LogisticsStatusTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="logi", slug="logi")

    _counter = 0

    def _po(self, ordered="10", shipped="0", received="0", arrived=None):
        LogisticsStatusTest._counter += 1
        n = LogisticsStatusTest._counter
        po = PurchaseOrder.objects.create(team=self.team, external_id=f"po-{n}", po_number=f"PO-{n}")
        line = PurchaseOrderLine.objects.create(
            team=self.team,
            purchase_order=po,
            external_id=f"line-{n}",
            line_no="10000",
            item_no="ITEM",
            ordered_qty=Decimal(ordered),
            shipped_qty=Decimal(shipped),
            received_qty=Decimal(received),
        )
        if arrived is not None:
            delivery = SupplierDelivery.objects.create(
                team=self.team,
                purchase_order=po,
                delivery_reference=f"DEL-{n}",
                status=SupplierDeliveryStatus.ARRIVED,
            )
            SupplierDeliveryLine.objects.create(
                team=self.team,
                delivery=delivery,
                purchase_order_line=line,
                delivery_qty=Decimal(arrived),
            )
        return po

    def test_not_started_nothing_moved(self):
        po = self._po(ordered="10")
        self.assertEqual(get_purchase_order_logistics_status(po), PurchaseOrderLogisticsStatus.NOT_STARTED)

    def test_not_started_when_nothing_ordered(self):
        po = self._po(ordered="0")
        self.assertEqual(get_purchase_order_logistics_status(po), PurchaseOrderLogisticsStatus.NOT_STARTED)

    def test_partially_shipped(self):
        po = self._po(ordered="10", shipped="4")
        self.assertEqual(get_purchase_order_logistics_status(po), PurchaseOrderLogisticsStatus.PARTIALLY_SHIPPED)

    def test_fully_shipped(self):
        po = self._po(ordered="10", shipped="10")
        self.assertEqual(get_purchase_order_logistics_status(po), PurchaseOrderLogisticsStatus.FULLY_SHIPPED)

    def test_arrived(self):
        po = self._po(ordered="10", shipped="10", received="0", arrived="10")
        self.assertEqual(get_purchase_order_logistics_status(po), PurchaseOrderLogisticsStatus.ARRIVED)

    def test_partially_received(self):
        po = self._po(ordered="10", shipped="10", received="4")
        self.assertEqual(get_purchase_order_logistics_status(po), PurchaseOrderLogisticsStatus.PARTIALLY_RECEIVED)

    def test_completed(self):
        po = self._po(ordered="10", shipped="10", received="10")
        self.assertEqual(get_purchase_order_logistics_status(po), PurchaseOrderLogisticsStatus.COMPLETED)

    def test_exception_over_received(self):
        po = self._po(ordered="10", received="12")
        self.assertEqual(get_purchase_order_logistics_status(po), PurchaseOrderLogisticsStatus.EXCEPTION)

    def test_received_takes_precedence_over_arrived(self):
        po = self._po(ordered="10", shipped="10", received="4", arrived="6")
        self.assertEqual(get_purchase_order_logistics_status(po), PurchaseOrderLogisticsStatus.PARTIALLY_RECEIVED)
