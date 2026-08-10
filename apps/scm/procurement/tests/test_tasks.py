"""Tests for the Business Central purchase-order sync Celery task."""

from unittest import mock

from celery.exceptions import Retry
from django.test import TestCase

from apps.scm.integrations.business_systems.business_central.exceptions import (
    BusinessCentralConfigurationError,
    BusinessCentralError,
)
from apps.scm.integrations.models import Integration, IntegrationSyncRun
from apps.scm.procurement.tasks import sync_purchase_orders_from_bc
from apps.teams.models import Team

_SYNC_PATH = "apps.scm.integrations.business_systems.business_central.sync.sync_purchase_orders_from_business_central"


def _team(slug="task-team"):
    return Team.objects.create(name=slug, slug=slug)


def _integration(team):
    return Integration.objects.create(
        team=team,
        name="BC",
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        is_active=True,
        config={"sync_enabled": True},
    )


class SyncTaskTest(TestCase):
    def test_success_calls_sync_with_integration(self):
        team = _team()
        integration = _integration(team)
        fake_run = mock.Mock(spec=IntegrationSyncRun)
        fake_run.status = "completed"
        fake_run.records_created = 2
        fake_run.records_updated = 0
        fake_run.records_unchanged = 0
        fake_run.records_failed = 0
        with mock.patch(_SYNC_PATH, return_value=fake_run) as sync:
            sync_purchase_orders_from_bc(team.id)
        sync.assert_called_once_with(integration)

    def test_missing_team_is_skipped(self):
        with mock.patch(_SYNC_PATH) as sync:
            sync_purchase_orders_from_bc(999999)
        sync.assert_not_called()

    def test_no_active_integration_is_skipped(self):
        team = _team("task-team-no-int")
        with mock.patch(_SYNC_PATH) as sync:
            sync_purchase_orders_from_bc(team.id)
        sync.assert_not_called()

    def test_inactive_integration_is_skipped(self):
        team = _team("task-team-inactive")
        integration = _integration(team)
        integration.is_active = False
        integration.save(update_fields=["is_active"])
        with mock.patch(_SYNC_PATH) as sync:
            sync_purchase_orders_from_bc(team.id)
        sync.assert_not_called()

    def test_configuration_error_not_retried(self):
        team = _team("task-team-config")
        _integration(team)
        with (
            mock.patch(_SYNC_PATH, side_effect=BusinessCentralConfigurationError("bad")),
            mock.patch.object(sync_purchase_orders_from_bc, "retry", side_effect=Retry()) as retry,
        ):
            # Should NOT raise / retry — permanent config problem is logged and skipped.
            sync_purchase_orders_from_bc(team.id)
        retry.assert_not_called()

    def test_transient_error_is_retried(self):
        team = _team("task-team-transient")
        _integration(team)
        with (
            mock.patch(_SYNC_PATH, side_effect=BusinessCentralError("timeout")),
            mock.patch.object(sync_purchase_orders_from_bc, "retry", side_effect=Retry()) as retry,
            self.assertRaises(Retry),
        ):
            sync_purchase_orders_from_bc(team.id)
        retry.assert_called_once()
