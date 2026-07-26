"""End-to-end proof of the Maersk vertical.

Drives the real sync engine against a configured Maersk integration with an
injected HTTP session: subscription → carrier call → stored raw response →
normalised events → subscription state. Nothing here is mocked except the socket
layer, so this is the closest the suite gets to a live sync.
"""

import json
import pathlib
from unittest import mock

import requests
from django.test import TestCase, override_settings

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.carriers.maersk.client import MaerskClient
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential, IntegrationRequestLog
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.models import (
    TrackingEvent,
    TrackingProvider,
    TrackingRawPayload,
    TrackingSubscription,
    TrackingSyncRun,
)
from apps.scm.tracking.sync import sync_tracking_subscription
from apps.teams.models import Team

FIXTURES = pathlib.Path(__file__).parents[2] / "integrations" / "tests" / "fixtures" / "carriers"
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "maersk-pipeline"}}

API_KEY = "pipeline-secret-key"
CONTAINER_NUMBER = "MRKU1234567"

MAERSK_CONFIG = {
    "base_url": "https://example.invalid/maersk",
    "tracking_path": "/track-and-trace/events",
    "auth_style": "api_key_header",
    "api_key_header_name": "Consumer-Key",
    "reference_params": {"container_number": "equipmentReference"},
    "test_connection_reference": CONTAINER_NUMBER,
    "max_retries": 0,
    "retry_backoff_seconds": 0,
    "min_poll_interval_minutes": 45,
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"url": url, "headers": headers or {}, "params": params or {}})
        if self.error is not None:
            raise self.error
        return self.responses.pop(0) if self.responses else FakeResponse(200, {"events": []})


def _maersk_payload() -> dict:
    return json.loads((FIXTURES / "maersk_tracking_response.json").read_text())


@override_settings(CACHES=_LOCMEM)
class MaerskSyncPipelineTest(TestCase):
    """A configured Maersk integration produces normalised, stored tracking data."""

    def setUp(self):
        self.team = Team.objects.create(name="maersk-pipeline", slug="maersk-pipeline")
        self.integration = Integration.objects.create(
            team=self.team,
            name="Maersk",
            provider_code="maersk",
            provider_family=Integration.ProviderFamily.CARRIER,
            api_style=Integration.ApiStyle.DCSA,
            config=MAERSK_CONFIG,
            is_active=True,
        )
        set_integration_credentials(self.integration, IntegrationCredential.AuthType.API_KEY, {"api_key": API_KEY})
        self.provider = TrackingProvider.objects.create(code="maersk", name="Maersk")
        equipment_type = EquipmentType.objects.get_or_create(
            iso_code="22G1",
            defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
        )[0]
        self.container = Container.objects.create(
            team=self.team,
            owner_code="MRK",
            category_id="U",
            serial_number="123456",
            check_digit=3,
            equipment_type=equipment_type,
        )
        self.shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-MAERSK-1", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=self.shipment, container=self.container)
        self.subscription = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.provider,
            container=self.container,
            shipment=self.shipment,
            tracking_reference=CONTAINER_NUMBER,
            reference_type=TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
        )

    def _sync(self, session):
        """Run the real sync engine with a Maersk client bound to a fake session."""
        client = MaerskClient(self.integration, session=session)
        with mock.patch(
            "apps.scm.integrations.carriers.factory.build_carrier_client",
            return_value=client,
        ):
            return sync_tracking_subscription(self.subscription)

    def test_sync_succeeds_and_creates_events(self):
        run = self._sync(FakeSession([FakeResponse(200, _maersk_payload())]))
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.events_created, 2)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 2)

    def test_the_carrier_was_called_with_the_configured_parameter(self):
        session = FakeSession([FakeResponse(200, _maersk_payload())])
        self._sync(session)
        self.assertEqual(session.requests[0]["params"], {"equipmentReference": CONTAINER_NUMBER})
        self.assertEqual(session.requests[0]["headers"]["Consumer-Key"], API_KEY)

    def test_raw_response_is_stored_and_marked_parsed(self):
        payload = _maersk_payload()
        self._sync(FakeSession([FakeResponse(200, payload)]))
        stored = TrackingRawPayload.objects.get(team=self.team)
        self.assertEqual(stored.payload_json, payload)
        self.assertTrue(stored.parsed_successfully)
        self.assertTrue(stored.payload_hash)

    def test_events_link_back_to_the_stored_payload(self):
        self._sync(FakeSession([FakeResponse(200, _maersk_payload())]))
        stored = TrackingRawPayload.objects.get(team=self.team)
        for event in TrackingEvent.objects.filter(team=self.team):
            self.assertEqual(event.raw_payload_id, stored.pk)

    def test_events_are_normalised_with_vessel_and_location(self):
        self._sync(FakeSession([FakeResponse(200, _maersk_payload())]))
        loaded = TrackingEvent.objects.get(team=self.team, source_event_id="MAERSK-EVT-001")
        self.assertEqual(loaded.event_type, TrackingEvent.EventType.LOADED_ON_VESSEL)
        self.assertTrue(loaded.is_actual)
        self.assertEqual(loaded.vessel_name, "MAERSK EINDHOVEN")
        self.assertEqual(loaded.voyage_number, "213E")
        self.assertEqual(loaded.location_unlocode, "GBFXT")
        self.assertEqual(loaded.transport_mode, TrackingEvent.TransportMode.VESSEL)

    def test_estimated_arrival_is_not_recorded_as_actual(self):
        self._sync(FakeSession([FakeResponse(200, _maersk_payload())]))
        arrival = TrackingEvent.objects.get(team=self.team, source_event_id="MAERSK-EVT-002")
        self.assertEqual(arrival.event_type, TrackingEvent.EventType.VESSEL_ARRIVED)
        self.assertTrue(arrival.is_estimated)
        self.assertFalse(arrival.is_actual)

    def test_events_are_linked_to_the_container_and_shipment(self):
        self._sync(FakeSession([FakeResponse(200, _maersk_payload())]))
        for event in TrackingEvent.objects.filter(team=self.team):
            self.assertEqual(event.container_id, self.container.pk)
            self.assertEqual(event.shipment_id, self.shipment.pk)

    def test_subscription_moves_to_tracking(self):
        self._sync(FakeSession([FakeResponse(200, _maersk_payload())]))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)
        self.assertIsNotNone(self.subscription.last_event_at)

    def test_carrier_minimum_poll_interval_is_honoured(self):
        from datetime import timedelta

        from django.utils import timezone

        self._sync(FakeSession([FakeResponse(200, _maersk_payload())]))
        self.subscription.refresh_from_db()
        self.assertGreater(self.subscription.next_sync_at, timezone.now() + timedelta(minutes=40))

    def test_second_sync_of_the_same_payload_adds_no_duplicates(self):
        self._sync(FakeSession([FakeResponse(200, _maersk_payload())]))
        run = self._sync(FakeSession([FakeResponse(200, _maersk_payload())]))
        self.assertEqual(run.events_created, 0)
        self.assertEqual(run.events_updated, 2)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 2)

    def test_request_is_logged_without_the_api_key(self):
        self._sync(FakeSession([FakeResponse(200, _maersk_payload())]))
        log = IntegrationRequestLog.objects.get(team=self.team)
        self.assertTrue(log.success)
        self.assertNotIn(API_KEY, log.endpoint + log.error_message)

    def test_no_data_is_a_successful_sync_with_no_events(self):
        run = self._sync(FakeSession([FakeResponse(404)]))
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.events_created, 0)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.NO_DATA)
        self.assertEqual(self.subscription.consecutive_failures, 0)

    def test_authentication_failure_is_classified_and_creates_no_events(self):
        run = self._sync(FakeSession([FakeResponse(401), FakeResponse(401)]))
        self.assertEqual(run.status, TrackingSyncRun.Status.FAILED)
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.AUTHENTICATION)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)

    def test_timeout_is_classified_as_a_transient_failure(self):
        run = self._sync(FakeSession(error=requests.Timeout("timed out")))
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.TIMEOUT)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.consecutive_failures, 1)

    def test_rate_limit_is_classified_and_defers_the_next_poll(self):
        from datetime import timedelta

        from django.utils import timezone

        run = self._sync(FakeSession([FakeResponse(429, headers={"Retry-After": "3600"})]))
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.RATE_LIMIT)
        self.subscription.refresh_from_db()
        self.assertGreaterEqual(self.subscription.next_sync_at, timezone.now() + timedelta(minutes=55))

    def test_unconfigured_integration_is_skipped_not_reported_as_empty(self):
        self.integration.config = {}
        self.integration.save(update_fields=["config"])
        run = self._sync(FakeSession([FakeResponse(200, _maersk_payload())]))
        self.assertEqual(run.status, TrackingSyncRun.Status.SKIPPED)
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.NOT_CONFIGURED)
        self.assertEqual(TrackingRawPayload.objects.filter(team=self.team).count(), 0)

    def test_events_stay_inside_the_team(self):
        other_team = Team.objects.create(name="maersk-pipeline-other", slug="maersk-pipeline-other")
        self._sync(FakeSession([FakeResponse(200, _maersk_payload())]))
        self.assertEqual(TrackingEvent.objects.filter(team=other_team).count(), 0)
        self.assertEqual(TrackingRawPayload.objects.filter(team=other_team).count(), 0)
        self.assertEqual(IntegrationRequestLog.objects.filter(team=other_team).count(), 0)
