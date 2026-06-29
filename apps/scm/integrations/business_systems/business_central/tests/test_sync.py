"""Tests for the Business Central purchase order sync."""

from decimal import Decimal

from django.test import TestCase

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.sync import sync_purchase_orders_from_business_central
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine
from apps.teams.models import Team


def _team(slug: str = "bc-sync-team") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _dummy_client() -> BusinessCentralClient:
    return BusinessCentralClient(use_dummy=True)


class SyncCreatesOrdersTest(TestCase):
    def test_creates_purchase_orders(self):
        team = _team()
        orders = sync_purchase_orders_from_business_central(team=team, client=_dummy_client())
        self.assertEqual(len(orders), 2)
        self.assertEqual(PurchaseOrder.objects.filter(team=team).count(), 2)

    def test_creates_purchase_order_lines(self):
        team = _team()
        sync_purchase_orders_from_business_central(team=team, client=_dummy_client())
        po = PurchaseOrder.objects.get(team=team, po_number="PO100245")
        self.assertEqual(po.lines.count(), 1)

    def test_po100246_has_two_lines(self):
        team = _team()
        sync_purchase_orders_from_business_central(team=team, client=_dummy_client())
        po = PurchaseOrder.objects.get(team=team, po_number="PO100246")
        self.assertEqual(po.lines.count(), 2)

    def test_po_header_fields(self):
        team = _team()
        sync_purchase_orders_from_business_central(team=team, client=_dummy_client())
        po = PurchaseOrder.objects.get(team=team, po_number="PO100245")
        self.assertEqual(po.supplier_no, "L00141")
        self.assertEqual(po.supplier_name, "Shanghai Containers Ltd")
        self.assertEqual(po.status, "open")
        self.assertEqual(po.currency, "USD")

    def test_po_line_unit_price_saved(self):
        team = _team()
        sync_purchase_orders_from_business_central(team=team, client=_dummy_client())
        po = PurchaseOrder.objects.get(team=team, po_number="PO100245")
        line = po.lines.get(item_no="40HC-NEW")
        self.assertEqual(line.unit_price, Decimal("1940.0"))

    def test_po_line_ordered_qty(self):
        team = _team()
        sync_purchase_orders_from_business_central(team=team, client=_dummy_client())
        po = PurchaseOrder.objects.get(team=team, po_number="PO100245")
        line = po.lines.first()
        self.assertEqual(line.ordered_qty, Decimal("25"))


class SyncIdempotencyTest(TestCase):
    def test_sync_is_idempotent_orders(self):
        team = _team()
        sync_purchase_orders_from_business_central(team=team, client=_dummy_client())
        sync_purchase_orders_from_business_central(team=team, client=_dummy_client())
        self.assertEqual(PurchaseOrder.objects.filter(team=team).count(), 2)

    def test_sync_is_idempotent_lines(self):
        team = _team()
        sync_purchase_orders_from_business_central(team=team, client=_dummy_client())
        sync_purchase_orders_from_business_central(team=team, client=_dummy_client())
        po = PurchaseOrder.objects.get(team=team, po_number="PO100245")
        self.assertEqual(po.lines.count(), 1)

    def test_sync_updates_existing_po(self):
        """Re-syncing with updated supplier name should update the PO, not create a duplicate."""
        team = _team()
        sync_purchase_orders_from_business_central(team=team, client=_dummy_client())

        # Simulate a BC update by using a custom client that overrides vendor name
        from unittest.mock import patch

        updated_pos = [
            {
                **po,
                "vendorName": "Updated Vendor Name",
            }
            for po in _dummy_client().fetch_purchase_orders()
        ]
        with patch.object(BusinessCentralClient, "fetch_purchase_orders", return_value=updated_pos):
            sync_purchase_orders_from_business_central(team=team, client=_dummy_client())

        self.assertEqual(PurchaseOrder.objects.filter(team=team).count(), 2)
        po = PurchaseOrder.objects.get(team=team, po_number="PO100245")
        self.assertEqual(po.supplier_name, "Updated Vendor Name")


class SyncMultiTenancyTest(TestCase):
    def test_same_external_id_different_teams_creates_separate_pos(self):
        team_a = _team(slug="bc-team-a")
        team_b = _team(slug="bc-team-b")

        sync_purchase_orders_from_business_central(team=team_a, client=_dummy_client())
        sync_purchase_orders_from_business_central(team=team_b, client=_dummy_client())

        self.assertEqual(PurchaseOrder.objects.filter(team=team_a).count(), 2)
        self.assertEqual(PurchaseOrder.objects.filter(team=team_b).count(), 2)
        self.assertEqual(PurchaseOrder.objects.count(), 4)

    def test_team_isolation_po_numbers(self):
        team_a = _team(slug="bc-iso-a")
        team_b = _team(slug="bc-iso-b")

        sync_purchase_orders_from_business_central(team=team_a, client=_dummy_client())
        sync_purchase_orders_from_business_central(team=team_b, client=_dummy_client())

        # Each team has its own PO100245
        pos_a = PurchaseOrder.objects.filter(team=team_a, po_number="PO100245")
        pos_b = PurchaseOrder.objects.filter(team=team_b, po_number="PO100245")
        self.assertEqual(pos_a.count(), 1)
        self.assertEqual(pos_b.count(), 1)
        self.assertNotEqual(pos_a.first().pk, pos_b.first().pk)


class SyncLineDetailsTest(TestCase):
    def test_line_external_id_stored(self):
        team = _team()
        sync_purchase_orders_from_business_central(team=team, client=_dummy_client())
        self.assertTrue(PurchaseOrderLine.objects.filter(external_id="bc-line-id-001").exists())

    def test_line_no_is_sequence_as_string(self):
        team = _team()
        sync_purchase_orders_from_business_central(team=team, client=_dummy_client())
        line = PurchaseOrderLine.objects.get(external_id="bc-line-id-001")
        self.assertEqual(line.line_no, "10000")

    def test_second_po_line_unit_prices(self):
        team = _team()
        sync_purchase_orders_from_business_central(team=team, client=_dummy_client())
        line_reefer = PurchaseOrderLine.objects.get(external_id="bc-line-id-002")
        line_pti = PurchaseOrderLine.objects.get(external_id="bc-line-id-003")
        self.assertEqual(line_reefer.unit_price, Decimal("7200.0"))
        self.assertEqual(line_pti.unit_price, Decimal("400.0"))
