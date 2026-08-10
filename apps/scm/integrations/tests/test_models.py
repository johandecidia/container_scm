"""Tests for delete/cascade/protect behavior in integration models.

Basic model tests (create, str, defaults, unique constraints) are covered in
test_integration_models.py. This file covers cascade/protect delete semantics
and relation direction tests.
"""

from django.db import IntegrityError
from django.test import TestCase

from apps.scm.integrations.models import (
    Integration,
    IntegrationCredential,
    IntegrationRequestLog,
    IntegrationWebhookEvent,
)
from apps.teams.models import Team


def _team(slug):
    return Team.objects.create(name=slug, slug=slug)


def _integration(team, provider_code="carrier-x", name="Carrier X"):
    return Integration.objects.create(team=team, name=name, provider_code=provider_code)


class IntegrationCascadeDeleteTest(TestCase):
    def test_deleting_integration_cascades_credential(self):
        team = _team("cascade-cred-team")
        integration = _integration(team, provider_code="cred-carrier")
        IntegrationCredential.objects.create(
            team=team, integration=integration, auth_type=IntegrationCredential.AuthType.API_KEY
        )
        integration_pk = integration.pk
        integration.delete()
        self.assertEqual(IntegrationCredential.objects.filter(integration_id=integration_pk).count(), 0)

    def test_deleting_integration_nullifies_request_logs(self):
        team = _team("cascade-log-team")
        integration = _integration(team, provider_code="log-carrier")
        log = IntegrationRequestLog.objects.create(
            team=team,
            integration=integration,
            provider_code="log-carrier",
            method="GET",
            endpoint="/api/tracking",
        )
        integration.delete()
        log.refresh_from_db()
        self.assertIsNone(log.integration)

    def test_deleting_integration_nullifies_webhook_events(self):
        team = _team("cascade-webhook-team")
        integration = _integration(team, provider_code="webhook-carrier")
        event = IntegrationWebhookEvent.objects.create(
            team=team, integration=integration, provider_code="webhook-carrier", payload={}
        )
        integration.delete()
        event.refresh_from_db()
        self.assertIsNone(event.integration)


class IntegrationCredentialRelationTest(TestCase):
    def test_one_to_one_prevents_duplicate_credential(self):
        team = _team("onetoone-team")
        integration = _integration(team, provider_code="onetoone-carrier")
        IntegrationCredential.objects.create(
            team=team, integration=integration, auth_type=IntegrationCredential.AuthType.API_KEY
        )
        with self.assertRaises(IntegrityError):
            IntegrationCredential.objects.create(
                team=team, integration=integration, auth_type=IntegrationCredential.AuthType.OAUTH2
            )

    def test_credential_reverse_relation_on_integration(self):
        team = _team("cred-rev-team")
        integration = _integration(team, provider_code="cred-rev-carrier")
        cred = IntegrationCredential.objects.create(
            team=team, integration=integration, auth_type=IntegrationCredential.AuthType.BEARER
        )
        self.assertEqual(integration.credential, cred)


class IntegrationRequestLogRelationTest(TestCase):
    def test_request_log_reverse_relation_on_integration(self):
        team = _team("log-rev-team")
        integration = _integration(team, provider_code="log-rev-carrier")
        log = IntegrationRequestLog.objects.create(
            team=team,
            integration=integration,
            provider_code="log-rev-carrier",
            method="POST",
            endpoint="/api/subscribe",
        )
        self.assertIn(log, integration.request_logs.all())


class IntegrationWebhookEventRelationTest(TestCase):
    def test_webhook_event_reverse_relation_on_integration(self):
        team = _team("wh-rev-team")
        integration = _integration(team, provider_code="wh-rev-carrier")
        event = IntegrationWebhookEvent.objects.create(
            team=team,
            integration=integration,
            provider_code="wh-rev-carrier",
            payload={"event": "test"},
        )
        self.assertIn(event, integration.webhook_events.all())
