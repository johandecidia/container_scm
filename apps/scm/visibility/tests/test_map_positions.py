"""LOC-4: what the map is allowed to say, and what it must refuse to say.

Every test here exists because the failure it prevents is invisible. A map does not
look wrong — a dot is a dot, and it carries the same confidence whether it came from
an operator's gate-in, a carrier's guess, or a booking's destination field. The
errors this module guards against all end the same way: somebody drives to a
terminal to collect a container that is not there.

The four refusals, in order of how expensive they are to get wrong:

**A destination is not a position.** The single most tempting fallback, because it
is always available and always plausible, and it silently reports every unlocated
container as having arrived where it was going.

**A forecast is not an observation.** An ETA at Oceanterminalen is a plan. Drawing
it puts the box at a terminal it has not reached.

**An unresolved place is not a canonical place.** The resolver refuses to choose
between two candidate terminals; a map that picks one anyway makes that refusal
worthless.

**A carrier's word does not outrank an accepted movement.** LOC-2's projection has
already weighed a late-arriving event against an operator's gate-in. The map reads
that answer rather than re-deciding it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.choices import LocationResolutionStatus, LocationSource, MovementType
from apps.scm.containers.models import Container, ContainerLocation
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.models import TrackingEvent
from apps.scm.visibility.arrival_lifecycle import ArrivalState
from apps.scm.visibility.geojson import map_feature_collection
from apps.scm.visibility.map_positions import (
    MapFilters,
    PositionClass,
    get_operational_map,
    parse_map_filters,
)
from apps.scm.visibility.selectors import list_visibility_objects
from apps.teams.models import Team

from .factories import (
    equipment_type,
    ingest_maersk_events,
    make_container,
    make_location,
    make_user_and_team,
    place_container_at,
    resolve_tracking_to,
)

# Real coordinates for real places, used only as fixture values. Nothing in the
# product invents these — see the module docstring of map_positions.
GOTHENBURG = ("57.708870", "11.974560")
OCEANTERMINALEN = ("57.696629", "11.858448")
ROTTERDAM = ("51.949760", "4.144830")
STOCKHOLM = ("59.329323", "18.068581")


class MapFixture(TestCase):
    """One shipment, one container, and the four places it might be said to be.

    Oceanterminalen and Göteborg deliberately share ``SEGOT`` and are not nested,
    which is LOC-1's documented plurality case. Two consequences the tests below
    depend on, both of them real behaviour rather than fixture convenience:

    * The fixture's carrier events resolve to AMBIGUOUS, so ingestion records no
      canonical location and LOC-2 records no automatic movement from them. Every
      container therefore starts with nothing plottable, and each test establishes
      the position it is about.
    * A test that wants resolved tracking says so, through ``resolve_tracking_to``.
    """

    team: Team
    oceanterminalen: ContainerLocation
    goteborg: ContainerLocation
    rotterdam: ContainerLocation
    stockholm: ContainerLocation

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("map@example.com", "map-team")
        cls.oceanterminalen = make_location(
            cls.team, "Oceanterminalen", unlocode="SEGOT", latitude=OCEANTERMINALEN[0], longitude=OCEANTERMINALEN[1]
        )
        cls.goteborg = make_location(
            cls.team, "Göteborg", unlocode="SEGOT", latitude=GOTHENBURG[0], longitude=GOTHENBURG[1]
        )
        cls.rotterdam = make_location(
            cls.team, "Rotterdam", unlocode="NLRTM", latitude=ROTTERDAM[0], longitude=ROTTERDAM[1]
        )
        cls.stockholm = make_location(
            cls.team, "Stockholm", unlocode="SESTO", latitude=STOCKHOLM[0], longitude=STOCKHOLM[1]
        )

    def make_shipment(self, number: str = "SHP-MAP", *, destination=None, eta=None) -> Shipment:
        return Shipment.objects.create(
            team=self.team,
            shipment_number=number,
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            destination_location=destination,
            eta=eta,
        )

    def tracked_container(self, shipment=None, number: str | None = None):
        container = make_container(self.team) if number is None else _container(self.team, number)
        if shipment is not None:
            ShipmentContainer.objects.create(shipment=shipment, container=container)
        ingest_maersk_events(self.team, container, shipment=shipment)
        return container

    def build_map(self, filters: MapFilters | None = None):
        return get_operational_map(self.team, list_visibility_objects(self.team), filters)

    def markers(self, filters: MapFilters | None = None) -> list[dict]:
        return [f["properties"] for f in map_feature_collection(self.build_map(filters))["features"]]

    def classes_by_place(self, filters: MapFilters | None = None) -> dict[tuple[str, str], int]:
        return {
            (marker["position_class"], marker["location_name"]): marker["container_count"]
            for marker in self.markers(filters)
        }


def _container(team: Team, number: str) -> Container:
    return Container.objects.create(
        team=team,
        owner_code=number[:3],
        category_id=number[3],
        serial_number=number[4:10],
        check_digit=int(number[10]),
        equipment_type=equipment_type(),
    )


class PositionPrecedenceTest(MapFixture):
    """Which of three available answers becomes the marker."""

    def test_physical_beats_tracking_and_destination(self):
        """The example from the brief: at Oceanterminalen, seen at Göteborg, bound for Stockholm.

        Only one of those three is a statement somebody accepted about where the box
        is, and it is the one the map draws.
        """
        shipment = self.make_shipment(destination=self.stockholm)
        container = self.tracked_container(shipment)
        resolve_tracking_to(self.team, container, self.goteborg)
        place_container_at(self.team, container, self.oceanterminalen)

        self.assertEqual(self.classes_by_place(), {(PositionClass.PHYSICAL, "Oceanterminalen"): 1})

    def test_tracking_answers_when_there_is_no_accepted_physical_position(self):
        """No physical, resolved actual tracking at Rotterdam, bound for Oceanterminalen."""
        shipment = self.make_shipment(destination=self.oceanterminalen)
        container = self.tracked_container(shipment)
        resolve_tracking_to(self.team, container, self.rotterdam)

        self.assertEqual(self.classes_by_place(), {(PositionClass.TRACKING, "Rotterdam"): 1})

    def test_a_tracking_marker_says_it_came_from_the_carrier(self):
        shipment = self.make_shipment(destination=self.oceanterminalen)
        container = self.tracked_container(shipment)
        resolve_tracking_to(self.team, container, self.rotterdam)

        marker = self.markers()[0]

        self.assertEqual(marker["source_label"], "Maersk")
        self.assertEqual(marker["place_statement"], "Last reported at Rotterdam")

    def test_a_physical_marker_says_which_movement_and_whose_word_it_is(self):
        container = self.tracked_container(self.make_shipment())
        place_container_at(self.team, container, self.oceanterminalen, movement_type=MovementType.GATE_IN)

        marker = self.markers()[0]

        self.assertEqual(marker["detail"], "Gate In")
        self.assertEqual(marker["source_label"], "Manual")
        self.assertEqual(marker["place_statement"], "At Oceanterminalen")
        self.assertTrue(marker["age_display"])

    def test_no_current_marker_when_only_a_destination_is_known(self):
        """The refusal that matters most.

        Nothing has been accepted, nothing usable has been reported, and the box is
        booked to Oceanterminalen. A marker there would say it had arrived.
        """
        shipment = self.make_shipment(destination=self.oceanterminalen)
        self.tracked_container(shipment)

        self.assertEqual(self.markers(), [])

    def test_a_container_with_no_position_is_counted_rather_than_hidden(self):
        shipment = self.make_shipment(destination=self.oceanterminalen)
        self.tracked_container(shipment)

        coverage = self.build_map().coverage

        self.assertEqual(coverage.unplottable_containers, 1)
        self.assertEqual(coverage.plotted_containers, 0)

    def test_a_gated_out_container_has_no_physical_marker(self):
        """Gated out and nowhere else recorded is not "still at the terminal".

        LOC-2 projects ``current_location`` to NULL for a departure, and the map
        follows it rather than keeping the last place it knew.
        """
        container = self.tracked_container(self.make_shipment())
        place_container_at(self.team, container, self.oceanterminalen)
        place_container_at(self.team, container, None, movement_type=MovementType.GATE_OUT)

        self.assertEqual(self.markers(), [])


class HistoricalMovementTest(MapFixture):
    """The map consumes LOC-2's projection; it does not recompute one."""

    def test_an_older_movement_does_not_beat_the_projected_current_location(self):
        """A carrier event ingested late, describing something that happened earlier.

        LOC-2 has already decided this does not move the box. A map that took the
        most recently *recorded* movement would move it back to Rotterdam.
        """
        container = self.tracked_container(self.make_shipment())
        place_container_at(
            self.team,
            container,
            self.oceanterminalen,
            occurred_at=datetime(2026, 8, 9, 14, 32, tzinfo=UTC),
        )
        place_container_at(
            self.team,
            container,
            self.rotterdam,
            occurred_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
            source=LocationSource.TRACKING_EVENT,
        )

        container.refresh_from_db()
        self.assertEqual(container.current_location_id, self.oceanterminalen.pk)
        self.assertEqual(self.classes_by_place(), {(PositionClass.PHYSICAL, "Oceanterminalen"): 1})

    def test_the_marker_reports_the_time_of_the_winning_movement(self):
        container = self.tracked_container(self.make_shipment())
        accepted = datetime(2026, 8, 9, 14, 32, tzinfo=UTC)
        place_container_at(self.team, container, self.oceanterminalen, occurred_at=accepted)
        place_container_at(
            self.team,
            container,
            self.rotterdam,
            occurred_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
            source=LocationSource.TRACKING_EVENT,
        )

        self.assertEqual(self.markers()[0]["occurred_at"], accepted.isoformat())


class TrackingEvidenceTest(MapFixture):
    """Which tracking evidence is good enough to place a container."""

    def test_a_forecast_never_becomes_a_current_tracking_position(self):
        """An ETA at Oceanterminalen is a plan, not a place the box has been."""
        shipment = self.make_shipment(destination=self.stockholm)
        container = self.tracked_container(shipment)
        TrackingEvent.objects.filter(team=self.team, container=container).update(
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED
        )
        resolve_tracking_to(self.team, container, self.oceanterminalen)

        self.assertEqual(self.markers(), [])

    def test_a_forecast_is_ignored_even_when_an_older_observation_exists(self):
        """The newest event is a forecast; the newest *observation* is what counts."""
        shipment = self.make_shipment(destination=self.stockholm)
        container = self.tracked_container(shipment)
        events = TrackingEvent.objects.filter(team=self.team, container=container)
        observed = events.filter(event_time_type=TrackingEvent.EventTimeType.ACTUAL).order_by("event_datetime").first()
        observed.location = self.rotterdam
        observed.location_resolution_status = LocationResolutionStatus.RESOLVED
        observed.save(update_fields=["location", "location_resolution_status"])
        TrackingEvent.objects.create(
            team=self.team,
            provider=observed.provider,
            container=container,
            event_type=TrackingEvent.EventType.VESSEL_ARRIVED,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
            event_datetime=timezone.now() + timedelta(days=4),
            location=self.oceanterminalen,
            location_resolution_status=LocationResolutionStatus.RESOLVED,
            event_fingerprint="forecast-after-observation",
        )

        self.assertEqual(self.classes_by_place(), {(PositionClass.TRACKING, "Rotterdam"): 1})

    def test_an_ambiguous_place_produces_no_canonical_marker(self):
        """The resolver refused to choose between two terminals sharing SEGOT.

        Choosing one here would make that refusal pointless and put the box in the
        wrong half of a port.
        """
        shipment = self.make_shipment(destination=self.stockholm)
        container = self.tracked_container(shipment)
        resolve_tracking_to(self.team, container, self.goteborg, status=LocationResolutionStatus.AMBIGUOUS)

        self.assertEqual(self.markers(), [])

    def test_an_unresolved_place_produces_no_canonical_marker(self):
        shipment = self.make_shipment(destination=self.stockholm)
        container = self.tracked_container(shipment)
        resolve_tracking_to(self.team, container, self.goteborg, status=LocationResolutionStatus.UNRESOLVED)

        self.assertEqual(self.markers(), [])

    def test_raw_carrier_coordinates_alone_do_not_place_a_container(self):
        """The fixture's events carry real coordinates and no canonical identity.

        They are evidence, and they are drawn on the container's own journey map.
        They are not a canonical position, because nobody has said which of MCR's
        places they are.
        """
        container = self.tracked_container(self.make_shipment())
        located = TrackingEvent.objects.filter(team=self.team, container=container, location_latitude__isnull=False)

        self.assertTrue(located.exists())
        self.assertEqual(self.markers(), [])


class MissingCoordinatesTest(MapFixture):
    """A canonical place without coordinates is valid, undrawable, and reported."""

    def test_a_physical_position_without_coordinates_is_not_drawn(self):
        depot = make_location(self.team, "John Evans Depot")
        container = self.tracked_container(self.make_shipment())
        place_container_at(self.team, container, depot)

        self.assertEqual(self.markers(), [])

    def test_it_is_reported_as_fixable_data_quality(self):
        depot = make_location(self.team, "John Evans Depot")
        container = self.tracked_container(self.make_shipment())
        place_container_at(self.team, container, depot)

        coverage = self.build_map().coverage

        self.assertEqual(coverage.containers_missing_coordinates, 1)
        self.assertEqual(coverage.unplottable_containers, 1)
        self.assertEqual([location.pk for location in coverage.locations_missing_coordinates], [depot.pk])

    def test_the_location_is_named_once_however_many_containers_are_there(self):
        depot = make_location(self.team, "John Evans Depot")
        shipment = self.make_shipment()
        for number in ("MSKU0000006", "MSKU0000109"):
            container = self.tracked_container(shipment, number=number)
            place_container_at(self.team, container, depot)

        coverage = self.build_map().coverage

        self.assertEqual(coverage.locations_missing_coordinates_count, 1)
        self.assertEqual(coverage.containers_missing_coordinates, 2)


class GroupingTest(MapFixture):
    """One marker per canonical place per class, carrying a count."""

    def test_containers_at_one_place_become_one_marker(self):
        shipment = self.make_shipment()
        for number in (None, "MSKU0000006", "MSKU0000109"):
            container = self.tracked_container(shipment, number=number)
            place_container_at(self.team, container, self.oceanterminalen)

        markers = self.markers()

        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["container_count"], 3)

    def test_a_grouped_marker_carries_no_single_container_number(self):
        """The first container's number is not a label for the other eighty."""
        shipment = self.make_shipment()
        for number in (None, "MSKU0000006"):
            place_container_at(self.team, self.tracked_container(shipment, number=number), self.oceanterminalen)

        self.assertEqual(self.markers()[0]["container_number"], "")

    def test_a_grouped_marker_reports_the_freshest_claim_in_it(self):
        shipment = self.make_shipment()
        older, newer = datetime(2026, 8, 1, 9, 0, tzinfo=UTC), datetime(2026, 8, 9, 14, 32, tzinfo=UTC)
        place_container_at(self.team, self.tracked_container(shipment), self.oceanterminalen, occurred_at=older)
        place_container_at(
            self.team,
            self.tracked_container(shipment, number="MSKU0000006"),
            self.oceanterminalen,
            occurred_at=newer,
        )

        self.assertEqual(self.markers()[0]["occurred_at"], newer.isoformat())

    def test_two_places_sharing_a_coordinate_stay_two_markers(self):
        """Grouping is by canonical location, never by coordinate.

        Two locations MCR chose to record separately are two places, even if
        somebody typed the same latitude into both.
        """
        twin = make_location(
            self.team, "APM Terminals", unlocode="SEGOT", latitude=OCEANTERMINALEN[0], longitude=OCEANTERMINALEN[1]
        )
        shipment = self.make_shipment()
        place_container_at(self.team, self.tracked_container(shipment), self.oceanterminalen)
        place_container_at(self.team, self.tracked_container(shipment, number="MSKU0000006"), twin)

        self.assertEqual(len(self.markers()), 2)


class DestinationOverlayTest(MapFixture):
    """Where inbound volume is heading — a second question, asked explicitly."""

    def test_destinations_are_absent_until_asked_for(self):
        shipment = self.make_shipment(destination=self.oceanterminalen)
        container = self.tracked_container(shipment)
        resolve_tracking_to(self.team, container, self.rotterdam)

        classes = {marker["position_class"] for marker in self.markers()}

        self.assertEqual(classes, {PositionClass.TRACKING})

    def test_asking_for_them_adds_a_labelled_destination_marker(self):
        shipment = self.make_shipment(destination=self.oceanterminalen)
        container = self.tracked_container(shipment)
        resolve_tracking_to(self.team, container, self.rotterdam)

        markers = self.classes_by_place(MapFilters(show_destinations=True))

        self.assertEqual(
            markers,
            {
                (PositionClass.TRACKING, "Rotterdam"): 1,
                (PositionClass.DESTINATION, "Oceanterminalen"): 1,
            },
        )

    def test_a_destination_marker_says_it_is_not_a_position(self):
        shipment = self.make_shipment(destination=self.oceanterminalen)
        self.tracked_container(shipment)

        marker = next(
            m
            for m in self.markers(MapFilters(show_destinations=True))
            if m["position_class"] == PositionClass.DESTINATION
        )

        self.assertTrue(marker["is_destination"])
        self.assertFalse(marker["is_current"])
        self.assertEqual(marker["place_statement"], "Bound for Oceanterminalen")

    def test_destination_markers_are_not_counted_as_current_positions(self):
        """The count that must never absorb the overlay."""
        shipment = self.make_shipment(destination=self.oceanterminalen)
        container = self.tracked_container(shipment)
        resolve_tracking_to(self.team, container, self.rotterdam)

        operational_map = self.build_map(MapFilters(show_destinations=True))

        self.assertEqual(operational_map.plotted_container_count, 1)
        self.assertEqual(len(operational_map.destination_groups), 1)
        self.assertEqual(operational_map.coverage.plotted_containers, 1)

    def test_inbound_volume_to_one_place_is_grouped(self):
        shipment = self.make_shipment(destination=self.oceanterminalen)
        for number in (None, "MSKU0000006", "MSKU0000109"):
            self.tracked_container(shipment, number=number)

        destination = next(
            m
            for m in self.markers(MapFilters(show_destinations=True))
            if m["position_class"] == PositionClass.DESTINATION
        )

        self.assertEqual(destination["container_count"], 3)

    def test_an_arrived_container_is_no_longer_inbound_volume(self):
        """It has got there. Leaving it on the overlay would answer the wrong question.

        "What is still coming to Oceanterminalen" must not include what is standing
        in it.
        """
        shipment = self.make_shipment(destination=self.oceanterminalen)
        container = self.tracked_container(shipment)
        place_container_at(self.team, container, self.oceanterminalen)

        classes = {m["position_class"] for m in self.markers(MapFilters(show_destinations=True))}

        self.assertEqual(classes, {PositionClass.PHYSICAL})

    def test_a_shipment_with_no_canonical_destination_contributes_nothing(self):
        """Not matched to a location by the booking's free text."""
        shipment = self.make_shipment()
        shipment.destination_port = "Gothenburg"
        shipment.save(update_fields=["destination_port"])
        self.tracked_container(shipment)

        self.assertEqual(self.markers(MapFilters(show_destinations=True)), [])


class ArrivalLifecycleOnTheMapTest(MapFixture):
    """LOC-3's answer travels to the marker; it is not re-derived there."""

    def test_a_marker_carries_the_lifecycle_state(self):
        shipment = self.make_shipment(destination=self.oceanterminalen, eta=timezone.localdate() + timedelta(days=6))
        container = self.tracked_container(shipment)
        resolve_tracking_to(self.team, container, self.rotterdam)

        self.assertEqual(self.markers()[0]["arrival_state"], ArrivalState.EXPECTED)

    def test_a_container_accepted_at_its_destination_reads_as_arrived(self):
        shipment = self.make_shipment(destination=self.oceanterminalen, eta=timezone.localdate())
        container = self.tracked_container(shipment)
        place_container_at(self.team, container, self.oceanterminalen)

        self.assertEqual(self.markers()[0]["arrival_state"], ArrivalState.ARRIVED)

    def test_a_received_container_reads_as_received(self):
        shipment = self.make_shipment(destination=self.oceanterminalen, eta=timezone.localdate())
        container = self.tracked_container(shipment)
        place_container_at(self.team, container, self.oceanterminalen)
        place_container_at(self.team, container, self.oceanterminalen, movement_type=MovementType.RECEIVED)

        self.assertEqual(self.markers()[0]["arrival_state"], ArrivalState.RECEIVED)

    def test_an_overdue_arrival_is_flagged_on_the_marker(self):
        shipment = self.make_shipment(destination=self.oceanterminalen, eta=timezone.localdate() - timedelta(days=4))
        container = self.tracked_container(shipment)
        resolve_tracking_to(self.team, container, self.rotterdam)

        marker = self.markers()[0]

        self.assertEqual(marker["arrival_state"], ArrivalState.EXPECTED)
        self.assertEqual(marker["overdue_count"], 1)

    def test_a_grouped_marker_breaks_the_states_down_rather_than_picking_one(self):
        """Two boxes at one terminal at different stages are not one badge."""
        shipment = self.make_shipment(destination=self.oceanterminalen, eta=timezone.localdate())
        arrived = self.tracked_container(shipment)
        received = self.tracked_container(shipment, number="MSKU0000006")
        place_container_at(self.team, arrived, self.oceanterminalen)
        place_container_at(self.team, received, self.oceanterminalen)
        place_container_at(self.team, received, self.oceanterminalen, movement_type=MovementType.RECEIVED)

        counts = self.markers()[0]["arrival_state_counts"]

        self.assertEqual(counts, [{"label": "Arrived", "count": 1}, {"label": "Received", "count": 1}])


class MapFilterTest(MapFixture):
    """The map's own view state, parsed from a query string."""

    def test_no_parameters_means_both_current_classes(self):
        filters = parse_map_filters(_params())
        self.assertTrue(filters.includes(PositionClass.PHYSICAL))
        self.assertTrue(filters.includes(PositionClass.TRACKING))
        self.assertFalse(filters.includes(PositionClass.DESTINATION))

    def test_choosing_physical_excludes_tracking(self):
        filters = parse_map_filters(_params(position=["physical"]))
        self.assertTrue(filters.includes(PositionClass.PHYSICAL))
        self.assertFalse(filters.includes(PositionClass.TRACKING))

    def test_an_unrecognised_class_narrows_nothing_rather_than_erroring(self):
        """A hand-edited or stale link shows the default map, not a 500."""
        filters = parse_map_filters(_params(position=["vessel"]))
        self.assertTrue(filters.includes(PositionClass.PHYSICAL))
        self.assertTrue(filters.includes(PositionClass.TRACKING))

    def test_a_destination_class_cannot_be_asked_for_as_a_position_type(self):
        """Only the destinations toggle turns the overlay on."""
        filters = parse_map_filters(_params(position=["destination"]))
        self.assertFalse(filters.includes(PositionClass.DESTINATION))

    def test_filtering_to_physical_removes_the_tracking_markers(self):
        shipment = self.make_shipment()
        physical = self.tracked_container(shipment)
        tracked = self.tracked_container(shipment, number="MSKU0000006")
        place_container_at(self.team, physical, self.oceanterminalen)
        resolve_tracking_to(self.team, tracked, self.rotterdam)

        self.assertEqual(len(self.markers()), 2)
        self.assertEqual(
            self.classes_by_place(MapFilters(position_classes=frozenset({PositionClass.PHYSICAL}))),
            {(PositionClass.PHYSICAL, "Oceanterminalen"): 1},
        )

    def test_the_coverage_counts_describe_the_whole_selection_not_the_visible_layers(self):
        """Switching a layer off does not make its containers unplottable.

        The counts answer "how much of this fleet can be drawn at all", which does
        not change when somebody hides a layer to read the map.
        """
        shipment = self.make_shipment()
        physical = self.tracked_container(shipment)
        tracked = self.tracked_container(shipment, number="MSKU0000006")
        place_container_at(self.team, physical, self.oceanterminalen)
        resolve_tracking_to(self.team, tracked, self.rotterdam)

        coverage = self.build_map(MapFilters(position_classes=frozenset({PositionClass.PHYSICAL}))).coverage

        self.assertEqual(coverage.physical_containers, 1)
        self.assertEqual(coverage.tracking_containers, 1)


class MapQueryCountTest(MapFixture):
    """The map's cost must not grow with the fleet.

    A map is the one page that shows everything at once, so a per-container query
    here is the difference between a page and an outage. LOC-3 pinned its own
    interpreter the same way; this pins the two reads LOC-4 adds on top.
    """

    shipment: Shipment
    added: int

    def setUp(self):
        self.shipment = self.make_shipment(destination=self.oceanterminalen)
        self.added = 0

    def _add_containers(self, count: int) -> None:
        for _index in range(count):
            self.added += 1
            container = _container(self.team, _with_check_digit(f"MSKU{self.added:06d}"))
            ShipmentContainer.objects.create(shipment=self.shipment, container=container)
            ingest_maersk_events(self.team, container, shipment=self.shipment)
            place_container_at(self.team, container, self.oceanterminalen)

    def _map_queries(self) -> int:
        objects = list_visibility_objects(self.team)
        with self.assertNumQueries(2):
            get_operational_map(self.team, objects, MapFilters(show_destinations=True))
        return sum(obj.container_count for obj in objects)

    def test_building_the_map_costs_two_queries_whatever_the_fleet_size(self):
        """One for the winning movements, one for the canonical observations.

        Measured twice, at two fleet sizes, rather than once: a fixed number that
        happens to match at one size proves nothing, and the failure mode being
        guarded against is growth.
        """
        self._add_containers(1)
        self.assertEqual(self._map_queries(), 1)

        self._add_containers(5)
        self.assertEqual(self._map_queries(), 6)


def _params(**kwargs):
    """A QueryDict-alike for the filter parser, which only needs getlist and get."""
    from django.http import QueryDict

    query = QueryDict(mutable=True)
    for key, value in kwargs.items():
        if isinstance(value, list):
            query.setlist(key, value)
        else:
            query[key] = value
    return query


def _with_check_digit(body: str) -> str:
    """Complete a ten-character ISO 6346 body into a valid container number.

    Computed rather than hard-coded so the query-count test can generate as many
    containers as it needs without a table of magic numbers.
    """
    from apps.scm.containers.utils import calculate_check_digit

    digit = calculate_check_digit(body[:3], body[3], body[4:10])
    return f"{body[:10]}{digit}"
