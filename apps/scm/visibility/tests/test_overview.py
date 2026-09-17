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
from apps.scm.visibility.geojson import map_feature_collection
from apps.scm.visibility.map_positions import PositionClass, get_operational_map
from apps.scm.visibility.read_models import Health, ObjectKind
from apps.scm.visibility.selectors import (
    VIEW_ALL,
    VisibilityFilters,
    VisibilityView,
    get_visibility_overview,
    list_visibility_objects,
    parse_visibility_filters,
)
from apps.teams.models import Team

from .factories import (
    equipment_type,
    ingest_maersk_events,
    make_container,
    make_location,
    make_user_and_team,
    resolve_tracking_to,
)


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

    def test_containers_at_the_same_canonical_place_share_one_marker(self):
        """Three identical dots on one terminal tell nobody anything."""
        terminal = make_location(
            self.team, "Oceanterminalen", unlocode="SEGOT", latitude="57.696629", longitude="11.858448"
        )
        for container in self.on_shipment:
            resolve_tracking_to(self.team, container, terminal)

        features = self._canonical_features()

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["properties"]["container_count"], 3)
        self.assertEqual(features[0]["properties"]["location_id"], terminal.pk)

    def test_containers_at_different_canonical_places_get_separate_markers(self):
        terminal = make_location(
            self.team, "Oceanterminalen", unlocode="SEGOT", latitude="57.696629", longitude="11.858448"
        )
        rotterdam = make_location(self.team, "Rotterdam", unlocode="NLRTM", latitude="51.949760", longitude="4.144830")
        resolve_tracking_to(self.team, self.on_shipment[0], rotterdam)
        for container in self.on_shipment[1:]:
            resolve_tracking_to(self.team, container, terminal)

        counts = {
            f["properties"]["location_id"]: f["properties"]["container_count"] for f in self._canonical_features()
        }

        self.assertEqual(counts, {terminal.pk: 2, rotterdam.pk: 1})

    def test_a_marker_never_merges_two_position_classes_at_one_place(self):
        """Physically here and merely reported here are two different claims.

        Collapsing them would produce one marker of three at Oceanterminalen, and
        the operator would have no way of telling which of the boxes anybody has
        actually seen.
        """
        from .factories import place_container_at

        terminal = make_location(
            self.team, "Oceanterminalen", unlocode="SEGOT", latitude="57.696629", longitude="11.858448"
        )
        for container in self.on_shipment:
            resolve_tracking_to(self.team, container, terminal)
        place_container_at(self.team, self.on_shipment[0], terminal)

        by_class = {
            f["properties"]["position_class"]: f["properties"]["container_count"] for f in self._canonical_features()
        }

        self.assertEqual(by_class, {PositionClass.PHYSICAL: 1, PositionClass.TRACKING: 2})

    def _map_features(self):
        team_objects = list_visibility_objects(self.team)
        return map_feature_collection(get_operational_map(self.team, team_objects))["features"]

    def _canonical_features(self):
        """Only the markers standing on one of MCR's own locations.

        The grouping rules below are about canonical places, and the fixture's
        standalone container is reported at a place that never resolved — a real
        state, drawn as the carrier's word, and not something a test about grouping
        by location should have to account for.
        """
        return [f for f in self._map_features() if f["properties"]["is_canonical"]]

    def test_statistics_count_shipments_and_containers_separately(self):
        """Three boxes on one vessel plus one standalone: one shipment, four boxes.

        The container number is distinct containers under a live watch — see
        VisibilityOverview.tracking_container_count — so the three folded into a
        shipment are still counted individually, which is what an operator means by
        "how many containers are we tracking".
        """
        overview = get_visibility_overview(self.team)
        self.assertEqual(overview.active_shipments, 1)
        self.assertEqual(overview.tracking_container_count, 4)

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

    def test_the_delayed_view_uses_the_existing_delay_engine(self):
        """SHP-LATE's ETA moved ten days; that is the delay engine's own verdict."""
        self.assertEqual(self._labels(view=VisibilityView.DELAYED), {"SHP-LATE"})

    def test_a_delayed_object_reports_delayed_health(self):
        overview = get_visibility_overview(self.team, VisibilityFilters(view=VisibilityView.DELAYED))
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
            {"view": "delayed", "status": "in_transit", "carrier": "Maersk", "eta": "7", "search": " box "}
        )
        self.assertEqual(filters.view, VisibilityView.DELAYED)
        self.assertEqual(filters.status, "in_transit")
        self.assertEqual(filters.carrier, "Maersk")
        self.assertEqual(filters.eta_window, "7")
        self.assertEqual(filters.search, "box")

    def test_an_empty_query_string_asks_for_the_tracking_view(self):
        filters = parse_visibility_filters({})
        self.assertEqual(filters.view, VisibilityView.TRACKING)
        self.assertFalse(filters.is_active)

    def test_an_unrecognised_view_falls_back_to_tracking_rather_than_erroring(self):
        self.assertEqual(parse_visibility_filters({"view": "everything"}).view, VisibilityView.TRACKING)

    def test_a_legacy_exceptions_flag_still_selects_the_exceptions_view(self):
        """Links written before the views existed carried the filter as a flag."""
        self.assertEqual(parse_visibility_filters({"exceptions": "1"}).view, VisibilityView.EXCEPTIONS)

    def test_a_legacy_delayed_flag_still_selects_the_delayed_view(self):
        self.assertEqual(parse_visibility_filters({"delayed": "1"}).view, VisibilityView.DELAYED)

    def test_an_explicit_view_wins_over_a_legacy_flag(self):
        params = {"view": "tracking", "delayed": "1"}
        self.assertEqual(parse_visibility_filters(params).view, VisibilityView.TRACKING)

    def test_the_filter_object_defaults_to_narrowing_nothing(self):
        """The dataclass is filter state; the URL is where Tracking is the default.

        Callers that compose these reads rather than serving a request — the location
        quality queue, the work queues — must be able to say "no filters" and mean it.
        """
        self.assertEqual(VisibilityFilters().view, VIEW_ALL)
        self.assertFalse(VisibilityFilters().is_active)
