"""Tests for the Business Central purchase order sync (integration-based)."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from apps.scm.integrations.business_systems.business_central import sync as sync_module
from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import (
    BusinessCentralConfigurationError,
    BusinessCentralError,
    BusinessCentralSyncInProgressError,
)
from apps.scm.integrations.business_systems.business_central.sync import (
    sync_purchase_orders_from_business_central,
)
from apps.scm.integrations.models import Integration, IntegrationCredential, IntegrationSyncRun
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine
from apps.scm.procurement.services import UpsertResult
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-sync"}}


def _team(slug: str = "bc-sync-team") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _integration(
    team=None, *, active=True, sync_enabled=True, with_creds=False, provider="business_central", family=None
):
    team = team or _team()
    family = family if family is not None else Integration.ProviderFamily.BUSINESS_SYSTEM
    integration = Integration.objects.create(
        team=team,
        name="BC",
        provider_code=provider,
        provider_family=family,
        status=Integration.Status.ACTIVE if active else Integration.Status.INACTIVE,
        is_active=active,
        config={"sync_enabled": sync_enabled, "tenant_id": "t", "environment": "Production", "company_id": "c"},
    )
    if with_creds:
        from apps.scm.integrations.credentials import set_integration_credentials

        set_integration_credentials(
            integration, IntegrationCredential.AuthType.OAUTH2, {"client_id": "cid", "client_secret": "sec"}
        )
    return integration


def _dummy_client() -> BusinessCentralClient:
    return BusinessCentralClient(use_dummy=True)


def _sync(integration, **kwargs):
    return sync_purchase_orders_from_business_central(integration, client=_dummy_client(), **kwargs)


class SyncCreatesOrdersTest(TestCase):
    def test_creates_purchase_orders(self):
        integration = _integration()
        run = _sync(integration)
        self.assertEqual(PurchaseOrder.objects.filter(team=integration.team).count(), 2)
        self.assertEqual(run.records_created, 2)
        self.assertEqual(run.records_fetched, 2)

    def test_creates_purchase_order_lines(self):
        integration = _integration()
        _sync(integration)
        po = PurchaseOrder.objects.get(team=integration.team, po_number="PO100245")
        self.assertEqual(po.lines.count(), 1)

    def test_po_header_fields(self):
        integration = _integration()
        _sync(integration)
        po = PurchaseOrder.objects.get(team=integration.team, po_number="PO100245")
        self.assertEqual(po.supplier_no, "L00141")
        self.assertEqual(po.supplier_name, "Shanghai Containers Ltd")
        self.assertEqual(po.status, "open")
        self.assertEqual(po.currency, "USD")

    def test_po_line_values(self):
        integration = _integration()
        _sync(integration)
        po = PurchaseOrder.objects.get(team=integration.team, po_number="PO100245")
        line = po.lines.get(item_no="40HC-NEW")
        self.assertEqual(line.unit_price, Decimal("1940.0"))
        self.assertEqual(line.ordered_qty, Decimal("25"))

    def test_run_recorded_completed(self):
        integration = _integration()
        run = _sync(integration)
        self.assertEqual(run.status, IntegrationSyncRun.Status.COMPLETED)
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)
        self.assertTrue(run.correlation_id)


class SyncIdempotencyTest(TestCase):
    def test_second_sync_reports_unchanged(self):
        integration = _integration()
        _sync(integration)
        run2 = _sync(integration)
        self.assertEqual(PurchaseOrder.objects.filter(team=integration.team).count(), 2)
        self.assertEqual(run2.records_created, 0)
        self.assertEqual(run2.records_unchanged, 2)
        self.assertEqual(run2.records_updated, 0)

    def test_update_existing_po_counts_as_updated(self):
        integration = _integration()
        _sync(integration)

        updated_pos = [{**po, "vendorName": "Updated Vendor Name"} for po in _dummy_client().fetch_purchase_orders()]
        with mock.patch.object(BusinessCentralClient, "fetch_purchase_orders", return_value=updated_pos):
            run = _sync(integration)

        self.assertEqual(PurchaseOrder.objects.filter(team=integration.team).count(), 2)
        po = PurchaseOrder.objects.get(team=integration.team, po_number="PO100245")
        self.assertEqual(po.supplier_name, "Updated Vendor Name")
        self.assertEqual(run.records_updated, 2)
        self.assertEqual(run.records_unchanged, 0)


class SyncMultiTenancyTest(TestCase):
    def test_same_external_id_different_teams(self):
        int_a = _integration(_team("bc-team-a"))
        int_b = _integration(_team("bc-team-b"))
        _sync(int_a)
        _sync(int_b)
        self.assertEqual(PurchaseOrder.objects.filter(team=int_a.team).count(), 2)
        self.assertEqual(PurchaseOrder.objects.filter(team=int_b.team).count(), 2)
        self.assertEqual(PurchaseOrder.objects.count(), 4)

    def test_sync_run_scoped_to_integration(self):
        int_a = _integration(_team("bc-run-a"))
        int_b = _integration(_team("bc-run-b"))
        _sync(int_a)
        _sync(int_b)
        self.assertEqual(int_a.sync_runs.count(), 1)
        self.assertEqual(int_b.sync_runs.count(), 1)


class SyncValidationTest(TestCase):
    def test_inactive_integration_rejected(self):
        integration = _integration(active=False)
        with self.assertRaises(BusinessCentralConfigurationError):
            _sync(integration)

    def test_sync_disabled_rejected(self):
        integration = _integration(sync_enabled=False)
        with self.assertRaises(BusinessCentralConfigurationError):
            _sync(integration)

    def test_wrong_provider_rejected(self):
        integration = _integration(provider="john_evans")
        with self.assertRaises(BusinessCentralConfigurationError):
            _sync(integration)

    def test_wrong_family_rejected(self):
        integration = _integration(family=Integration.ProviderFamily.CARRIER)
        with self.assertRaises(BusinessCentralConfigurationError):
            _sync(integration)

    def test_live_without_credentials_rejected(self):
        # No injected client → live path → credentials required.
        integration = _integration(with_creds=False)
        with self.assertRaises(BusinessCentralConfigurationError):
            sync_purchase_orders_from_business_central(integration)


class SyncWatermarkTest(TestCase):
    def test_first_sync_has_no_watermark_from(self):
        integration = _integration()
        run = _sync(integration)
        self.assertIsNone(run.watermark_from)
        # dummy fixtures carry lastModifiedDateTime → watermark_to is the max of them
        self.assertEqual(run.watermark_to, datetime(2026, 6, 21, 14, 5, 0, tzinfo=UTC))

    def test_watermark_advances_after_success(self):
        integration = _integration()
        run1 = _sync(integration)
        run2 = _sync(integration)
        self.assertEqual(run2.watermark_from, run1.watermark_to)

    def test_watermark_not_advanced_after_failure(self):
        integration = _integration()
        with mock.patch.object(
            BusinessCentralClient, "fetch_purchase_orders", side_effect=BusinessCentralError("boom")
        ), self.assertRaises(BusinessCentralError):
            _sync(integration)
        run = integration.sync_runs.latest("created_at")
        self.assertEqual(run.status, IntegrationSyncRun.Status.FAILED)
        self.assertIsNone(run.watermark_to)
        from apps.scm.integrations.services import get_last_successful_watermark

        self.assertIsNone(get_last_successful_watermark(integration, IntegrationSyncRun.ResourceType.PURCHASE_ORDERS))


class SyncPartialFailureTest(TestCase):
    def test_partial_failure_marks_partial_and_holds_watermark(self):
        integration = _integration()
        partial = UpsertResult(created=1, failed=1, errors=[{"external_id": "x", "error": "bad"}])
        with mock.patch("apps.scm.procurement.services.upsert_purchase_orders", return_value=partial):
            run = _sync(integration)
        self.assertEqual(run.status, IntegrationSyncRun.Status.PARTIALLY_COMPLETED)
        self.assertEqual(run.records_failed, 1)
        self.assertIsNone(run.watermark_to)  # not advanced on partial
        self.assertIn("failed", run.error_summary)


@override_settings(CACHES=_LOCMEM)
class SyncLockTest(TestCase):
    def test_second_run_blocked_while_locked(self):
        from django.core.cache import cache

        integration = _integration()
        # Pre-acquire the lock as if another run holds it.
        cache.add(sync_module._lock_key(integration), "1", 600)
        with self.assertRaises(BusinessCentralSyncInProgressError):
            _sync(integration)

    def test_lock_released_after_run(self):
        from django.core.cache import cache

        integration = _integration()
        _sync(integration)
        # Lock is released, so a subsequent run acquires it fine.
        self.assertIsNone(cache.get(sync_module._lock_key(integration)))
        run = _sync(integration)
        self.assertEqual(run.status, IntegrationSyncRun.Status.COMPLETED)


class SyncLineIdentifierTest(TestCase):
    def test_live_uses_guid_dummy_uses_number(self):
        raw = {"id": "guid-1", "number": "PO1"}
        dummy = _dummy_client()
        live = mock.Mock(use_dummy=False)
        self.assertEqual(sync_module._line_identifier(raw, dummy), "PO1")
        self.assertEqual(sync_module._line_identifier(raw, live), "guid-1")


class SyncLineDetailsTest(TestCase):
    def test_line_external_id_and_line_no(self):
        integration = _integration()
        _sync(integration)
        line = PurchaseOrderLine.objects.get(external_id="bc-line-id-001")
        self.assertEqual(line.line_no, "10000")
