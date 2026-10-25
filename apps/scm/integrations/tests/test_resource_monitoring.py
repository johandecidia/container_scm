"""Per-resource monitoring, integration health across resources, and the stage-0 clean-up.

No Business Central calls: syncs use the dummy fixture client, and a
non-purchase-order resource is made "implemented" only for a test.
"""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.scm.integrations.business_systems.base import BaseBusinessSystemClient, BusinessSystemCapability
from apps.scm.integrations.business_systems.business_central import resources
from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralConnectionError
from apps.scm.integrations.business_systems.business_central.lifecycle import SyncOutcome, run_business_central_sync
from apps.scm.integrations.business_systems.business_central.sync import sync_purchase_orders_from_business_central
from apps.scm.integrations.business_systems.registry import get_business_system_definition
from apps.scm.integrations.models import Integration, IntegrationSyncRun
from apps.scm.integrations.selectors import get_integration_monitoring_context, get_resource_sync_statuses
from apps.scm.procurement.tasks import sync_purchase_orders_from_bc
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-monitoring"}}
_PO = IntegrationSyncRun.ResourceType.PURCHASE_ORDERS
_ILE = IntegrationSyncRun.ResourceType.ITEM_LEDGER_ENTRIES
_FAKE_IMPL = {_ILE: f"{__name__}.fake_ile_sync"}


def fake_ile_sync(*args, **kwargs):  # only registered as "implemented"; tests drive the lifecycle directly
    raise AssertionError("not used")


def _integration(slug: str) -> Integration:
    team = Team.objects.create(name=slug, slug=slug)
    return Integration.objects.create(
        team=team,
        name="BC",
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        is_active=True,
        config={"environment": "Production", "company_id": "company-guid", "resources": {_ILE: {"enabled": True}}},
    )


def _sync_po(integration):
    return sync_purchase_orders_from_business_central(integration, client=BusinessCentralClient(use_dummy=True))


def _run_ile(integration, *, fail=False):
    def step(context):
        if fail:
            raise BusinessCentralConnectionError("ledger endpoint down")
        return SyncOutcome(fetched=4, created=4)

    return run_business_central_sync(integration, _ILE, step, BusinessCentralClient(use_dummy=True))


def _status(integration, resource_type):
    return next(s for s in get_resource_sync_statuses(integration) if s.resource_type == resource_type)


@override_settings(CACHES=_LOCMEM)
@mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
class ResourceSyncStatusTest(TestCase):
    def tearDown(self):
        cache.clear()

    def test_every_resource_type_is_reported_and_never_synced(self):
        integration = _integration("mon-empty")
        statuses = get_resource_sync_statuses(integration)
        self.assertEqual([s.resource_type for s in statuses], IntegrationSyncRun.ResourceType.values)
        self.assertTrue(all(s.never_synced and not s.has_open_error for s in statuses))

    def test_implemented_and_enabled_flags(self):
        integration = _integration("mon-flags")
        sales = _status(integration, IntegrationSyncRun.ResourceType.SALES_ORDERS)
        self.assertFalse(sales.implemented)
        self.assertFalse(sales.enabled)
        self.assertTrue(_status(integration, _PO).implemented)
        self.assertTrue(_status(integration, _ILE).enabled)

    def test_status_and_counts_are_per_resource(self):
        integration = _integration("mon-counts")
        _sync_po(integration)
        with self.assertRaises(BusinessCentralConnectionError):
            _run_ile(integration, fail=True)
        po, ile = _status(integration, _PO), _status(integration, _ILE)
        self.assertEqual(po.latest_run.status, IntegrationSyncRun.Status.COMPLETED)
        self.assertEqual(po.last_finished_run.records_fetched, 2)
        self.assertFalse(po.has_open_error)
        self.assertEqual(ile.latest_run.status, IntegrationSyncRun.Status.FAILED)
        self.assertTrue(ile.has_open_error)
        self.assertIn("ledger endpoint down", ile.error_summary)
        self.assertTrue(_status(integration, IntegrationSyncRun.ResourceType.SALES_SHIPMENTS).never_synced)

    def test_later_success_clears_the_resource_error(self):
        integration = _integration("mon-recover")
        with self.assertRaises(BusinessCentralConnectionError):
            _run_ile(integration, fail=True)
        _run_ile(integration)
        self.assertFalse(_status(integration, _ILE).has_open_error)

    def test_monitoring_context_keeps_purchase_order_keys(self):
        integration = _integration("mon-context")
        run = _sync_po(integration)
        context = get_integration_monitoring_context(integration)
        self.assertEqual(context["latest_run"], run)
        self.assertEqual(context["last_completed_run"], run)
        self.assertEqual(context["current_watermark"], run.watermark_to)
        self.assertFalse(context["in_progress"])
        self.assertEqual(context["resource_errors"], [])
        self.assertEqual(len(context["resources"]), len(IntegrationSyncRun.ResourceType.values))


@override_settings(CACHES=_LOCMEM)
@mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
class IntegrationHealthAcrossResourcesTest(TestCase):
    """``Integration.last_error_message`` is shared; a success must not hide another resource's error."""

    def tearDown(self):
        cache.clear()

    def test_success_of_one_resource_keeps_other_resource_error(self):
        integration = _integration("hl-keep")
        with self.assertRaises(BusinessCentralConnectionError):
            _run_ile(integration, fail=True)
        _sync_po(integration)
        integration.refresh_from_db()
        self.assertEqual(integration.status, Integration.Status.ERROR)
        self.assertIn("Item ledger entries", integration.last_error_message)
        self.assertIn("ledger endpoint down", integration.last_error_message)
        self.assertIsNotNone(integration.last_success_at)

    def test_error_cleared_when_failing_resource_recovers(self):
        integration = _integration("hl-clear")
        with self.assertRaises(BusinessCentralConnectionError):
            _run_ile(integration, fail=True)
        _sync_po(integration)
        _run_ile(integration)
        integration.refresh_from_db()
        self.assertEqual(integration.status, Integration.Status.ACTIVE)
        self.assertEqual(integration.last_error_message, "")

    def test_purchase_order_only_behaviour_is_unchanged(self):
        integration = _integration("hl-po")
        with (
            mock.patch.object(
                BusinessCentralClient, "fetch_purchase_orders", side_effect=BusinessCentralConnectionError()
            ),
            self.assertRaises(BusinessCentralConnectionError),
        ):
            _sync_po(integration)
        integration.refresh_from_db()
        self.assertEqual(integration.status, Integration.Status.ERROR)
        self.assertTrue(integration.last_error_message.startswith("BusinessCentralConnectionError"))
        _sync_po(integration)
        integration.refresh_from_db()
        self.assertEqual(integration.status, Integration.Status.ACTIVE)
        self.assertEqual(integration.last_error_message, "")


@override_settings(CACHES=_LOCMEM)
@mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
class ResourceMonitoringUITest(TestCase):
    def setUp(self):
        self.integration = _integration("mon-ui")
        user = CustomUser.objects.create_user(username="mon-ui@example.com", email="mon-ui@example.com", password="pw")
        Membership.objects.create(team=self.integration.team, user=user, role="admin")
        self.client.force_login(user)
        session = self.client.session
        session["team"] = self.integration.team.pk
        session.save()

    def tearDown(self):
        cache.clear()

    def test_detail_lists_resources_and_their_errors(self):
        _sync_po(self.integration)
        with self.assertRaises(BusinessCentralConnectionError):
            _run_ile(self.integration, fail=True)
        resp = self.client.get(reverse("integrations:detail", args=[self.integration.pk]))
        self.assertContains(resp, "Resources")
        self.assertContains(resp, "Item ledger entries")
        self.assertContains(resp, "ledger endpoint down")
        self.assertContains(resp, "Never synced")
        self.assertContains(resp, "Not available")

    def test_list_flags_resource_errors(self):
        with self.assertRaises(BusinessCentralConnectionError):
            _run_ile(self.integration, fail=True)
        resp = self.client.get(reverse("integrations:list"))
        self.assertContains(resp, "1 resource error")


class CapabilityCleanupTest(TestCase):
    def test_base_capabilities_is_a_capability_not_a_dataclass_field(self):
        self.assertIsInstance(BaseBusinessSystemClient.capabilities, BusinessSystemCapability)

    def test_business_central_claims_only_what_it_implements(self):
        caps = BusinessCentralClient.capabilities
        self.assertTrue(caps.supports_purchase_orders)
        self.assertTrue(caps.supports_polling)
        for flag in ("supports_sales_orders", "supports_customers", "supports_vendors", "supports_items"):
            self.assertFalse(getattr(caps, flag), flag)
        self.assertFalse(caps.supports_webhooks)

    def test_registry_matches_client(self):
        self.assertEqual(
            get_business_system_definition("business_central").capabilities, BusinessCentralClient.capabilities
        )


class LegacyProcurementTaskTest(TestCase):
    _SYNC = "apps.scm.integrations.business_systems.business_central.sync.sync_purchase_orders_from_business_central"

    def test_honours_per_resource_switch(self):
        integration = _integration("legacy-off")
        integration.config.update(
            {"sync_enabled": True, "scheduled_sync_enabled": True, "resources": {_PO: {"enabled": False}}}
        )
        integration.save()
        with mock.patch(self._SYNC) as sync:
            sync_purchase_orders_from_bc(integration.team_id)
        sync.assert_not_called()
