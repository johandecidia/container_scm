"""Posted purchase receipt sync: import, idempotency, incremental watermark, isolation, no side effects.

Uses the dummy fixture (4 receipts, 3 per page) presented as a live source. No Business Central calls.
"""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.scm.containers.models import Container, ContainerMovement
from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import (
    BusinessCentralConfigurationError,
    BusinessCentralSyncInProgressError,
)
from apps.scm.integrations.business_systems.business_central.locks import cache_lock_key, sync_lock_name
from apps.scm.integrations.business_systems.business_central.receipt_sync import (
    sync_purchase_receipts_from_business_central,
    upsert_purchase_receipts,
)
from apps.scm.integrations.models import BusinessCentralPurchaseReceipt, Integration, IntegrationSyncRun
from apps.scm.tracking.models import TrackingSubscription
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-receipt-sync"}}
_COMPANY = "782d7133-5033-4c4b-9a29-83f7f44f062d"
_RECEIPTS = IntegrationSyncRun.ResourceType.PURCHASE_RECEIPTS
Receipt = BusinessCentralPurchaseReceipt


class SourceClient(BusinessCentralClient):
    """Dummy fixtures presented as a live source."""

    def __init__(self, company_id: str = _COMPANY):
        super().__init__(use_dummy=True)
        self.company_id = company_id

    def source_identity(self) -> dict:
        return {"mode": "live", "tenant_id": "tenant", "environment": "sandbox", "company_id": self.company_id}


def receipt_integration(slug: str, enabled: bool = True) -> Integration:
    team = Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]
    resources = {_RECEIPTS: {"enabled": True}} if enabled else {}
    return Integration.objects.create(
        team=team,
        name="BC",
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        is_active=True,
        config={"company_id": _COMPANY, "resources": resources},
    )


def sync(integration, client=None):
    return sync_purchase_receipts_from_business_central(
        integration, client=client or SourceClient(), trigger_type=IntegrationSyncRun.TriggerType.MANUAL
    )


@override_settings(CACHES=_LOCMEM)
class ReceiptSyncTest(TestCase):
    def tearDown(self):
        cache.clear()

    def test_imports_headers_and_lines(self):
        integration = receipt_integration("rcpt-import")
        run = sync(integration)
        self.assertEqual(run.status, IntegrationSyncRun.Status.COMPLETED)
        self.assertEqual((run.records_fetched, run.records_created, run.records_failed), (4, 4, 0))
        receipt = Receipt.objects.get(integration=integration, receipt_no="IN27608")
        self.assertEqual(
            (receipt.team, receipt.company_id, receipt.order_no, receipt.vendor_no, receipt.line_count),
            (integration.team, _COMPANY, "100017", "1203", 3),
        )
        self.assertEqual(str(receipt.posting_date), "2026-08-10")
        self.assertEqual([line["quantity"] for line in receipt.lines], [2, 1, 1])
        self.assertIsNone(Receipt.objects.get(integration=integration, receipt_no="IN10000").bc_last_modified)
        self.assertEqual(run.watermark_to.isoformat(), "2026-10-09T14:34:54.890000+00:00")

    def test_second_run_is_incremental_and_idempotent(self):
        integration = receipt_integration("rcpt-again")
        sync(integration)
        run = sync(integration)
        # Only the receipt inside the watermark overlap is read again, and it is unchanged.
        self.assertEqual((run.records_fetched, run.records_created, run.records_unchanged), (1, 0, 1))
        self.assertEqual(Receipt.objects.filter(integration=integration).count(), 4)

    def test_reimporting_the_same_page_creates_no_duplicates(self):
        integration = receipt_integration("rcpt-upsert")
        rows = list(SourceClient().iter_purchase_receipt_pages())[0]
        upsert_purchase_receipts(integration, _COMPANY, rows)
        counts = upsert_purchase_receipts(integration, _COMPANY, rows)
        self.assertEqual((counts.created, counts.unchanged), (0, 3))
        changed = [{**rows[1], "orderNumber": "999"}]
        counts = upsert_purchase_receipts(integration, _COMPANY, changed)
        self.assertEqual(counts.updated, 1)
        self.assertEqual(Receipt.objects.get(integration=integration, receipt_no=rows[1]["number"]).order_no, "999")
        self.assertEqual(Receipt.objects.filter(integration=integration).count(), 3)

    def test_receipt_without_number_fails_and_holds_the_watermark(self):
        integration = receipt_integration("rcpt-bad")
        bad = [[{"id": "x", "number": "", "lastModifiedDateTime": "2026-10-10T00:00:00Z"}]]
        with mock.patch.object(SourceClient, "iter_purchase_receipt_pages", return_value=iter(bad)):
            run = sync(integration)
        self.assertEqual((run.status, run.records_failed), (IntegrationSyncRun.Status.PARTIALLY_COMPLETED, 1))
        self.assertIsNone(run.watermark_to)

    def test_same_number_in_another_company_is_separate(self):
        integration = receipt_integration("rcpt-company")
        rows = list(SourceClient().iter_purchase_receipt_pages())[0]
        upsert_purchase_receipts(integration, _COMPANY, rows)
        upsert_purchase_receipts(integration, "other-company", rows)
        self.assertEqual(Receipt.objects.filter(integration=integration, receipt_no="IN27576").count(), 2)

    def test_no_operational_side_effects(self):
        integration = receipt_integration("rcpt-side")
        sync(integration)
        self.assertFalse(Container.objects.exists())
        self.assertFalse(ContainerMovement.objects.exists())
        self.assertFalse(TrackingSubscription.objects.exists())

    def test_disabled_by_default(self):
        integration = receipt_integration("rcpt-off", enabled=False)
        with self.assertRaises(BusinessCentralConfigurationError):
            sync(integration)

    def test_parallel_run_is_refused(self):
        integration = receipt_integration("rcpt-lock")
        cache.set(cache_lock_key(sync_lock_name(integration, _RECEIPTS)), "held", 60)
        with self.assertRaises(BusinessCentralSyncInProgressError):
            sync(integration)

    def test_source_binding_refuses_another_company(self):
        integration = receipt_integration("rcpt-binding")
        sync(integration)
        with self.assertRaises(BusinessCentralConfigurationError):
            sync(integration, SourceClient(company_id="11111111-1111-1111-1111-111111111111"))

    def test_teams_are_isolated(self):
        first, second = receipt_integration("rcpt-team-a"), receipt_integration("rcpt-team-b")
        sync(first)
        sync(second)
        self.assertEqual(Receipt.objects.filter(team=first.team).count(), 4)
        self.assertEqual(Receipt.objects.filter(team=second.team).count(), 4)
        self.assertFalse(Receipt.objects.filter(team=first.team, integration=second).exists())
