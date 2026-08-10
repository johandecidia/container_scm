"""Tests for the scheduled Business Central sync dispatcher."""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.scm.integrations.models import Integration, IntegrationSyncRun
from apps.scm.integrations.services import get_due_business_central_integrations
from apps.scm.integrations.tasks import sync_enabled_business_central_integrations_task
from apps.teams.models import Team

_DISPATCH_TARGET = "apps.scm.integrations.tasks.sync_business_central_purchase_orders_task.delay"


def _integration(slug, *, active=True, sync_enabled=True, interval=30, provider="business_central", family=None):
    team = Team.objects.create(name=slug, slug=slug)
    family = family if family is not None else Integration.ProviderFamily.BUSINESS_SYSTEM
    return Integration.objects.create(
        team=team,
        name="BC",
        provider_code=provider,
        provider_family=family,
        is_active=active,
        config={"sync_enabled": sync_enabled, "purchase_order_sync_interval_minutes": interval},
    )


def _run(integration, *, status, minutes_ago):
    ts = timezone.now() - timedelta(minutes=minutes_ago)
    return IntegrationSyncRun.objects.create(
        team=integration.team,
        integration=integration,
        resource_type=IntegrationSyncRun.ResourceType.PURCHASE_ORDERS,
        status=status,
        started_at=ts,
        finished_at=ts if status != IntegrationSyncRun.Status.RUNNING else None,
    )


class DueDetectionTest(TestCase):
    def test_never_synced_is_due(self):
        integration = _integration("d-never")
        self.assertIn(integration, get_due_business_central_integrations())

    def test_recent_success_not_due(self):
        integration = _integration("d-recent", interval=30)
        _run(integration, status=IntegrationSyncRun.Status.COMPLETED, minutes_ago=5)
        self.assertNotIn(integration, get_due_business_central_integrations())

    def test_old_success_is_due(self):
        integration = _integration("d-old", interval=30)
        _run(integration, status=IntegrationSyncRun.Status.COMPLETED, minutes_ago=45)
        self.assertIn(integration, get_due_business_central_integrations())

    def test_disabled_not_due(self):
        integration = _integration("d-disabled", sync_enabled=False)
        self.assertNotIn(integration, get_due_business_central_integrations())

    def test_inactive_not_due(self):
        integration = _integration("d-inactive", active=False)
        self.assertNotIn(integration, get_due_business_central_integrations())

    def test_wrong_provider_ignored(self):
        integration = _integration("d-carrier", family=Integration.ProviderFamily.CARRIER, provider="maersk")
        self.assertNotIn(integration, get_due_business_central_integrations())

    def test_running_is_skipped(self):
        integration = _integration("d-running")
        _run(integration, status=IntegrationSyncRun.Status.RUNNING, minutes_ago=1)
        self.assertNotIn(integration, get_due_business_central_integrations())

    def test_stale_running_is_due(self):
        integration = _integration("d-stale")
        _run(integration, status=IntegrationSyncRun.Status.RUNNING, minutes_ago=200)
        self.assertIn(integration, get_due_business_central_integrations())

    def test_failure_backoff(self):
        integration = _integration("d-failed", interval=30)
        # Failed 5 min ago; within the 15-min backoff → not due.
        _run(integration, status=IntegrationSyncRun.Status.FAILED, minutes_ago=5)
        self.assertNotIn(integration, get_due_business_central_integrations())
        # Failed 20 min ago; past backoff → due.
        integration2 = _integration("d-failed2", interval=30)
        _run(integration2, status=IntegrationSyncRun.Status.FAILED, minutes_ago=20)
        self.assertIn(integration2, get_due_business_central_integrations())

    def test_interval_respected_per_integration(self):
        short = _integration("d-short", interval=10)
        _run(short, status=IntegrationSyncRun.Status.COMPLETED, minutes_ago=15)
        long = _integration("d-long", interval=60)
        _run(long, status=IntegrationSyncRun.Status.COMPLETED, minutes_ago=15)
        due = get_due_business_central_integrations()
        self.assertIn(short, due)
        self.assertNotIn(long, due)


class DispatcherTaskTest(TestCase):
    def test_queues_only_due_integrations(self):
        due = _integration("q-due")
        not_due = _integration("q-notdue", interval=30)
        _run(not_due, status=IntegrationSyncRun.Status.COMPLETED, minutes_ago=1)
        with mock.patch(_DISPATCH_TARGET) as delay:
            result = sync_enabled_business_central_integrations_task()
        self.assertEqual(result["queued"], 1)
        delay.assert_called_once_with(due.id, "scheduled")
