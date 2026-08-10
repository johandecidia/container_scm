"""Tests for procurement selectors — team isolation is critical."""

from django.test import TestCase

from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderEvent, PurchaseOrderEventType
from apps.scm.procurement.selectors import (
    get_purchase_order_events,
    get_purchase_order_for_team,
    get_purchase_order_lines,
    get_team_purchase_orders,
)
from apps.teams.models import Team


def _team(slug) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _po(team, external_id, po_number) -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=external_id,
        po_number=po_number,
        supplier_no="SUP-001",
        supplier_name="Supplier",
        status="open",
    )


class TeamPurchaseOrderIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team_a = _team("sel-team-a")
        cls.team_b = _team("sel-team-b")
        cls.po_a = _po(cls.team_a, "bc-a-1", "PO-A1")
        cls.po_b = _po(cls.team_b, "bc-b-1", "PO-B1")

    def test_team_a_sees_only_own_orders(self):
        qs = get_team_purchase_orders(self.team_a)
        pks = list(qs.values_list("pk", flat=True))
        self.assertIn(self.po_a.pk, pks)
        self.assertNotIn(self.po_b.pk, pks)

    def test_team_b_does_not_see_team_a_orders(self):
        qs = get_team_purchase_orders(self.team_b)
        pks = list(qs.values_list("pk", flat=True))
        self.assertIn(self.po_b.pk, pks)
        self.assertNotIn(self.po_a.pk, pks)

    def test_get_purchase_order_for_team_correct_team(self):
        po = get_purchase_order_for_team(self.team_a, self.po_a.pk)
        self.assertEqual(po, self.po_a)

    def test_get_purchase_order_for_team_wrong_team_raises(self):
        with self.assertRaises(PurchaseOrder.DoesNotExist):
            get_purchase_order_for_team(self.team_b, self.po_a.pk)


class GetPurchaseOrderLinesTest(TestCase):
    def test_returns_lines_for_order(self):
        from apps.scm.procurement.models import PurchaseOrderLine

        team = _team("lines-team")
        po = _po(team, "bc-lines-1", "PO-LINES")
        PurchaseOrderLine.objects.create(
            team=team, purchase_order=po, external_id="l1", line_no="10000", item_no="X", ordered_qty=10
        )
        PurchaseOrderLine.objects.create(
            team=team, purchase_order=po, external_id="l2", line_no="20000", item_no="Y", ordered_qty=5
        )
        lines = get_purchase_order_lines(po)
        self.assertEqual(lines.count(), 2)


class GetPurchaseOrderEventsTest(TestCase):
    def test_returns_events_for_order(self):
        team = _team("events-team")
        po = _po(team, "bc-evt-1", "PO-EVT")
        PurchaseOrderEvent.objects.create(purchase_order=po, event_type=PurchaseOrderEventType.CREATED)
        PurchaseOrderEvent.objects.create(purchase_order=po, event_type=PurchaseOrderEventType.FULLY_SHIPPED)
        events = get_purchase_order_events(po)
        self.assertEqual(events.count(), 2)
