"""Tests for purchase order source metadata (source_system, raw_payload, etc.)."""

from datetime import UTC, datetime

from django.test import TestCase

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.sync import (
    sync_purchase_orders_from_business_central,
)
from apps.scm.integrations.models import Integration
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderSource
from apps.scm.procurement.services import create_purchase_order, upsert_purchase_orders
from apps.teams.models import Team


def _bc_integration(team):
    return Integration.objects.create(
        team=team,
        name="BC",
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        is_active=True,
        config={"sync_enabled": True, "company_id": "company-guid-123"},
    )


class SourceMetadataFromSyncTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="meta", slug="meta")

    def _sync(self):
        integration = _bc_integration(self.team)
        return sync_purchase_orders_from_business_central(integration, client=BusinessCentralClient(use_dummy=True))

    def test_po_source_fields_populated(self):
        self._sync()
        po = PurchaseOrder.objects.get(team=self.team, po_number="PO100245")
        self.assertEqual(po.source_system, PurchaseOrderSource.BUSINESS_CENTRAL)
        self.assertEqual(po.source_company_id, "company-guid-123")
        self.assertTrue(po.source_active)
        self.assertIsNone(po.source_deleted_at)
        self.assertIsNotNone(po.last_synced_at)
        self.assertEqual(po.source_last_modified_at, datetime(2026, 6, 18, 9, 30, 0, tzinfo=UTC))
        self.assertTrue(po.is_business_central)

    def test_raw_payload_stored_without_secrets(self):
        self._sync()
        po = PurchaseOrder.objects.get(team=self.team, po_number="PO100245")
        self.assertEqual(po.raw_payload["id"], "bc-po-id-001")
        # Sanity: no auth material ever present in the BC OData payload.
        self.assertNotIn("Authorization", po.raw_payload)
        self.assertNotIn("access_token", po.raw_payload)

    def test_line_source_fields_populated(self):
        self._sync()
        po = PurchaseOrder.objects.get(team=self.team, po_number="PO100245")
        line = po.lines.first()
        self.assertTrue(line.source_active)
        self.assertIsNotNone(line.last_synced_at)
        self.assertEqual(line.raw_payload["id"], "bc-line-id-001")

    def test_last_synced_at_bumped_on_unchanged(self):
        integration = _bc_integration(self.team)
        sync_purchase_orders_from_business_central(integration, client=BusinessCentralClient(use_dummy=True))
        po = PurchaseOrder.objects.get(team=self.team, po_number="PO100245")
        first_sync = po.last_synced_at
        run2 = sync_purchase_orders_from_business_central(integration, client=BusinessCentralClient(use_dummy=True))
        po.refresh_from_db()
        self.assertEqual(run2.records_unchanged, 2)  # not counted as updates
        self.assertGreaterEqual(po.last_synced_at, first_sync)  # but technically touched


class SourceSystemPerSourceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="src", slug="src")

    def test_manual_create_marks_manual(self):
        po = create_purchase_order(self.team, po_number="M1")
        self.assertEqual(po.source_system, PurchaseOrderSource.MANUAL)
        self.assertFalse(po.is_business_central)

    def test_upsert_default_is_business_central(self):
        upsert_purchase_orders(self.team, [{"external_id": "x", "po_number": "P", "lines": []}])
        po = PurchaseOrder.objects.get(team=self.team, external_id="x")
        self.assertEqual(po.source_system, PurchaseOrderSource.BUSINESS_CENTRAL)

    def test_upsert_document_import_source(self):
        upsert_purchase_orders(
            self.team,
            [{"external_id": "d", "po_number": "P", "lines": []}],
            source_system=PurchaseOrderSource.DOCUMENT_IMPORT,
        )
        po = PurchaseOrder.objects.get(team=self.team, external_id="d")
        self.assertEqual(po.source_system, PurchaseOrderSource.DOCUMENT_IMPORT)
