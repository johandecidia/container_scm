"""Tests for procurement models."""

from django.test import TestCase

from apps.scm.procurement.models import (
    PurchaseOrder,
    PurchaseOrderEvent,
    PurchaseOrderEventType,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from apps.teams.models import Team


def _team(slug="test-team") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _po(team, po_number="PO-001", external_id="bc-po-001") -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=external_id,
        po_number=po_number,
        supplier_no="SUP-001",
        supplier_name="Test Supplier",
        status=PurchaseOrderStatus.OPEN,
    )


class PurchaseOrderModelTest(TestCase):
    def test_create(self):
        team = _team()
        po = _po(team)
        self.assertEqual(po.po_number, "PO-001")
        self.assertEqual(po.team, team)
        self.assertEqual(po.status, PurchaseOrderStatus.OPEN)

    def test_str(self):
        team = _team()
        po = _po(team)
        self.assertIn("PO-001", str(po))
        self.assertIn("Test Supplier", str(po))

    def test_timestamps_set(self):
        team = _team()
        po = _po(team)
        self.assertIsNotNone(po.created_at)
        self.assertIsNotNone(po.updated_at)


class PurchaseOrderLineModelTest(TestCase):
    def test_create_and_link(self):
        team = _team()
        po = _po(team)
        line = PurchaseOrderLine.objects.create(
            team=team,
            purchase_order=po,
            external_id="bc-line-1",
            line_no="10000",
            item_no="ITEM-001",
            description="Yoga mat",
            ordered_qty=100,
        )
        self.assertEqual(line.purchase_order, po)
        self.assertEqual(po.lines.count(), 1)

    def test_str(self):
        team = _team()
        po = _po(team)
        line = PurchaseOrderLine.objects.create(
            team=team,
            purchase_order=po,
            external_id="bc-line-1",
            line_no="10000",
            item_no="ITEM-001",
            description="Yoga mat",
            ordered_qty=100,
        )
        self.assertIn("PO-001", str(line))
        self.assertIn("ITEM-001", str(line))


class PurchaseOrderEventModelTest(TestCase):
    def test_create_and_link(self):
        team = _team()
        po = _po(team)
        event = PurchaseOrderEvent.objects.create(
            purchase_order=po,
            event_type=PurchaseOrderEventType.CREATED,
            description="Order placed",
        )
        self.assertEqual(event.purchase_order, po)
        self.assertEqual(po.events.count(), 1)

    def test_str(self):
        team = _team()
        po = _po(team)
        event = PurchaseOrderEvent.objects.create(
            purchase_order=po,
            event_type=PurchaseOrderEventType.CREATED,
        )
        self.assertIn("PO-001", str(event))
