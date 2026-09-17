"""Tests for the unified, multi-source container journey.

The behaviour under test is that a container is not assumed to have one tracking
source. Several carriers can each have reported part of one physical journey, our
own depot record is a source in its own right, and the journey has to hold all of
them at once — chronologically, attributed, and without any of them displacing the
others.
"""

from datetime import UTC, datetime, timedelta

from django.test import TestCase

from apps.scm.containers.choices import LocationSource, LocationType
from apps.scm.containers.models import Container, ContainerLocation, EquipmentType
from apps.scm.tracking.journey import (
    PHYSICAL_SOURCE_CODE,
    CurrentLocationBasis,
    JourneySourceKind,
    get_container_journey,
)
from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingSubscription
from apps.scm.tracking.positions import PositionType
from apps.teams.models import Team

# The journey from the brief: an ocean leg to Born, then an unreported move to
# Gothenburg, where the box is physically received.
DISCHARGED_AT_BORN = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)
AT_GOTHENBURG = datetime(2026, 8, 16, 9, 30, tzinfo=UTC)
LOADED_IN_CHINA = datetime(2026, 7, 20, 6, 0, tzinfo=UTC)

CONTAINER_NUMBER = "MCUU2009300"


class JourneyTestBase(TestCase):
    """A container with two carrier providers available to report about it."""

    team_slug: str

    def setUp(self):
        self.team = _team(self.team_slug)
        self.cma = _provider("cma_cgm", "CMA CGM")
        self.maersk = _provider("maersk", "Maersk")
        self.container = _container(self.team, CONTAINER_NUMBER)

    # -- fixtures ----------------------------------------------------------

    def event(self, provider, event_type, when, **kwargs):
        defaults = {
            "team": self.team,
            "provider": provider,
            "container": self.container,
            "event_type": event_type,
            "event_time_type": TrackingEvent.EventTimeType.ACTUAL,
            "event_datetime": when,
            "event_fingerprint": f"{provider.code}-{event_type}-{when}-{kwargs.get('location_unlocode', '')}",
        }
        defaults.update(kwargs)
        return TrackingEvent.objects.create(**defaults)

    def subscription(self, provider, **kwargs):
        defaults = {
            "container": self.container,
            "tracking_reference": self.container.container_id,
            "status": TrackingSubscription.Status.ACTIVE,
        }
        defaults.update(kwargs)
        return TrackingSubscription.objects.create(team=self.team, provider=provider, **defaults)

    def physically_at(self, name, when, *, source=LocationSource.MANUAL, **location_kwargs):
        location = ContainerLocation.objects.create(
            team=self.team,
            name=name,
            location_type=location_kwargs.pop("location_type", LocationType.DEPOT),
            **location_kwargs,
        )
        self.container.current_location = location
        self.container.last_location_update = when
        self.container.location_source = source
        self.container.save()
        return location

    def journey(self):
        return get_container_journey(self.team, self.container)


class JourneyHoldsEverySourceTest(JourneyTestBase):
    """Every provider's events belong to the same journey."""

    team_slug = "journey-sources"

    def test_events_from_two_providers_appear_in_one_journey(self):
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_unlocode="NLBON")
        self.event(self.maersk, TrackingEvent.EventType.GATE_OUT, AT_GOTHENBURG, location_unlocode="SEGOT")

        journey = self.journey()

        self.assertEqual([point.location_unlocode for point in journey.points], ["NLBON", "SEGOT"])
        self.assertEqual([point.source.name for point in journey.points], ["CMA CGM", "Maersk"])

    def test_points_are_chronological_whichever_provider_reported_them(self):
        self.event(self.maersk, TrackingEvent.EventType.GATE_OUT, AT_GOTHENBURG, location_unlocode="SEGOT")
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_unlocode="NLBON")
        self.event(self.cma, TrackingEvent.EventType.LOADED_ON_VESSEL, LOADED_IN_CHINA, location_unlocode="CNSHA")

        occurred = [point.occurred_at for point in self.journey().points]

        self.assertEqual(occurred, sorted(occurred))
        self.assertEqual(occurred[0], LOADED_IN_CHINA)

    def test_the_timeline_reads_newest_first(self):
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_unlocode="NLBON")
        self.event(self.maersk, TrackingEvent.EventType.GATE_OUT, AT_GOTHENBURG, location_unlocode="SEGOT")

        self.assertEqual(
            [point.occurred_at for point in self.journey().newest_first], [AT_GOTHENBURG, DISCHARGED_AT_BORN]
        )

    def test_sources_lists_every_verified_subscription(self):
        self.subscription(self.cma)
        self.subscription(self.maersk)

        self.assertEqual([source.code for source in self.journey().sources], ["cma_cgm", "maersk"])
        self.assertTrue(self.journey().has_multiple_sources)

    def test_a_cancelled_watch_is_not_a_source(self):
        """Someone stopped it deliberately; it no longer speaks for the container."""
        self.subscription(self.cma)
        self.subscription(self.maersk, status=TrackingSubscription.Status.CANCELLED)

        self.assertEqual([source.code for source in self.journey().sources], ["cma_cgm"])

    def test_a_completed_source_stays_a_source(self):
        """Its leg is over; its events are still part of the journey."""
        self.subscription(self.cma, status=TrackingSubscription.Status.COMPLETED)

        self.assertEqual([source.code for source in self.journey().sources], ["cma_cgm"])

    def test_a_provider_with_events_but_no_watch_is_still_attributed(self):
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_unlocode="NLBON")

        self.assertEqual([source.code for source in self.journey().sources], ["cma_cgm"])

    def test_a_new_source_does_not_remove_the_older_providers_events(self):
        first = self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_unlocode="NLBON")
        self.subscription(self.cma)
        # A second carrier is later found for the onward leg.
        self.subscription(self.maersk)
        self.event(self.maersk, TrackingEvent.EventType.GATE_OUT, AT_GOTHENBURG, location_unlocode="SEGOT")

        journey = self.journey()

        self.assertIn(first.pk, [point.event_id for point in journey.points])
        self.assertEqual(len(journey.points), 2)
        self.assertEqual(TrackingEvent.objects.filter(container=self.container).count(), 2)

    def test_the_physical_record_is_a_source_of_its_own(self):
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_unlocode="NLBON")
        self.physically_at("Oceanterminalen", AT_GOTHENBURG, city="Göteborg", country="Sweden")

        journey = self.journey()

        self.assertEqual([source.code for source in journey.sources], ["cma_cgm", PHYSICAL_SOURCE_CODE])
        self.assertEqual(journey.sources[-1].kind, JourneySourceKind.PHYSICAL)
        self.assertEqual(journey.physical_observation.location_name, "Oceanterminalen")
        self.assertEqual(journey.points[-1].source.code, PHYSICAL_SOURCE_CODE)

    def test_a_location_written_from_a_carrier_event_is_not_a_second_opinion(self):
        """Otherwise one carrier report would corroborate itself."""
        self.physically_at(
            "Port of Born", AT_GOTHENBURG, source=LocationSource.TRACKING_EVENT, location_type=LocationType.PORT
        )

        self.assertIsNone(self.journey().physical_observation)

    def test_an_undated_location_cannot_be_placed_in_the_journey(self):
        location = ContainerLocation.objects.create(team=self.team, name="Depot", location_type=LocationType.DEPOT)
        self.container.current_location = location
        self.container.location_source = LocationSource.MANUAL
        self.container.save()

        self.assertIsNone(self.journey().physical_observation)

    def test_free_text_location_is_enough_to_be_an_observation(self):
        self.container.location_text = "MCR yard, Gothenburg"
        self.container.last_location_update = AT_GOTHENBURG
        self.container.location_source = LocationSource.MANUAL
        self.container.save()

        self.assertEqual(self.journey().physical_observation.location_name, "MCR yard, Gothenburg")

    def test_undated_events_are_kept_and_sort_last(self):
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_unlocode="NLBON")
        self.event(self.cma, TrackingEvent.EventType.BOOKING_CREATED, None, event_fingerprint="cma-undated")

        journey = self.journey()

        self.assertEqual(len(journey.points), 2)
        self.assertIsNone(journey.points[-1].occurred_at)
        self.assertIsNone(journey.newest_first[-1].occurred_at)

    def test_another_teams_events_and_watches_never_join_the_journey(self):
        other_team = _team("journey-sources-other")
        other_container = _container(other_team, CONTAINER_NUMBER)
        TrackingEvent.objects.create(
            team=other_team,
            provider=self.maersk,
            container=other_container,
            event_type=TrackingEvent.EventType.DISCHARGED,
            event_time_type=TrackingEvent.EventTimeType.ACTUAL,
            event_datetime=DISCHARGED_AT_BORN,
            location_unlocode="NLBON",
            event_fingerprint="other-team-discharge",
        )
        TrackingSubscription.objects.create(
            team=other_team,
            provider=self.maersk,
            container=other_container,
            tracking_reference=CONTAINER_NUMBER,
        )
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_unlocode="NLBON")

        self.assertEqual([source.code for source in self.journey().sources], ["cma_cgm"])
        self.assertEqual(len(self.journey().points), 1)
        # And the other team sees only its own.
        other = get_container_journey(other_team, other_container)
        self.assertEqual([source.code for source in other.sources], ["maersk"])


class CorroborationTest(JourneyTestBase):
    """Two providers describing one physical event read as one event, two sources."""

    team_slug = "journey-corroboration"

    def _discharge(self, provider, when, **kwargs):
        return self.event(
            provider,
            TrackingEvent.EventType.DISCHARGED,
            when,
            location_name="Born",
            location_unlocode="NLBON",
            **kwargs,
        )

    def test_the_same_event_minutes_apart_becomes_one_point_with_two_sources(self):
        first = self._discharge(self.cma, DISCHARGED_AT_BORN)
        second = self._discharge(self.maersk, DISCHARGED_AT_BORN + timedelta(minutes=3))

        points = self.journey().points

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].event_id, first.pk)
        self.assertEqual(points[0].source_names, ["CMA CGM", "Maersk"])
        # Neither event is lost — the second is reachable through the point.
        self.assertEqual([c.event.pk for c in points[0].corroborations], [second.pk])
        self.assertEqual(TrackingEvent.objects.filter(container=self.container).count(), 2)

    def test_the_earliest_report_stays_the_point(self):
        """It is the closest thing to the observation itself."""
        self._discharge(self.maersk, DISCHARGED_AT_BORN + timedelta(minutes=5))
        self._discharge(self.cma, DISCHARGED_AT_BORN)

        point = self.journey().points[0]

        self.assertEqual(point.occurred_at, DISCHARGED_AT_BORN)
        self.assertEqual(point.source.name, "CMA CGM")

    def test_reports_further_apart_than_the_window_stay_two_points(self):
        self._discharge(self.cma, DISCHARGED_AT_BORN)
        self._discharge(self.maersk, DISCHARGED_AT_BORN + timedelta(hours=4))

        self.assertEqual(len(self.journey().points), 2)

    def test_different_places_are_never_merged(self):
        self._discharge(self.cma, DISCHARGED_AT_BORN)
        self.event(
            self.maersk,
            TrackingEvent.EventType.DISCHARGED,
            DISCHARGED_AT_BORN + timedelta(minutes=2),
            location_name="Gothenburg",
            location_unlocode="SEGOT",
        )

        self.assertEqual(len(self.journey().points), 2)

    def test_different_event_types_are_never_merged(self):
        self._discharge(self.cma, DISCHARGED_AT_BORN)
        self.event(
            self.maersk,
            TrackingEvent.EventType.GATE_OUT,
            DISCHARGED_AT_BORN + timedelta(minutes=2),
            location_name="Born",
            location_unlocode="NLBON",
        )

        self.assertEqual(len(self.journey().points), 2)

    def test_a_forecast_is_not_a_report_of_the_observed_event(self):
        self._discharge(self.cma, DISCHARGED_AT_BORN)
        self._discharge(
            self.maersk,
            DISCHARGED_AT_BORN + timedelta(minutes=2),
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )

        self.assertEqual(len(self.journey().points), 2)

    def test_one_provider_reporting_twice_is_two_events(self):
        """Its own fingerprint already settled what a duplicate is at write time."""
        self._discharge(self.cma, DISCHARGED_AT_BORN)
        self._discharge(self.cma, DISCHARGED_AT_BORN + timedelta(minutes=2))

        self.assertEqual(len(self.journey().points), 2)

    def test_unclassified_events_are_never_merged(self):
        """An event we could not map has no identity to match on."""
        self.event(
            self.cma,
            TrackingEvent.EventType.UNKNOWN,
            DISCHARGED_AT_BORN,
            location_unlocode="NLBON",
            carrier_event_type="EQUIPMENT",
        )
        self.event(
            self.maersk,
            TrackingEvent.EventType.UNKNOWN,
            DISCHARGED_AT_BORN + timedelta(minutes=1),
            location_unlocode="NLBON",
            carrier_event_type="EQUIPMENT",
        )

        self.assertEqual(len(self.journey().points), 2)

    def test_names_alone_match_when_neither_side_has_a_unlocode(self):
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_name="Born")
        self.event(
            self.maersk,
            TrackingEvent.EventType.DISCHARGED,
            DISCHARGED_AT_BORN + timedelta(minutes=1),
            location_name="born",
        )

        self.assertEqual(len(self.journey().points), 1)


class CurrentLocationTest(JourneyTestBase):
    """Which source gets to say where the container is now."""

    team_slug = "journey-current"

    def test_the_last_carrier_observation_is_used_when_it_is_all_there_is(self):
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_name="Born")

        current = self.journey().current_location

        self.assertEqual(current.basis, CurrentLocationBasis.CARRIER_ACTUAL)
        self.assertEqual(current.label, "Born")
        self.assertEqual(current.source.name, "CMA CGM")

    def test_a_later_physical_observation_wins(self):
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_name="Born")
        self.physically_at("Oceanterminalen", AT_GOTHENBURG, city="Göteborg")

        current = self.journey().current_location

        self.assertEqual(current.basis, CurrentLocationBasis.PHYSICAL)
        self.assertEqual(current.label, "Oceanterminalen")
        self.assertTrue(current.is_physical)

    def test_an_earlier_physical_observation_does_not_win(self):
        self.physically_at("Oceanterminalen", DISCHARGED_AT_BORN - timedelta(days=2), city="Göteborg")
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_name="Born")

        current = self.journey().current_location

        self.assertEqual(current.basis, CurrentLocationBasis.CARRIER_ACTUAL)
        self.assertEqual(current.label, "Born")

    def test_a_forecast_never_displaces_a_later_observation(self):
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_name="Born")
        self.event(
            self.cma,
            TrackingEvent.EventType.VESSEL_ARRIVED,
            AT_GOTHENBURG + timedelta(days=5),
            location_name="Gothenburg",
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )

        current = self.journey().current_location

        self.assertEqual(current.basis, CurrentLocationBasis.CARRIER_ACTUAL)
        self.assertEqual(current.label, "Born")

    def test_a_forecast_never_displaces_a_physical_observation(self):
        self.physically_at("Oceanterminalen", AT_GOTHENBURG, city="Göteborg")
        self.event(
            self.cma,
            TrackingEvent.EventType.VESSEL_ARRIVED,
            AT_GOTHENBURG + timedelta(days=5),
            location_name="Rotterdam",
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )

        self.assertEqual(self.journey().current_location.basis, CurrentLocationBasis.PHYSICAL)

    def test_a_forecast_stands_in_only_when_nothing_has_been_observed(self):
        self.event(
            self.cma,
            TrackingEvent.EventType.VESSEL_ARRIVED,
            AT_GOTHENBURG,
            location_name="Gothenburg",
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )

        current = self.journey().current_location

        self.assertEqual(current.basis, CurrentLocationBasis.CARRIER_FORECAST)
        self.assertEqual(current.point.position_type, PositionType.ESTIMATED)

    def test_a_placeless_observation_does_not_erase_a_known_place(self):
        """The last carrier event is often paperwork, which happens nowhere."""
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_name="Born")
        self.event(
            self.cma,
            TrackingEvent.EventType.DELAY,
            DISCHARGED_AT_BORN + timedelta(days=1),
            description="Transport document released",
        )

        self.assertEqual(self.journey().current_location.label, "Born")

    def test_nothing_reported_means_no_current_location(self):
        self.assertIsNone(self.journey().current_location)

    def test_the_physical_observation_reads_as_a_facility_not_a_fix(self):
        self.physically_at("Oceanterminalen", AT_GOTHENBURG, city="Göteborg")

        position = self.journey().current_location.position

        self.assertEqual(position.position_type, PositionType.FACILITY)
        self.assertFalse(position.is_realtime)
        self.assertFalse(position.has_coordinates)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _provider(code: str, name: str) -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(code=code, defaults={"name": name})[0]


def _container(team: Team, number: str) -> Container:
    equipment_type = EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]
    return Container.objects.create(
        team=team,
        owner_code=number[:3],
        category_id=number[3],
        serial_number=number[4:10],
        check_digit=int(number[10]),
        equipment_type=equipment_type,
    )
