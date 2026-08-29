"""End-to-end proof that Vizion feeds the existing tracking domain.

Drives the real ingestion path with an injected HTTP session: Vizion call → stored raw
payload → normalised events → TrackingEvent rows → subscription state → the selectors the
timeline and ETA logic read. Nothing is mocked but the socket layer.

The two questions this file exists to answer are the ones the POC brief asks last:

*Does Vizion data reach canonical form through the existing architecture?* — no new table,
no new write path, no Vizion column anywhere.

*Is it idempotent?* — the same payload ingested twice must not double the history, and the
documented ETA→ATA flip must not silently rewrite a forecast out of existence.
"""

import json
import pathlib
from datetime import UTC, datetime

from django.test import TestCase, override_settings

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.vizion import PROVIDER_CODE
from apps.scm.integrations.vizion.client import VizionClient
from apps.scm.integrations.vizion.schemas import read_reference
from apps.scm.integrations.vizion.service import get_vizion_provider, ingest_vizion_container
from apps.scm.tracking.models import (
    ETAHistory,
    TrackingEvent,
    TrackingProvider,
    TrackingRawPayload,
    TrackingSubscription,
    TrackingSyncRun,
)
from apps.scm.tracking.selectors import (
    get_container_tracking_eta_event,
    get_tracking_events_for_container,
)
from apps.scm.tracking.sources import get_non_carrier_source, is_polled_by_carrier_sync
from apps.teams.models import Team

FIXTURES = pathlib.Path(__file__).parents[2] / "integrations" / "tests" / "fixtures" / "vizion"
CONTAINER_NUMBER = "BBCU3273070"
REFERENCE_ID = "e8991c95-5db2-4c0c-8a02-119611f769df"
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "vizion-pipeline"}}


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def direct_journey_updates() -> list[dict]:
    """The same journey with the transshipment call removed.

    Needed because an *actual* transshipment arrival makes the canonical model consider
    the whole journey arrived — see
    ``test_a_transshipment_arrival_suppresses_the_pod_eta_a_canonical_gap``. Removing it
    is what lets the ETA path be tested on its own, rather than the gap masking it.
    """
    updates = fixture("updates_transshipment.json")
    for update in updates:
        update["payload"]["milestones"] = [
            milestone
            for milestone in update["payload"]["milestones"]
            if not (
                milestone.get("journey_event", {}).get("event_type") == "ARRI"
                and milestone.get("shipment_location", {}).get("type_code") != "POD"
            )
        ]
    return updates


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, get=None):
        self.get_responses = list(get or [])
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"url": url})
        if self.get_responses:
            return self.get_responses.pop(0)
        return FakeResponse(200, fixture("updates_transshipment.json"))

    def post(self, url, headers=None, params=None, json=None, timeout=None):  # pragma: no cover
        raise AssertionError("Ingestion must not create a reference.")


@override_settings(CACHES=_LOCMEM)
class VizionIngestionPipelineTest(TestCase):
    """A Vizion response becomes canonical TrackingEvents through the existing services."""

    def setUp(self):
        self.team = Team.objects.create(name="vizion-pipeline", slug="vizion-pipeline")
        equipment_type = EquipmentType.objects.get_or_create(
            iso_code="45G1",
            defaults={"category": "GP", "length_ft": 40, "high_cube": True, "description": "40' HC"},
        )[0]
        self.container = Container.objects.create(
            team=self.team,
            owner_code="BBC",
            category_id="U",
            serial_number="327307",
            check_digit=0,
            equipment_type=equipment_type,
        )
        self.reference = read_reference(fixture("reference_aci_completed_oney.json"))

    def _ingest(self, updates=None):
        payload = updates if updates is not None else fixture("updates_transshipment.json")
        session = FakeSession(get=[FakeResponse(200, payload)])
        client = VizionClient(api_key="test-key", session=session)
        return ingest_vizion_container(
            team=self.team,
            container=self.container,
            reference_id=REFERENCE_ID,
            client=client,
            reference=self.reference,
        )

    # ------------------------------------------------------------------
    # Provider and subscription
    # ------------------------------------------------------------------

    def test_the_vizion_provider_is_created_once_with_its_base_url(self):
        first = get_vizion_provider()
        second = get_vizion_provider()

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.code, PROVIDER_CODE)
        self.assertEqual(first.name, "Vizion")
        self.assertEqual(first.provider_type, TrackingProvider.ProviderType.API)
        self.assertEqual(first.base_url, "https://prod.vizionapi.com")
        self.assertEqual(TrackingProvider.objects.filter(code=PROVIDER_CODE).count(), 1)

    def test_ingesting_creates_a_vizion_subscription_for_the_container(self):
        subscription = self._ingest().subscription

        self.assertEqual(subscription.provider.code, PROVIDER_CODE)
        self.assertEqual(subscription.container_id, self.container.pk)
        self.assertEqual(subscription.tracking_reference, CONTAINER_NUMBER)
        self.assertEqual(subscription.reference_type, TrackingSubscription.ReferenceType.CONTAINER_NUMBER)

    def test_the_subscription_is_reported_as_tracking_after_events_arrive(self):
        result = self._ingest()
        result.subscription.refresh_from_db()

        self.assertEqual(result.subscription.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)
        self.assertIsNotNone(result.subscription.last_event_at)

    def test_the_attempt_is_recorded_as_a_sync_run(self):
        run = self._ingest().sync_run
        run.refresh_from_db()

        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.provider.code, PROVIDER_CODE)
        self.assertEqual(run.events_created, 9)

    def test_the_raw_response_is_preserved_for_comparison(self):
        self._ingest()

        stored = TrackingRawPayload.objects.get(team=self.team, provider__code=PROVIDER_CODE)
        self.assertTrue(stored.parsed_successfully)
        # Both halves are kept: the updates carry the milestones, the reference carries
        # which carrier ACI attached.
        self.assertEqual(len(stored.payload_json["updates"]), 2)
        self.assertEqual(stored.payload_json["reference"]["carrier_code"], "ONEY")

    # ------------------------------------------------------------------
    # Canonical events
    # ------------------------------------------------------------------

    def test_events_reach_the_canonical_model_and_are_classified(self):
        self._ingest()

        events = {event.event_code: event for event in TrackingEvent.objects.filter(team=self.team)}
        self.assertEqual(events["GTIN"].event_type, TrackingEvent.EventType.GATE_IN)
        self.assertEqual(events["LOAD"].event_type, TrackingEvent.EventType.LOADED_ON_VESSEL)
        self.assertEqual(events["DEPA"].event_type, TrackingEvent.EventType.VESSEL_DEPARTED)
        self.assertEqual(events["DISC"].event_type, TrackingEvent.EventType.DISCHARGED)

    def test_actual_estimated_and_planned_survive_as_three_distinct_states(self):
        self._ingest()
        rows = TrackingEvent.objects.filter(team=self.team)

        self.assertEqual(rows.filter(event_time_type=TrackingEvent.EventTimeType.ACTUAL).count(), 6)
        self.assertEqual(rows.filter(event_time_type=TrackingEvent.EventTimeType.ESTIMATED).count(), 2)
        self.assertEqual(rows.filter(event_time_type=TrackingEvent.EventTimeType.PLANNED).count(), 1)

    def test_locations_vessels_and_voyages_reach_canonical_fields(self):
        self._ingest()
        loaded = TrackingEvent.objects.get(team=self.team, event_code="LOAD")

        self.assertEqual(loaded.location_unlocode, "VNSGN")
        self.assertEqual(str(loaded.location_latitude), "10.762600")
        self.assertEqual(str(loaded.location_longitude), "106.660200")
        self.assertEqual(loaded.vessel_name, "ONE OLYMPUS")
        self.assertEqual(loaded.vessel_imo, "9868284")
        self.assertEqual(loaded.voyage_number, "047E")
        self.assertEqual(loaded.transport_mode, TrackingEvent.TransportMode.VESSEL)
        self.assertEqual(loaded.equipment_reference, CONTAINER_NUMBER)

    def test_both_transshipment_legs_are_stored_as_distinct_voyages(self):
        self._ingest()

        voyages = set(
            TrackingEvent.objects.filter(team=self.team)
            .exclude(voyage_number="")
            .values_list("voyage_number", flat=True)
        )
        self.assertEqual(voyages, {"047E", "112W"})

    def test_an_unclassifiable_event_keeps_its_description_rather_than_being_dropped(self):
        self._ingest()

        customs = TrackingEvent.objects.get(team=self.team, description="Customs released")
        self.assertEqual(customs.event_type, TrackingEvent.EventType.UNKNOWN)
        self.assertEqual(customs.location_unlocode, "SGSIN")
        self.assertTrue(customs.is_actual)

    def test_provider_only_facts_are_recoverable_from_the_stored_event(self):
        self._ingest()
        loaded = TrackingEvent.objects.get(team=self.team, event_code="LOAD")

        detail = loaded.raw_data["_vizion"]
        self.assertEqual(detail["vessel_mmsi"], "636020947")
        self.assertEqual(detail["source"], "carrier")
        self.assertEqual(detail["shipment_location_type_code"], "POL")
        self.assertEqual(detail["raw_description"], "LOADED ON BOARD")

    def test_the_timeline_selector_reads_them_without_knowing_the_provider(self):
        self._ingest()

        events = list(get_tracking_events_for_container(self.team, self.container))
        self.assertEqual(len(events), 9)

    # ------------------------------------------------------------------
    # ETA
    # ------------------------------------------------------------------

    def test_the_forecast_arrival_reaches_the_canonical_model_as_a_forecast_event(self):
        self._ingest()

        # Unlike Traqo, whose ETA existed only as a top-level field, Vizion's ETA *is* a
        # milestone — so it becomes an ordinary ESTIMATED VESSEL_ARRIVED row with no
        # Vizion-specific code at all.
        forecast = (
            TrackingEvent.objects.filter(
                team=self.team,
                event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
                event_type=TrackingEvent.EventType.VESSEL_ARRIVED,
            )
            .order_by("-event_datetime")
            .first()
        )
        self.assertIsNotNone(forecast)
        # The newest update's forecast wins, because updates are applied oldest-first.
        self.assertEqual(forecast.event_datetime, datetime(2026, 9, 12, 7, 30, tzinfo=UTC))
        self.assertEqual(forecast.location_unlocode, "NLRTM")

    def test_a_transshipment_arrival_suppresses_the_pod_eta_a_canonical_gap(self):
        """CANONICAL GAP, demonstrated. Not Vizion's fault and not fixed here.

        ``normalize_dcsa_event_type`` maps *every* TRANSPORT/ARRI to VESSEL_ARRIVED,
        because the canonical classifier has no way to know which leg an arrival belongs
        to. ``ARRIVAL_ACTUAL_EVENT_TYPES`` then contains VESSEL_ARRIVED, and both
        ``has_journey_arrived`` and ``get_container_tracking_eta_event`` test only whether
        such an event *exists* — never whether it is at or after the forecast, which is
        what their own docstrings claim.

        So a box that has genuinely arrived at Singapore, with the POD five weeks away, is
        treated as arrived: its ETA is hidden and no ETA observation is recorded. Any DCSA
        provider that reports transshipment calls hits this; Vizion's multi-leg data is
        simply the first payload in this installation rich enough to expose it.
        """
        self._ingest()

        # The forecast is stored (previous test) but not offered as the container's ETA.
        self.assertIsNone(get_container_tracking_eta_event(self.team, self.container))

    def test_the_provider_eta_observation_is_recorded_when_the_journey_is_open(self):
        result = self._ingest(updates=direct_journey_updates())

        self.assertTrue(result.eta_observation_recorded)
        history = ETAHistory.objects.get(team=self.team)
        self.assertEqual(history.source, PROVIDER_CODE)
        self.assertEqual(history.location_unlocode, "NLRTM")
        # A *specific* target, because Vizion labels the milestone POD — unlike Traqo,
        # whose ETA had to be recorded as provider-defined.
        self.assertEqual(history.raw_payload["eta_target"], "vessel_arrival_pod")

    def test_no_eta_observation_is_recorded_once_the_journey_counts_as_arrived(self):
        # The same canonical gap as above, seen from the ETA writer's side.
        result = self._ingest()

        self.assertFalse(result.eta_observation_recorded)
        self.assertEqual(ETAHistory.objects.filter(team=self.team).count(), 0)

    def test_re_ingesting_an_unchanged_eta_writes_no_second_history_row(self):
        self._ingest(updates=direct_journey_updates())
        self._ingest(updates=direct_journey_updates())

        self.assertEqual(ETAHistory.objects.filter(team=self.team).count(), 1)

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def test_the_same_payload_ingested_twice_creates_no_duplicates(self):
        first = self._ingest()
        second = self._ingest()

        # Ten milestone DTOs across the two updates, nine distinct events: the gate-in is
        # repeated verbatim in both envelopes and collapses onto one row.
        self.assertEqual(first.events_created, 9)
        self.assertEqual(second.events_created, 0)
        self.assertEqual(second.events_updated, 10)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 9)

    def test_every_event_gets_a_fingerprint(self):
        self._ingest()

        self.assertFalse(TrackingEvent.objects.filter(team=self.team, event_fingerprint="").exists())

    def test_a_moved_eta_adds_a_row_rather_than_rewriting_the_old_forecast(self):
        # Only the first update, so the earlier ETA is all that is stored.
        self._ingest(updates=[fixture("updates_transshipment.json")[0]])
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 8)

        self._ingest()

        # The revised forecast is a new row: Vizion offers no stable event identity, so
        # the field-based fingerprint treats a changed instant as a different event. An
        # extra forecast row is recoverable; a silently rewritten history is not.
        forecasts = TrackingEvent.objects.filter(
            team=self.team, event_time_type=TrackingEvent.EventTimeType.ESTIMATED
        ).order_by("event_datetime")
        self.assertEqual(
            [event.event_datetime for event in forecasts],
            [
                datetime(2026, 9, 10, 5, 0, tzinfo=UTC),
                datetime(2026, 9, 12, 7, 30, tzinfo=UTC),
            ],
        )

    def test_an_eta_that_becomes_an_actual_arrival_stops_being_shown_as_an_eta(self):
        """The documented ETA→ATA flip, and the canonical rule that makes it safe.

        Vizion reuses one milestone for both: ``planned`` flips true→false and the
        classifier EST→ACT. Because the mapper claims no event identity, that produces two
        rows rather than editing one — which is what DCSA models anyway, and which the
        canonical ETA selector handles correctly on its own.
        """
        self._ingest(updates=[direct_journey_updates()[0]])
        self.assertIsNotNone(get_container_tracking_eta_event(self.team, self.container))

        arrived = direct_journey_updates()[1]
        for milestone in arrived["payload"]["milestones"]:
            if milestone["id"] == "m-0007":
                milestone["planned"] = False
                milestone["journey_event"]["event_classifier"] = "ACT"
        self._ingest(updates=[arrived])

        # Two rows now exist for the same Vizion milestone — the forecast and the actual
        # arrival — which is what DCSA models anyway. The canonical selector suppresses
        # the forecast because an actual arrival exists, so nothing displays a future
        # arrival for a box that has already berthed.
        self.assertIsNone(get_container_tracking_eta_event(self.team, self.container))
        self.assertTrue(
            TrackingEvent.objects.filter(
                team=self.team,
                event_type=TrackingEvent.EventType.VESSEL_ARRIVED,
                event_time_type=TrackingEvent.EventTimeType.ACTUAL,
            ).exists()
        )

    # ------------------------------------------------------------------
    # Isolation from the rest of the architecture
    # ------------------------------------------------------------------

    def test_the_scheduled_carrier_poller_does_not_drive_vizion(self):
        self.assertFalse(is_polled_by_carrier_sync(PROVIDER_CODE))
        source = get_non_carrier_source(PROVIDER_CODE)
        self.assertIsNotNone(source)
        self.assertIn("vizion_test", source.refresh_hint)

    def test_a_stored_payload_can_be_re_read_without_refetching(self):
        self._ingest()
        stored = TrackingRawPayload.objects.get(team=self.team, provider__code=PROVIDER_CODE)

        source = get_non_carrier_source(PROVIDER_CODE)
        events = source.read_payload(stored.payload_json, CONTAINER_NUMBER)

        self.assertEqual(len(events), 10)

    def test_vizion_is_absent_from_the_carrier_registry(self):
        from apps.scm.integrations.carriers.registry import list_carriers

        self.assertNotIn(PROVIDER_CODE, {carrier.provider_code for carrier in list_carriers()})

    def test_ingesting_leaves_another_providers_events_untouched(self):
        other = TrackingProvider.objects.create(code="maersk", name="Maersk")
        TrackingEvent.objects.create(
            team=self.team,
            container=self.container,
            provider=other,
            event_type=TrackingEvent.EventType.GATE_IN,
            event_time_type=TrackingEvent.EventTimeType.ACTUAL,
            event_datetime=datetime(2026, 8, 1, 1, 15, tzinfo=UTC),
            event_fingerprint="maersk-fingerprint",
        )

        self._ingest()

        self.assertEqual(TrackingEvent.objects.filter(team=self.team, provider=other).count(), 1)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team, provider__code=PROVIDER_CODE).count(), 9)
