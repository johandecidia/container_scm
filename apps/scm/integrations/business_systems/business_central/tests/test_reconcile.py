"""Tests for Business Central purchase order source reconciliation (soft-delete)."""

from unittest import mock

from django.test import TestCase, override_settings

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.reconcile import reconcile_purchase_orders
from apps.scm.integrations.business_systems.business_central.sync import (
    sync_purchase_orders_from_business_central,
)
from apps.scm.integrations.models import Integration
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderStatus
from apps.scm.supplier_deliveries.models import SupplierDelivery
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-recon"}}

# Only PO100245 present at source (PO100246 has gone away).
_ONLY_FIRST_PO = [
    {
        "id": "bc-po-id-001",
        "number": "PO100245",
        "vendorNumber": "L00141",
        "vendorName": "Shanghai Containers Ltd",
        "currencyCode": "USD",
        "status": "Open",
        "lastModifiedDateTime": "2026-06-18T09:30:00Z",
    }
]
_FIRST_PO_LINES = {"PO100245": [{"id": "bc-line-id-001", "sequence": 10000, "itemNumber": "40HC-NEW", "quantity": 25}]}


def _integration(team):
    return Integration.objects.create(
        team=team,
        name="BC",
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        is_active=True,
        config={"sync_enabled": True, "company_id": "c"},
    )


def _subset_client():
    client = mock.Mock()
    client.use_dummy = True
    client.fetch_purchase_orders.return_value = _ONLY_FIRST_PO
    client.fetch_purchase_order_lines.side_effect = lambda ident: _FIRST_PO_LINES.get(ident, [])
    return client


@override_settings(CACHES=_LOCMEM)
class ReconcileTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="recon", slug="recon")
        self.integration = _integration(self.team)
        # Initial full sync from fixtures → PO100245 + PO100246 both active.
        sync_purchase_orders_from_business_central(self.integration, client=BusinessCentralClient(use_dummy=True))

    def test_absent_po_is_soft_deleted(self):
        result = reconcile_purchase_orders(self.integration, client=_subset_client())
        self.assertEqual(result.deactivated_pos, 1)
        gone = PurchaseOrder.objects.get(team=self.team, po_number="PO100246")
        self.assertFalse(gone.source_active)
        self.assertIsNotNone(gone.source_deleted_at)
        # Not hard-deleted — the row is preserved.
        self.assertTrue(PurchaseOrder.objects.filter(pk=gone.pk).exists())

    def test_present_po_stays_active(self):
        reconcile_purchase_orders(self.integration, client=_subset_client())
        present = PurchaseOrder.objects.get(team=self.team, po_number="PO100245")
        self.assertTrue(present.source_active)
        self.assertIsNone(present.source_deleted_at)

    def test_incremental_sync_does_not_deactivate(self):
        # A normal (bounded) sync returning only one PO must NOT deactivate the other.
        sync_purchase_orders_from_business_central(self.integration, client=_subset_client())
        other = PurchaseOrder.objects.get(team=self.team, po_number="PO100246")
        self.assertTrue(other.source_active)

    def test_related_history_preserved(self):
        po = PurchaseOrder.objects.get(team=self.team, po_number="PO100246")
        delivery = SupplierDelivery.objects.create(team=self.team, purchase_order=po, delivery_reference="D-1")
        reconcile_purchase_orders(self.integration, client=_subset_client())
        self.assertTrue(SupplierDelivery.objects.filter(pk=delivery.pk).exists())

    def test_reappearing_po_is_reactivated(self):
        reconcile_purchase_orders(self.integration, client=_subset_client())
        self.assertFalse(PurchaseOrder.objects.get(team=self.team, po_number="PO100246").source_active)
        # Full sync sees it again → reactivated.
        sync_purchase_orders_from_business_central(self.integration, client=BusinessCentralClient(use_dummy=True))
        revived = PurchaseOrder.objects.get(team=self.team, po_number="PO100246")
        self.assertTrue(revived.source_active)
        self.assertIsNone(revived.source_deleted_at)

    def test_closed_but_present_is_not_deactivated(self):
        # A closed BC document that still exists at source stays source_active.
        po = PurchaseOrder.objects.get(team=self.team, po_number="PO100245")
        po.status = PurchaseOrderStatus.CLOSED
        po.save(update_fields=["status"])
        reconcile_purchase_orders(self.integration, client=_subset_client())
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrderStatus.CLOSED)
        self.assertTrue(po.source_active)

    def test_removed_line_is_soft_deleted(self):
        # PO100245 present but with no lines at source → its line is deactivated.
        client = mock.Mock()
        client.use_dummy = True
        client.fetch_purchase_orders.return_value = _ONLY_FIRST_PO
        client.fetch_purchase_order_lines.side_effect = lambda ident: []
        result = reconcile_purchase_orders(self.integration, client=client)
        self.assertGreaterEqual(result.deactivated_lines, 1)
        po = PurchaseOrder.objects.get(team=self.team, po_number="PO100245")
        self.assertFalse(po.lines.first().source_active)
