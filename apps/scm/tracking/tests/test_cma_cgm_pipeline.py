"""End-to-end proof of the CMA CGM vertical.

Drives the real sync engine against a configured CMA CGM integration with an injected
HTTP session: subscription → carrier call → stored raw response → normalised events →
subscription state. The same engine, parser and persistence Maersk uses; only the
configuration differs, which is the point of carrier #2.

Nothing here is mocked except the HTTP session, so this is the closest the suite gets
to a live CMA CGM sync.
"""

import json
import pathlib
from unittest import mock

from django.test import TestCase, override_settings

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.carriers.cma_cgm.client import (
    PROVIDER_CODE,
    PUBLIC_TRACK_AND_TRACE_CONFIG,
    CmaCgmClient,
)
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
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "cma-pipeline"}}

API_KEY = "test-cma-api-key"
CONTAINER_NUMBER = "CMAU1234564"

CMA_CONFIG = {
    **PUBLIC_TRACK_AND_TRACE_CONFIG,
    "base_url": "https://example.invalid/cma",
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
        return self.responses.pop(0) if self.responses else FakeResponse(200, [])


def _cma_events() -> list:
    return json.loads((FIXTURES / "cma_cgm_events_response.json").read_text())


@override_settings(CACHES=_LOCMEM)
class CmaCgmSyncPipelineTest(TestCase):
    """A configured CMA CGM integration produces normalised, stored tracking data."""

    def setUp(self):
        self.team = Team.objects.create(name="cma-pipeline", slug="cma-pipeline")
        self.integration = Integration.objects.create(
            team=self.team,
            name="CMA CGM",
            provider_code=PROVIDER_CODE,
            provider_family=Integration.ProviderFamily.CARRIER,
            api_style=Integration.ApiStyle.DCSA,
            config=CMA_CONFIG,
            is_active=True,
        )
        set_integration_credentials(self.integration, IntegrationCredential.AuthType.API_KEY, {"api_key": API_KEY})
        self.provider = TrackingProvider.objects.create(code=PROVIDER_CODE, name="CMA CGM")
        equipment_type = EquipmentType.objects.get_or_create(
            iso_code="42G1",
            defaults={"category": "GP", "length_ft": 40, "high_cube": False, "description": "40' GP"},
        )[0]
        self.container = Container.objects.create(
            team=self.team,
            owner_code="CMA",
            category_id="U",
            serial_number="123456",
            check_digit=4,
            equipment_type=equipment_type,
        )
        self.shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-CMA-1", carrier="CMA CGM")
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
        """Run the real sync engine with a CMA CGM client bound to a fake session."""
        client = CmaCgmClient(self.integration, session=session)
        with mock.patch(
            "apps.scm.integrations.carriers.factory.build_carrier_client",
            return_value=client,
        ):
            return sync_tracking_subscription(self.subscription)

    def test_sync_succeeds_and_creates_every_event(self):
        run = self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.events_created, 4)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 4)

    def test_the_carrier_was_asked_by_equipment_reference_with_the_key_id_header(self):
        session = FakeSession([FakeResponse(200, _cma_events())])
        self._sync(session)
        self.assertEqual(
            session.requests[0]["url"],
            "https://example.invalid/cma/operation/trackandtrace/v1/events",
        )
        self.assertEqual(session.requests[0]["params"]["equipmentReference"], CONTAINER_NUMBER)
        self.assertEqual(session.requests[0]["headers"]["keyId"], API_KEY)

    def test_raw_response_is_stored_and_marked_parsed(self):
        self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        stored = TrackingRawPayload.objects.get(team=self.team)
        self.assertEqual(stored.payload_json, {"events": _cma_events()})
        self.assertTrue(stored.parsed_successfully)
        self.assertTrue(stored.payload_hash)

    def test_carrier_specific_data_survives_in_the_stored_payload(self):
        """No CMA CGM column exists; the raw response keeps every extension field."""
        self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        stored = TrackingRawPayload.objects.get(team=self.team)
        first = stored.payload_json["events"][0]
        self.assertEqual(first["carrierSpecificData"]["internalEventCode"], "LOA")
        self.assertEqual(first["eventLocation"]["facilityCode"], "SHAPORT")

    def test_events_link_back_to_the_stored_payload(self):
        self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        stored = TrackingRawPayload.objects.get(team=self.team)
        for event in TrackingEvent.objects.filter(team=self.team):
            self.assertEqual(event.raw_payload_id, stored.pk)

    def test_the_load_event_is_normalised_with_vessel_and_location(self):
        self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        loaded = TrackingEvent.objects.get(team=self.team, source_event_id="CMA-EVT-LOAD-1")
        self.assertEqual(loaded.event_type, TrackingEvent.EventType.LOADED_ON_VESSEL)
        self.assertTrue(loaded.is_actual)
        self.assertEqual(loaded.vessel_name, "CMA CGM ANTOINE DE SAINT EXUPERY")
        self.assertEqual(loaded.vessel_imo, "9776418")
        self.assertEqual(loaded.location_unlocode, "CNSHA")
        self.assertEqual(loaded.voyage_number, "0FE5ME1MA")
        self.assertEqual(loaded.transport_mode, TrackingEvent.TransportMode.VESSEL)

    def test_coordinates_are_persisted_for_the_container_map(self):
        self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        loaded = TrackingEvent.objects.get(team=self.team, source_event_id="CMA-EVT-LOAD-1")
        self.assertIsNotNone(loaded.location_latitude)
        self.assertIsNotNone(loaded.location_longitude)
        self.assertAlmostEqual(float(loaded.location_latitude), 31.2304, places=3)
        self.assertAlmostEqual(float(loaded.location_longitude), 121.4737, places=3)

    def test_the_estimated_arrival_is_not_recorded_as_actual(self):
        self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        arrival = TrackingEvent.objects.get(team=self.team, source_event_id="CMA-EVT-ARRI-1")
        self.assertTrue(arrival.is_estimated)
        self.assertFalse(arrival.is_actual)

    def test_events_are_linked_to_the_container_and_shipment(self):
        self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        for event in TrackingEvent.objects.filter(team=self.team):
            self.assertEqual(event.container_id, self.container.pk)
            self.assertEqual(event.shipment_id, self.shipment.pk)

    def test_subscription_moves_to_tracking(self):
        self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)
        self.assertIsNotNone(self.subscription.last_event_at)

    def test_paged_events_all_reach_the_timeline(self):
        """Both pages are ingested through the ordinary pipeline, each event once."""
        events = _cma_events()
        run = self._sync(
            FakeSession(
                [
                    FakeResponse(200, events[:2], headers={"Next-Page": "cursor123"}),
                    FakeResponse(200, events[2:]),
                ]
            )
        )
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.events_created, 4)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 4)

    def test_the_second_page_is_requested_with_the_cursor(self):
        events = _cma_events()
        session = FakeSession(
            [
                FakeResponse(200, events[:2], headers={"Next-Page": "cursor123"}),
                FakeResponse(200, events[2:]),
            ]
        )
        self._sync(session)
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(session.requests[1]["params"]["cursor"], "cursor123")

    def test_a_second_sync_of_the_same_payload_adds_no_duplicates(self):
        self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        run = self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        self.assertEqual(run.events_created, 0)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 4)

    def test_the_timeline_surfaces_the_carrier_events(self):
        """The ordinary timeline layer reads CMA CGM events like any other carrier's."""
        from apps.scm.tracking.timeline import get_tracking_timeline_items_for_shipment

        self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        items = get_tracking_timeline_items_for_shipment(team=self.team, shipment=self.shipment)
        self.assertEqual(len(items), 4)
        # Newest first: the estimated arrival is the furthest-out event in the fixture.
        self.assertEqual(items[0].source, "CMA CGM")
        self.assertIn("Southampton", items[0].location)

    def test_request_is_logged_without_the_api_key(self):
        self._sync(FakeSession([FakeResponse(200, _cma_events())]))
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

    def test_an_empty_event_array_is_a_successful_sync_with_no_events(self):
        run = self._sync(FakeSession([FakeResponse(200, [])]))
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.events_created, 0)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)

    def test_authentication_failure_is_classified_and_creates_no_events(self):
        run = self._sync(FakeSession([FakeResponse(401), FakeResponse(401)]))
        self.assertEqual(run.status, TrackingSyncRun.Status.FAILED)
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.AUTHENTICATION)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)

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
        run = self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        self.assertEqual(run.status, TrackingSyncRun.Status.SKIPPED)
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.NOT_CONFIGURED)
        self.assertEqual(TrackingRawPayload.objects.filter(team=self.team).count(), 0)

    def test_events_stay_inside_the_team(self):
        other_team = Team.objects.create(name="cma-pipeline-other", slug="cma-pipeline-other")
        self._sync(FakeSession([FakeResponse(200, _cma_events())]))
        self.assertEqual(TrackingEvent.objects.filter(team=other_team).count(), 0)
        self.assertEqual(TrackingRawPayload.objects.filter(team=other_team).count(), 0)
        self.assertEqual(IntegrationRequestLog.objects.filter(team=other_team).count(), 0)


@override_settings(CACHES=_LOCMEM)
class CmaCgmFactoryResolutionTest(TestCase):
    """The tracking flow reaches CMA CGM through the same factory path as Maersk."""

    def setUp(self):
        self.team = Team.objects.create(name="cma-factory", slug="cma-factory")

    def test_build_carrier_client_resolves_the_teams_active_integration(self):
        from apps.scm.integrations.carriers.factory import build_carrier_client

        integration = Integration.objects.create(
            team=self.team,
            name="CMA CGM",
            provider_code=PROVIDER_CODE,
            provider_family=Integration.ProviderFamily.CARRIER,
            api_style=Integration.ApiStyle.DCSA,
            config=CMA_CONFIG,
            is_active=True,
        )
        set_integration_credentials(integration, IntegrationCredential.AuthType.API_KEY, {"api_key": API_KEY})

        client = build_carrier_client(PROVIDER_CODE, team=self.team, require_integration=True)
        self.assertIsInstance(client, CmaCgmClient)
        self.assertEqual(client.integration.pk, integration.pk)
        self.assertEqual(client.credentials, {"api_key": API_KEY})

    def test_an_inactive_integration_is_not_resolved(self):
        from apps.scm.integrations.carriers.exceptions import CarrierConfigurationError
        from apps.scm.integrations.carriers.factory import build_carrier_client

        Integration.objects.create(
            team=self.team,
            name="CMA CGM",
            provider_code=PROVIDER_CODE,
            provider_family=Integration.ProviderFamily.CARRIER,
            config=CMA_CONFIG,
            is_active=False,
        )
        with self.assertRaises(CarrierConfigurationError):
            build_carrier_client(PROVIDER_CODE, team=self.team, require_integration=True)
