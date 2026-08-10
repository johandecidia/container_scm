"""The overview: grouping, statistics, filters and health.

The grouping rules are the ones worth protecting. Twenty boxes discharged at one
terminal must be one point on the map and one row in the list, or the map becomes
unreadable at exactly the scale it is meant to help with — and a container tracked
without a shipment must still be its own object, or standalone tracking silently
disappears from the page that exists to show everything.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.models import Container
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.models import TrackingEvent
from apps.scm.visibility.geojson import overview_feature_collection
from apps.scm.visibility.read_models import Health, ObjectKind
from apps.scm.visibility.selectors import (
    VisibilityFilters,
    get_visibility_overview,
    list_visibility_objects,
    parse_visibility_filters,
)
from apps.teams.models import Team

from .factories import equipment_type, ingest_maersk_events, make_container, make_user_and_team


def _container(team, number: str) -> Container:
    return Container.objects.create(
        team=team,
        owner_code=number[:3],
        category_id=number[3],
        serial_number=number[4:10],
        check_digit=int(number[10]),
        equipment_type=equipment_type(),
    )


class OverviewGroupingTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        _user, cls.team = make_user_and_team("grp@example.com", "grp-team")
        cls.shipment = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-GRP",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            eta=timezone.localdate() + timedelta(days=3),
        )
        # Three boxes on one vessel, all reported at the same terminal.
        cls.on_shipment = [
            make_container(cls.team),
            _container(cls.team, "MSKU0000006"),
            _container(cls.team, "MSKU0000109"),
        ]
        for container in cls.on_shipment:
            ShipmentContainer.objects.create(shipment=cls.shipment, container=container)
            ingest_maersk_events(cls.team, container, shipment=cls.shipment)
        # And one tracked on its own.
        cls.standalone = _container(cls.team, "MSKU0000201")
        ingest_maersk_events(cls.team, cls.standalone)

    def test_a_shipment_is_one_object_however_many_containers_it_carries(self):
        objects = list_visibility_objects(self.team)
        shipments = [obj for obj in objects if obj.kind == ObjectKind.SHIPMENT]
        self.assertEqual(len(shipments), 1)
        self.assertEqual(shipments[0].container_count, 3)

    def test_a_standalone_container_is_its_own_object(self):
        containers = [obj for obj in list_visibility_objects(self.team) if obj.kind == ObjectKind.CONTAINER]
        self.assertEqual(len(containers), 1)
        self.assertEqual(containers[0].container.pk, self.standalone.pk)

    def test_containers_on_a_shipment_are_not_also_listed_separately(self):
        objects = list_visibility_objects(self.team)
        self.assertEqual(len(objects), 2)

    def test_containers_reported_at_the_same_place_share_one_map_point(self):
        """Three identical dots on one terminal tell nobody anything."""
        features = overview_feature_collection(list_visibility_objects(self.team))["features"]
        shipment_points = [f for f in features if f["properties"]["object_type"] == ObjectKind.SHIPMENT]
        self.assertEqual(len(shipment_points), 1)
        self.assertEqual(shipment_points[0]["properties"]["container_count"], 3)

    def test_containers_that_have_gone_separate_ways_get_separate_points(self):
        moved = self.on_shipment[0]
        TrackingEvent.objects.filter(team=self.team, container=moved).update(location_unlocode="NLRTM")
        features = overview_feature_collection(list_visibility_objects(self.team))["features"]
        shipment_points = [f for f in features if f["properties"]["object_type"] == ObjectKind.SHIPMENT]
        self.assertEqual(len(shipment_points), 2)

    def test_statistics_count_shipments_and_containers_separately(self):
        overview = get_visibility_overview(self.team)
        self.assertEqual(overview.active_shipments, 1)
        self.assertEqual(overview.tracked_containers, 4)

    def test_a_draft_shipment_is_not_on_the_board(self):
        Shipment.objects.create(team=self.team, shipment_number="SHP-DRAFT", status=Shipment.Status.DRAFT)
        labels = {obj.label for obj in list_visibility_objects(self.team)}
        self.assertNotIn("SHP-DRAFT", labels)


class OverviewFilterTest(TestCase):
    team: Team

    @classmethod
    def setUpTestData(cls):
        _user, cls.team = make_user_and_team("flt@example.com", "flt-team")
        cls.soon = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-SOON",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            eta=timezone.localdate() + timedelta(days=3),
            original_eta=timezone.localdate() + timedelta(days=3),
        )
        cls.late = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-LATE",
            carrier="MSC",
            status=Shipment.Status.IN_TRANSIT,
            eta=timezone.localdate() + timedelta(days=20),
            original_eta=timezone.localdate() + timedelta(days=10),
        )

    def _labels(self, **kwargs) -> set[str]:
        overview = get_visibility_overview(self.team, VisibilityFilters(**kwargs))
        return {obj.label for obj in overview.objects}

    def test_no_filter_shows_everything(self):
        self.assertEqual(self._labels(), {"SHP-SOON", "SHP-LATE"})

    def test_search_matches_a_shipment_number(self):
        self.assertEqual(self._labels(search="soon"), {"SHP-SOON"})

    def test_the_carrier_filter_narrows_to_one_carrier(self):
        self.assertEqual(self._labels(carrier="MSC"), {"SHP-LATE"})

    def test_the_eta_window_filter_uses_the_current_eta(self):
        self.assertEqual(self._labels(eta_window="7"), {"SHP-SOON"})

    def test_delayed_only_uses_the_existing_delay_engine(self):
        """SHP-LATE's ETA moved ten days; that is the delay engine's own verdict."""
        self.assertEqual(self._labels(delayed_only=True), {"SHP-LATE"})

    def test_a_delayed_object_reports_delayed_health(self):
        overview = get_visibility_overview(self.team, VisibilityFilters(delayed_only=True))
        self.assertEqual(overview.objects[0].health, Health.DELAYED)

    def test_an_undelayed_object_with_an_eta_is_on_time(self):
        overview = get_visibility_overview(self.team, VisibilityFilters(search="soon"))
        self.assertEqual(overview.objects[0].health, Health.ON_TIME)

    def test_an_object_with_no_eta_is_unknown_rather_than_on_time(self):
        """Nothing to judge against is not the same as nothing wrong."""
        Shipment.objects.create(team=self.team, shipment_number="SHP-NOETA", status=Shipment.Status.IN_TRANSIT)
        overview = get_visibility_overview(self.team, VisibilityFilters(search="noeta"))
        self.assertEqual(overview.objects[0].health, Health.UNKNOWN)

    def test_carrier_choices_are_offered_before_filtering_narrows_them(self):
        overview = get_visibility_overview(self.team, VisibilityFilters(carrier="MSC"))
        self.assertEqual(overview.carrier_choices, ["MSC", "Maersk"])

    def test_the_overdue_window_finds_a_passed_eta(self):
        Shipment.objects.create(
            team=self.team,
            shipment_number="SHP-OVERDUE",
            status=Shipment.Status.IN_TRANSIT,
            eta=timezone.localdate() - timedelta(days=2),
        )
        self.assertIn("SHP-OVERDUE", self._labels(eta_window="overdue"))


class OverviewQueryCountTest(TestCase):
    """The overview must not issue queries per object.

    It is the one page that covers a whole fleet, so a per-container query here is
    the difference between a page and an outage. The assertion is on the *shape* of
    the cost — the same work for twice the containers — rather than on an exact
    number, which would break on any unrelated select_related.
    """

    @classmethod
    def setUpTestData(cls):
        _user, cls.team = make_user_and_team("nplus1@example.com", "nplus1-team")
        cls.shipment = Shipment.objects.create(
            team=cls.team, shipment_number="SHP-N", carrier="Maersk", status=Shipment.Status.IN_TRANSIT
        )

    def _add_containers(self, numbers):
        for number in numbers:
            container = _container(self.team, number)
            ShipmentContainer.objects.create(shipment=self.shipment, container=container)
            ingest_maersk_events(self.team, container, shipment=self.shipment)

    def test_doubling_the_containers_does_not_change_the_query_count(self):
        self._add_containers(["MSKU0000006", "MSKU0000109"])
        before = _count_queries(self.team)

        self._add_containers(["MSKU0000201", "MSKU0000304"])
        after = _count_queries(self.team)

        self.assertEqual(after, before, "The overview is issuing queries per container.")

    def test_the_overview_stays_within_a_small_fixed_budget(self):
        """Currently 16, whatever the fleet size. The headroom is for select_related."""
        self._add_containers(["MSKU0000006", "MSKU0000109"])
        self.assertLessEqual(_count_queries(self.team), 20)


def _count_queries(team) -> int:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        list_visibility_objects(team)
    return len(captured)


class FilterParsingTest(TestCase):
    def test_query_parameters_map_onto_the_filter_object(self):
        filters = parse_visibility_filters(
            {"status": "in_transit", "carrier": "Maersk", "eta": "7", "delayed": "1", "search": " box "}
        )
        self.assertEqual(filters.status, "in_transit")
        self.assertEqual(filters.carrier, "Maersk")
        self.assertEqual(filters.eta_window, "7")
        self.assertTrue(filters.delayed_only)
        self.assertFalse(filters.exceptions_only)
        self.assertEqual(filters.search, "box")

    def test_an_empty_query_string_is_not_an_active_filter(self):
        self.assertFalse(parse_visibility_filters({}).is_active)
