"""Tests for the carrier webhook intake view."""

from django.test import RequestFactory, TestCase

from apps.scm.integrations.models import IntegrationWebhookEvent
from apps.scm.integrations.webhooks import carrier_webhook
from apps.teams.models import Team


def _team(slug):
    return Team.objects.create(name=slug, slug=slug)


class CarrierWebhookKnownProviderTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.team = _team("webhook-view-team")

    def test_known_provider_returns_202(self):
        request = self.factory.post(
            "/fake/",
            data=b'{"eventType": "EQUIPMENT"}',
            content_type="application/json",
        )
        response = carrier_webhook(request, team_slug=self.team.slug, provider_code="maersk")
        self.assertEqual(response.status_code, 202)

    def test_webhook_event_created_after_202(self):
        payload = b'{"eventType": "EQUIPMENT", "eventID": "evt-test-001"}'
        request = self.factory.post("/fake/", data=payload, content_type="application/json")
        carrier_webhook(request, team_slug=self.team.slug, provider_code="maersk")
        self.assertTrue(IntegrationWebhookEvent.objects.filter(team=self.team, provider_code="maersk").exists())

    def test_webhook_event_provider_code_matches(self):
        payload = b'{"eventType": "TRANSPORT"}'
        request = self.factory.post("/fake/", data=payload, content_type="application/json")
        carrier_webhook(request, team_slug=self.team.slug, provider_code="maersk")
        event = IntegrationWebhookEvent.objects.filter(team=self.team, provider_code="maersk").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.provider_code, "maersk")

    def test_payload_stored_correctly(self):
        payload_dict = {"eventType": "EQUIPMENT", "eventID": "evt-payload-test"}
        import json

        request = self.factory.post(
            "/fake/",
            data=json.dumps(payload_dict).encode(),
            content_type="application/json",
        )
        carrier_webhook(request, team_slug=self.team.slug, provider_code="maersk")
        event = IntegrationWebhookEvent.objects.filter(team=self.team, provider_code="maersk").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.payload, payload_dict)

    def test_empty_body_stored_as_empty_dict(self):
        request = self.factory.post("/fake/", data=b"", content_type="application/json")
        carrier_webhook(request, team_slug=self.team.slug, provider_code="msc")
        event = IntegrationWebhookEvent.objects.filter(team=self.team, provider_code="msc").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.payload, {})

    def test_default_status_is_received(self):
        request = self.factory.post(
            "/fake/",
            data=b'{"eventType": "SHIPMENT"}',
            content_type="application/json",
        )
        carrier_webhook(request, team_slug=self.team.slug, provider_code="cma_cgm")
        event = IntegrationWebhookEvent.objects.filter(team=self.team, provider_code="cma_cgm").first()
        self.assertEqual(event.status, IntegrationWebhookEvent.Status.RECEIVED)


class CarrierWebhookUnknownProviderTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.team = _team("webhook-unknown-team")

    def test_unknown_provider_returns_404(self):
        request = self.factory.post(
            "/fake/",
            data=b'{"eventType": "EQUIPMENT"}',
            content_type="application/json",
        )
        response = carrier_webhook(request, team_slug=self.team.slug, provider_code="unknown_carrier")
        self.assertEqual(response.status_code, 404)

    def test_no_webhook_event_created_for_unknown_provider(self):
        initial_count = IntegrationWebhookEvent.objects.count()
        request = self.factory.post("/fake/", data=b"{}", content_type="application/json")
        carrier_webhook(request, team_slug=self.team.slug, provider_code="nonexistent_xyz")
        self.assertEqual(IntegrationWebhookEvent.objects.count(), initial_count)
