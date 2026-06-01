"""Tests for integration selectors — team isolation is critical."""

from django.test import TestCase

from apps.scm.integrations.models import Integration, IntegrationRequestLog, IntegrationWebhookEvent
from apps.scm.integrations.selectors import (
    get_active_team_integrations,
    get_recent_request_logs,
    get_team_integration_by_provider,
    get_team_integrations,
    get_unprocessed_webhook_events,
)
from apps.teams.models import Team


def _team(slug):
    return Team.objects.create(name=slug, slug=slug)


def _integration(team, provider_code, name=None, status=Integration.Status.INACTIVE, is_active=True):
    return Integration.objects.create(
        team=team,
        name=name or f"{provider_code} integration",
        provider_code=provider_code,
        status=status,
        is_active=is_active,
    )


class GetTeamIntegrationsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sel-int-team")
        cls.other_team = _team("sel-int-other-team")
        cls.own = _integration(cls.team, provider_code="maersk")
        cls.other = _integration(cls.other_team, provider_code="msc")

    def test_returns_own_team_integrations(self):
        qs = get_team_integrations(self.team)
        self.assertIn(self.own, qs)

    def test_does_not_return_other_team_integrations(self):
        qs = get_team_integrations(self.team)
        self.assertNotIn(self.other, qs)


class GetActiveTeamIntegrationsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sel-active-team")
        cls.active = _integration(
            cls.team,
            provider_code="maersk",
            status=Integration.Status.ACTIVE,
            is_active=True,
        )
        cls.inactive = _integration(
            cls.team,
            provider_code="msc",
            status=Integration.Status.INACTIVE,
            is_active=False,
        )
        cls.error = _integration(
            cls.team,
            provider_code="cosco",
            status=Integration.Status.ERROR,
            is_active=True,
        )

    def test_returns_active_integrations(self):
        qs = get_active_team_integrations(self.team)
        self.assertIn(self.active, qs)

    def test_does_not_return_inactive_integrations(self):
        qs = get_active_team_integrations(self.team)
        self.assertNotIn(self.inactive, qs)

    def test_does_not_return_error_integrations(self):
        qs = get_active_team_integrations(self.team)
        self.assertNotIn(self.error, qs)


class GetTeamIntegrationByProviderTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sel-by-prov-team")
        cls.other_team = _team("sel-by-prov-other-team")
        cls.own = _integration(cls.team, provider_code="hapag_lloyd")
        cls.other = _integration(cls.other_team, provider_code="hapag_lloyd")

    def test_returns_own_team_integration(self):
        result = get_team_integration_by_provider(self.team, "hapag_lloyd")
        self.assertEqual(result, self.own)

    def test_raises_does_not_exist_for_other_team_provider(self):
        with self.assertRaises(Integration.DoesNotExist):
            get_team_integration_by_provider(self.team, "zim")

    def test_raises_does_not_exist_for_missing_provider(self):
        with self.assertRaises(Integration.DoesNotExist):
            get_team_integration_by_provider(self.team, "nonexistent_provider")


class GetRecentRequestLogsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sel-log-team")
        cls.other_team = _team("sel-log-other-team")
        cls.own_log = IntegrationRequestLog.objects.create(
            team=cls.team,
            provider_code="maersk",
            method="GET",
            endpoint="/tracking",
        )
        cls.other_log = IntegrationRequestLog.objects.create(
            team=cls.other_team,
            provider_code="msc",
            method="GET",
            endpoint="/tracking",
        )

    def test_returns_own_team_logs(self):
        logs = get_recent_request_logs(self.team)
        self.assertIn(self.own_log, logs)

    def test_does_not_return_other_team_logs(self):
        logs = get_recent_request_logs(self.team)
        self.assertNotIn(self.other_log, logs)

    def test_limited_to_fifty_results(self):
        team = _team("sel-log-limit-team")
        for i in range(60):
            IntegrationRequestLog.objects.create(
                team=team,
                provider_code="msc",
                method="GET",
                endpoint=f"/tracking/{i}",
            )
        logs = get_recent_request_logs(team)
        self.assertLessEqual(len(list(logs)), 50)


class GetUnprocessedWebhookEventsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sel-webhook-team")
        cls.other_team = _team("sel-webhook-other-team")
        cls.received = IntegrationWebhookEvent.objects.create(
            team=cls.team,
            provider_code="maersk",
            payload={"eventType": "EQUIPMENT"},
            status=IntegrationWebhookEvent.Status.RECEIVED,
        )
        cls.processed = IntegrationWebhookEvent.objects.create(
            team=cls.team,
            provider_code="maersk",
            payload={"eventType": "TRANSPORT"},
            status=IntegrationWebhookEvent.Status.PROCESSED,
        )
        cls.other_received = IntegrationWebhookEvent.objects.create(
            team=cls.other_team,
            provider_code="msc",
            payload={"eventType": "EQUIPMENT"},
            status=IntegrationWebhookEvent.Status.RECEIVED,
        )

    def test_returns_received_events_for_team(self):
        qs = get_unprocessed_webhook_events(self.team)
        self.assertIn(self.received, qs)

    def test_does_not_return_processed_events(self):
        qs = get_unprocessed_webhook_events(self.team)
        self.assertNotIn(self.processed, qs)

    def test_does_not_return_other_team_events(self):
        qs = get_unprocessed_webhook_events(self.team)
        self.assertNotIn(self.other_received, qs)
