"""Tests for integration services — lifecycle, logging, and connection testing."""

from django.test import TestCase
from django.utils import timezone

from apps.scm.integrations.models import Integration, IntegrationRequestLog
from apps.scm.integrations.services import (
    log_integration_request,
    mark_integration_error,
    mark_integration_success,
    test_integration_connection,
)
from apps.teams.models import Team


def _team(slug):
    return Team.objects.create(name=slug, slug=slug)


def _integration(team, provider_code="maersk", name=None):
    return Integration.objects.create(
        team=team,
        name=name or f"{provider_code} integration",
        provider_code=provider_code,
    )


class TestIntegrationConnectionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("svc-conn-team")

    def test_updates_last_tested_at(self):
        integration = _integration(self.team, provider_code="maersk")
        self.assertIsNone(integration.last_tested_at)
        before = timezone.now()
        test_integration_connection(integration)
        integration.refresh_from_db()
        self.assertIsNotNone(integration.last_tested_at)
        self.assertGreaterEqual(integration.last_tested_at, before)

    def test_returns_success_false_because_not_implemented(self):
        """All carrier clients raise NotImplementedError — result must have success=False."""
        integration = _integration(self.team, provider_code="msc")
        result = test_integration_connection(integration)
        self.assertFalse(result["success"])

    def test_last_error_message_set_after_failure(self):
        integration = _integration(self.team, provider_code="cosco")
        test_integration_connection(integration)
        integration.refresh_from_db()
        self.assertTrue(len(integration.last_error_message) > 0)

    def test_status_set_to_error_after_failure(self):
        integration = _integration(self.team, provider_code="one")
        test_integration_connection(integration)
        integration.refresh_from_db()
        self.assertEqual(integration.status, Integration.Status.ERROR)

    def test_result_has_message_key(self):
        integration = _integration(self.team, provider_code="zim")
        result = test_integration_connection(integration)
        self.assertIn("message", result)


class MarkIntegrationSuccessTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("svc-success-team")

    def test_mark_success_sets_status_active(self):
        integration = _integration(self.team, provider_code="hapag_lloyd")
        mark_integration_success(integration)
        integration.refresh_from_db()
        self.assertEqual(integration.status, Integration.Status.ACTIVE)

    def test_mark_success_sets_last_success_at(self):
        integration = _integration(self.team, provider_code="evergreen")
        before = timezone.now()
        mark_integration_success(integration)
        integration.refresh_from_db()
        self.assertIsNotNone(integration.last_success_at)
        self.assertGreaterEqual(integration.last_success_at, before)

    def test_mark_success_clears_last_error_message(self):
        integration = _integration(self.team, provider_code="hmm")
        integration.last_error_message = "Some previous error"
        integration.save()
        mark_integration_success(integration)
        integration.refresh_from_db()
        self.assertEqual(integration.last_error_message, "")


class MarkIntegrationErrorTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("svc-error-team")

    def test_mark_error_sets_status_error(self):
        integration = _integration(self.team, provider_code="yang_ming")
        mark_integration_error(integration, "Connection refused")
        integration.refresh_from_db()
        self.assertEqual(integration.status, Integration.Status.ERROR)

    def test_mark_error_records_error_message(self):
        integration = _integration(self.team, provider_code="cma_cgm")
        mark_integration_error(integration, "Timeout after 30s")
        integration.refresh_from_db()
        self.assertEqual(integration.last_error_message, "Timeout after 30s")

    def test_mark_error_sets_last_error_at(self):
        integration = _integration(self.team, provider_code="zim")
        before = timezone.now()
        mark_integration_error(integration, "Auth failed")
        integration.refresh_from_db()
        self.assertIsNotNone(integration.last_error_at)
        self.assertGreaterEqual(integration.last_error_at, before)


class LogIntegrationRequestTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("svc-log-team")
        cls.integration = _integration(cls.team, provider_code="maersk", name="Maersk Log Svc Test")

    def test_creates_an_integration_request_log(self):
        log = log_integration_request(
            team=self.team,
            provider_code="maersk",
            method="GET",
            endpoint="/v2/track/BKG123",
            integration=self.integration,
            status_code=200,
            duration_ms=150,
            success=True,
        )
        self.assertIsNotNone(log.pk)
        self.assertIsInstance(log, IntegrationRequestLog)

    def test_log_fields_are_persisted(self):
        log = log_integration_request(
            team=self.team,
            provider_code="msc",
            method="POST",
            endpoint="/api/subscribe",
            status_code=422,
            success=False,
            error_message="Validation error",
        )
        saved = IntegrationRequestLog.objects.get(pk=log.pk)
        self.assertEqual(saved.method, "POST")
        self.assertEqual(saved.endpoint, "/api/subscribe")
        self.assertEqual(saved.status_code, 422)
        self.assertFalse(saved.success)
        self.assertEqual(saved.error_message, "Validation error")

    def test_log_can_be_created_without_integration_fk(self):
        log = log_integration_request(
            team=self.team,
            provider_code="cosco",
            method="GET",
            endpoint="/tracking",
        )
        self.assertIsNone(log.integration)
