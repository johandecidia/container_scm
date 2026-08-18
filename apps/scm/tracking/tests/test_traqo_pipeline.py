"""End-to-end proof that Traqo feeds the existing tracking domain.

Drives the real ingestion path with an injected HTTP session: Traqo call → stored raw
payload → normalised events → TrackingEvent rows → subscription state → the selectors
the timeline and position logic read. Nothing is mocked but the socket layer.

The acceptance case is the one in the brief: MRSU6859427, sealine MAEU, Traqo sandbox.
"""

import json
import pathlib

from django.test import TestCase, override_settings

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.traqo import PROVIDER_CODE
from apps.scm.integrations.traqo.client import TraqoClient
from apps.scm.integrations.traqo.service import get_traqo_provider, ingest_traqo_container
from apps.scm.tracking.models import (
    TrackingEvent,
    TrackingProvider,
    TrackingRawPayload,
    TrackingSubscription,
    TrackingSyncRun,
)
from apps.scm.tracking.positions import PositionType, get_latest_container_position
from apps.scm.tracking.selectors import (
    get_container_tracking_eta_event,
    get_latest_meaningful_actual_event,
    get_tracking_events_for_container,
)
from apps.teams.models import Team

FIXTURES = pathlib.Path(__file__).parents[2] / "integrations" / "tests" / "fixtures" / "traqo"
CONTAINER_NUMBER = "MRSU6859427"
SEALINE = "MAEU"
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "traqo-pipeline"}}


def sandbox_payload() -> dict:
    return json.loads((FIXTURES / "sandbox_container_MRSU6859427.json").read_text())


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"url": url, "headers": headers or {}, "params": params or {}})
        return self.responses.pop(0) if self.responses else FakeResponse(200, sandbox_payload())


@override_settings(CACHES=_LOCMEM)
class TraqoIngestionPipelineTest(TestCase):
    """A Traqo sandbox response becomes canonical TrackingEvents."""

    def setUp(self):
        self.team = Team.objects.create(name="traqo-pipeline", slug="traqo-pipeline")
        equipment_type = EquipmentType.objects.get_or_create(
            iso_code="22G1",
            defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
        )[0]
        self.container = Container.objects.create(
            team=self.team,
            owner_code="MRS",
            category_id="U",
            serial_number="685942",
            check_digit=7,
            equipment_type=equipment_type,
        )

    def _ingest(self, payload=None):
        session = FakeSession([FakeResponse(200, payload or sandbox_payload())])
        client = TraqoClient(sandbox=True, session=session)
        return ingest_traqo_container(
            team=self.team,
            container=self.container,
            sealine=SEALINE,
            sandbox=True,
            client=client,
        )

    # ------------------------------------------------------------------
    # Provider and subscription
    # ------------------------------------------------------------------

    def test_the_traqo_provider_is_created_once_with_its_base_url(self):
        first = get_traqo_provider()
        second = get_traqo_provider()

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.code, PROVIDER_CODE)
        self.assertEqual(first.name, "Traqo Ocean")
        self.assertEqual(first.provider_type, TrackingProvider.ProviderType.API)
        self.assertEqual(first.base_url, "https://traqocontainer.com/api/v1")
        self.assertEqual(TrackingProvider.objects.filter(code=PROVIDER_CODE).count(), 1)

    def test_ingesting_creates_a_traqo_subscription_for_the_container(self):
        result = self._ingest()

        subscription = result.subscription
        self.assertEqual(subscription.provider.code, PROVIDER_CODE)
        self.assertEqual(subscription.container_id, self.container.pk)
        self.assertEqual(subscription.team_id, self.team.pk)
        self.assertEqual(subscription.tracking_reference, CONTAINER_NUMBER)
        self.assertEqual(subscription.reference_type, TrackingSubscription.ReferenceType.CONTAINER_NUMBER)

    def test_a_second_ingest_reuses_the_same_subscription(self):
        first = self._ingest()
        second = self._ingest()

        self.assertEqual(first.subscription.pk, second.subscription.pk)
        self.assertEqual(TrackingSubscription.objects.filter(team=self.team).count(), 1)

    def test_the_subscription_is_reported_as_tracking_after_events_arrive(self):
        result = self._ingest()
        result.subscription.refresh_from_db()

        self.assertEqual(result.subscription.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)
        self.assertIsNotNone(result.subscription.last_event_at)

    def test_the_attempt_is_recorded_as_a_sync_run(self):
        result = self._ingest()
        run = result.sync_run
        run.refresh_from_db()

        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.provider.code, PROVIDER_CODE)
        self.assertEqual(run.events_created, 3)

    # ------------------------------------------------------------------
    # Raw payload
    # ------------------------------------------------------------------

    def test_the_original_response_is_stored_before_it_is_trusted(self):
        self._ingest()

        payload = TrackingRawPayload.objects.get(team=self.team)
        self.assertEqual(payload.provider.code, PROVIDER_CODE)
        self.assertEqual(payload.payload_type, TrackingRawPayload.PayloadType.API_RESPONSE)
        self.assertTrue(payload.parsed_successfully)
        self.assertTrue(payload.payload_hash)
        self.assertIsNotNone(payload.subscription_id)

    def test_the_stored_payload_keeps_the_whole_envelope_for_reprocessing(self):
        self._ingest()

        stored = TrackingRawPayload.objects.get(team=self.team).payload_json
        self.assertTrue(stored["sandbox"])
        data = stored["data"]
        # Shipment-level values Phase 1 does not model still survive in full.
        self.assertEqual(data["eta"], "2026-05-17 00:00:00")
        self.assertEqual(data["status"], "IN_TRANSIT")
        self.assertEqual(data["latitude"], "-35")
        self.assertEqual(data["longitude"], "18")
        self.assertEqual(len(data["vessels_table"]), 1)

    def test_events_are_linked_to_the_payload_they_were_parsed_from(self):
        self._ingest()

        payload = TrackingRawPayload.objects.get(team=self.team)
        self.assertEqual(payload.events.count(), 3)

    # ------------------------------------------------------------------
    # Canonical events
    # ------------------------------------------------------------------

    def test_traqo_events_reach_tracking_event_with_the_right_links(self):
        result = self._ingest()

        events = TrackingEvent.objects.filter(team=self.team)
        self.assertEqual(events.count(), 3)
        for event in events:
            self.assertEqual(event.provider.code, PROVIDER_CODE)
            self.assertEqual(event.container_id, self.container.pk)
            self.assertEqual(event.subscription_id, result.subscription.pk)
            self.assertEqual(event.equipment_reference, CONTAINER_NUMBER)
            self.assertTrue(event.event_fingerprint)

    def test_the_gate_in_event_is_classified_and_placed(self):
        self._ingest()

        event = TrackingEvent.objects.get(team=self.team, event_code="GTIN")
        self.assertEqual(event.event_type, TrackingEvent.EventType.GATE_IN)
        self.assertEqual(event.event_time_type, TrackingEvent.EventTimeType.ACTUAL)
        self.assertEqual(event.carrier_event_type, "EQUIPMENT")
        self.assertEqual(event.transport_mode, TrackingEvent.TransportMode.TRUCK)
        self.assertEqual(event.location_name, "Mundra")
        self.assertEqual(event.description, "Gate in full")
        self.assertEqual(event.carrier_description, "Gate in full")

    def test_the_load_event_is_classified_as_loaded_on_vessel(self):
        self._ingest()

        event = TrackingEvent.objects.get(team=self.team, event_code="LOAD")
        self.assertEqual(event.event_type, TrackingEvent.EventType.LOADED_ON_VESSEL)
        self.assertTrue(event.is_actual)

    def test_the_forecast_arrival_is_stored_as_a_forecast(self):
        self._ingest()

        event = TrackingEvent.objects.get(team=self.team, event_code="ARRI")
        self.assertEqual(event.event_type, TrackingEvent.EventType.VESSEL_ARRIVED)
        self.assertEqual(event.event_time_type, TrackingEvent.EventTimeType.ESTIMATED)
        self.assertFalse(event.is_actual)
        self.assertTrue(event.is_estimated)

    def test_the_traqo_event_payload_survives_on_the_row(self):
        self._ingest()

        event = TrackingEvent.objects.get(team=self.team, event_code="GTIN")
        self.assertEqual(event.raw_data["status"], "CGI")
        self.assertEqual(event.raw_data["country"], "India")
        self.assertEqual(event.raw_data["idx"], 1)

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def test_reprocessing_the_same_response_creates_no_duplicate_events(self):
        first = self._ingest()
        second = self._ingest()

        self.assertEqual(first.events_created, 3)
        self.assertEqual(second.events_created, 0)
        self.assertEqual(second.events_updated, 3)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 3)

    def test_a_corrected_event_time_is_a_new_event_without_a_stable_source_id(self):
        """Traqo supplies no event ID, so a moved timestamp is a different event.

        Deliberate: with only a positional ``idx`` to go on, treating a changed time as
        a correction would let a later inserted event overwrite an earlier one. An
        extra row is recoverable; a silently rewritten history is not.
        """
        self._ingest()

        corrected = sandbox_payload()
        corrected["data"]["events_table"][0]["timestamp"] = "2026-03-01 06:30:00"
        self._ingest(corrected)

        self.assertEqual(TrackingEvent.objects.filter(team=self.team, event_code="GTIN").count(), 2)

    def test_a_changed_description_updates_the_existing_event_in_place(self):
        self._ingest()

        revised = sandbox_payload()
        revised["data"]["events_table"][0]["description"] = "Gate in full (revised)"
        self._ingest(revised)

        self.assertEqual(TrackingEvent.objects.filter(team=self.team, event_code="GTIN").count(), 1)
        event = TrackingEvent.objects.get(team=self.team, event_code="GTIN")
        self.assertEqual(event.description, "Gate in full (revised)")

    # ------------------------------------------------------------------
    # Read models
    # ------------------------------------------------------------------

    def test_the_events_are_visible_through_the_container_timeline_selector(self):
        self._ingest()

        events = list(get_tracking_events_for_container(self.team, self.container))

        self.assertEqual(len(events), 3)
        self.assertEqual([event.event_code for event in events], ["ARRI", "LOAD", "GTIN"])

    def test_the_container_status_is_derived_from_the_latest_observed_event(self):
        self._ingest()

        event = get_latest_meaningful_actual_event(self.team, self.container)

        # Not the forecast arrival: an estimate is not evidence the box arrived.
        self.assertEqual(event.event_type, TrackingEvent.EventType.LOADED_ON_VESSEL)

    def test_traqos_forecast_arrival_becomes_the_containers_eta(self):
        self._ingest()

        eta_event = get_container_tracking_eta_event(self.team, self.container)

        self.assertIsNotNone(eta_event)
        self.assertEqual(eta_event.event_code, "ARRI")
        # The shipment-level eta in the payload, arriving as a forecast arrival event.
        self.assertEqual(eta_event.event_datetime.date().isoformat(), "2026-05-17")

    def test_the_position_comes_from_the_last_located_observation(self):
        self._ingest()

        position = get_latest_container_position(self.team, self.container)

        self.assertEqual(position.location_name, "Mundra")
        self.assertEqual(position.position_type, PositionType.FACILITY)
        self.assertFalse(position.is_realtime)

    def test_the_shipment_level_position_is_not_promoted_to_a_container_position(self):
        self._ingest()

        # data.latitude/longitude is a provider observation of the voyage, kept raw.
        self.assertFalse(TrackingEvent.objects.filter(team=self.team, location_latitude__isnull=False).exists())


@override_settings(CACHES=_LOCMEM)
class TraqoAlongsideCarrierTrackingTest(TestCase):
    """Traqo is an additional source, never a replacement for a carrier's own."""

    def setUp(self):
        self.team = Team.objects.create(name="traqo-compare", slug="traqo-compare")
        equipment_type = EquipmentType.objects.get_or_create(
            iso_code="22G1",
            defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
        )[0]
        self.container = Container.objects.create(
            team=self.team,
            owner_code="MRS",
            category_id="U",
            serial_number="685942",
            check_digit=7,
            equipment_type=equipment_type,
        )
        self.maersk = TrackingProvider.objects.create(code="maersk", name="Maersk")
        self.maersk_subscription = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.maersk,
            container=self.container,
            tracking_reference=CONTAINER_NUMBER,
            reference_type=TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
        )
        self.maersk_event = TrackingEvent.objects.create(
            team=self.team,
            provider=self.maersk,
            container=self.container,
            subscription=self.maersk_subscription,
            event_type=TrackingEvent.EventType.GATE_IN,
            event_code="GTIN",
            carrier_event_type="EQUIPMENT",
            event_time_type=TrackingEvent.EventTimeType.ACTUAL,
            event_datetime="2026-03-01T00:00:00Z",
            location_name="Mundra",
            event_fingerprint="maersk-direct-gtin",
        )

    def _ingest(self):
        client = TraqoClient(sandbox=True, session=FakeSession([FakeResponse(200, sandbox_payload())]))
        return ingest_traqo_container(
            team=self.team,
            container=self.container,
            sealine=SEALINE,
            sandbox=True,
            client=client,
        )

    def test_the_container_can_be_watched_by_both_providers_at_once(self):
        result = self._ingest()

        providers = set(
            TrackingSubscription.objects.filter(team=self.team, container=self.container).values_list(
                "provider__code", flat=True
            )
        )
        self.assertEqual(providers, {"maersk", PROVIDER_CODE})
        self.assertNotEqual(result.subscription.pk, self.maersk_subscription.pk)

    def test_the_maersk_direct_subscription_is_left_untouched(self):
        before = TrackingSubscription.objects.get(pk=self.maersk_subscription.pk)
        before_state = (before.status, before.tracking_status, before.provider_id, before.updated_at)

        self._ingest()

        after = TrackingSubscription.objects.get(pk=self.maersk_subscription.pk)
        self.assertEqual((after.status, after.tracking_status, after.provider_id, after.updated_at), before_state)

    def test_an_equivalent_traqo_event_does_not_overwrite_the_carriers_own(self):
        self._ingest()

        # Same movement, two sources: the fingerprint is provider-scoped, so both are
        # kept and can be compared rather than one silently replacing the other.
        gate_ins = TrackingEvent.objects.filter(team=self.team, event_code="GTIN")
        self.assertEqual(gate_ins.count(), 2)
        self.assertEqual(
            set(gate_ins.values_list("provider__code", flat=True)),
            {"maersk", PROVIDER_CODE},
        )
        self.maersk_event.refresh_from_db()
        self.assertEqual(self.maersk_event.event_fingerprint, "maersk-direct-gtin")

    def test_both_sources_appear_on_the_containers_timeline(self):
        self._ingest()

        events = list(get_tracking_events_for_container(self.team, self.container))

        self.assertEqual(len(events), 4)
        self.assertEqual({event.provider.code for event in events}, {"maersk", PROVIDER_CODE})
