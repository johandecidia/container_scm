"""Data integrity and idempotency tests for SCM (8.7).

Tests:
- SupplierDelivery delivery_reference is unique per team
- import_purchase_orders_from_bc is fully idempotent (update_or_create)
- Duplicate delivery reference raises IntegrityError across teams
"""

from decimal import Decimal

from django.db import IntegrityError
from django.test import TestCase

from apps.scm.procurement.models import PurchaseOrder
from apps.scm.procurement.services import import_purchase_orders_from_bc
from apps.scm.supplier_deliveries.models import SupplierDelivery
from apps.teams.models import Team


def make_team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def make_purchase_order(team: Team, po_number: str = "PO-001") -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=f"EXT-{po_number}",
        po_number=po_number,
        supplier_no="SUP001",
        supplier_name="Test Supplier",
    )


class SupplierDeliveryUniqueConstraintTests(TestCase):
    """delivery_reference must be unique within a team."""

    def setUp(self):
        self.team = make_team("integrity-sd")
        self.po = make_purchase_order(self.team)

    def test_duplicate_delivery_reference_same_team_raises(self):
        SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=self.po,
            delivery_reference="DEL-001",
        )
        with self.assertRaises(IntegrityError):
            SupplierDelivery.objects.create(
                team=self.team,
                purchase_order=self.po,
                delivery_reference="DEL-001",
            )

    def test_same_delivery_reference_different_teams_allowed(self):
        team_b = make_team("integrity-sd-b")
        po_b = make_purchase_order(team_b, "PO-002")
        SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=self.po,
            delivery_reference="DEL-SHARED",
        )
        # Should not raise — different team
        delivery_b = SupplierDelivery.objects.create(
            team=team_b,
            purchase_order=po_b,
            delivery_reference="DEL-SHARED",
        )
        self.assertIsNotNone(delivery_b.pk)

    def test_unique_references_within_team_are_fine(self):
        SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=self.po,
            delivery_reference="DEL-AAA",
        )
        d2 = SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=self.po,
            delivery_reference="DEL-BBB",
        )
        self.assertIsNotNone(d2.pk)


class PurchaseOrderIdempotencyTests(TestCase):
    """import_purchase_orders_from_bc must be safe to run multiple times."""

    def setUp(self):
        self.team = make_team("integrity-po")

    def _sample_data(self, po_number: str = "PO-IDEM-001") -> list[dict]:
        return [
            {
                "external_id": f"EXT-{po_number}",
                "po_number": po_number,
                "supplier_no": "SUP001",
                "supplier_name": "Test Supplier",
                "status": "open",
                "currency": "EUR",
                "lines": [
                    {
                        "external_id": "LINE-001",
                        "line_no": "10",
                        "item_no": "ITEM-A",
                        "ordered_qty": "100",
                        "shipped_qty": "0",
                        "received_qty": "0",
                    }
                ],
            }
        ]

    def test_import_twice_does_not_duplicate_po(self):
        data = self._sample_data()
        import_purchase_orders_from_bc(self.team, data)
        import_purchase_orders_from_bc(self.team, data)
        count = PurchaseOrder.objects.filter(team=self.team, external_id="EXT-PO-IDEM-001").count()
        self.assertEqual(count, 1)

    def test_import_twice_does_not_duplicate_lines(self):
        from apps.scm.procurement.models import PurchaseOrderLine

        data = self._sample_data()
        import_purchase_orders_from_bc(self.team, data)
        import_purchase_orders_from_bc(self.team, data)
        po = PurchaseOrder.objects.get(team=self.team, external_id="EXT-PO-IDEM-001")
        line_count = PurchaseOrderLine.objects.filter(purchase_order=po).count()
        self.assertEqual(line_count, 1)

    def test_import_updates_existing_po_on_second_call(self):
        data = self._sample_data()
        import_purchase_orders_from_bc(self.team, data)

        updated_data = self._sample_data()
        updated_data[0]["status"] = "released"
        updated_data[0]["supplier_name"] = "Updated Supplier"
        import_purchase_orders_from_bc(self.team, updated_data)

        po = PurchaseOrder.objects.get(team=self.team, external_id="EXT-PO-IDEM-001")
        self.assertEqual(po.status, "released")
        self.assertEqual(po.supplier_name, "Updated Supplier")

    def test_import_updates_line_quantities_on_second_call(self):
        from apps.scm.procurement.models import PurchaseOrderLine

        data = self._sample_data()
        import_purchase_orders_from_bc(self.team, data)

        updated_data = self._sample_data()
        updated_data[0]["lines"][0]["shipped_qty"] = "50"
        import_purchase_orders_from_bc(self.team, updated_data)

        po = PurchaseOrder.objects.get(team=self.team, external_id="EXT-PO-IDEM-001")
        line = PurchaseOrderLine.objects.get(purchase_order=po, external_id="LINE-001")
        self.assertEqual(line.shipped_qty, Decimal("50"))
