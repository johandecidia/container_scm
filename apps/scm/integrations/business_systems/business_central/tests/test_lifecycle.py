"""Tests for the shared Business Central sync lifecycle and the integration-wide source binding.

Resource steps are plain functions; a non-purchase-order resource is made
"implemented" only for the duration of a test. No Business Central calls.
"""

from datetime import UTC, datetime, timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scm.integrations.business_systems.business_central import resources
from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import (
    BusinessCentralConfigurationError,
    BusinessCentralConnectionError,
    BusinessCentralSyncInProgressError,
)
from apps.scm.integrations.business_systems.business_central.lifecycle import SyncOutcome, run_business_central_sync
from apps.scm.integrations.business_systems.business_central.locks import cache_lock_key, sync_lock_name
from apps.scm.integrations.business_systems.business_central.sync import sync_purchase_orders_from_business_central
from apps.scm.integrations.models import Integration, IntegrationSyncRun
from apps.scm.integrations.services import get_last_successful_cursor
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-lifecycle"}}
_COMPANY = "782d7133-5033-4c4b-9a29-83f7f44f062d"
_OTHER_COMPANY = "99999999-9999-4999-8999-999999999999"
_PO = IntegrationSyncRun.ResourceType.PURCHASE_ORDERS
_ILE = IntegrationSyncRun.ResourceType.ITEM_LEDGER_ENTRIES
_SO = IntegrationSyncRun.ResourceType.SALES_ORDERS
_FAKE_IMPL = {_ILE: "apps.scm.integrations.business_systems.business_central.tests.test_lifecycle.fake_sync"}


def fake_sync(*args, **kwargs):  # registered in _FAKE_IMPL; never called directly by these tests
    raise AssertionError("not used")


def _source(environment: str = "production", company: str = _COMPANY) -> dict:
    return {"mode": "live", "tenant_id": "tenant", "environment": environment, "company_id": company}


class _SourceClient(BusinessCentralClient):
    """Dummy fixtures presented as a given live source."""

    def __init__(self, environment: str = "production", company: str = _COMPANY):
        super().__init__(use_dummy=True)
        self._source = _source(environment, company)

    def source_identity(self) -> dict:
        return self._source


def _integration(slug: str, **resource_config) -> Integration:
    team = Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]
    return Integration.objects.create(
        team=team,
        name="BC",
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        is_active=True,
        config={"resources": {_ILE: {"enabled": True, **resource_config}}},
    )


def _step(**outcome):
    return mock.Mock(side_effect=lambda ctx: SyncOutcome(**outcome))


def _run_ile(integration, step=None, client=None):
    return run_business_central_sync(
        integration,
        _ILE,
        step or _step(fetched=1, created=1),
        client or _SourceClient(),
        trigger_type=IntegrationSyncRun.TriggerType.MANUAL,
    )


def _sync_po(integration, client=None):
    return sync_purchase_orders_from_business_central(
        integration, client=client or _SourceClient(), trigger_type=IntegrationSyncRun.TriggerType.MANUAL
    )


@override_settings(CACHES=_LOCMEM)
@mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
class LifecycleRunTest(TestCase):
    def tearDown(self):
        cache.clear()

    def test_completed_run_records_resource_counts_and_source(self):
        integration = _integration("lc-complete")
        run = _run_ile(integration, _step(fetched=3, created=2, unchanged=1))
        self.assertEqual(run.resource_type, _ILE)
        self.assertEqual(run.status, IntegrationSyncRun.Status.COMPLETED)
        self.assertEqual((run.records_fetched, run.records_created, run.records_unchanged), (3, 2, 1))
        self.assertEqual(run.metadata["source"], _source())
        self.assertEqual(run.team, integration.team)

    def test_step_receives_run_context(self):
        integration = _integration("lc-context")
        step = _step()
        run = _run_ile(integration, step)
        context = step.call_args.args[0]
        self.assertEqual(context.run, run)
        self.assertEqual(context.integration, integration)
        self.assertEqual(context.source, _source())
        self.assertIsNone(context.watermark_from)
        self.assertIsNone(context.cursor_from)

    def test_markers_advance_on_completed_run(self):
        integration = _integration("lc-markers")
        moment = datetime(2030, 1, 1, tzinfo=UTC)
        run = _run_ile(integration, _step(watermark_to=moment, cursor_to=1234))
        self.assertEqual(run.watermark_to, moment)
        self.assertEqual(run.metadata["cursor_to"], 1234)
        step = _step()
        second = _run_ile(integration, step)
        self.assertEqual(step.call_args.args[0].watermark_from, moment)
        self.assertEqual(step.call_args.args[0].cursor_from, 1234)
        self.assertEqual(second.metadata["cursor_from"], 1234)

    def test_partial_run_does_not_advance_markers(self):
        integration = _integration("lc-partial")
        run = _run_ile(
            integration,
            _step(created=1, failed=1, errors=[{"external_id": "7"}], watermark_to=timezone.now(), cursor_to=99),
        )
        self.assertEqual(run.status, IntegrationSyncRun.Status.PARTIALLY_COMPLETED)
        self.assertIsNone(run.watermark_to)
        self.assertNotIn("cursor_to", run.metadata)
        self.assertIn("1 record(s) failed", run.error_summary)
        self.assertIsNone(get_last_successful_cursor(integration, _ILE))

    def test_failed_run_is_recorded_and_reraised(self):
        integration = _integration("lc-failed")
        step = mock.Mock(side_effect=BusinessCentralConnectionError("down"))
        with self.assertRaises(BusinessCentralConnectionError):
            _run_ile(integration, step)
        run = integration.sync_runs.get()
        self.assertEqual(run.status, IntegrationSyncRun.Status.FAILED)
        self.assertIn("BusinessCentralConnectionError", run.error_summary)
        self.assertIsNone(run.watermark_to)
        self.assertNotIn("cursor_to", run.metadata)
        integration.refresh_from_db()
        self.assertEqual(integration.status, Integration.Status.ERROR)

    def test_cursor_is_the_highest_completed_value(self):
        integration = _integration("lc-cursor")
        for value in (5, 120, 42):
            _run_ile(integration, _step(cursor_to=value))
        self.assertEqual(get_last_successful_cursor(integration, _ILE, source=_source()), 120)
        self.assertIsNone(get_last_successful_cursor(integration, _ILE, source=_source("sandbox")))
        self.assertIsNone(get_last_successful_cursor(integration, _PO))


@override_settings(CACHES=_LOCMEM)
class LifecycleValidationTest(TestCase):
    def tearDown(self):
        cache.clear()

    def test_unimplemented_resource_is_refused(self):
        integration = _integration("lc-unimpl")
        integration.config["resources"][_SO] = {"enabled": True}  # enabled in config, but no implementation
        integration.save()
        step = _step()
        with self.assertRaisesMessage(BusinessCentralConfigurationError, "no Business Central sync implementation"):
            run_business_central_sync(integration, _SO, step, _SourceClient())
        step.assert_not_called()
        self.assertFalse(integration.sync_runs.exists())

    @mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
    def test_disabled_resource_is_refused(self):
        integration = _integration("lc-disabled", enabled=False)
        with self.assertRaisesMessage(BusinessCentralConfigurationError, "not enabled"):
            _run_ile(integration)
        self.assertFalse(integration.sync_runs.exists())

    @mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
    def test_new_resource_without_config_is_refused(self):
        integration = _integration("lc-noconfig")
        integration.config = {}
        integration.save()
        with self.assertRaises(BusinessCentralConfigurationError):
            _run_ile(integration)

    def test_unknown_resource_is_refused(self):
        integration = _integration("lc-unknown")
        with self.assertRaisesMessage(BusinessCentralConfigurationError, "unknown"):
            run_business_central_sync(integration, "customers", _step(), _SourceClient())

    def test_sync_disabled_blocks_every_resource(self):
        integration = _integration("lc-sync-off")
        integration.config["sync_enabled"] = False
        integration.save()
        with self.assertRaisesMessage(BusinessCentralConfigurationError, "disabled"):
            _sync_po(integration)

    def test_purchase_orders_can_be_switched_off_per_resource(self):
        integration = _integration("lc-po-off")
        integration.config["resources"][_PO] = {"enabled": False}
        integration.save()
        with self.assertRaisesMessage(BusinessCentralConfigurationError, "not enabled"):
            _sync_po(integration)


@override_settings(CACHES=_LOCMEM)
@mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
class ResourceLockTest(TestCase):
    def tearDown(self):
        cache.clear()

    def test_same_resource_is_blocked(self):
        integration = _integration("lk-same")
        cache.add(cache_lock_key(sync_lock_name(integration, _ILE)), "held", 600)
        with self.assertRaises(BusinessCentralSyncInProgressError):
            _run_ile(integration)
        self.assertFalse(integration.sync_runs.exists())

    def test_other_resource_runs_while_one_is_locked(self):
        integration = _integration("lk-other")
        cache.add(cache_lock_key(sync_lock_name(integration, _ILE)), "held", 600)
        run = _sync_po(integration)
        self.assertEqual(run.status, IntegrationSyncRun.Status.COMPLETED)

    def test_purchase_order_lock_name_is_unchanged(self):
        integration = _integration("lk-name")
        self.assertEqual(sync_lock_name(integration, _PO), f"{integration.pk}:purchase_orders")


@override_settings(CACHES=_LOCMEM)
@mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
class IntegrationWideSourceBindingTest(TestCase):
    """Every resource of one integration reads the same BC tenant, environment and company."""

    def tearDown(self):
        cache.clear()

    def test_purchase_orders_from_existing_source_still_sync(self):
        integration = _integration("sb-po")
        _sync_po(integration)
        self.assertEqual(_sync_po(integration).status, IntegrationSyncRun.Status.COMPLETED)

    def test_new_resource_from_same_source_is_accepted(self):
        integration = _integration("sb-same")
        _sync_po(integration)
        self.assertEqual(_run_ile(integration).status, IntegrationSyncRun.Status.COMPLETED)

    def test_new_resource_from_other_environment_is_refused(self):
        integration = _integration("sb-env")
        _sync_po(integration)
        step = _step()
        with self.assertRaisesMessage(BusinessCentralConfigurationError, "separate Team"):
            _run_ile(integration, step, _SourceClient("sandbox"))
        step.assert_not_called()
        self.assertFalse(integration.sync_runs.filter(resource_type=_ILE).exists())

    def test_new_resource_from_other_company_is_refused(self):
        integration = _integration("sb-company")
        _sync_po(integration)
        with self.assertRaises(BusinessCentralConfigurationError):
            _run_ile(integration, client=_SourceClient(company=_OTHER_COMPANY))

    def test_purchase_orders_refused_after_other_resource_bound_the_integration(self):
        integration = _integration("sb-reverse")
        _run_ile(integration)
        with self.assertRaises(BusinessCentralConfigurationError):
            _sync_po(integration, _SourceClient("sandbox"))

    def test_teams_can_bind_different_sources(self):
        first = _integration("sb-team-a")
        second = _integration("sb-team-b")
        _sync_po(first, _SourceClient("production"))
        _run_ile(second, client=_SourceClient("sandbox"))
        self.assertEqual(_sync_po(second, _SourceClient("sandbox")).status, IntegrationSyncRun.Status.COMPLETED)
        self.assertEqual(_run_ile(first).status, IntegrationSyncRun.Status.COMPLETED)

    def test_in_progress_run_of_other_resource_binds(self):
        """A resource still running from source A blocks another resource starting from source B.

        This is the parallel case: neither run has completed, so only the in-flight
        claim (taken under the integration row lock) prevents the second source.
        """
        integration = _integration("sb-parallel")
        IntegrationSyncRun.objects.create(
            team=integration.team,
            integration=integration,
            resource_type=_PO,
            status=IntegrationSyncRun.Status.RUNNING,
            started_at=timezone.now(),
            metadata={"source": _source("production")},
        )
        with self.assertRaises(BusinessCentralConfigurationError):
            _run_ile(integration, client=_SourceClient("sandbox"))
        # The same source is fine while the other resource is running.
        self.assertEqual(_run_ile(integration).status, IntegrationSyncRun.Status.COMPLETED)

    def test_parallel_start_inside_step_cannot_switch_source(self):
        """A second resource starting *during* the first run sees the first run's claim."""
        integration = _integration("sb-nested")

        def step(context):
            with self.assertRaises(BusinessCentralConfigurationError):
                run_business_central_sync(integration, _ILE, _step(), _SourceClient("sandbox"))
            return SyncOutcome()

        run = run_business_central_sync(integration, _PO, step, _SourceClient("production"))
        self.assertEqual(run.status, IntegrationSyncRun.Status.COMPLETED)

    def test_stale_running_run_does_not_bind(self):
        integration = _integration("sb-stale")
        IntegrationSyncRun.objects.create(
            team=integration.team,
            integration=integration,
            resource_type=_PO,
            status=IntegrationSyncRun.Status.RUNNING,
            started_at=timezone.now() - timedelta(hours=3),
            metadata={"source": _source("production")},
        )
        run = _run_ile(integration, client=_SourceClient("sandbox"))
        self.assertEqual(run.status, IntegrationSyncRun.Status.COMPLETED)

    def test_failed_run_of_other_resource_does_not_bind(self):
        integration = _integration("sb-failed")
        with self.assertRaises(BusinessCentralConnectionError):
            _run_ile(integration, mock.Mock(side_effect=BusinessCentralConnectionError()), _SourceClient("typo"))
        self.assertEqual(_sync_po(integration).status, IntegrationSyncRun.Status.COMPLETED)
