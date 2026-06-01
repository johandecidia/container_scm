"""Tests for integration Django models."""

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


def _integration(team, provider_code="maersk", name="Maersk Integration"):
    return Integration.objects.create(team=team, name=name, provider_code=provider_code)


class IntegrationModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("int-model-team")

    def test_create_integration(self):
        integration = _integration(self.team)
        self.assertIsNotNone(integration.pk)

    def test_str_contains_name_and_provider_code(self):
        integration = _integration(self.team, provider_code="msc", name="MSC Integration")
        self.assertIn("MSC Integration", str(integration))
        self.assertIn("msc", str(integration))

    def test_default_status_is_inactive(self):
        integration = _integration(self.team, provider_code="cosco", name="COSCO")
        self.assertEqual(integration.status, Integration.Status.INACTIVE)

    def test_is_active_default_true(self):
        integration = _integration(self.team, provider_code="one", name="ONE")
        self.assertTrue(integration.is_active)

    def test_unique_together_team_and_provider_code(self):
        _integration(self.team, provider_code="hapag_lloyd", name="Hapag-Lloyd")
        with self.assertRaises(IntegrityError):
            Integration.objects.create(team=self.team, name="Hapag Duplicate", provider_code="hapag_lloyd")


class IntegrationCredentialModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("cred-model-team")
        cls.integration = _integration(cls.team, provider_code="maersk", name="Maersk Cred Test")

    def test_create_credential_linked_to_integration(self):
        cred = IntegrationCredential.objects.create(
            team=self.team,
            integration=self.integration,
            auth_type=IntegrationCredential.AuthType.API_KEY,
        )
        self.assertIsNotNone(cred.pk)
        self.assertEqual(cred.integration, self.integration)

    def test_credential_str_contains_integration_name(self):
        cred = IntegrationCredential.objects.create(
            team=self.team,
            integration=self.integration,
            auth_type=IntegrationCredential.AuthType.OAUTH2,
        )
        self.assertIn("Maersk Cred Test", str(cred))

    def test_encrypted_data_blank_by_default(self):
        cred = IntegrationCredential.objects.create(
            team=self.team,
            integration=self.integration,
            auth_type=IntegrationCredential.AuthType.BEARER,
        )
        self.assertEqual(cred.encrypted_data, "")


class IntegrationRequestLogModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("log-model-team")
        cls.integration = _integration(cls.team, provider_code="msc", name="MSC Log Test")

    def test_create_request_log_without_sensitive_data(self):
        log = IntegrationRequestLog.objects.create(
            team=self.team,
            integration=self.integration,
            provider_code="msc",
            method="GET",
            endpoint="/tracking/containers",
            status_code=200,
            success=True,
        )
        self.assertIsNotNone(log.pk)
        self.assertEqual(log.provider_code, "msc")
        self.assertEqual(log.method, "GET")
        self.assertTrue(log.success)

    def test_request_log_has_no_token_fields(self):
        """Ensure the model does not expose token or password fields."""
        log = IntegrationRequestLog.objects.create(
            team=self.team,
            provider_code="msc",
            method="POST",
            endpoint="/tracking/subscribe",
        )
        self.assertFalse(hasattr(log, "token"))
        self.assertFalse(hasattr(log, "password"))
        self.assertFalse(hasattr(log, "api_key"))

    def test_create_log_without_integration_foreign_key(self):
        log = IntegrationRequestLog.objects.create(
            team=self.team,
            provider_code="cosco",
            method="GET",
            endpoint="/v1/tracking",
        )
        self.assertIsNone(log.integration)


class IntegrationWebhookEventModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("webhook-model-team")
        cls.integration = _integration(cls.team, provider_code="maersk", name="Maersk Webhook Test")

    def test_create_webhook_event_saves_payload(self):
        payload = {"eventType": "EQUIPMENT", "eventID": "evt-abc"}
        event = IntegrationWebhookEvent.objects.create(
            team=self.team,
            integration=self.integration,
            provider_code="maersk",
            payload=payload,
        )
        self.assertIsNotNone(event.pk)
        self.assertEqual(event.payload, payload)

    def test_default_status_is_received(self):
        event = IntegrationWebhookEvent.objects.create(
            team=self.team,
            provider_code="maersk",
            payload={"data": "test"},
        )
        self.assertEqual(event.status, IntegrationWebhookEvent.Status.RECEIVED)

    def test_str_contains_provider_code(self):
        event = IntegrationWebhookEvent.objects.create(
            team=self.team,
            provider_code="cma_cgm",
            payload={},
        )
        self.assertIn("cma_cgm", str(event))
