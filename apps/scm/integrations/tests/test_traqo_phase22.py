"""Tests for the Phase 2.2 apparatus: candidate selection, ETA targets, snapshots, drift.

Offline and deterministic. Every payload is written here rather than fetched, so the
cases that matter — an arrived container rejected, two ETAs that must not be subtracted,
a provider that renamed its own rows between fetches — are stated explicitly instead of
depending on what a provider happened to return today. No test makes a network call.
"""

from datetime import UTC, datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.integrations.traqo.benchmark import (
    SnapshotMismatchError,
    assess_candidate,
    choose_candidate,
    compare_etas,
    compare_snapshots,
    read_carrier_eta_target,
    read_traqo_eta_target,
    render_drift_text,
)
from apps.scm.integrations.traqo.benchmark.candidates import assess_reference_candidates
from apps.scm.integrations.traqo.benchmark.drift import (
    ETA_DELAYED,
    ETA_IMPROVED,
    ETA_TARGET_CHANGED,
    ETA_UNCHANGED,
    IDENTITY_STABLE,
    IDENTITY_UNSTABLE,
)
from apps.scm.integrations.traqo.benchmark.eta_target import (
    COMPARABLE,
    FINAL_DESTINATION,
    NOT_COMPARABLE_DIFFERENT_TARGETS,
    NOT_COMPARABLE_MISSING_ETA,
    NOT_COMPARABLE_UNKNOWN_TARGET,
    PORT_ARRIVAL,
    POST_POD_DELIVERY,
    PROVIDER_DEFINED,
    UNKNOWN,
)
from apps.scm.integrations.traqo.benchmark.runner import compare_providers
from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingSubscription
from apps.scm.visibility.read_models import JourneyState
from apps.teams.models import Team

_EventType = TrackingEvent.EventType
_TimeType = TrackingEvent.EventTimeType

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class Phase22TestCase(TestCase):
    """One team, two providers, and factories for containers and events."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="phase22", slug="phase22")
        cls.equipment_type = EquipmentType.objects.get_or_create(
            iso_code="22G1",
            defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
        )[0]
        cls.maersk = TrackingProvider.objects.create(code="maersk", name="Maersk")
        cls.traqo = TrackingProvider.objects.create(code="traqo", name="Traqo Ocean")

    _serial = 100000
    _fingerprint = 0

    def container(self) -> Container:
        """Create a container with a real check digit — the model validates it."""
        type(self)._serial += 1
        serial = str(type(self)._serial)
        return Container.objects.create(
            team=self.team,
            owner_code="MRS",
            category_id="U",
            serial_number=serial,
            check_digit=calculate_check_digit("MRS", "U", serial),
            equipment_type=self.equipment_type,
        )

    def event(
        self,
        container,
        provider,
        *,
        event_type=_EventType.DISCHARGED,
        time_type=_TimeType.ACTUAL,
        when=NOW,
        location_name="Rotterdam",
        unlocode="",
        event_code="DISC",
    ) -> TrackingEvent:
        type(self)._fingerprint += 1
        return TrackingEvent.objects.create(
            team=self.team,
            container=container,
            provider=provider,
            event_type=event_type,
            event_time_type=time_type,
            event_datetime=when,
            received_at=when + timedelta(hours=2) if when else None,
            location_name=location_name,
            location_unlocode=unlocode,
            event_code=event_code,
            carrier_event_type="TRANSPORT",
            event_fingerprint=f"phase22-{type(self)._fingerprint}",
        )

    def subscription(self, container, provider, *, status=TrackingSubscription.Status.ACTIVE):
        return TrackingSubscription.objects.create(
            team=self.team,
            container=container,
            provider=provider,
            tracking_reference=container.container_id,
            status=status,
            last_synced_at=NOW,
        )


# ---------------------------------------------------------------------------
# A. Candidate selection
# ---------------------------------------------------------------------------


class CandidateSelectionTest(Phase22TestCase):
    """What may and may not be spent a live provider request on."""

    def _in_transit_container(self) -> Container:
        """A container that has departed and has a future forecast arrival."""
        container = self.container()
        self.subscription(container, self.maersk)
        self.event(container, self.maersk, event_type=_EventType.VESSEL_DEPARTED, when=NOW - timedelta(days=10))
        self.event(container, self.maersk, event_type=_EventType.LOADED_ON_VESSEL, when=NOW - timedelta(days=11))
        self.event(container, self.maersk, event_type=_EventType.GATE_OUT, when=NOW - timedelta(days=12))
        self.event(
            container,
            self.maersk,
            event_type=_EventType.VESSEL_ARRIVED,
            time_type=_TimeType.ESTIMATED,
            when=NOW + timedelta(days=5),
            location_name="Gothenburg",
        )
        return container

    def _assess(self, container):
        return assess_candidate(
            team=self.team,
            container=container,
            reference_provider_code="maersk",
            now=NOW,
        )

    def test_a_genuinely_in_transit_container_qualifies(self):
        assessment = self._assess(self._in_transit_container())

        self.assertEqual(assessment.journey_state, JourneyState.IN_TRANSIT)
        self.assertTrue(assessment.has_future_forecast)
        self.assertFalse(assessment.has_arrived)
        self.assertEqual(assessment.rejections, ())
        self.assertTrue(assessment.qualifies)

    def test_an_arrived_container_is_rejected(self):
        container = self._in_transit_container()
        self.event(
            container,
            self.maersk,
            event_type=_EventType.VESSEL_ARRIVED,
            when=NOW - timedelta(days=1),
            location_name="Gothenburg",
        )

        assessment = self._assess(container)

        self.assertEqual(assessment.journey_state, JourneyState.ARRIVED)
        self.assertFalse(assessment.qualifies)
        self.assertIn("not in transit", " ".join(assessment.rejections))
        self.assertIn("actual arrival", " ".join(assessment.rejections))

    def test_a_completed_container_is_rejected(self):
        container = self._in_transit_container()
        self.event(container, self.maersk, event_type=_EventType.DELIVERED, when=NOW - timedelta(hours=6))

        assessment = self._assess(container)

        self.assertEqual(assessment.journey_state, JourneyState.DELIVERED)
        self.assertFalse(assessment.qualifies)

    def test_a_container_whose_only_forecast_has_passed_is_rejected(self):
        """A stale forecast is not something ETA drift can be measured against."""
        container = self.container()
        self.subscription(container, self.maersk)
        for offset in (10, 11, 12):
            self.event(container, self.maersk, event_type=_EventType.VESSEL_DEPARTED, when=NOW - timedelta(days=offset))
        self.event(
            container,
            self.maersk,
            event_type=_EventType.VESSEL_ARRIVED,
            time_type=_TimeType.ESTIMATED,
            when=NOW - timedelta(days=2),
        )

        assessment = self._assess(container)

        self.assertFalse(assessment.has_future_forecast)
        self.assertFalse(assessment.qualifies)
        self.assertIn("already in the past", " ".join(assessment.rejections))

    def test_a_container_with_no_forecast_at_all_is_rejected(self):
        container = self.container()
        self.subscription(container, self.maersk)
        for offset in (10, 11, 12):
            self.event(container, self.maersk, event_type=_EventType.VESSEL_DEPARTED, when=NOW - timedelta(days=offset))

        assessment = self._assess(container)

        self.assertFalse(assessment.qualifies)
        self.assertIn("no canonical arrival forecast", " ".join(assessment.rejections))

    def test_too_little_reference_data_is_rejected(self):
        container = self.container()
        self.subscription(container, self.maersk)
        self.event(container, self.maersk, event_type=_EventType.VESSEL_DEPARTED, when=NOW - timedelta(days=10))
        self.event(
            container,
            self.maersk,
            event_type=_EventType.VESSEL_ARRIVED,
            time_type=_TimeType.ESTIMATED,
            when=NOW + timedelta(days=4),
        )

        assessment = self._assess(container)

        self.assertFalse(assessment.qualifies)
        self.assertIn("fewer than the 3", " ".join(assessment.rejections))

    def test_an_active_subscription_alone_does_not_qualify_an_arrived_container(self):
        """The canonical journey rule decides, not the state of the watch."""
        container = self._in_transit_container()
        self.event(container, self.maersk, event_type=_EventType.VESSEL_ARRIVED, when=NOW - timedelta(days=1))

        assessment = self._assess(container)

        self.assertEqual(assessment.subscription_status, TrackingSubscription.Status.ACTIVE)
        self.assertFalse(assessment.qualifies)

    def test_a_cancelled_watch_is_rejected(self):
        container = self._in_transit_container()
        TrackingSubscription.objects.filter(container=container).update(status=TrackingSubscription.Status.CANCELLED)

        assessment = self._assess(container)

        self.assertFalse(assessment.qualifies)
        self.assertIn("cancelled", " ".join(assessment.rejections))

    def test_choose_candidate_returns_none_when_nothing_qualifies(self):
        arrived = self._in_transit_container()
        self.event(arrived, self.maersk, event_type=_EventType.VESSEL_ARRIVED, when=NOW - timedelta(days=1))

        assessments = assess_reference_candidates(team=self.team, reference_provider_code="maersk", now=NOW)

        self.assertTrue(assessments)
        self.assertIsNone(choose_candidate(assessments))

    def test_choose_candidate_prefers_the_in_transit_container(self):
        arrived = self._in_transit_container()
        self.event(arrived, self.maersk, event_type=_EventType.VESSEL_ARRIVED, when=NOW - timedelta(days=1))
        moving = self._in_transit_container()

        assessments = assess_reference_candidates(team=self.team, reference_provider_code="maersk", now=NOW)
        chosen = choose_candidate(assessments)

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.container_number, moving.container_id)


# ---------------------------------------------------------------------------
# B. ETA target and comparability
# ---------------------------------------------------------------------------


def traqo_payload(
    *,
    eta: str,
    pod: str | None = "2026-07-01 15:09:00",
    postpod: str | None = "2026-07-13 09:30:12",
    destination: str = "BORAAS",
    last_event: str | None = "2026-07-13 13:15:00",
    predictive_eta: str | None = None,
) -> dict:
    """A production-shaped Traqo envelope with only the fields the classifier reads."""
    voyage_plan = []
    if pod:
        voyage_plan.append({"idx": 1, "phase": "pod", "location_id": 2, "date": pod, "is_actual": 1})
    if postpod:
        voyage_plan.append(
            {
                "idx": 2,
                "phase": "postpod",
                "location_id": 3,
                "date": postpod,
                "is_actual": 1,
                "predictive_eta": predictive_eta,
            }
        )
    events = []
    if last_event:
        events.append(
            {
                "idx": 1,
                "event_id": 1,
                "name": 4122770,
                "creation": "2026-08-19 19:55:37.097479",
                "modified": "2026-08-19 19:55:37.097479",
                "timestamp": last_event,
                "event_code": "GTIN",
                "location": "Goteborg",
                "location_id": 2,
                "is_actual": 1,
            }
        )
    return {
        "success": True,
        "data": {
            "reference_number": "CPWU2588297",
            "eta": eta,
            "eta_reliable": True,
            "eta_warning": None,
            "remaining_days": 0,
            "status": "DELIVERED",
            "destination": destination,
            "last_updated_at": "2026-08-19 19:55:37.211575",
            "last_synced_at": "2026-08-19 19:55:24.717817",
            "voyage_plan_table": voyage_plan,
            "events_table": events,
            "eta_history_table": [{"idx": 1, "eta": eta, "logged_at": "2026-08-19 19:55:37.029631"}],
            "locations_table": [
                {"location_id": 2, "location": "Goteborg", "timezone": "Europe/Stockholm"},
                {"location_id": 3, "location": "BORAAS", "timezone": None},
            ],
            "route_json": '[{"type": "sea", "from": {"locode": "CNYTN"}, "to": {"locode": "SEGOT"}}]',
        },
    }


class TraqoEtaTargetTest(TestCase):
    """What Traqo's data.eta is read as, from Traqo's data alone."""

    def test_an_eta_on_the_pod_phase_reads_as_a_port_arrival(self):
        reading = read_traqo_eta_target(traqo_payload(eta="2026-07-01 15:09:00"))

        self.assertEqual(reading.target, PORT_ARRIVAL)
        self.assertEqual(reading.matched_milestone, "voyage_plan.pod")
        self.assertTrue(reading.is_specific)

    def test_an_eta_on_the_postpod_phase_at_the_named_destination_reads_as_final(self):
        reading = read_traqo_eta_target(traqo_payload(eta="2026-07-13 09:30:12", last_event=None, destination="BORAAS"))

        self.assertEqual(reading.target, FINAL_DESTINATION)
        self.assertEqual(reading.matched_location, "BORAAS")

    def test_an_eta_on_a_postpod_phase_that_is_not_the_destination_reads_as_post_pod_only(self):
        """An inland leg short of the final place must not be called the final place."""
        reading = read_traqo_eta_target(
            traqo_payload(eta="2026-07-13 09:30:12", last_event=None, destination="Jönköping")
        )

        self.assertEqual(reading.target, POST_POD_DELIVERY)

    def test_an_eta_equal_to_the_last_event_reads_as_provider_defined(self):
        """The production case: eta restates the empty return, not a future milestone."""
        reading = read_traqo_eta_target(traqo_payload(eta="2026-07-13 13:15:00"))

        self.assertEqual(reading.target, PROVIDER_DEFINED)
        self.assertEqual(reading.matched_milestone, "events_table.last")
        self.assertFalse(reading.is_specific)

    def test_an_eta_matching_nothing_reads_as_provider_defined(self):
        reading = read_traqo_eta_target(traqo_payload(eta="2026-09-30 06:00:00"))

        self.assertEqual(reading.target, PROVIDER_DEFINED)
        self.assertEqual(reading.matched_milestone, "")

    def test_a_missing_eta_reads_as_unknown(self):
        payload = traqo_payload(eta="2026-07-01 15:09:00")
        payload["data"]["eta"] = None

        self.assertEqual(read_traqo_eta_target(payload).target, UNKNOWN)

    def test_the_pod_phase_wins_when_two_milestones_coincide(self):
        """Attribution is to the earlier, more conservative milestone."""
        reading = read_traqo_eta_target(
            traqo_payload(eta="2026-07-01 15:09:00", postpod="2026-07-01 15:09:00", last_event="2026-07-01 15:09:00")
        )

        self.assertEqual(reading.matched_milestone, "voyage_plan.pod")

    def test_a_predictive_eta_is_preferred_over_a_settled_date(self):
        reading = read_traqo_eta_target(
            traqo_payload(
                eta="2026-07-20 08:00:00",
                last_event=None,
                predictive_eta="2026-07-20 08:00:00",
            )
        )

        self.assertEqual(reading.matched_milestone, "voyage_plan.postpod")

    def test_the_reading_records_every_milestone_it_tested(self):
        reading = read_traqo_eta_target(traqo_payload(eta="2026-07-13 13:15:00"))

        self.assertEqual(reading.candidates["voyage_plan.pod"], "2026-07-01T15:09:00")
        self.assertEqual(reading.candidates["voyage_plan.postpod_location"], "BORAAS")
        self.assertEqual(reading.candidates["destination"], "BORAAS")


class CarrierEtaTargetTest(Phase22TestCase):
    """What a carrier's canonical forecast event is read as."""

    def test_a_forecast_vessel_arrival_reads_as_a_port_arrival(self):
        container = self.container()
        event = self.event(
            container,
            self.maersk,
            event_type=_EventType.VESSEL_ARRIVED,
            time_type=_TimeType.ESTIMATED,
            when=NOW + timedelta(days=3),
            location_name="Gothenburg",
            unlocode="SEGOT",
        )

        reading = read_carrier_eta_target(event)

        self.assertEqual(reading.target, PORT_ARRIVAL)
        self.assertIn("SEGOT", reading.reason)
        self.assertTrue(reading.is_specific)

    def test_a_bare_eta_update_reads_as_provider_defined(self):
        container = self.container()
        event = self.event(
            container,
            self.maersk,
            event_type=_EventType.ETA_UPDATED,
            time_type=_TimeType.ESTIMATED,
            when=NOW + timedelta(days=3),
        )

        self.assertEqual(read_carrier_eta_target(event).target, PROVIDER_DEFINED)

    def test_no_event_reads_as_unknown(self):
        self.assertEqual(read_carrier_eta_target(None).target, UNKNOWN)


class EtaComparabilityTest(TestCase):
    """When two ETAs may be subtracted, and when the number is withheld."""

    def _compare(self, *, reference_target, candidate_target, hours_apart=24):
        return compare_etas(
            reference_provider="maersk",
            candidate_provider="traqo",
            reference_eta_at=NOW,
            candidate_eta_at=NOW + timedelta(hours=hours_apart),
            reference_target=reference_target,
            candidate_target=candidate_target,
        )

    def test_the_same_specific_target_allows_a_numeric_delta(self):
        comparison = self._compare(reference_target=PORT_ARRIVAL, candidate_target=PORT_ARRIVAL)

        self.assertTrue(comparison.comparable)
        self.assertEqual(comparison.verdict, COMPARABLE)
        self.assertEqual(comparison.difference_hours, 24.0)

    def test_different_targets_are_not_comparable_and_report_no_number(self):
        comparison = self._compare(reference_target=PORT_ARRIVAL, candidate_target=FINAL_DESTINATION)

        self.assertFalse(comparison.comparable)
        self.assertEqual(comparison.verdict, NOT_COMPARABLE_DIFFERENT_TARGETS)
        self.assertIsNone(comparison.difference_hours)

    def test_two_provider_defined_targets_are_not_comparable(self):
        """Matching ignorance is not agreement."""
        comparison = self._compare(reference_target=PROVIDER_DEFINED, candidate_target=PROVIDER_DEFINED)

        self.assertFalse(comparison.comparable)
        self.assertEqual(comparison.verdict, NOT_COMPARABLE_UNKNOWN_TARGET)
        self.assertIsNone(comparison.difference_hours)

    def test_two_unknown_targets_are_not_comparable(self):
        comparison = self._compare(reference_target=UNKNOWN, candidate_target=UNKNOWN)

        self.assertFalse(comparison.comparable)
        self.assertIsNone(comparison.difference_hours)

    def test_a_missing_eta_is_not_comparable(self):
        comparison = compare_etas(
            reference_provider="maersk",
            candidate_provider="traqo",
            reference_eta_at=None,
            candidate_eta_at=NOW,
            reference_target=PORT_ARRIVAL,
            candidate_target=PORT_ARRIVAL,
        )

        self.assertFalse(comparison.comparable)
        self.assertEqual(comparison.verdict, NOT_COMPARABLE_MISSING_ETA)
        self.assertIsNone(comparison.difference_hours)


# ---------------------------------------------------------------------------
# C. Snapshot
# ---------------------------------------------------------------------------


class SnapshotTest(Phase22TestCase):
    """What a T0 run preserves, and what it must not."""

    def setUp(self):
        self.box = self.container()
        self.subscription(self.box, self.maersk)
        self.subscription(self.box, self.traqo)
        self.event(self.box, self.maersk, event_type=_EventType.VESSEL_DEPARTED, when=NOW - timedelta(days=20))
        self.event(
            self.box,
            self.maersk,
            event_type=_EventType.VESSEL_ARRIVED,
            time_type=_TimeType.ESTIMATED,
            when=NOW + timedelta(days=4),
            location_name="Gothenburg",
            unlocode="SEGOT",
        )
        self.event(self.box, self.traqo, event_type=_EventType.VESSEL_DEPARTED, when=NOW - timedelta(days=20))

    def _run(self):
        return compare_providers(
            team=self.team,
            container=self.box,
            sealine="MAEU",
            ingest_candidate=False,
            tolerance_hours=24,
        )

    def test_a_run_produces_a_snapshot_with_the_expected_sections(self):
        snapshot = self._run().snapshot

        for key in (
            "run_at",
            "container",
            "journey_state",
            "reference",
            "candidate",
            "eta_comparison",
            "event_match",
            "freshness",
        ):
            self.assertIn(key, snapshot)
        self.assertEqual(snapshot["container"], self.box.container_id)
        self.assertEqual(snapshot["journey_state"], JourneyState.IN_TRANSIT)

    def test_the_snapshot_preserves_each_provider_as_its_own_source(self):
        snapshot = self._run().snapshot

        self.assertEqual(snapshot["reference"]["provider"], "maersk")
        self.assertEqual(snapshot["candidate"]["provider"], "traqo")
        self.assertTrue(all(event["provider"] == "maersk" for event in snapshot["reference"]["events"]))
        self.assertTrue(all(event["provider"] == "traqo" for event in snapshot["candidate"]["events"]))

    def test_the_snapshot_records_the_reference_eta_target(self):
        snapshot = self._run().snapshot

        self.assertEqual(snapshot["reference"]["eta_target"]["target"], PORT_ARRIVAL)
        self.assertEqual(snapshot["reference"]["current_eta_at"], (NOW + timedelta(days=4)).isoformat())

    def test_the_snapshot_separates_forecast_events_from_observed_ones(self):
        snapshot = self._run().snapshot

        self.assertEqual(snapshot["reference"]["actual"], 1)
        self.assertEqual(snapshot["reference"]["forecast"], 1)
        self.assertEqual(len(snapshot["reference"]["forecast_events"]), 1)
        self.assertEqual(snapshot["reference"]["latest_actual"]["event_type"], _EventType.VESSEL_DEPARTED)

    def test_the_snapshot_serialises_to_json_without_credentials(self):
        import json

        from apps.scm.integrations.traqo.benchmark import render_json

        text = json.dumps(render_json(self._run()))

        for forbidden in ("api_key", "apikey", "authorization", "bearer", "secret", "password"):
            self.assertNotIn(forbidden, text.lower())

    def test_a_run_that_observed_no_candidate_payload_says_so_rather_than_reporting_zeros(self):
        snapshot = self._run().snapshot

        self.assertFalse(snapshot["candidate"]["observed"])
        self.assertIsNone(snapshot["provider_structure"])
        self.assertEqual(snapshot["candidate_event_identity"], [])

    def test_no_synthetic_tracking_event_is_created_by_a_snapshot_run(self):
        before = TrackingEvent.objects.filter(team=self.team, container=self.box).count()

        self._run()

        self.assertEqual(TrackingEvent.objects.filter(team=self.team, container=self.box).count(), before)


# ---------------------------------------------------------------------------
# D. Previous-run comparison
# ---------------------------------------------------------------------------


def snapshot_fixture(
    *,
    run_at="2026-08-20T12:00:00+00:00",
    container="MRSU1000017",
    reference_eta="2026-08-24T12:00:00+00:00",
    reference_target=PORT_ARRIVAL,
    candidate_eta="2026-08-25T12:00:00+00:00",
    candidate_target=PROVIDER_DEFINED,
    reference_events=None,
    candidate_events=None,
    identity=None,
) -> dict:
    """A snapshot with only the fields the drift comparison reads."""
    reference_events = (
        reference_events
        if reference_events is not None
        else [event_fixture("maersk", "vessel_departed", "2026-08-01T09:00:00+00:00", "Yantian")]
    )
    candidate_events = (
        candidate_events
        if candidate_events is not None
        else [event_fixture("traqo", "vessel_departed", "2026-08-01T09:00:00+00:00", "Yantian")]
    )
    return {
        "schema": "traqo-benchmark-snapshot/1",
        "run_at": run_at,
        "container": container,
        "journey_state": "in_transit",
        "reference": {
            "provider": "maersk",
            "total": len(reference_events),
            "events": reference_events,
            "latest_actual": reference_events[-1] if reference_events else None,
            "current_eta_at": reference_eta,
            "eta_target": {"target": reference_target},
        },
        "candidate": {
            "provider": "traqo",
            "total": len(candidate_events),
            "events": candidate_events,
            "latest_actual": candidate_events[-1] if candidate_events else None,
            "provider_eta_at": candidate_eta,
            "eta_target": {"target": candidate_target},
        },
        "candidate_event_identity": identity if identity is not None else [identity_fixture()],
    }


def event_fixture(provider, event_type, when, location, *, time_type="actual") -> dict:
    return {
        "provider": provider,
        "event_type": event_type,
        "event_time_type": time_type,
        "event_datetime": when,
        "location_name": location,
        "fingerprint": f"{provider}|{event_type}|{time_type}|{when}|{location.upper()}",
    }


def identity_fixture(*, name=4122770, event_id=1, creation="2026-08-19 19:55:37.097479") -> dict:
    return {
        "idx": 1,
        "event_id": event_id,
        "name": name,
        "creation": creation,
        "modified": creation,
        "timestamp": "2026-08-01 09:00:00",
        "event_code": "DEPA",
        "location": "Yantian",
    }


class DriftComparisonTest(TestCase):
    """What a second observation reports about the first."""

    def test_an_unchanged_eta_reports_unchanged_with_zero_drift(self):
        diff = compare_snapshots(snapshot_fixture(), snapshot_fixture(run_at="2026-08-27T12:00:00+00:00"))

        self.assertEqual(diff["eta_drift"]["reference"]["verdict"], ETA_UNCHANGED)
        self.assertEqual(diff["eta_drift"]["reference"]["drift_hours"], 0.0)
        self.assertEqual(diff["interval_hours"], 168.0)

    def test_a_later_eta_reports_delayed(self):
        diff = compare_snapshots(
            snapshot_fixture(),
            snapshot_fixture(reference_eta="2026-08-26T12:00:00+00:00"),
        )

        self.assertEqual(diff["eta_drift"]["reference"]["verdict"], ETA_DELAYED)
        self.assertEqual(diff["eta_drift"]["reference"]["drift_hours"], 48.0)

    def test_an_earlier_eta_reports_improved(self):
        diff = compare_snapshots(
            snapshot_fixture(),
            snapshot_fixture(reference_eta="2026-08-23T00:00:00+00:00"),
        )

        self.assertEqual(diff["eta_drift"]["reference"]["verdict"], ETA_IMPROVED)
        self.assertEqual(diff["eta_drift"]["reference"]["drift_hours"], -36.0)

    def test_drift_is_per_provider_and_never_across_providers(self):
        """Traqo's new ETA must not be differenced against Maersk's old one."""
        diff = compare_snapshots(
            snapshot_fixture(),
            snapshot_fixture(candidate_eta="2026-08-28T12:00:00+00:00"),
        )

        self.assertEqual(diff["eta_drift"]["reference"]["verdict"], ETA_UNCHANGED)
        self.assertEqual(diff["eta_drift"]["candidate"]["drift_hours"], 72.0)

    def test_a_changed_eta_target_withholds_the_drift_figure(self):
        diff = compare_snapshots(
            snapshot_fixture(reference_target=PORT_ARRIVAL),
            snapshot_fixture(reference_target=FINAL_DESTINATION, reference_eta="2026-08-30T12:00:00+00:00"),
        )

        self.assertEqual(diff["eta_drift"]["reference"]["verdict"], ETA_TARGET_CHANGED)
        self.assertIsNone(diff["eta_drift"]["reference"]["drift_hours"])

    def test_a_new_observed_event_is_reported_as_new(self):
        later = [
            event_fixture("traqo", "vessel_departed", "2026-08-01T09:00:00+00:00", "Yantian"),
            event_fixture("traqo", "vessel_arrived", "2026-08-22T06:00:00+00:00", "Gothenburg"),
        ]

        diff = compare_snapshots(snapshot_fixture(), snapshot_fixture(candidate_events=later))

        candidate = diff["events"]["candidate"]
        self.assertEqual(len(candidate["new_actual"]), 1)
        self.assertEqual(candidate["new_actual"][0]["event_type"], "vessel_arrived")
        self.assertTrue(candidate["timeline_grew"])

    def test_a_new_forecast_is_not_counted_as_an_observation(self):
        later = [
            event_fixture("traqo", "vessel_departed", "2026-08-01T09:00:00+00:00", "Yantian"),
            event_fixture("traqo", "vessel_arrived", "2026-09-02T06:00:00+00:00", "Gothenburg", time_type="estimated"),
        ]

        diff = compare_snapshots(snapshot_fixture(), snapshot_fixture(candidate_events=later))

        candidate = diff["events"]["candidate"]
        self.assertEqual(candidate["new_actual"], [])
        self.assertEqual(len(candidate["new_forecast"]), 1)
        self.assertFalse(candidate["timeline_grew"])

    def test_a_re_timestamped_event_is_a_correction_not_an_add_and_a_loss(self):
        later = [event_fixture("traqo", "vessel_departed", "2026-08-01T15:00:00+00:00", "Yantian")]

        diff = compare_snapshots(snapshot_fixture(), snapshot_fixture(candidate_events=later))

        candidate = diff["events"]["candidate"]
        self.assertEqual(candidate["new_actual"], [])
        self.assertEqual(candidate["disappeared"], [])
        self.assertEqual(len(candidate["corrections"]), 1)
        self.assertEqual(candidate["corrections"][0]["shift_hours"], 6.0)

    def test_stable_provider_identities_are_reported_stable(self):
        diff = compare_snapshots(snapshot_fixture(), snapshot_fixture())

        identity = diff["event_identity"]
        self.assertEqual(identity["verdict"], IDENTITY_STABLE)
        self.assertEqual(identity["compared_events"], 1)
        self.assertTrue(identity["fields"]["name"]["stable"])

    def test_a_renamed_provider_row_is_reported_unstable(self):
        """Traqo rebuilding its child table would reassign `name`."""
        diff = compare_snapshots(
            snapshot_fixture(identity=[identity_fixture(name=4122770)]),
            snapshot_fixture(identity=[identity_fixture(name=5500001)]),
        )

        identity = diff["event_identity"]
        self.assertEqual(identity["verdict"], IDENTITY_UNSTABLE)
        self.assertIn("name", identity["unstable_fields"])
        self.assertEqual(identity["fields"]["name"]["changed_events"], 1)

    def test_a_rebuilt_creation_timestamp_is_reported_unstable(self):
        diff = compare_snapshots(
            snapshot_fixture(identity=[identity_fixture(creation="2026-08-19 19:55:37.097479")]),
            snapshot_fixture(identity=[identity_fixture(creation="2026-08-27 08:11:02.001122")]),
        )

        self.assertIn("creation", diff["event_identity"]["unstable_fields"])

    def test_freshness_names_the_provider_that_reported_a_new_movement(self):
        later = [
            event_fixture("traqo", "vessel_departed", "2026-08-01T09:00:00+00:00", "Yantian"),
            event_fixture("traqo", "vessel_arrived", "2026-08-22T06:00:00+00:00", "Gothenburg"),
        ]

        diff = compare_snapshots(snapshot_fixture(), snapshot_fixture(candidate_events=later))

        self.assertEqual(diff["freshness"]["reported_new_movement_first"], "traqo")

    def test_freshness_reports_neither_rather_than_ranking_on_nothing(self):
        diff = compare_snapshots(snapshot_fixture(), snapshot_fixture())

        self.assertEqual(diff["freshness"]["reported_new_movement_first"], "neither")

    def test_two_different_containers_are_refused(self):
        with self.assertRaises(SnapshotMismatchError):
            compare_snapshots(snapshot_fixture(container="AAAU1000000"), snapshot_fixture(container="BBBU2000000"))

    def test_an_unrecognised_schema_is_refused(self):
        future = snapshot_fixture()
        future["schema"] = "traqo-benchmark-snapshot/99"

        with self.assertRaises(SnapshotMismatchError):
            compare_snapshots(snapshot_fixture(), future)

    def test_the_drift_report_renders_without_a_traceback(self):
        diff = compare_snapshots(snapshot_fixture(), snapshot_fixture(reference_eta="2026-08-26T12:00:00+00:00"))

        text = render_drift_text(diff)

        self.assertIn("ETA DRIFT", text)
        self.assertIn(ETA_DELAYED, text)
        self.assertIn("PROVIDER EVENT IDENTITY", text)


# ---------------------------------------------------------------------------
# E. Stale subscription repair
# ---------------------------------------------------------------------------


class StaleSubscriptionRepairTest(Phase22TestCase):
    """Repairing what the retired carrier-poller bug wrote, and nothing else."""

    def _run(self, subscription, *, events_created=10):
        """Give a subscription a successful sync run in its history."""
        from apps.scm.tracking.models import TrackingSyncRun

        return TrackingSyncRun.objects.create(
            team=self.team,
            subscription=subscription,
            provider=subscription.provider,
            status=TrackingSyncRun.Status.SUCCESS,
            started_at=timezone.now() - timedelta(days=10),
            finished_at=timezone.now() - timedelta(days=10),
            events_created=events_created,
        )

    def _stale_traqo_subscription(self):
        container = self.container()
        subscription = self.subscription(container, self.traqo)
        subscription.tracking_status = TrackingSubscription.TrackingStatus.NOT_CONFIGURED
        subscription.last_error_message = "No carrier registered for provider_code 'traqo'."
        subscription.last_event_at = timezone.now() - timedelta(days=10)
        subscription.save()
        self._run(subscription)
        return subscription

    def test_a_stale_traqo_subscription_is_returned_to_tracking(self):
        from apps.scm.tracking.repair import repair_non_carrier_tracking_status

        subscription = self._stale_traqo_subscription()

        repair = repair_non_carrier_tracking_status(subscription)

        subscription.refresh_from_db()
        self.assertTrue(repair.changed)
        self.assertEqual(repair.before, TrackingSubscription.TrackingStatus.NOT_CONFIGURED)
        self.assertEqual(repair.after, TrackingSubscription.TrackingStatus.TRACKING)
        self.assertEqual(subscription.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)
        self.assertEqual(subscription.last_error_message, "")

    def test_the_repair_does_not_move_the_last_sync_clock(self):
        """Freshness evidence must survive the repair."""
        from apps.scm.tracking.repair import repair_non_carrier_tracking_status

        subscription = self._stale_traqo_subscription()
        before = subscription.last_synced_at

        repair_non_carrier_tracking_status(subscription)

        subscription.refresh_from_db()
        self.assertEqual(subscription.last_synced_at, before)

    def test_the_repair_creates_no_sync_run(self):
        from apps.scm.tracking.models import TrackingSyncRun
        from apps.scm.tracking.repair import repair_non_carrier_tracking_status

        subscription = self._stale_traqo_subscription()
        before = TrackingSyncRun.objects.count()

        repair_non_carrier_tracking_status(subscription)

        self.assertEqual(TrackingSyncRun.objects.count(), before)

    def test_a_carrier_subscription_is_never_touched(self):
        from apps.scm.tracking.repair import repair_non_carrier_tracking_status

        container = self.container()
        subscription = self.subscription(container, self.maersk)
        subscription.tracking_status = TrackingSubscription.TrackingStatus.NOT_CONFIGURED
        subscription.save()
        self._run(subscription)

        repair = repair_non_carrier_tracking_status(subscription)

        subscription.refresh_from_db()
        self.assertFalse(repair.changed)
        self.assertEqual(subscription.tracking_status, TrackingSubscription.TrackingStatus.NOT_CONFIGURED)
        self.assertIn("carrier poller", repair.reason)

    def test_a_valid_traqo_subscription_is_not_modified(self):
        from apps.scm.tracking.repair import repair_non_carrier_tracking_status

        container = self.container()
        subscription = self.subscription(container, self.traqo)
        subscription.tracking_status = TrackingSubscription.TrackingStatus.TRACKING
        subscription.save()
        self._run(subscription)

        repair = repair_non_carrier_tracking_status(subscription)

        subscription.refresh_from_db()
        self.assertFalse(repair.changed)
        self.assertEqual(subscription.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)

    def test_a_subscription_with_no_successful_run_is_left_alone(self):
        """NOT_CONFIGURED may simply be true, and this cannot tell the difference."""
        from apps.scm.tracking.repair import repair_non_carrier_tracking_status

        container = self.container()
        subscription = self.subscription(container, self.traqo)
        subscription.tracking_status = TrackingSubscription.TrackingStatus.NOT_CONFIGURED
        subscription.save()

        repair = repair_non_carrier_tracking_status(subscription)

        subscription.refresh_from_db()
        self.assertFalse(repair.changed)
        self.assertEqual(subscription.tracking_status, TrackingSubscription.TrackingStatus.NOT_CONFIGURED)
        self.assertIn("no successful sync run", repair.reason)

    def test_a_successful_run_with_no_events_repairs_to_no_data(self):
        """The status follows the stored run, not a wish for it to say TRACKING."""
        from apps.scm.tracking.repair import repair_non_carrier_tracking_status

        container = self.container()
        subscription = self.subscription(container, self.traqo)
        subscription.tracking_status = TrackingSubscription.TrackingStatus.NOT_CONFIGURED
        subscription.save()
        self._run(subscription, events_created=0)

        repair = repair_non_carrier_tracking_status(subscription)

        self.assertEqual(repair.after, TrackingSubscription.TrackingStatus.NO_DATA)

    def test_dry_run_reports_the_change_without_making_it(self):
        from apps.scm.tracking.repair import repair_non_carrier_tracking_status

        subscription = self._stale_traqo_subscription()

        repair = repair_non_carrier_tracking_status(subscription, commit=False)

        subscription.refresh_from_db()
        self.assertFalse(repair.changed)
        self.assertEqual(repair.after, TrackingSubscription.TrackingStatus.TRACKING)
        self.assertEqual(subscription.tracking_status, TrackingSubscription.TrackingStatus.NOT_CONFIGURED)

    def test_the_sweep_only_considers_non_carrier_providers(self):
        from apps.scm.tracking.repair import repair_non_carrier_tracking_statuses

        stale = self._stale_traqo_subscription()
        carrier = self.subscription(self.container(), self.maersk)
        carrier.tracking_status = TrackingSubscription.TrackingStatus.NOT_CONFIGURED
        carrier.save()
        self._run(carrier)

        repairs = repair_non_carrier_tracking_statuses(team=self.team)

        self.assertEqual([repair.subscription_id for repair in repairs], [stale.pk])
        carrier.refresh_from_db()
        self.assertEqual(carrier.tracking_status, TrackingSubscription.TrackingStatus.NOT_CONFIGURED)

    def test_the_sweep_is_idempotent(self):
        from apps.scm.tracking.repair import repair_non_carrier_tracking_statuses

        self._stale_traqo_subscription()

        first = repair_non_carrier_tracking_statuses(team=self.team)
        second = repair_non_carrier_tracking_statuses(team=self.team)

        self.assertEqual([repair.changed for repair in first], [True])
        self.assertEqual(second, [])
