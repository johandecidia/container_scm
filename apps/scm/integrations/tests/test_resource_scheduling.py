"""Per-resource scheduling of Business Central syncs.

A resource is only scheduled when it is defined, implemented *and* enabled, and
the integration has opted in to scheduled sync. Purchase orders keep their
legacy settings and behaviour.
"""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.scm.integrations.business_systems.business_central import resources
from apps.scm.integrations.models import Integration, IntegrationSyncRun
from apps.scm.integrations.services import get_due_business_central_syncs
from apps.scm.integrations.tasks import (
    sync_business_central_resource_task,
    sync_enabled_business_central_integrations_task,
)
from apps.teams.models import Team

_PO = IntegrationSyncRun.ResourceType.PURCHASE_ORDERS
_ILE = IntegrationSyncRun.ResourceType.ITEM_LEDGER_ENTRIES
_SO = IntegrationSyncRun.ResourceType.SALES_ORDERS
_FAKE_PATH = "apps.scm.integrations.tests.test_resource_scheduling.fake_ile_sync"
_FAKE_IMPL = {_ILE: _FAKE_PATH}
_PO_TASK = "apps.scm.integrations.tasks.sync_business_central_purchase_orders_task.delay"
_RESOURCE_TASK = "apps.scm.integrations.tasks.sync_business_central_resource_task.delay"

fake_ile_sync = mock.Mock(name="fake_ile_sync")


def _integration(slug, *, scheduled=True, **config):
    team = Team.objects.create(name=slug, slug=slug)
    return Integration.objects.create(
        team=team,
        name="BC",
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        is_active=True,
        config={"sync_enabled": True, "scheduled_sync_enabled": scheduled, **config},
    )


def _run(integration, resource_type, *, status, minutes_ago):
    ts = timezone.now() - timedelta(minutes=minutes_ago)
    return IntegrationSyncRun.objects.create(
        team=integration.team,
        integration=integration,
        resource_type=resource_type,
        status=status,
        started_at=ts,
        finished_at=ts if status != IntegrationSyncRun.Status.RUNNING else None,
    )


def _due(integration):
    return [r for i, r in get_due_business_central_syncs() if i == integration]


class ResourceSettingsTest(TestCase):
    def test_defaults_without_resources_config(self):
        integration = _integration("rs-defaults")
        po = resources.get_resource_settings(integration, _PO)
        self.assertTrue(po.implemented and po.enabled)
        self.assertEqual(po.interval, timedelta(minutes=30))
        self.assertEqual(po.failure_backoff, timedelta(minutes=15))
        for resource_type in (_ILE, _SO, IntegrationSyncRun.ResourceType.PURCHASE_RECEIPTS):
            settings = resources.get_resource_settings(integration, resource_type)
            self.assertFalse(settings.implemented, resource_type)
            self.assertFalse(settings.enabled, resource_type)

    def test_legacy_purchase_order_keys_still_apply(self):
        integration = _integration(
            "rs-legacy",
            purchase_order_sync_interval_minutes=10,
            purchase_order_sync_failure_backoff_minutes=3,
        )
        po = resources.get_resource_settings(integration, _PO)
        self.assertEqual(po.interval, timedelta(minutes=10))
        self.assertEqual(po.failure_backoff, timedelta(minutes=3))

    def test_resources_config_overrides_legacy_keys(self):
        integration = _integration(
            "rs-override",
            purchase_order_sync_interval_minutes=10,
            resources={_PO: {"interval_minutes": 45}, _ILE: {"enabled": True, "interval_minutes": 5}},
        )
        self.assertEqual(resources.get_resource_settings(integration, _PO).interval, timedelta(minutes=45))
        ile = resources.get_resource_settings(integration, _ILE)
        self.assertTrue(ile.enabled)
        self.assertEqual(ile.interval, timedelta(minutes=5))

    def test_only_literal_true_enables_a_resource(self):
        integration = _integration("rs-truthy", resources={_ILE: {"enabled": "yes"}})
        self.assertFalse(resources.get_resource_settings(integration, _ILE).enabled)

    def test_unknown_resource_is_rejected(self):
        with self.assertRaises(ValueError):
            resources.get_resource_settings(_integration("rs-unknown"), "customers")


class ResourceSchedulingTest(TestCase):
    def test_existing_integration_schedules_purchase_orders_only(self):
        integration = _integration("sc-po-only")
        self.assertEqual(_due(integration), [_PO])

    def test_enabled_but_unimplemented_resource_is_never_scheduled(self):
        integration = _integration("sc-unimpl", resources={_ILE: {"enabled": True}, _SO: {"enabled": True}})
        self.assertFalse(resources.is_resource_schedulable(integration, _ILE))
        self.assertEqual(_due(integration), [_PO])

    @mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
    def test_implemented_but_disabled_resource_is_not_scheduled(self):
        integration = _integration("sc-disabled")
        self.assertEqual(_due(integration), [_PO])

    @mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
    def test_implemented_and_enabled_needs_integration_opt_in(self):
        integration = _integration("sc-optin", scheduled=False, resources={_ILE: {"enabled": True}})
        self.assertEqual(_due(integration), [])

    @mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
    def test_all_three_conditions_schedule_the_resource(self):
        integration = _integration("sc-all", resources={_ILE: {"enabled": True}})
        self.assertEqual(sorted(_due(integration)), sorted([_PO, _ILE]))

    @mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
    def test_intervals_are_per_resource(self):
        integration = _integration(
            "sc-interval",
            purchase_order_sync_interval_minutes=60,
            resources={_ILE: {"enabled": True, "interval_minutes": 10}},
        )
        _run(integration, _PO, status=IntegrationSyncRun.Status.COMPLETED, minutes_ago=20)
        _run(integration, _ILE, status=IntegrationSyncRun.Status.COMPLETED, minutes_ago=20)
        self.assertEqual(_due(integration), [_ILE])

    @mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
    def test_running_resource_is_not_queued_again(self):
        integration = _integration("sc-running", resources={_ILE: {"enabled": True}})
        _run(integration, _ILE, status=IntegrationSyncRun.Status.RUNNING, minutes_ago=1)
        self.assertEqual(_due(integration), [_PO])

    @mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
    def test_failure_of_one_resource_does_not_delay_another(self):
        integration = _integration("sc-backoff", resources={_ILE: {"enabled": True}})
        _run(integration, _ILE, status=IntegrationSyncRun.Status.FAILED, minutes_ago=1)
        self.assertEqual(_due(integration), [_PO])


class ResourceDispatchTest(TestCase):
    def test_dispatcher_uses_purchase_order_task_for_purchase_orders(self):
        integration = _integration("dp-po", resources={_SO: {"enabled": True}})
        with mock.patch(_PO_TASK) as po_delay, mock.patch(_RESOURCE_TASK) as resource_delay:
            result = sync_enabled_business_central_integrations_task()
        self.assertEqual(result["queued"], 1)
        po_delay.assert_called_once_with(integration.id, "scheduled")
        resource_delay.assert_not_called()

    @mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
    def test_dispatcher_queues_one_task_per_resource(self):
        integration = _integration("dp-many", resources={_ILE: {"enabled": True}})
        with mock.patch(_PO_TASK) as po_delay, mock.patch(_RESOURCE_TASK) as resource_delay:
            result = sync_enabled_business_central_integrations_task()
        self.assertEqual(result["queued"], 2)
        po_delay.assert_called_once_with(integration.id, "scheduled")
        resource_delay.assert_called_once_with(integration.id, _ILE, "scheduled")


@mock.patch.dict(resources._SYNC_IMPLEMENTATIONS, _FAKE_IMPL)
class ResourceTaskTest(TestCase):
    def setUp(self):
        fake_ile_sync.reset_mock()
        fake_ile_sync.return_value = mock.Mock(
            status="completed", records_created=0, records_updated=0, records_unchanged=0, records_failed=0
        )

    def test_scheduled_task_rechecks_resource_enabled(self):
        integration = _integration("tk-disabled")
        sync_business_central_resource_task.run(integration.id, _ILE, "scheduled")
        fake_ile_sync.assert_not_called()

    def test_scheduled_task_runs_enabled_resource(self):
        integration = _integration("tk-enabled", resources={_ILE: {"enabled": True}})
        sync_business_central_resource_task.run(integration.id, _ILE, "scheduled")
        fake_ile_sync.assert_called_once_with(integration, trigger_type="scheduled")

    def test_task_skips_resource_without_implementation(self):
        integration = _integration("tk-unimpl", resources={_SO: {"enabled": True}})
        with mock.patch(
            "apps.scm.integrations.business_systems.business_central.sync.sync_purchase_orders_from_business_central"
        ) as po_sync:
            sync_business_central_resource_task.run(integration.id, _SO, "manual")
        po_sync.assert_not_called()
        self.assertFalse(integration.sync_runs.exists())
