"""Tests for the Phase 2 provider benchmark: matching, metrics and reporting.

Deterministic and offline. The events are canonical ``TrackingEvent`` rows built here
rather than fetched, so each case — a clean match, a drifted timestamp, an event only one
provider has, a genuinely ambiguous pair, a missing UN/LOCODE, a missing vessel — is
stated explicitly instead of depending on what a provider happened to return today.
"""

import json
from datetime import UTC, datetime, timedelta

from django.test import TestCase

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.traqo.benchmark import (
    AMBIGUOUS,
    CANDIDATE_ONLY,
    MATCHED,
    REFERENCE_ONLY,
    match_events,
    render_json,
    render_text,
)
from apps.scm.integrations.traqo.benchmark.metrics import (
    freshness_metrics,
    location_metrics,
    summarise_matches,
    summarise_provider_events,
    vessel_metrics,
)
from apps.scm.integrations.traqo.benchmark.runner import ComparisonResult, compare_providers
from apps.scm.tracking.models import TrackingEvent, TrackingProvider
from apps.teams.models import Team

_EventType = TrackingEvent.EventType
_TimeType = TrackingEvent.EventTimeType

BASE = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)


class BenchmarkTestCase(TestCase):
    """Shared fixtures: one container, two providers, and an event factory."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="benchmark", slug="benchmark")
        equipment_type = EquipmentType.objects.get_or_create(
            iso_code="22G1",
            defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
        )[0]
        cls.container = Container.objects.create(
            team=cls.team,
            owner_code="MRS",
            category_id="U",
            serial_number="685942",
            check_digit=7,
            equipment_type=equipment_type,
        )
        cls.maersk = TrackingProvider.objects.create(code="maersk", name="Maersk")
        cls.traqo = TrackingProvider.objects.create(code="traqo", name="Traqo Ocean")

    _fingerprint = 0

    def event(
        self,
        provider,
        *,
        event_type=_EventType.DISCHARGED,
        time_type=_TimeType.ACTUAL,
        when=BASE,
        received_at=None,
        location_name="Rotterdam",
        unlocode="",
        vessel_name="",
        vessel_imo="",
        voyage="",
        transport_mode="",
        event_code="DISC",
        carrier_event_type="EQUIPMENT",
        latitude=None,
        longitude=None,
        description="",
    ) -> TrackingEvent:
        """Create one canonical event. Every field the benchmark reads is settable."""
        type(self)._fingerprint += 1
        return TrackingEvent.objects.create(
            team=self.team,
            container=self.container,
            provider=provider,
            event_type=event_type,
            event_time_type=time_type,
            event_datetime=when,
            received_at=received_at or (when + timedelta(hours=2) if when else None),
            location_name=location_name,
            location_unlocode=unlocode,
            location_latitude=latitude,
            location_longitude=longitude,
            vessel_name=vessel_name,
            vessel_imo=vessel_imo,
            voyage_number=voyage,
            transport_mode=transport_mode,
            event_code=event_code,
            carrier_event_type=carrier_event_type,
            description=description,
            event_fingerprint=f"benchmark-{type(self)._fingerprint}",
        )

    def verdicts(self, comparisons) -> list[str]:
        return [comparison.verdict for comparison in comparisons]


class EventMatchingTest(BenchmarkTestCase):
    """The pairing rules, stated one at a time."""

    def test_identical_events_match(self):
        reference = self.event(self.maersk, unlocode="NLRTM")
        candidate = self.event(self.traqo)

        comparisons = match_events([reference], [candidate])

        self.assertEqual(self.verdicts(comparisons), [MATCHED])
        self.assertEqual(comparisons[0].delta_seconds, 0)
        self.assertTrue(comparisons[0].is_tight)

    def test_a_small_timestamp_difference_still_matches(self):
        reference = self.event(self.maersk, when=BASE, unlocode="NLRTM")
        candidate = self.event(self.traqo, when=BASE + timedelta(minutes=28))

        comparisons = match_events([reference], [candidate])

        self.assertEqual(self.verdicts(comparisons), [MATCHED])
        self.assertEqual(comparisons[0].delta_minutes, 28.0)
        self.assertTrue(comparisons[0].is_tight)

    def test_a_difference_inside_tolerance_but_over_an_hour_matches_but_is_not_tight(self):
        reference = self.event(self.maersk, when=BASE)
        candidate = self.event(self.traqo, when=BASE + timedelta(hours=10))

        comparisons = match_events([reference], [candidate])

        self.assertEqual(self.verdicts(comparisons), [MATCHED])
        self.assertFalse(comparisons[0].is_tight)

    def test_a_difference_beyond_tolerance_does_not_match(self):
        reference = self.event(self.maersk, when=BASE)
        candidate = self.event(self.traqo, when=BASE + timedelta(hours=30))

        comparisons = match_events([reference], [candidate])

        self.assertEqual(sorted(self.verdicts(comparisons)), [CANDIDATE_ONLY, REFERENCE_ONLY])

    def test_the_tolerance_is_configurable(self):
        reference = self.event(self.maersk, when=BASE)
        candidate = self.event(self.traqo, when=BASE + timedelta(hours=30))

        comparisons = match_events([reference], [candidate], tolerance_hours=48)

        self.assertEqual(self.verdicts(comparisons), [MATCHED])

    def test_different_milestones_never_match(self):
        reference = self.event(self.maersk, event_type=_EventType.GATE_IN, event_code="GTIN")
        candidate = self.event(self.traqo, event_type=_EventType.GATE_OUT, event_code="GTOT")

        comparisons = match_events([reference], [candidate])

        self.assertEqual(sorted(self.verdicts(comparisons)), [CANDIDATE_ONLY, REFERENCE_ONLY])

    def test_an_actual_event_is_never_merged_with_an_estimated_one(self):
        """The most important rule: a forecast must not be credited as an observation."""
        reference = self.event(self.maersk, event_type=_EventType.VESSEL_ARRIVED, time_type=_TimeType.ACTUAL)
        candidate = self.event(self.traqo, event_type=_EventType.VESSEL_ARRIVED, time_type=_TimeType.ESTIMATED)

        comparisons = match_events([reference], [candidate])

        self.assertEqual(sorted(self.verdicts(comparisons)), [CANDIDATE_ONLY, REFERENCE_ONLY])

    def test_a_planned_event_is_not_merged_with_an_estimated_one(self):
        reference = self.event(self.maersk, time_type=_TimeType.PLANNED)
        candidate = self.event(self.traqo, time_type=_TimeType.ESTIMATED)

        comparisons = match_events([reference], [candidate])

        self.assertEqual(sorted(self.verdicts(comparisons)), [CANDIDATE_ONLY, REFERENCE_ONLY])

    def test_conflicting_unlocodes_disqualify_a_pair(self):
        reference = self.event(self.maersk, unlocode="NLRTM", location_name="Rotterdam")
        candidate = self.event(self.traqo, unlocode="SEGOT", location_name="Gothenburg")

        comparisons = match_events([reference], [candidate])

        self.assertEqual(sorted(self.verdicts(comparisons)), [CANDIDATE_ONLY, REFERENCE_ONLY])

    def test_conflicting_imos_disqualify_a_pair(self):
        reference = self.event(self.maersk, vessel_imo="9948750")
        candidate = self.event(self.traqo, vessel_imo="9784271")

        comparisons = match_events([reference], [candidate])

        self.assertEqual(sorted(self.verdicts(comparisons)), [CANDIDATE_ONLY, REFERENCE_ONLY])

    def test_a_place_name_difference_does_not_block_a_match_but_is_recorded(self):
        # Providers name ports differently; that is a measurement, not proof of two events.
        reference = self.event(self.maersk, unlocode="CNYTN", location_name="Yantian")
        candidate = self.event(self.traqo, location_name="Shenzhen")

        comparisons = match_events([reference], [candidate])

        self.assertEqual(self.verdicts(comparisons), [MATCHED])
        summary = summarise_matches(comparisons, reference_events=[reference], tolerance_hours=24)
        self.assertEqual(summary.location_name_disagreements, 1)

    def test_place_names_differing_only_in_accent_or_country_are_treated_as_equal(self):
        reference = self.event(self.maersk, location_name="Göteborg")
        candidate = self.event(self.traqo, location_name="Goteborg, Sweden")

        comparisons = match_events([reference], [candidate])

        self.assertEqual(self.verdicts(comparisons), [MATCHED])
        self.assertEqual(comparisons[0].score, 2)

    def test_shared_unlocode_scores_higher_than_a_shared_name_alone(self):
        reference = self.event(self.maersk, unlocode="NLRTM")
        strong = match_events([reference], [self.event(self.traqo, unlocode="NLRTM")])
        weak = match_events([reference], [self.event(self.traqo, unlocode="")])

        self.assertGreater(strong[0].score, weak[0].score)

    def test_indistinguishable_candidates_stay_ambiguous(self):
        """Two candidates identical in every discriminator must not be guessed between."""
        reference = self.event(self.maersk, when=BASE, location_name="Rotterdam")
        first = self.event(self.traqo, when=BASE + timedelta(hours=1), location_name="Rotterdam")
        second = self.event(self.traqo, when=BASE - timedelta(hours=1), location_name="Rotterdam")

        comparisons = match_events([reference], [first, second])

        self.assertEqual(self.verdicts(comparisons), [AMBIGUOUS] * 3)
        self.assertNotIn(MATCHED, self.verdicts(comparisons))
        self.assertTrue(all(comparison.ambiguous_with >= 2 for comparison in comparisons))

    def test_a_closer_candidate_wins_over_a_further_one(self):
        reference = self.event(self.maersk, when=BASE, location_name="Rotterdam")
        near = self.event(self.traqo, when=BASE + timedelta(minutes=10), location_name="Rotterdam")
        far = self.event(self.traqo, when=BASE + timedelta(hours=6), location_name="Rotterdam")

        comparisons = match_events([reference], [near, far])
        matched = [comparison for comparison in comparisons if comparison.verdict == MATCHED]

        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].candidate_event.pk, near.pk)

    def test_a_stronger_candidate_wins_over_a_closer_but_weaker_one(self):
        reference = self.event(self.maersk, when=BASE, unlocode="NLRTM", location_name="Rotterdam")
        strong = self.event(self.traqo, when=BASE + timedelta(hours=3), unlocode="NLRTM", location_name="Rotterdam")
        weak = self.event(self.traqo, when=BASE + timedelta(minutes=5), unlocode="", location_name="")

        comparisons = match_events([reference], [strong, weak])
        matched = [comparison for comparison in comparisons if comparison.verdict == MATCHED]

        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].candidate_event.pk, strong.pk)

    def test_an_event_without_a_timestamp_is_never_matched(self):
        reference = self.event(self.maersk, when=None)
        candidate = self.event(self.traqo, when=BASE)

        comparisons = match_events([reference], [candidate])

        self.assertEqual(sorted(self.verdicts(comparisons)), [CANDIDATE_ONLY, REFERENCE_ONLY])

    def test_every_event_appears_exactly_once(self):
        reference_events = [
            self.event(self.maersk, event_type=_EventType.GATE_IN, when=BASE - timedelta(days=20), event_code="GTIN"),
            self.event(self.maersk, event_type=_EventType.DISCHARGED, when=BASE, event_code="DISC"),
            self.event(self.maersk, event_type=_EventType.DELIVERED, when=BASE + timedelta(days=3), event_code="DELV"),
        ]
        candidate_events = [self.event(self.traqo, event_type=_EventType.DISCHARGED, when=BASE, event_code="DISC")]

        comparisons = match_events(reference_events, candidate_events)

        self.assertEqual(len(comparisons), 3)
        self.assertEqual(sorted(self.verdicts(comparisons)), [MATCHED, REFERENCE_ONLY, REFERENCE_ONLY])

    def test_matching_does_not_modify_any_event(self):
        reference = self.event(self.maersk, unlocode="NLRTM", vessel_name="MAERSK EDINBURGH", vessel_imo="9948750")
        candidate = self.event(self.traqo, unlocode="", vessel_name="", vessel_imo="")

        match_events([reference], [candidate])

        candidate.refresh_from_db()
        reference.refresh_from_db()
        # The candidate is not enriched from the reference, and the reference is untouched.
        self.assertEqual(candidate.location_unlocode, "")
        self.assertEqual(candidate.vessel_name, "")
        self.assertEqual(candidate.vessel_imo, "")
        self.assertEqual(reference.location_unlocode, "NLRTM")

    def test_a_matched_pair_records_what_the_candidate_lacks(self):
        reference = self.event(self.maersk, unlocode="NLRTM", vessel_name="MAERSK EDINBURGH", vessel_imo="9948750")
        candidate = self.event(self.traqo)

        comparison = match_events([reference], [candidate])[0]

        self.assertIn("location_unlocode", comparison.missing_from_candidate)
        self.assertIn("vessel_name", comparison.missing_from_candidate)
        self.assertIn("vessel_imo", comparison.missing_from_candidate)


class CoverageMetricTest(BenchmarkTestCase):
    """Coverage is reported against two denominators, and the exclusion is named."""

    def test_coverage_excludes_events_container_scm_could_not_classify(self):
        classified = [
            self.event(self.maersk, event_type=_EventType.GATE_IN, when=BASE, event_code="GTIN"),
            self.event(self.maersk, event_type=_EventType.DISCHARGED, when=BASE + timedelta(days=5), event_code="DISC"),
        ]
        paperwork = self.event(
            self.maersk,
            event_type=_EventType.UNKNOWN,
            when=BASE + timedelta(days=1),
            event_code="ISSU",
            carrier_event_type="SHIPMENT",
            location_name="",
        )
        reference_events = [*classified, paperwork]
        candidate_events = [self.event(self.traqo, event_type=_EventType.GATE_IN, when=BASE, event_code="GTIN")]

        comparisons = match_events(reference_events, candidate_events)
        summary = summarise_matches(comparisons, reference_events=reference_events, tolerance_hours=24)

        self.assertEqual(summary.comparable_reference_events, 2)
        self.assertEqual(summary.total_reference_events, 3)
        self.assertEqual(summary.matched_comparable_reference_events, 1)
        self.assertEqual(summary.benchmark_event_coverage_percent, 50.0)
        # And the same numerator over every event, so neither figure can flatter.
        self.assertEqual(summary.raw_event_coverage_percent, 33.3)
        self.assertIn("SHIPMENT / ISSU", summary.excluded_reference_codes)

    def test_coverage_is_none_when_there_is_nothing_to_compare(self):
        summary = summarise_matches([], reference_events=[], tolerance_hours=24)

        self.assertIsNone(summary.benchmark_event_coverage_percent)
        self.assertIsNone(summary.raw_event_coverage_percent)

    def test_verdict_counts_are_split_by_side(self):
        reference_events = [self.event(self.maersk, when=BASE)]
        candidate_events = [
            self.event(self.traqo, when=BASE),
            self.event(self.traqo, event_type=_EventType.DELAY, when=BASE + timedelta(days=2), event_code="DLAY"),
        ]

        comparisons = match_events(reference_events, candidate_events)
        summary = summarise_matches(comparisons, reference_events=reference_events, tolerance_hours=24)

        self.assertEqual(summary.matched, 1)
        self.assertEqual(summary.reference_only, 0)
        self.assertEqual(summary.candidate_only, 1)


class ProviderSummaryTest(BenchmarkTestCase):
    """Each provider's own classification profile."""

    def test_events_are_counted_by_time_type_and_classification(self):
        events = [
            self.event(self.maersk, time_type=_TimeType.ACTUAL),
            self.event(self.maersk, time_type=_TimeType.ESTIMATED),
            self.event(self.maersk, time_type=_TimeType.PLANNED),
            self.event(self.maersk, time_type=_TimeType.REQUESTED),
            self.event(self.maersk, time_type=_TimeType.UNKNOWN, event_type=_EventType.UNKNOWN),
            self.event(self.maersk, when=None),
        ]

        summary = summarise_provider_events("maersk", events)

        self.assertEqual(summary.total, 6)
        self.assertEqual(summary.actual, 2)  # the undated one defaults to ACTUAL
        self.assertEqual(summary.estimated, 1)
        self.assertEqual(summary.planned, 1)
        self.assertEqual(summary.requested, 1)
        self.assertEqual(summary.unknown_time_type, 1)
        self.assertEqual(summary.unclassified_event_type, 1)
        self.assertEqual(summary.without_timestamp, 1)
        self.assertEqual(summary.comparable, 5)


class LocationRichnessTest(BenchmarkTestCase):
    """Location detail is counted, never inferred from the other provider."""

    def test_richness_is_counted_per_field(self):
        maersk_events = [
            self.event(self.maersk, unlocode="NLRTM", latitude="51.95", longitude="4.14"),
            self.event(self.maersk, unlocode="SEGOT"),
            self.event(self.maersk, unlocode="", location_name="Somewhere"),
        ]
        traqo_events = [self.event(self.traqo, unlocode="", location_name="Rotterdam")]

        maersk = location_metrics("maersk", maersk_events)
        traqo = location_metrics("traqo", traqo_events)

        self.assertEqual((maersk.with_location_name, maersk.with_unlocode, maersk.with_coordinates), (3, 2, 1))
        self.assertEqual(maersk.distinct_unlocodes, ["NLRTM", "SEGOT"])
        self.assertEqual((traqo.with_location_name, traqo.with_unlocode, traqo.with_coordinates), (1, 0, 0))
        self.assertEqual(traqo.as_dict()["unlocode_percent"], 0.0)
        self.assertEqual(maersk.as_dict()["unlocode_percent"], 66.7)

    def test_a_place_name_is_never_credited_as_a_unlocode(self):
        # The discipline of the experiment: Traqo saying "Rotterdam" is not NLRTM.
        traqo = location_metrics("traqo", [self.event(self.traqo, location_name="Rotterdam", unlocode="")])

        self.assertEqual(traqo.with_location_name, 1)
        self.assertEqual(traqo.with_unlocode, 0)

    def test_facility_richness_is_declared_unmeasurable_rather_than_zero(self):
        metrics = location_metrics("traqo", [self.event(self.traqo)])

        self.assertIn("not represented separately", metrics.as_dict()["facility_information"])


class VesselRichnessTest(BenchmarkTestCase):
    """Whether a provider ties a ship to a leg, or only to the shipment."""

    def test_vessel_detail_is_counted_per_event(self):
        events = [
            self.event(
                self.maersk,
                vessel_name="MAERSK EDINBURGH",
                vessel_imo="9948750",
                voyage="512W",
                transport_mode=TrackingEvent.TransportMode.VESSEL,
            ),
            self.event(self.maersk, transport_mode=TrackingEvent.TransportMode.VESSEL),
            self.event(self.maersk, transport_mode=TrackingEvent.TransportMode.TRUCK),
        ]

        metrics = vessel_metrics("maersk", events)

        self.assertEqual(metrics.with_vessel_name, 1)
        self.assertEqual(metrics.with_vessel_imo, 1)
        self.assertEqual(metrics.with_voyage, 1)
        self.assertEqual(metrics.vessel_mode_events, 2)
        self.assertEqual(metrics.distinct_vessel_imos, ["9948750"])
        self.assertTrue(metrics.attributes_vessels_to_legs)
        self.assertEqual(metrics.as_dict()["vessel_name_percent_of_vessel_legs"], 50.0)

    def test_a_provider_that_names_no_vessel_per_event_is_flagged(self):
        events = [self.event(self.traqo, transport_mode=TrackingEvent.TransportMode.VESSEL)]

        metrics = vessel_metrics("traqo", events)

        self.assertFalse(metrics.attributes_vessels_to_legs)
        self.assertEqual(metrics.distinct_vessel_names, [])


class FreshnessMetricTest(BenchmarkTestCase):
    """Reporting lag, observation lag, and what a single run may claim."""

    def test_lags_are_measured_from_matched_actual_events(self):
        reference = self.event(self.maersk, when=BASE, received_at=BASE + timedelta(hours=1))
        candidate = self.event(self.traqo, when=BASE, received_at=BASE + timedelta(hours=7))

        comparisons = match_events([reference], [candidate])
        metrics = freshness_metrics(
            comparisons,
            reference_provider="maersk",
            candidate_provider="traqo",
            reference_events=[reference],
            candidate_events=[candidate],
            first_observation=False,
        )

        self.assertEqual(metrics.matched_actual_events, 1)
        self.assertEqual(metrics.reference_median_reporting_lag_hours, 1.0)
        self.assertEqual(metrics.candidate_median_reporting_lag_hours, 7.0)
        self.assertEqual(metrics.median_observation_lag_hours, 6.0)

    def test_a_forecast_is_excluded_from_freshness(self):
        reference = self.event(self.maersk, time_type=_TimeType.ESTIMATED)
        candidate = self.event(self.traqo, time_type=_TimeType.ESTIMATED)

        metrics = freshness_metrics(
            match_events([reference], [candidate]),
            reference_provider="maersk",
            candidate_provider="traqo",
            reference_events=[reference],
            candidate_events=[candidate],
            first_observation=False,
        )

        self.assertEqual(metrics.matched_actual_events, 0)
        self.assertIsNone(metrics.median_observation_lag_hours)

    def test_a_first_observation_is_labelled_a_backfill_artefact(self):
        reference = self.event(self.maersk, when=BASE, received_at=BASE + timedelta(hours=1))
        candidate = self.event(self.traqo, when=BASE, received_at=BASE + timedelta(days=9))

        metrics = freshness_metrics(
            match_events([reference], [candidate]),
            reference_provider="maersk",
            candidate_provider="traqo",
            reference_events=[reference],
            candidate_events=[candidate],
            first_observation=True,
        )

        self.assertTrue(metrics.first_observation)
        self.assertTrue(any("backfill" in note for note in metrics.notes))

    def test_milestone_recency_gap_is_measurable_from_one_run(self):
        reference_events = [self.event(self.maersk, when=BASE + timedelta(days=2))]
        candidate_events = [self.event(self.traqo, when=BASE)]

        metrics = freshness_metrics(
            [],
            reference_provider="maersk",
            candidate_provider="traqo",
            reference_events=reference_events,
            candidate_events=candidate_events,
            first_observation=False,
        )

        self.assertEqual(metrics.milestone_recency_gap_hours, 48.0)
        self.assertTrue(any("behind" in note for note in metrics.notes))

    def test_the_providers_own_sync_timestamp_is_reported_separately(self):
        metrics = freshness_metrics(
            [],
            reference_provider="maersk",
            candidate_provider="traqo",
            reference_events=[],
            candidate_events=[],
            first_observation=False,
            candidate_payload_last_updated_at="2026-08-19 04:00:00",
        )

        self.assertEqual(metrics.as_dict()["candidate_payload_last_updated_at"], "2026-08-19 04:00:00")


class EtaComparisonTest(BenchmarkTestCase):
    """Each provider's forecast, read through the canonical selector."""

    def test_each_provider_gets_its_own_eta_and_the_difference_is_reported(self):
        self.event(
            self.maersk,
            event_type=_EventType.VESSEL_ARRIVED,
            time_type=_TimeType.ESTIMATED,
            when=BASE + timedelta(days=13),
            unlocode="DOCAU",
        )
        self.event(
            self.traqo,
            event_type=_EventType.VESSEL_ARRIVED,
            time_type=_TimeType.ESTIMATED,
            when=BASE + timedelta(days=13, hours=6),
        )

        result = compare_providers(
            team=self.team,
            container=self.container,
            sealine="MAEU",
            ingest_candidate=False,
        )

        self.assertTrue(result.eta.reference.has_eta)
        self.assertTrue(result.eta.candidate.has_eta)
        self.assertEqual(result.eta.difference_hours, 6.0)
        self.assertEqual(result.eta.reference.location_unlocode, "DOCAU")
        self.assertEqual(result.eta.candidate.location_unlocode, "")

    def test_a_providers_forecast_is_retired_by_its_own_actual_arrival(self):
        # The canonical rule, scoped to one provider: Maersk has arrived, Traqo has not
        # noticed, so only Traqo still shows a forecast.
        self.event(
            self.maersk,
            event_type=_EventType.VESSEL_ARRIVED,
            time_type=_TimeType.ESTIMATED,
            when=BASE + timedelta(days=13),
        )
        self.event(
            self.maersk,
            event_type=_EventType.VESSEL_ARRIVED,
            time_type=_TimeType.ACTUAL,
            when=BASE + timedelta(days=13, hours=1),
        )
        self.event(
            self.traqo,
            event_type=_EventType.VESSEL_ARRIVED,
            time_type=_TimeType.ESTIMATED,
            when=BASE + timedelta(days=13),
        )

        result = compare_providers(
            team=self.team,
            container=self.container,
            sealine="MAEU",
            ingest_candidate=False,
        )

        self.assertFalse(result.eta.reference.has_eta)
        self.assertTrue(result.eta.candidate.has_eta)
        self.assertIsNone(result.eta.difference_hours)

    def test_the_unfiltered_selector_still_sees_every_provider(self):
        """The production ETA rule is unchanged by the benchmark's provider filter."""
        from apps.scm.tracking.selectors import get_container_tracking_eta_event

        self.event(
            self.traqo,
            event_type=_EventType.VESSEL_ARRIVED,
            time_type=_TimeType.ESTIMATED,
            when=BASE + timedelta(days=13),
        )

        event = get_container_tracking_eta_event(self.team, self.container)

        self.assertIsNotNone(event)
        self.assertEqual(event.provider.code, "traqo")


class ComparisonRunnerTest(BenchmarkTestCase):
    """The runner reads canonical rows and writes nothing when asked not to fetch."""

    def test_a_comparison_without_a_fetch_writes_nothing(self):
        reference = self.event(self.maersk, unlocode="NLRTM")
        self.event(self.traqo)
        before = TrackingEvent.objects.filter(team=self.team).count()

        result = compare_providers(
            team=self.team,
            container=self.container,
            sealine="MAEU",
            ingest_candidate=False,
        )

        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), before)
        self.assertEqual(result.match_summary.matched, 1)
        self.assertEqual(result.candidate_ingest_created, 0)
        self.assertIn("not refetched", " ".join(result.notes))
        reference.refresh_from_db()
        self.assertEqual(reference.location_unlocode, "NLRTM")

    def test_the_result_names_the_container_mode_and_providers(self):
        self.event(self.maersk)

        result = compare_providers(
            team=self.team,
            container=self.container,
            sealine="MAEU",
            ingest_candidate=False,
        )

        self.assertEqual(result.container_number, "MRSU6859427")
        self.assertEqual(result.mode, "production")
        self.assertEqual(result.reference_provider_code, "maersk")
        self.assertEqual(result.candidate_provider_code, "traqo")

    def test_events_of_other_providers_are_ignored(self):
        other = TrackingProvider.objects.create(code="cma_cgm", name="CMA CGM")
        self.event(self.maersk)
        self.event(other)

        result = compare_providers(
            team=self.team,
            container=self.container,
            sealine="MAEU",
            ingest_candidate=False,
        )

        self.assertEqual(result.reference_summary.total, 1)
        self.assertEqual(result.candidate_summary.total, 0)


class ReportOutputTest(BenchmarkTestCase):
    """The report must make information loss visible and leak nothing."""

    def _result(self) -> ComparisonResult:
        self.event(
            self.maersk,
            when=BASE,
            unlocode="NLRTM",
            location_name="Rotterdam",
            vessel_name="MAERSK EDINBURGH",
            vessel_imo="9948750",
            voyage="512W",
            description="Discharged from vessel",
        )
        self.event(
            self.traqo,
            when=BASE + timedelta(minutes=28),
            location_name="Rotterdam",
            description="Discharged",
        )
        self.event(
            self.maersk,
            event_type=_EventType.GATE_OUT,
            when=BASE + timedelta(days=1),
            unlocode="NLRTM",
            event_code="GTOT",
        )
        return compare_providers(
            team=self.team,
            container=self.container,
            sealine="MAEU",
            ingest_candidate=False,
        )

    def test_the_text_report_shows_every_required_section(self):
        text = render_text(self._result())

        for heading in (
            "EVENTS",
            "EVENT BY EVENT",
            "LOCATION QUALITY",
            "VESSEL / VOYAGE QUALITY",
            "ETA",
            "FRESHNESS",
            "SNAPSHOT",
        ):
            self.assertIn(heading, text)

    def test_the_text_report_names_what_the_candidate_lacks(self):
        text = render_text(self._result())

        self.assertIn("no location unlocode", text)
        self.assertIn("no vessel imo", text)
        self.assertIn("<- lost", text)

    def test_the_text_report_states_the_scope_of_the_coverage_figure(self):
        text = render_text(self._result())

        self.assertIn("benchmark event coverage", text)
        self.assertIn("not a statement about carrier coverage", text)

    def test_the_text_report_shows_the_time_delta_of_a_matched_pair(self):
        text = render_text(self._result())

        self.assertIn("+28 min", text)

    def test_a_maersk_only_event_is_visible_in_the_table(self):
        text = render_text(self._result())

        self.assertIn("maersk only", text)

    def test_the_verbose_report_adds_field_detail_without_raw_json(self):
        result = self._result()

        plain = render_text(result)
        verbose = render_text(result, verbose=True)

        # The providers word the same event differently; that is detail, not loss, so it
        # only appears on request.
        self.assertNotIn("Discharged from vessel", plain)
        self.assertIn("Discharged from vessel", verbose)
        self.assertNotIn("raw_data", verbose)

    def test_the_json_report_carries_measurements_and_no_payloads(self):
        payload = render_json(self._result())

        for key in (
            "container",
            "run_at",
            "reference_summary",
            "candidate_summary",
            "match_summary",
            "matched_events",
            "reference_only_events",
            "candidate_only_events",
            "ambiguous_events",
            "location_metrics",
            "vessel_metrics",
            "eta_metrics",
            "freshness_metrics",
        ):
            self.assertIn(key, payload)

        serialised = json.dumps(payload)
        self.assertNotIn("raw_data", serialised)
        self.assertNotIn("payload_json", serialised)
        self.assertNotIn("events_table", serialised)

    def test_the_json_report_records_missing_fields_explicitly(self):
        payload = render_json(self._result())

        matched = payload["matched_events"][0]
        self.assertIn("location_unlocode", matched["missing_from_candidate"])
        self.assertEqual(matched["candidate"]["location_unlocode"], "")
        self.assertEqual(matched["reference"]["location_unlocode"], "NLRTM")

    def test_the_json_report_is_serialisable_and_contains_no_secrets(self):
        payload = render_json(self._result())

        serialised = json.dumps(payload)
        for secret_ish in ("api_key", "Authorization", "Bearer", "TRAQO_API_KEY"):
            self.assertNotIn(secret_ish, serialised)

    def test_a_sandbox_run_says_its_numbers_mean_nothing(self):
        self.event(self.maersk)
        result = compare_providers(
            team=self.team,
            container=self.container,
            sealine="MAEU",
            sandbox=True,
            ingest_candidate=False,
        )

        self.assertIn("say nothing about", render_text(result))


class CompareCommandTest(BenchmarkTestCase):
    """The CLI surface: explicit modes, stated side effects, and a JSON file."""

    def _call(self, *args, **options) -> str:
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("traqo_test", *args, stdout=out, stderr=StringIO(), **options)
        return out.getvalue()

    def test_compare_refuses_to_guess_between_sandbox_and_production(self):
        from django.core.management.base import CommandError

        self.event(self.maersk)

        with self.assertRaises(CommandError) as ctx:
            self._call("MRSU6859427", sealine="MAEU", compare=True, team="benchmark")

        self.assertIn("--compare needs an explicit mode", str(ctx.exception))

    def test_compare_refuses_a_container_the_team_does_not_have(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError) as ctx:
            self._call("MRKU1234563", sealine="MAEU", compare=True, live=True, no_fetch=True, team="benchmark")

        self.assertIn("has no container", str(ctx.exception))

    def test_compare_refuses_to_spend_a_request_with_nothing_to_compare_against(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError) as ctx:
            self._call("MRSU6859427", sealine="MAEU", compare=True, live=True, team="benchmark")

        self.assertIn("nothing to", str(ctx.exception))

    def test_a_production_run_states_the_shipment_slot_side_effect(self):
        self.event(self.maersk)

        output = self._call("MRSU6859427", sealine="MAEU", compare=True, live=True, no_fetch=True, team="benchmark")

        self.assertIn("Container:", output)
        self.assertIn("MAEU", output)
        self.assertIn("PRODUCTION", output)

    def test_a_run_that_would_fetch_warns_about_the_slot(self):
        # --no-fetch is what keeps this test offline; the banner is rendered before any
        # request would be made, which is exactly the point of it.
        self.event(self.maersk)

        output = self._call("MRSU6859427", sealine="MAEU", compare=True, live=True, no_fetch=True, team="benchmark")

        self.assertIn("Traqo request:", output)

    def test_json_output_is_valid_json_and_carries_the_benchmark(self):
        self.event(self.maersk)

        output = self._call(
            "MRSU6859427", sealine="MAEU", compare=True, live=True, no_fetch=True, team="benchmark", json=True
        )

        payload = json.loads(output[output.index("{") :])
        self.assertEqual(payload["container"], "MRSU6859427")
        self.assertIn("match_summary", payload)

    def test_the_benchmark_can_be_written_to_a_file(self):
        import tempfile

        self.event(self.maersk)
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/nested/run.json"

            output = self._call(
                "MRSU6859427",
                sealine="MAEU",
                compare=True,
                live=True,
                no_fetch=True,
                team="benchmark",
                output=path,
            )

            saved = json.loads(open(path).read())  # noqa: SIM115 — read once in a temp dir

        self.assertIn("written to", output)
        self.assertEqual(saved["container"], "MRSU6859427")
        self.assertNotIn("api_key", json.dumps(saved))

    def test_the_sealine_is_still_validated_in_compare_mode(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self._call("MRSU6859427", sealine="evergreen", compare=True, live=True, team="benchmark")

    def test_a_live_run_without_a_key_says_what_to_set_and_writes_nothing(self):
        """The credential gate must instruct, never quietly use the sandbox instead."""
        from django.core.management.base import CommandError
        from django.test import override_settings

        self.event(self.maersk)
        before = TrackingEvent.objects.filter(team=self.team).count()

        with override_settings(TRAQO_ENABLED=False, TRAQO_API_KEY=""), self.assertRaises(CommandError) as ctx:
            self._call("MRSU6859427", sealine="MAEU", compare=True, live=True, team="benchmark")

        message = str(ctx.exception)
        self.assertIn("TRAQO_ENABLED", message)
        self.assertIn("TRAQO_API_KEY", message)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), before)
        self.assertFalse(TrackingProvider.objects.filter(code="traqo").exclude(pk=self.traqo.pk).exists())


class ReferenceRefreshTest(BenchmarkTestCase):
    """Refreshing the reference reuses its own sync service, and tolerates failure."""

    def test_the_existing_sync_service_is_used_when_a_refresh_is_asked_for(self):
        from unittest import mock

        from apps.scm.tracking.models import TrackingSubscription, TrackingSyncRun

        subscription = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.maersk,
            container=self.container,
            tracking_reference="MRSU6859427",
        )
        self.event(self.maersk)
        run = TrackingSyncRun(
            subscription=subscription,
            provider=self.maersk,
            status=TrackingSyncRun.Status.SUCCESS,
            events_created=1,
            events_updated=0,
        )

        with mock.patch("apps.scm.tracking.sync.sync_tracking_subscription", return_value=run) as sync:
            result = compare_providers(
                team=self.team,
                container=self.container,
                sealine="MAEU",
                ingest_candidate=False,
                refresh_reference=True,
            )

        sync.assert_called_once_with(subscription)
        self.assertIn("maersk refreshed", " ".join(result.notes))

    def test_a_failed_reference_refresh_does_not_lose_the_comparison(self):
        from unittest import mock

        from apps.scm.tracking.models import TrackingSubscription

        TrackingSubscription.objects.create(
            team=self.team,
            provider=self.maersk,
            container=self.container,
            tracking_reference="MRSU6859427",
        )
        self.event(self.maersk)

        with mock.patch("apps.scm.tracking.sync.sync_tracking_subscription", side_effect=RuntimeError("carrier down")):
            result = compare_providers(
                team=self.team,
                container=self.container,
                sealine="MAEU",
                ingest_candidate=False,
                refresh_reference=True,
            )

        self.assertIn("refresh failed", " ".join(result.notes))
        self.assertEqual(result.reference_summary.total, 1)

    def test_a_refresh_is_reported_when_there_is_no_subscription_to_refresh(self):
        self.event(self.maersk)

        result = compare_providers(
            team=self.team,
            container=self.container,
            sealine="MAEU",
            ingest_candidate=False,
            refresh_reference=True,
        )

        self.assertIn("No maersk subscription to refresh", " ".join(result.notes))
