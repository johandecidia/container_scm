"""Tests for supplier delivery models."""

from django.db import IntegrityError
from django.test import TestCase

from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine, PurchaseOrderStatus
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryLine, SupplierDeliveryStatus
from apps.teams.models import Team


def _team(slug="sd-model-team") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _po(team, po_number="PO-001", external_id="bc-sd-po-001") -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=external_id,
        po_number=po_number,
        supplier_no="SUP-001",
        supplier_name="Test Supplier",
        status=PurchaseOrderStatus.OPEN,
    )


def _po_line(team, po, line_no="10000", ordered_qty=1000) -> PurchaseOrderLine:
    return PurchaseOrderLine.objects.create(
        team=team,
        purchase_order=po,
        external_id=f"bc-line-{line_no}",
        line_no=line_no,
        item_no="ITEM-001",
        description="Test Item",
        ordered_qty=ordered_qty,
    )


def _delivery(team, po, reference="DEL-001") -> SupplierDelivery:
    return SupplierDelivery.objects.create(
        team=team,
        purchase_order=po,
        delivery_reference=reference,
        status=SupplierDeliveryStatus.PLANNED,
    )


class SupplierDeliveryModelTest(TestCase):
    def test_create(self):
        team = _team()
        po = _po(team)
        delivery = _delivery(team, po)
        self.assertEqual(delivery.delivery_reference, "DEL-001")
        self.assertEqual(delivery.team, team)
        self.assertEqual(delivery.purchase_order, po)
        self.assertEqual(delivery.status, SupplierDeliveryStatus.PLANNED)

    def test_str(self):
        team = _team()
        po = _po(team)
        delivery = _delivery(team, po)
        self.assertIn("DEL-001", str(delivery))
        self.assertIn("PO-001", str(delivery))

    def test_timestamps_set(self):
        team = _team()
        po = _po(team)
        delivery = _delivery(team, po)
        self.assertIsNotNone(delivery.created_at)
        self.assertIsNotNone(delivery.updated_at)

    def test_dates_optional(self):
        team = _team()
        po = _po(team)
        delivery = SupplierDelivery.objects.create(
            team=team,
            purchase_order=po,
            delivery_reference="DEL-NODATES",
        )
        self.assertIsNone(delivery.planned_ship_date)
        self.assertIsNone(delivery.planned_arrival_date)
        self.assertIsNone(delivery.actual_ship_date)
        self.assertIsNone(delivery.actual_arrival_date)


class SupplierDeliveryLineModelTest(TestCase):
    def test_create_and_link(self):
        team = _team()
        po = _po(team)
        line = _po_line(team, po)
        delivery = _delivery(team, po)
        dl = SupplierDeliveryLine.objects.create(
            team=team,
            delivery=delivery,
            purchase_order_line=line,
            delivery_qty=300,
            article="ITEM-001",
            unit="PCS",
        )
        self.assertEqual(dl.delivery, delivery)
        self.assertEqual(dl.purchase_order_line, line)
        self.assertEqual(delivery.lines.count(), 1)

    def test_container_can_be_null(self):
        team = _team()
        po = _po(team)
        line = _po_line(team, po)
        delivery = _delivery(team, po)
        dl = SupplierDeliveryLine.objects.create(
            team=team,
            delivery=delivery,
            purchase_order_line=line,
            delivery_qty=100,
            container=None,
        )
        self.assertIsNone(dl.container)

    def test_str(self):
        team = _team()
        po = _po(team)
        line = _po_line(team, po)
        delivery = _delivery(team, po)
        dl = SupplierDeliveryLine.objects.create(
            team=team,
            delivery=delivery,
            purchase_order_line=line,
            delivery_qty=100,
            article="ITEM-001",
        )
        self.assertIn("DEL-001", str(dl))

    def test_line_reverse_relation_on_delivery(self):
        team = _team("sd-line-rev-team")
        po = _po(team, po_number="PO-SDREV", external_id="bc-sdrev-001")
        line = _po_line(team, po)
        delivery = _delivery(team, po, "DEL-SDREV")
        dl = SupplierDeliveryLine.objects.create(
            team=team, delivery=delivery, purchase_order_line=line, delivery_qty=100
        )
        self.assertIn(dl, delivery.lines.all())


class SupplierDeliveryConstraintsTest(TestCase):
    def test_unique_delivery_reference_per_team(self):
        team = _team("sd-unique-team")
        po = _po(team, po_number="PO-UNIQ", external_id="bc-sd-uniq-001")
        _delivery(team, po, "DEL-UNIQUE")
        with self.assertRaises(IntegrityError):
            SupplierDelivery.objects.create(
                team=team,
                purchase_order=po,
                delivery_reference="DEL-UNIQUE",
            )

    def test_same_reference_different_teams_allowed(self):
        team1 = _team("sd-team-alpha")
        team2 = _team("sd-team-beta")
        po1 = _po(team1, po_number="PO-SDREF1", external_id="bc-sdref-001")
        po2 = _po(team2, po_number="PO-SDREF2", external_id="bc-sdref-002")
        _delivery(team1, po1, "DEL-SHARED")
        d2 = _delivery(team2, po2, "DEL-SHARED")
        self.assertIsNotNone(d2.pk)

    def test_deleting_delivery_cascades_lines(self):
        team = _team("sd-cascade-team")
        po = _po(team, po_number="PO-CASCADE-SD", external_id="bc-sd-casc-001")
        line = _po_line(team, po)
        delivery = _delivery(team, po, "DEL-CASCADE")
        SupplierDeliveryLine.objects.create(team=team, delivery=delivery, purchase_order_line=line, delivery_qty=100)
        pk = delivery.pk
        delivery.delete()
        self.assertEqual(SupplierDeliveryLine.objects.filter(delivery_id=pk).count(), 0)

    def test_deleting_po_cascades_deliveries(self):
        team = _team("sd-po-cascade-team")
        po = _po(team, po_number="PO-PO-CASCADE", external_id="bc-po-casc-001")
        _delivery(team, po, "DEL-PO-CASCADE")
        pk = po.pk
        po.delete()
        self.assertEqual(SupplierDelivery.objects.filter(purchase_order_id=pk).count(), 0)
