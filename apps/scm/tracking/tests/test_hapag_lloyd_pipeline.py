"""End-to-end proof of the Hapag-Lloyd vertical.

Drives the real sync engine against a configured Hapag-Lloyd integration with an
injected HTTP session: subscription → gateway call → stored raw response → normalised
events → canonical locations → the reads the Container Workspace and Control Tower
make. Same engine, parser, persistence, resolver and map as Maersk and CMA CGM; only
the configuration and the gateway auth differ.

Two things beyond the other carrier pipelines are load-bearing here.

*Unresolved locations stay usable.* Hapag-Lloyd names inland facilities MCR has no
canonical row for. Those events must keep the carrier's own name and coordinates and
must still place the container on the operational map — see
:class:`HapagLloydUnresolvedLocationTest`.

*Coexistence.* A Hapag-Lloyd direct watch must not displace a Traqo one on the same
container — see :class:`HapagLloydCoexistsWithTraqoTest`.

Nothing here is mocked except the HTTP session, so this is the closest the suite gets
to a live Hapag-Lloyd sync.
"""

import json
import pathlib
from unittest import mock

from django.test import TestCase, override_settings

from apps.scm.containers.choices import LocationResolutionStatus, LocationType
from apps.scm.containers.models import Container, ContainerLocation, EquipmentType
from apps.scm.integrations.carriers.hapag_lloyd.client import (
    CARRIER_NAME,
    PROVIDER_CODE,
    TRACK_AND_TRACE_CONFIG,
    HapagLloydClient,
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
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "hapag-pipeline"}}

CLIENT_ID = "test-hapag-client-id"
CLIENT_SECRET = "test-hapag-client-secret"
CONTAINER_NUMBER = "HLXU8891233"

HAPAG_CONFIG = {
    **TRACK_AND_TRACE_CONFIG,
    "base_url": "https://example.invalid/hapag",
    "tracking_path": "/hlag/external/v2/events",
    "test_connection_reference": CONTAINER_NUMBER,
    "max_retries": 0,
    "retry_backoff_seconds": 0,
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


def _hapag_events() -> dict:
    return json.loads((FIXTURES / "hapag_lloyd_dcsa_events.json").read_text())


class HapagPipelineBase(TestCase):
    """A team with a configured Hapag-Lloyd integration and one tracked container."""

    def setUp(self):
        self.team = Team.objects.create(name=self._team_slug(), slug=self._team_slug())
        self.integration = Integration.objects.create(
            team=self.team,
            name=CARRIER_NAME,
            provider_code=PROVIDER_CODE,
            provider_family=Integration.ProviderFamily.CARRIER,
            api_style=Integration.ApiStyle.DCSA,
            config=HAPAG_CONFIG,
            is_active=True,
        )
        set_integration_credentials(
            self.integration,
            IntegrationCredential.AuthType.API_KEY,
            {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )
        self.provider = TrackingProvider.objects.create(code=PROVIDER_CODE, name=CARRIER_NAME)
        equipment_type = EquipmentType.objects.get_or_create(
            iso_code="45G1",
            defaults={"category": "GP", "length_ft": 40, "high_cube": True, "description": "40' HC"},
        )[0]
        self.container = Container.objects.create(
            team=self.team,
            owner_code="HLX",
            category_id="U",
            serial_number="889123",
            check_digit=3,
            equipment_type=equipment_type,
        )
        self.shipment = Shipment.objects.create(
            team=self.team, shipment_number=f"SHP-{self._team_slug()}", carrier="Hapag-Lloyd"
        )
        ShipmentContainer.objects.create(shipment=self.shipment, container=self.container)
        self.subscription = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.provider,
            container=self.container,
            shipment=self.shipment,
            tracking_reference=CONTAINER_NUMBER,
            reference_type=TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
            carrier_code=PROVIDER_CODE,
            carrier_name=CARRIER_NAME,
        )

    def _team_slug(self) -> str:
        return "hapag-pipeline"

    def _sync(self, session=None):
        """Run the real sync engine with a Hapag-Lloyd client bound to a fake session."""
        session = session or FakeSession([FakeResponse(200, _hapag_events())])
        client = HapagLloydClient(self.integration, session=session)
        with mock.patch(
            "apps.scm.integrations.carriers.factory.build_carrier_client",
            return_value=client,
        ):
            return sync_tracking_subscription(self.subscription)

    def _event(self, code: str) -> TrackingEvent:
        return TrackingEvent.objects.get(team=self.team, event_code=code)


@override_settings(CACHES=_LOCMEM)
class HapagLloydSyncPipelineTest(HapagPipelineBase):
    """A configured Hapag-Lloyd integration produces normalised, stored tracking data."""

    def _team_slug(self) -> str:
        return "hapag-sync"

    def test_sync_succeeds_and_creates_every_event(self):
        run = self._sync()
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.events_created, 5)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 5)

    def test_the_gateway_is_asked_by_equipment_reference_with_both_credential_headers(self):
        session = FakeSession([FakeResponse(200, _hapag_events())])
        self._sync(session)
        request = session.requests[0]
        self.assertEqual(request["url"], "https://example.invalid/hapag/hlag/external/v2/events")
        self.assertEqual(request["params"]["equipmentReference"], CONTAINER_NUMBER)
        self.assertEqual(request["headers"]["X-IBM-Client-Id"], CLIENT_ID)
        self.assertEqual(request["headers"]["X-IBM-Client-Secret"], CLIENT_SECRET)

    def test_raw_response_is_stored_and_marked_parsed(self):
        self._sync()
        stored = TrackingRawPayload.objects.get(team=self.team)
        self.assertEqual(stored.payload_json, _hapag_events())
        self.assertTrue(stored.parsed_successfully)
        self.assertTrue(stored.payload_hash)

    def test_hapag_specific_fields_survive_in_the_stored_payload(self):
        """No Hapag-Lloyd column exists; the raw response keeps every extension field."""
        self._sync()
        stored = TrackingRawPayload.objects.get(team=self.team)
        gate_in = next(event for event in stored.payload_json["events"] if event.get("eventID", "").endswith("a002"))
        self.assertEqual(gate_in["ISOEquipmentCode"], "45G1")
        self.assertEqual(gate_in["emptyIndicatorCode"], "LADEN")
        self.assertEqual(gate_in["eventLocation"]["facilityCode"], "CTA")
        self.assertEqual(gate_in["transportCall"]["carrierServiceCode"], "AX1")

    def test_events_link_back_to_the_stored_payload(self):
        self._sync()
        stored = TrackingRawPayload.objects.get(team=self.team)
        for event in TrackingEvent.objects.filter(team=self.team):
            self.assertEqual(event.raw_payload_id, stored.pk)

    def test_the_gate_in_event_is_normalised_with_place_and_carriage(self):
        self._sync()
        gate_in = self._event("GTIN")
        self.assertEqual(gate_in.event_type, TrackingEvent.EventType.GATE_IN)
        self.assertTrue(gate_in.is_actual)
        self.assertEqual(gate_in.location_name, "Container Terminal Altenwerder")
        self.assertEqual(gate_in.location_unlocode, "DEHAM")
        self.assertEqual(gate_in.vessel_name, "HAMBURG EXPRESS")
        self.assertEqual(gate_in.vessel_imo, "9450648")
        self.assertEqual(gate_in.voyage_number, "0034W")
        self.assertEqual(gate_in.transport_mode, TrackingEvent.TransportMode.VESSEL)
        self.assertEqual(gate_in.equipment_reference, CONTAINER_NUMBER)

    def test_coordinates_are_persisted_for_the_map(self):
        self._sync()
        gate_in = self._event("GTIN")
        self.assertAlmostEqual(float(gate_in.location_latitude), 53.5, places=4)
        self.assertAlmostEqual(float(gate_in.location_longitude), 9.933333, places=4)

    def test_the_offset_timestamp_and_its_timezone_are_both_kept(self):
        self._sync()
        gate_in = self._event("GTIN")
        self.assertEqual(gate_in.event_timezone, "+02:00")
        self.assertEqual(gate_in.event_datetime.isoformat(), "2026-06-14T06:24:00+00:00")

    def test_the_estimated_arrival_is_not_recorded_as_actual(self):
        self._sync()
        arrival = self._event("ARRI")
        self.assertTrue(arrival.is_estimated)
        self.assertFalse(arrival.is_actual)
        self.assertEqual(arrival.event_type, TrackingEvent.EventType.VESSEL_ARRIVED)

    def test_an_unmapped_code_is_stored_rather_than_discarded(self):
        """STUF has no internal counterpart; the carrier's own wording is kept."""
        self._sync()
        stuffing = self._event("STUF")
        self.assertEqual(stuffing.event_type, TrackingEvent.EventType.UNKNOWN)
        self.assertEqual(stuffing.carrier_event_type, "EQUIPMENT")
        self.assertEqual(stuffing.carrier_description, "Container stuffing completed at inland depot")
        self.assertTrue(stuffing.is_actual)

    def test_the_shipment_milestone_keeps_the_carriers_reason_text(self):
        self._sync()
        booking = self._event("RECE")
        self.assertEqual(booking.event_type, TrackingEvent.EventType.BOOKING_CREATED)
        self.assertEqual(booking.carrier_description, "Booking request received")

    def test_events_are_linked_to_the_container_and_shipment(self):
        self._sync()
        for event in TrackingEvent.objects.filter(team=self.team):
            self.assertEqual(event.container_id, self.container.pk)
            self.assertEqual(event.shipment_id, self.shipment.pk)

    def test_subscription_moves_to_tracking(self):
        self._sync()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)
        self.assertIsNotNone(self.subscription.last_event_at)

    def test_a_second_sync_of_the_same_payload_adds_no_duplicates(self):
        self._sync()
        run = self._sync()
        self.assertEqual(run.events_created, 0)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 5)

    def test_no_data_is_a_successful_sync_with_no_events(self):
        run = self._sync(FakeSession([FakeResponse(404)]))
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.events_created, 0)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.NO_DATA)
        self.assertEqual(self.subscription.consecutive_failures, 0)

    def test_authentication_failure_is_not_reported_as_no_data(self):
        """A rejected credential must never read as a container the carrier lacks."""
        run = self._sync(FakeSession([FakeResponse(401), FakeResponse(401)]))
        self.assertEqual(run.status, TrackingSyncRun.Status.FAILED)
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.AUTHENTICATION)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)

    def test_unconfigured_integration_is_skipped_not_reported_as_empty(self):
        self.integration.config = {}
        self.integration.save(update_fields=["config"])
        run = self._sync()
        self.assertEqual(run.status, TrackingSyncRun.Status.SKIPPED)
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.NOT_CONFIGURED)
        self.assertEqual(TrackingRawPayload.objects.filter(team=self.team).count(), 0)

    def test_credentials_never_reach_the_request_log(self):
        self._sync()
        log = IntegrationRequestLog.objects.get(team=self.team)
        self.assertTrue(log.success)
        logged = log.endpoint + log.error_message
        self.assertNotIn(CLIENT_ID, logged)
        self.assertNotIn(CLIENT_SECRET, logged)

    def test_the_timeline_surfaces_the_carrier_events(self):
        """The ordinary timeline layer reads Hapag-Lloyd events like any other carrier's."""
        from apps.scm.tracking.timeline import get_tracking_timeline_items_for_shipment

        self._sync()
        items = get_tracking_timeline_items_for_shipment(team=self.team, shipment=self.shipment)
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0].source, CARRIER_NAME)

    def test_the_container_eta_comes_from_the_estimated_arrival(self):
        from apps.scm.containers.workspace import get_container_workspace

        self._sync()
        workspace = get_container_workspace(self.team, self.container)
        self.assertIsNotNone(workspace.tracking_eta)
        self.assertEqual(workspace.tracking_eta.isoformat(), "2026-07-04")

    def test_events_stay_inside_the_team(self):
        other_team = Team.objects.create(name="hapag-sync-other", slug="hapag-sync-other")
        self._sync()
        self.assertEqual(TrackingEvent.objects.filter(team=other_team).count(), 0)
        self.assertEqual(TrackingRawPayload.objects.filter(team=other_team).count(), 0)
        self.assertEqual(IntegrationRequestLog.objects.filter(team=other_team).count(), 0)


@override_settings(CACHES=_LOCMEM)
class HapagLloydLocationResolutionTest(HapagPipelineBase):
    """Hapag-Lloyd places go through the shared resolver, and are recorded as it decided."""

    def _team_slug(self) -> str:
        return "hapag-locations"

    def setUp(self):
        super().setUp()
        self.hamburg = ContainerLocation.objects.create(
            team=self.team,
            name="Hamburg",
            location_type=LocationType.PORT,
            unlocode="DEHAM",
            country_code="DE",
            latitude="53.550000",
            longitude="9.993000",
        )
        self._sync()

    def test_the_unlocode_resolves_a_terminal_to_the_port_it_names(self):
        gate_in = self._event("GTIN")
        self.assertEqual(gate_in.location_id, self.hamburg.pk)
        self.assertEqual(gate_in.location_resolution_status, LocationResolutionStatus.RESOLVED)

    def test_a_matching_name_resolves_too(self):
        departure = self._event("DEPA")
        self.assertEqual(departure.location_id, self.hamburg.pk)
        self.assertEqual(departure.location_resolution_status, LocationResolutionStatus.RESOLVED)

    def test_resolution_does_not_overwrite_the_carriers_own_wording(self):
        """The canonical link is an addition to the evidence, never a replacement."""
        gate_in = self._event("GTIN")
        self.assertEqual(gate_in.location_name, "Container Terminal Altenwerder")
        self.assertEqual(gate_in.location_unlocode, "DEHAM")
        self.assertIsNotNone(gate_in.location_latitude)

    def test_an_unknown_facility_is_left_unresolved_with_its_evidence_intact(self):
        stuffing = self._event("STUF")
        self.assertIsNone(stuffing.location_id)
        self.assertEqual(stuffing.location_resolution_status, LocationResolutionStatus.UNRESOLVED)
        self.assertEqual(stuffing.location_name, "Bremer Binnenterminal Nord")
        self.assertAlmostEqual(float(stuffing.location_latitude), 53.108, places=3)
        self.assertAlmostEqual(float(stuffing.location_longitude), 8.752, places=3)

    def test_a_placeless_milestone_is_unresolved_without_being_a_gap(self):
        booking = self._event("RECE")
        self.assertIsNone(booking.location_id)
        self.assertEqual(booking.location_name, "")

    def test_recording_an_alias_resolves_the_facility_on_the_next_sync(self):
        """The operator's fix reaches an already-stored event, not just future ones."""
        from apps.scm.containers.models import LocationAlias

        depot = ContainerLocation.objects.create(
            team=self.team,
            name="Bremen Inland Terminal North",
            location_type=LocationType.DEPOT,
            country_code="DE",
        )
        LocationAlias.objects.create(
            team=self.team,
            location=depot,
            source=PROVIDER_CODE,
            external_name="Bremer Binnenterminal Nord",
        )

        self._sync()

        stuffing = self._event("STUF")
        self.assertEqual(stuffing.location_id, depot.pk)
        self.assertEqual(stuffing.location_resolution_status, LocationResolutionStatus.RESOLVED)


@override_settings(CACHES=_LOCMEM)
class HapagLloydUnresolvedLocationTest(HapagPipelineBase):
    """An unresolved Hapag-Lloyd place must stay usable evidence, not vanish.

    The team has no canonical locations at all, so every event resolves to nothing.
    The container is still placed — on its own workspace *and* on the operational
    map — from the carrier's own name and coordinates. Requiring a resolved
    ``ContainerLocation`` here is the regression this class exists to prevent: it
    made a container the carrier had confidently located read as one nobody could
    place.
    """

    def _team_slug(self) -> str:
        return "hapag-unresolved"

    def setUp(self):
        super().setUp()
        self._sync()

    def test_nothing_resolved(self):
        self.assertEqual(ContainerLocation.objects.filter(team=self.team).count(), 0)
        self.assertFalse(TrackingEvent.objects.filter(team=self.team, location__isnull=False).exists())

    def test_the_latest_position_is_the_carriers_own_report(self):
        from apps.scm.tracking.positions import PositionType, get_latest_container_position

        position = get_latest_container_position(self.team, self.container)
        self.assertIsNotNone(position)
        self.assertEqual(position.location_name, "Bremer Binnenterminal Nord")
        self.assertTrue(position.has_coordinates)
        self.assertEqual(position.position_type, PositionType.FACILITY)

    def test_the_container_is_still_on_the_operational_map(self):
        from apps.scm.visibility.map_positions import PositionClass, get_container_map_positions

        positions = get_container_map_positions(self.team, self.container)
        current = [position for position in positions if position.position_class == PositionClass.TRACKING]
        self.assertEqual(len(current), 1)

        place = current[0].place
        self.assertIsNotNone(place)
        self.assertTrue(place.has_coordinates)
        self.assertAlmostEqual(float(place.latitude), 53.108, places=3)
        self.assertEqual(place.name, "Bremer Binnenterminal Nord")

    def test_the_marker_does_not_pretend_to_be_a_canonical_place(self):
        from apps.scm.visibility.map_positions import PositionClass, get_container_map_positions

        positions = get_container_map_positions(self.team, self.container)
        current = next(position for position in positions if position.position_class == PositionClass.TRACKING)
        self.assertFalse(current.place.is_canonical)
        self.assertIsNone(current.place.location_id)

    def test_the_map_credits_the_provider_that_carried_the_observation(self):
        from apps.scm.visibility.map_positions import PositionClass, get_container_map_positions

        positions = get_container_map_positions(self.team, self.container)
        current = next(position for position in positions if position.position_class == PositionClass.TRACKING)
        self.assertEqual(current.source_label, CARRIER_NAME)


@override_settings(CACHES=_LOCMEM)
class HapagLloydDiscoveryTest(TestCase):
    """A Hapag-Lloyd container reaches the direct provider through the shared sweep."""

    def setUp(self):
        self.team = Team.objects.create(name="hapag-discovery", slug="hapag-discovery")
        self.integration = Integration.objects.create(
            team=self.team,
            name=CARRIER_NAME,
            provider_code=PROVIDER_CODE,
            provider_family=Integration.ProviderFamily.CARRIER,
            api_style=Integration.ApiStyle.DCSA,
            config=HAPAG_CONFIG,
            is_active=True,
        )
        set_integration_credentials(
            self.integration,
            IntegrationCredential.AuthType.API_KEY,
            {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )

    def test_the_owner_prefix_puts_hapag_lloyd_on_the_candidate_list(self):
        from apps.scm.integrations.carriers.carrier_discovery import SOURCE_OWNER_PREFIX, build_carrier_candidates

        candidates = build_carrier_candidates(team=self.team, container_number=CONTAINER_NUMBER)
        hapag = next(candidate for candidate in candidates if candidate.carrier_code == PROVIDER_CODE)
        self.assertTrue(hapag.usable)
        self.assertEqual(hapag.source, SOURCE_OWNER_PREFIX)

    def test_a_container_hapag_lloyd_answers_for_resolves_to_it(self):
        from apps.scm.integrations.carriers.carrier_discovery import discover_carrier_for_container

        client = HapagLloydClient(self.integration, session=FakeSession([FakeResponse(200, _hapag_events())]))
        outcome = discover_carrier_for_container(
            team=self.team,
            container_number=CONTAINER_NUMBER,
            clients={PROVIDER_CODE: client},
        )
        self.assertTrue(outcome.found)
        self.assertEqual(outcome.carrier_code, PROVIDER_CODE)
        self.assertEqual(outcome.carrier_name, CARRIER_NAME)
        self.assertEqual(len(outcome.events), 5)

    def test_no_data_leaves_the_container_open_to_another_carrier(self):
        from apps.scm.integrations.carriers.carrier_discovery import discover_carrier_for_container

        client = HapagLloydClient(self.integration, session=FakeSession([FakeResponse(404)]))
        outcome = discover_carrier_for_container(
            team=self.team,
            container_number=CONTAINER_NUMBER,
            clients={PROVIDER_CODE: client},
        )
        self.assertFalse(outcome.found)
        self.assertEqual([attempt.carrier_code for attempt in outcome.not_found], [PROVIDER_CODE])
        self.assertEqual(outcome.errored, [])

    def test_routing_prefers_the_direct_provider_for_a_connected_hapag_team(self):
        from apps.scm.tracking.provider_routing import DIRECT_PROVIDER_AVAILABLE, resolve_tracking_route

        route = resolve_tracking_route(team=self.team, carrier_code="Hapag-Lloyd")
        self.assertTrue(route.available)
        self.assertTrue(route.is_direct)
        self.assertEqual(route.provider_code, PROVIDER_CODE)
        self.assertEqual(route.reason, DIRECT_PROVIDER_AVAILABLE)

    def test_an_unconnected_team_is_not_routed_to_hapag_lloyd(self):
        from apps.scm.tracking.provider_routing import resolve_tracking_route

        other = Team.objects.create(name="hapag-unconnected", slug="hapag-unconnected")
        route = resolve_tracking_route(team=other, carrier_code="Hapag-Lloyd")
        self.assertFalse(route.is_direct)


@override_settings(CACHES=_LOCMEM)
class HapagLloydCoexistsWithTraqoTest(HapagPipelineBase):
    """Adding Hapag-Lloyd direct must not displace an aggregator watch on the same box.

    A container can be watched by several providers covering different legs of one
    journey, and the newest subscription is not the sole truth about it. Both watches
    stay verified, both providers' events land in one timeline, and each event still
    says which provider carried it.
    """

    def _team_slug(self) -> str:
        return "hapag-traqo"

    def setUp(self):
        super().setUp()
        self.traqo_provider = TrackingProvider.objects.create(code="traqo", name="Traqo Ocean")
        self.traqo_subscription = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.traqo_provider,
            container=self.container,
            shipment=self.shipment,
            tracking_reference=CONTAINER_NUMBER,
            reference_type=TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
            carrier_code=PROVIDER_CODE,
            carrier_name=CARRIER_NAME,
        )
        self.traqo_event = TrackingEvent.objects.create(
            team=self.team,
            provider=self.traqo_provider,
            subscription=self.traqo_subscription,
            container=self.container,
            shipment=self.shipment,
            event_fingerprint="traqo-coexistence-fixture",
            event_type=TrackingEvent.EventType.DISCHARGED,
            event_time_type=TrackingEvent.EventTimeType.ACTUAL,
            event_code="DISC",
            event_datetime="2026-07-05T09:00:00+00:00",
            location_name="New York",
            location_unlocode="USNYC",
            equipment_reference=CONTAINER_NUMBER,
        )

    def test_both_subscriptions_survive_a_hapag_sync(self):
        from apps.scm.tracking.selectors import get_verified_container_subscriptions

        self._sync()
        subscriptions = get_verified_container_subscriptions(self.team, self.container)
        self.assertEqual(
            sorted(subscription.provider.code for subscription in subscriptions),
            ["hapag_lloyd", "traqo"],
        )

    def test_the_traqo_event_is_not_touched_by_the_hapag_sync(self):
        self._sync()
        self.traqo_event.refresh_from_db()
        self.assertEqual(self.traqo_event.provider_id, self.traqo_provider.pk)
        self.assertEqual(self.traqo_event.event_type, TrackingEvent.EventType.DISCHARGED)

    def test_both_providers_events_reach_one_timeline(self):
        from apps.scm.tracking.timeline import get_tracking_timeline_items_for_shipment

        self._sync()
        items = get_tracking_timeline_items_for_shipment(team=self.team, shipment=self.shipment)
        self.assertEqual(len(items), 6)
        self.assertEqual({item.source for item in items}, {CARRIER_NAME, "Traqo Ocean"})

    def test_each_event_still_names_the_provider_that_supplied_it(self):
        self._sync()
        by_provider = {}
        for event in TrackingEvent.objects.filter(team=self.team).select_related("provider"):
            by_provider.setdefault(event.provider.code, 0)
            by_provider[event.provider.code] += 1
        self.assertEqual(by_provider, {"hapag_lloyd": 5, "traqo": 1})

    def test_the_two_providers_events_are_fingerprinted_apart(self):
        """Deduplication is per provider, so neither silently absorbs the other's events."""
        self._sync()
        fingerprints = TrackingEvent.objects.filter(team=self.team).values_list("event_fingerprint", flat=True)
        self.assertEqual(len(set(fingerprints)), 6)
