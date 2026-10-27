"""Outstanding quantity on purchase order lines, and sources that do not send received quantities."""

from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from apps.scm.procurement.models import PurchaseOrderLine, PurchaseOrderSource
from apps.scm.procurement.services import create_purchase_order, upsert_purchase_orders
from apps.teams.models import Team

_LINE = {"external_id": "l1", "line_no": "10000", "item_no": "CON22G1", "ordered_qty": 49}


class OutstandingQuantityTest(SimpleTestCase):
    def test_ordered_minus_received(self):
        line = PurchaseOrderLine(ordered_qty=Decimal("49"), received_qty=Decimal("3"))
        self.assertEqual(line.outstanding_qty, Decimal("46"))

    def test_decimals(self):
        line = PurchaseOrderLine(ordered_qty=Decimal("10.5"), received_qty=Decimal("2.25"))
        self.assertEqual(line.outstanding_qty, Decimal("8.25"))

    def test_over_receipt_is_not_negative(self):
        line = PurchaseOrderLine(ordered_qty=Decimal("2"), received_qty=Decimal("3"))
        self.assertEqual(line.outstanding_qty, Decimal("0"))


class OtherSourcesTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="qty-sources", slug="qty-sources")

    def test_document_import_without_received_quantity_still_writes_zero(self):
        upsert_purchase_orders(
            self.team,
            [{"external_id": "doc-1", "po_number": "D1", "lines": [_LINE]}],
            source_system=PurchaseOrderSource.DOCUMENT_IMPORT,
        )
        line = PurchaseOrderLine.objects.get(team=self.team)
        self.assertEqual(line.received_qty, Decimal("0"))

    def test_manual_order_line_is_unchanged(self):
        po = create_purchase_order(self.team, po_number="M1", supplier_name="Manual")
        line = PurchaseOrderLine.objects.create(
            team=self.team, purchase_order=po, external_id="m-1", line_no="1", item_no="X", ordered_qty=5
        )
        self.assertEqual(po.source_system, PurchaseOrderSource.MANUAL)
        self.assertEqual(line.received_qty, Decimal("0"))
        self.assertEqual(line.outstanding_qty, Decimal("5"))
