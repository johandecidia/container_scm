"""Tests for tracking gap detection.

The point of these is as much what does *not* raise a gap as what does. A gap is
a contradiction between two sources — the box was observed somewhere no source
followed it to — and never carrier silence, a rename of the same place, or a
forecast. A platform that cried gap on any of those would train people to ignore
the warning that matters.
"""

from datetime import UTC, datetime, timedelta

from django.test import TestCase

from apps.scm.containers.choices import LocationSource, LocationType
from apps.scm.containers.models import Container, ContainerLocation, EquipmentType
from apps.scm.tracking.gaps import GapConfidence, GapReason, detect_tracking_gap
from apps.scm.tracking.journey import PHYSICAL_SOURCE_CODE, get_container_journey
from apps.scm.tracking.models import TrackingEvent, TrackingProvider
from apps.teams.models import Team

DISCHARGED_AT_BORN = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)
AT_GOTHENBURG = datetime(2026, 8, 16, 9, 30, tzinfo=UTC)

CONTAINER_NUMBER = "MCUU2009300"


class GapTestBase(TestCase):
    team_slug: str

    def setUp(self):
        self.team = _team(self.team_slug)
        self.cma = _provider("cma_cgm", "CMA CGM")
        self.cosco = _provider("cosco", "COSCO Shipping")
        self.container = _container(self.team, CONTAINER_NUMBER)

    def carrier_event(self, provider, event_type, when, **kwargs):
        defaults = {
            "team": self.team,
            "provider": provider,
            "container": self.container,
            "event_type": event_type,
            "event_time_type": TrackingEvent.EventTimeType.ACTUAL,
            "event_datetime": when,
            "event_fingerprint": f"{provider.code}-{event_type}-{when}-{kwargs.get('location_name', '')}",
        }
        defaults.update(kwargs)
        return TrackingEvent.objects.create(**defaults)

    def discharged_at_born(self, when=DISCHARGED_AT_BORN):
        return self.carrier_event(
            self.cma,
            TrackingEvent.EventType.DISCHARGED,
            when,
            location_name="Born",
            location_unlocode="NLBON",
        )

    def physically_at(self, name, when, *, source=LocationSource.MANUAL, **kwargs):
        location = ContainerLocation.objects.create(
            team=self.team,
            name=name,
            location_type=kwargs.pop("location_type", LocationType.DEPOT),
            **kwargs,
        )
        self.container.current_location = location
        self.container.last_location_update = when
        self.container.location_source = source
        self.container.save()
        return location

    def gap(self):
        return detect_tracking_gap(get_container_journey(self.team, self.container))


class GapIsRaisedByAContradictionTest(GapTestBase):
    """A physical observation beyond the last carrier report is a real gap."""

    team_slug = "gap-raised"

    def test_a_later_observation_elsewhere_is_a_gap(self):
        self.discharged_at_born()
        self.physically_at("Oceanterminalen", AT_GOTHENBURG, city="Gothenburg", country="Sweden")

        gap = self.gap()

        self.assertIsNotNone(gap)
        self.assertEqual(gap.from_location, "Born")
        self.assertEqual(gap.from_unlocode, "NLBON")
        self.assertEqual(gap.from_datetime, DISCHARGED_AT_BORN)
        self.assertEqual(gap.from_source.name, "CMA CGM")
        self.assertEqual(gap.to_location, "Oceanterminalen")
        self.assertEqual(gap.to_datetime, AT_GOTHENBURG)
        self.assertEqual(gap.to_source.code, PHYSICAL_SOURCE_CODE)
        self.assertEqual(gap.reason, GapReason.UNEXPLAINED_PHYSICAL_MOVE)
        self.assertEqual(gap.segment, "Born → Oceanterminalen")

    def test_a_gap_no_carrier_ever_mentioned_is_a_strong_signal(self):
        self.discharged_at_born()
        self.physically_at("Oceanterminalen", AT_GOTHENBURG, city="Gothenburg")

        gap = self.gap()

        self.assertEqual(gap.confidence, GapConfidence.HIGH)
        self.assertTrue(gap.is_strong)

    def test_a_forecast_for_the_destination_lowers_the_confidence_without_closing_it(self):
        """Somebody expected the box there. Nobody confirmed it arrived."""
        self.discharged_at_born()
        self.carrier_event(
            self.cma,
            TrackingEvent.EventType.VESSEL_ARRIVED,
            AT_GOTHENBURG + timedelta(days=3),
            location_name="Gothenburg",
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        self.physically_at("Gothenburg", AT_GOTHENBURG, city="Gothenburg")

        gap = self.gap()

        self.assertIsNotNone(gap)
        self.assertEqual(gap.confidence, GapConfidence.MEDIUM)
        self.assertFalse(gap.is_strong)

    def test_a_planned_event_alone_cannot_close_an_observed_gap(self):
        self.discharged_at_born()
        self.carrier_event(
            self.cma,
            TrackingEvent.EventType.GATE_OUT,
            AT_GOTHENBURG + timedelta(days=1),
            location_name="Gothenburg",
            event_time_type=TrackingEvent.EventTimeType.PLANNED,
        )
        self.physically_at("Gothenburg", AT_GOTHENBURG, city="Gothenburg")

        self.assertIsNotNone(self.gap())

    def test_the_description_names_both_ends(self):
        self.discharged_at_born()
        self.physically_at("Oceanterminalen", AT_GOTHENBURG, city="Gothenburg")

        description = str(self.gap().description)

        self.assertIn("Born", description)
        self.assertIn("Oceanterminalen", description)


class GapIsWithheldTest(GapTestBase):
    """Everything that must not be reported as a gap."""

    team_slug = "gap-withheld"

    def test_carrier_silence_alone_is_not_a_gap(self):
        """No news says nothing about whether the box moved."""
        self.discharged_at_born(datetime(2026, 7, 1, 8, 0, tzinfo=UTC))

        self.assertIsNone(self.gap())

    def test_an_observation_at_the_same_place_is_not_a_gap(self):
        self.carrier_event(
            self.cma,
            TrackingEvent.EventType.DISCHARGED,
            DISCHARGED_AT_BORN,
            location_name="Gothenburg",
            location_unlocode="SEGOT",
        )
        self.physically_at("Gothenburg", AT_GOTHENBURG, city="Gothenburg")

        self.assertIsNone(self.gap())

    def test_a_depot_inside_the_carriers_port_is_the_same_place(self):
        """ "Oceanterminalen, Göteborg" is not somewhere else than "Göteborg"."""
        self.carrier_event(
            self.cma,
            TrackingEvent.EventType.DISCHARGED,
            DISCHARGED_AT_BORN,
            location_name="Göteborg",
            location_unlocode="SEGOT",
        )
        self.physically_at("Oceanterminalen", AT_GOTHENBURG, city="Goteborg", country="Sweden")

        self.assertIsNone(self.gap())

    def test_a_unlocode_on_the_location_settles_the_match_exactly(self):
        """Which is the answer to a carrier naming the port in another language."""
        self.carrier_event(
            self.cma,
            TrackingEvent.EventType.DISCHARGED,
            DISCHARGED_AT_BORN,
            location_name="Gothenburg",
            location_unlocode="SEGOT",
        )
        self.physically_at("Oceanterminalen", AT_GOTHENBURG, external_reference="SEGOT")

        self.assertIsNone(self.gap())

    def test_a_carrier_that_reported_the_destination_explains_the_move(self):
        self.discharged_at_born()
        self.carrier_event(
            self.cma,
            TrackingEvent.EventType.GATE_IN,
            AT_GOTHENBURG - timedelta(hours=6),
            location_name="Gothenburg",
            location_unlocode="SEGOT",
        )
        self.physically_at("Gothenburg", AT_GOTHENBURG, city="Gothenburg")

        self.assertIsNone(self.gap())

    def test_a_second_carrier_can_explain_what_the_first_never_mentioned(self):
        """The whole reason a container may have several tracking sources."""
        self.discharged_at_born()
        self.carrier_event(
            self.cosco,
            TrackingEvent.EventType.GATE_IN,
            AT_GOTHENBURG - timedelta(days=1),
            location_name="Gothenburg",
            location_unlocode="SEGOT",
        )
        self.physically_at("Gothenburg", AT_GOTHENBURG, city="Gothenburg")

        self.assertIsNone(self.gap())

    def test_an_earlier_observation_is_not_evidence_of_a_missed_move(self):
        self.physically_at("Oceanterminalen", DISCHARGED_AT_BORN - timedelta(days=3), city="Gothenburg")
        self.discharged_at_born()

        self.assertIsNone(self.gap())

    def test_an_observation_at_the_same_moment_is_not_a_gap(self):
        self.discharged_at_born()
        self.physically_at("Oceanterminalen", DISCHARGED_AT_BORN, city="Gothenburg")

        self.assertIsNone(self.gap())

    def test_a_container_no_carrier_has_ever_observed_has_no_gap(self):
        """That is an untracked container, not a journey with a hole in it."""
        self.physically_at("Oceanterminalen", AT_GOTHENBURG, city="Gothenburg")

        self.assertIsNone(self.gap())

    def test_a_carrier_forecast_is_not_the_start_of_a_gap(self):
        self.carrier_event(
            self.cma,
            TrackingEvent.EventType.VESSEL_ARRIVED,
            DISCHARGED_AT_BORN,
            location_name="Born",
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        self.physically_at("Oceanterminalen", AT_GOTHENBURG, city="Gothenburg")

        self.assertIsNone(self.gap())

    def test_a_location_written_from_a_carrier_event_cannot_contradict_it(self):
        self.discharged_at_born()
        self.physically_at(
            "Gothenburg",
            AT_GOTHENBURG,
            source=LocationSource.TRACKING_EVENT,
            location_type=LocationType.PORT,
        )

        self.assertIsNone(self.gap())

    def test_an_undated_location_cannot_evidence_a_gap(self):
        self.discharged_at_born()
        location = ContainerLocation.objects.create(team=self.team, name="Depot", location_type=LocationType.DEPOT)
        self.container.current_location = location
        self.container.location_source = LocationSource.MANUAL
        self.container.save()

        self.assertIsNone(self.gap())

    def test_a_journey_with_nothing_in_it_has_no_gap(self):
        self.assertIsNone(self.gap())

    def test_a_gap_closes_by_itself_when_a_carrier_explains_the_segment(self):
        """Nothing is stored, so the gap is gone the moment the events explain it."""
        self.discharged_at_born()
        self.physically_at("Gothenburg", AT_GOTHENBURG, city="Gothenburg")
        self.assertIsNotNone(self.gap())

        self.carrier_event(
            self.cosco,
            TrackingEvent.EventType.GATE_IN,
            AT_GOTHENBURG - timedelta(hours=2),
            location_name="Gothenburg",
            location_unlocode="SEGOT",
        )

        self.assertIsNone(self.gap())


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
