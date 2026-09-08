"""TRACK-UX: the Control Tower answers "what are we tracking" first.

Two changes, and both of them are about two surfaces agreeing.

**One position truth.** The Control Tower map used to draw canonically-resolved
places only, while the Container Workspace drew the carrier's own coordinates. A
container tracked through an aggregator whose places had never been resolved was
therefore confidently placed on its own page and absent from the fleet map — and an
absent container reads as one nobody knows anything about, not as one whose port has
not been filed yet. ``BBCU3273070`` is the real box this happened to;
:class:`ReportedPositionOnBothSurfacesTest` is its regression.

**One dataset behind the list and the map.** The board opens on Tracking, and
Exceptions and Delayed are two other views of the same read model. Whichever is
selected, the map is drawn from the objects the list is showing — anything else and a
filter narrows the rows while the map keeps describing the whole fleet, which is the
most convincing way to be wrong about where things are.

Nothing here defines what tracking, an exception or a delay *is*. The tracking domain
decides the first and the two engines decide the others; these tests assert that the
board reads those answers rather than growing its own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.models import Container
from apps.scm.containers.workspace import get_container_workspace, get_container_workspaces
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.models import TrackingEvent, TrackingSubscription
from apps.scm.visibility.geojson import map_feature_collection
from apps.scm.visibility.map_positions import PositionClass, get_operational_map
from apps.scm.visibility.selectors import (
    VisibilityFilters,
    VisibilityView,
    get_visibility_overview,
    list_visibility_objects,
)
from apps.teams.models import Team

from .factories import (
    TEST_STORAGES,
    equipment_type,
    ingest_maersk_events,
    make_aggregator_subscription,
    make_container,
    make_location,
    make_provider,
    make_user_and_team,
    resolve_tracking_to,
    strip_reported_coordinates,
    watch_container,
)

# Shanghai, as the carrier reported it for BBCU3273070 — the observed position the
# workspace names and the fleet map used to leave off.
SHANGHAI = ("31.230416", "121.473701")
GOTHENBURG = ("57.694500", "11.952200")


def _container(team: Team, number: str) -> Container:
    return Container.objects.create(
        team=team,
        owner_code=number[:3],
        category_id=number[3],
        serial_number=number[4:10],
        check_digit=int(number[10]),
        equipment_type=equipment_type(),
    )


def _observed_event(team, container, *, when, name, latitude, longitude, provider=None, fingerprint=""):
    """One observed, located carrier event — the shape a Traqo payload lands in.

    Deliberately *unresolved*: ``location`` NULL and the status ingestion writes when
    no canonical location claimed the place. That is the state BBCU3273070's twelve
    events are actually in, and the state the map had no answer for.
    """
    from apps.scm.containers.choices import LocationResolutionStatus

    return TrackingEvent.objects.create(
        team=team,
        provider=provider or make_provider(code="traqo", name="Traqo Ocean"),
        container=container,
        event_type=TrackingEvent.EventType.VESSEL_DEPARTED,
        event_time_type=TrackingEvent.EventTimeType.ACTUAL,
        event_datetime=when,
        location_name=name,
        location_latitude=latitude,
        location_longitude=longitude,
        location_resolution_status=LocationResolutionStatus.UNRESOLVED,
        event_fingerprint=fingerprint or f"{container.pk}-{when.isoformat()}-{name}",
    )


# ---------------------------------------------------------------------------
# The map regression
# ---------------------------------------------------------------------------


@override_settings(STORAGES=TEST_STORAGES, MAPBOX_PUBLIC_TOKEN="pk.test-token")
class ReportedPositionOnBothSurfacesTest(TestCase):
    """BBCU3273070: tracked through Traqo under ONE, placed at Shanghai, unresolved.

    Reproduced from the real row — an aggregator watch whose ``provider`` is ``traqo``
    and whose ``carrier_code`` is ``one``, no accepted physical location, and observed
    events carrying the carrier's coordinates and nothing the resolver matched.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("bbcu@example.com", "bbcu-team")
        cls.container = _container(cls.team, "BBCU3273070")
        cls.subscription = make_aggregator_subscription(cls.team, cls.container)
        cls.event = _observed_event(
            cls.team,
            cls.container,
            when=datetime(2026, 8, 4, 3, 20, tzinfo=UTC),
            name="Shanghai",
            latitude=SHANGHAI[0],
            longitude=SHANGHAI[1],
            provider=cls.subscription.provider,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    # -- the premise --------------------------------------------------------

    def test_the_provider_is_not_the_carrier(self):
        """The shape that made every provider-equals-carrier read go wrong."""
        self.assertEqual(self.subscription.provider.code, "traqo")
        self.assertEqual(self.subscription.carrier_code, "one")
        self.assertFalse(self.subscription.is_direct)

    def test_nothing_canonical_places_this_container(self):
        self.assertIsNone(self.container.current_location_id)
        self.assertEqual(TrackingEvent.objects.filter(container=self.container, location__isnull=False).count(), 0)

    # -- the two surfaces ---------------------------------------------------

    def test_the_workspace_names_the_place_the_carrier_reported(self):
        workspace = get_container_workspace(self.team, self.container)
        position = workspace.current_position

        self.assertIsNotNone(position)
        self.assertEqual(position.label, "Shanghai")
        self.assertTrue(position.has_coordinates)

    def test_the_control_tower_dataset_includes_it(self):
        keys = {obj.key for obj in get_visibility_overview(self.team).objects}
        self.assertIn(f"container-{self.container.pk}", keys)

    def test_the_control_tower_map_draws_it(self):
        """The regression. Before TRACK-UX this collection was empty."""
        features = self.client.get(reverse("visibility:map_data")).json()["features"]

        self.assertEqual(len(features), 1)
        properties = features[0]["properties"]
        self.assertEqual(properties["position_class"], PositionClass.TRACKING)
        self.assertEqual(properties["location_name"], "Shanghai")
        self.assertEqual(properties["container_number"], "BBCU3273070")

    def test_the_map_uses_the_same_coordinates_the_workspace_does(self):
        """One position truth, not two agreeing by luck."""
        workspace = get_container_workspace(self.team, self.container)
        position = workspace.current_position
        geometry = self.client.get(reverse("visibility:map_data")).json()["features"][0]["geometry"]

        # GeoJSON order: longitude, then latitude.
        self.assertEqual(geometry["coordinates"], [float(position.longitude), float(position.latitude)])

    def test_the_marker_claims_no_canonical_identity(self):
        """Drawn as the carrier's word, which is what it is.

        No location id and no marker panel: the resolver has not said which of MCR's
        places Shanghai is, and offering to list what else is standing there would
        invent an identity for it.
        """
        properties = self.client.get(reverse("visibility:map_data")).json()["features"][0]["properties"]

        self.assertFalse(properties["is_canonical"])
        self.assertIsNone(properties["location_id"])
        self.assertEqual(properties["panel_url"], "")
        self.assertEqual(properties["place_statement"], "Last reported at Shanghai")

    def test_the_aggregator_watch_is_not_filtered_out_for_naming_two_parties(self):
        """``provider != carrier`` is the expected shape, never a reason to drop a row."""
        overview = get_visibility_overview(self.team)
        obj = next(obj for obj in overview.objects if obj.container == self.container)

        self.assertTrue(obj.is_actively_tracked)
        self.assertEqual(obj.carrier_name, "ONE (Ocean Network Express)")
        self.assertEqual(obj.tracking_provider_label, "Traqo Ocean")

    def test_the_row_prints_the_carrier_and_the_provider_as_two_facts(self):
        response = self.client.get(reverse("visibility:overview"))

        self.assertContains(response, "ONE (Ocean Network Express)")
        self.assertContains(response, "via Traqo Ocean")

    # -- precedence still holds --------------------------------------------

    def test_a_resolved_observation_takes_over_from_the_report(self):
        """A canonical place is the better answer as soon as there is one."""
        shanghai = make_location(self.team, "Shanghai", latitude=SHANGHAI[0], longitude=SHANGHAI[1])
        resolve_tracking_to(self.team, self.container, shanghai)

        properties = self.client.get(reverse("visibility:map_data")).json()["features"][0]["properties"]

        self.assertTrue(properties["is_canonical"])
        self.assertEqual(properties["location_id"], shanghai.pk)

    def test_an_accepted_physical_position_takes_over_from_both(self):
        from .factories import place_container_at

        depot = make_location(self.team, "Gothenburg Depot", latitude=GOTHENBURG[0], longitude=GOTHENBURG[1])
        place_container_at(self.team, self.container, depot)

        properties = self.client.get(reverse("visibility:map_data")).json()["features"][0]["properties"]

        self.assertEqual(properties["position_class"], PositionClass.PHYSICAL)
        self.assertEqual(properties["location_id"], depot.pk)

    def test_a_forecast_is_still_never_drawn(self):
        """The refusal the reported tier must not have loosened."""
        TrackingEvent.objects.filter(team=self.team, container=self.container).update(
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED
        )

        self.assertEqual(self.client.get(reverse("visibility:map_data")).json()["features"], [])


# ---------------------------------------------------------------------------
# What counts as being tracked
# ---------------------------------------------------------------------------


@override_settings(STORAGES=TEST_STORAGES)
class TrackingInclusionTest(TestCase):
    """Which containers the Tracking view is about, and which it refuses."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("incl@example.com", "incl-team")

    def _tracked_keys(self) -> set[str]:
        overview = get_visibility_overview(self.team, VisibilityFilters(view=VisibilityView.TRACKING))
        return {obj.key for obj in overview.objects}

    def test_a_direct_carrier_watch_is_tracked(self):
        container = make_container(self.team)
        ingest_maersk_events(self.team, container)

        self.assertIn(f"container-{container.pk}", self._tracked_keys())

    def test_an_aggregator_watch_naming_another_carrier_is_tracked(self):
        """provider = traqo, carrier = one. Both names, one tracked container."""
        container = _container(self.team, "BBCU3273070")
        make_aggregator_subscription(self.team, container)

        self.assertIn(f"container-{container.pk}", self._tracked_keys())

    def test_a_container_in_transit_with_no_watch_is_not_tracked(self):
        """The definition this view must not fall back to.

        A status is set by hand and by transport rules. It is not evidence that
        anybody is fetching anything, and a board that read IN_TRANSIT as tracked
        would list boxes no carrier has been asked about for a month.
        """
        container = make_container(self.team)
        shipment = Shipment.objects.create(
            team=self.team,
            shipment_number="SHP-NOWATCH",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            eta=timezone.localdate() + timedelta(days=4),
        )
        ShipmentContainer.objects.create(shipment=shipment, container=container)

        keys = self._tracked_keys()

        self.assertEqual(keys, set())
        # And it is still a real object on the board, reachable through another view.
        self.assertIn(f"shipment-{shipment.pk}", {obj.key for obj in list_visibility_objects(self.team)})

    def test_a_cancelled_watch_is_not_tracking(self):
        """Somebody stopped it on purpose."""
        container = make_container(self.team)
        watch_container(self.team, container, status=TrackingSubscription.Status.CANCELLED)

        self.assertEqual(self._tracked_keys(), set())

    def test_a_completed_watch_is_not_tracking(self):
        """Its leg is over. Its events stay on the journey."""
        container = make_container(self.team)
        watch_container(self.team, container, status=TrackingSubscription.Status.COMPLETED)

        self.assertEqual(self._tracked_keys(), set())

    def test_a_paused_watch_is_not_tracking(self):
        container = make_container(self.team)
        watch_container(self.team, container, status=TrackingSubscription.Status.PAUSED)

        self.assertEqual(self._tracked_keys(), set())

    def test_a_failing_watch_is_still_tracking(self):
        """A container we believe we are tracking and are not is exactly the point."""
        container = make_container(self.team)
        watch_container(self.team, container, status=TrackingSubscription.Status.FAILED)

        self.assertIn(f"container-{container.pk}", self._tracked_keys())

    def test_a_watch_mid_sync_does_not_blink_out_of_the_list(self):
        container = make_container(self.team)
        watch_container(self.team, container, status=TrackingSubscription.Status.SYNCING)

        self.assertIn(f"container-{container.pk}", self._tracked_keys())

    def test_the_board_and_the_container_panel_read_one_definition(self):
        """Two answers to "are we tracking this" would be the bug."""
        container = make_container(self.team)
        watch_container(self.team, container, status=TrackingSubscription.Status.FAILED)
        workspace = get_container_workspaces(self.team, [container])[container.pk]

        self.assertTrue(workspace.has_live_tracking)
        self.assertIn(f"container-{container.pk}", self._tracked_keys())


@override_settings(STORAGES=TEST_STORAGES)
class TrackingCountTest(TestCase):
    """Operational objects, not watches."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("cnt@example.com", "cnt-team")
        cls.container = _container(cls.team, "BBCU3273070")
        # The real multi-source shape: an old direct watch, and the current one
        # through an aggregator.
        watch_container(cls.team, cls.container, provider_code="cma_cgm")
        make_aggregator_subscription(cls.team, cls.container)

    def test_a_container_with_several_live_watches_is_counted_once(self):
        overview = get_visibility_overview(self.team)

        self.assertEqual(TrackingSubscription.objects.filter(container=self.container).count(), 2)
        self.assertEqual(overview.tracking_container_count, 1)

    def test_it_appears_in_the_list_once(self):
        keys = [obj.key for obj in get_visibility_overview(self.team).objects]
        self.assertEqual(keys.count(f"container-{self.container.pk}"), 1)

    def test_it_produces_one_marker_rather_than_one_per_source(self):
        _observed_event(
            self.team,
            self.container,
            when=datetime(2026, 8, 4, 3, 20, tzinfo=UTC),
            name="Shanghai",
            latitude=SHANGHAI[0],
            longitude=SHANGHAI[1],
        )
        objects = list_visibility_objects(self.team)

        features = map_feature_collection(get_operational_map(self.team, objects))["features"]

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["properties"]["container_count"], 1)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


@override_settings(STORAGES=TEST_STORAGES)
class ArrivalOrderingTest(TestCase):
    """Soonest arrival first, then whatever we have heard from most recently."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("ord@example.com", "ord-team")

    def _watched_shipment(self, number: str, *, eta, container_number: str) -> Shipment:
        shipment = Shipment.objects.create(
            team=self.team,
            shipment_number=number,
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            eta=eta,
        )
        container = _container(self.team, container_number)
        ShipmentContainer.objects.create(shipment=shipment, container=container)
        watch_container(self.team, container, shipment=shipment)
        return shipment

    def _labels(self) -> list[str]:
        return [obj.label for obj in get_visibility_overview(self.team).objects]

    def test_the_nearer_eta_comes_first(self):
        self._watched_shipment("SHP-15", eta=datetime(2026, 9, 15).date(), container_number="MSKU0000109")
        self._watched_shipment("SHP-10", eta=datetime(2026, 9, 10).date(), container_number="MSKU0000006")

        self.assertEqual(self._labels(), ["SHP-10", "SHP-15"])

    def test_an_object_with_no_eta_comes_after_every_object_with_one(self):
        """Nothing to answer "what is next" with sorts last, not as though it were 1970."""
        self._watched_shipment("SHP-NOETA", eta=None, container_number="MSKU0000006")
        self._watched_shipment("SHP-FAR", eta=datetime(2027, 1, 1).date(), container_number="MSKU0000109")

        self.assertEqual(self._labels(), ["SHP-FAR", "SHP-NOETA"])

    def test_objects_with_no_eta_are_ordered_by_what_we_heard_from_last(self):
        """Freshness descending — the boxes something is happening to lead."""
        quiet = self._watched_shipment("SHP-QUIET", eta=None, container_number="MSKU0000006")
        busy = self._watched_shipment("SHP-BUSY", eta=None, container_number="MSKU0000109")
        for shipment, when in (
            (quiet, datetime(2026, 7, 1, 8, 0, tzinfo=UTC)),
            (busy, datetime(2026, 9, 1, 8, 0, tzinfo=UTC)),
        ):
            container = shipment.shipment_containers.get().container
            _observed_event(
                self.team,
                container,
                when=when,
                name="Rotterdam",
                latitude="51.904383",
                longitude="4.442447",
            )

        self.assertEqual(self._labels(), ["SHP-BUSY", "SHP-QUIET"])

    def test_the_order_is_stable_between_two_identical_reads(self):
        """A list that reshuffles reads as data changing."""
        for index, number in enumerate(("MSKU0000006", "MSKU0000109", "MSKU0000201")):
            self._watched_shipment(f"SHP-SILENT-{index}", eta=None, container_number=number)

        self.assertEqual(self._labels(), self._labels())


# ---------------------------------------------------------------------------
# One dataset behind the list and the map
# ---------------------------------------------------------------------------


@override_settings(STORAGES=TEST_STORAGES, MAPBOX_PUBLIC_TOKEN="pk.test-token")
class MapFollowsTheSelectedViewTest(TestCase):
    """Whatever the list is showing is what the map draws.

    Three shipments, one per view: one merely tracked, one on customs hold, one whose
    ETA has moved. All three are placeable, so a map that ignored the view would
    happily draw all three whichever button was pressed.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("ds@example.com", "ds-team")
        cls.places = {
            "SHP-TRACKED": ("Shanghai", SHANGHAI),
            "SHP-HELD": ("Rotterdam", ("51.904383", "4.442447")),
            "SHP-LATE": ("Gothenburg", GOTHENBURG),
        }
        cls.containers = {}
        for index, (number, container_number) in enumerate(
            (("SHP-TRACKED", "MSKU0000006"), ("SHP-HELD", "MSKU0000109"), ("SHP-LATE", "MSKU0000201"))
        ):
            late = number == "SHP-LATE"
            shipment = Shipment.objects.create(
                team=cls.team,
                shipment_number=number,
                carrier="Maersk",
                status=Shipment.Status.IN_TRANSIT,
                eta=timezone.localdate() + timedelta(days=20 if late else index + 3),
                original_eta=timezone.localdate() + timedelta(days=index + 3),
            )
            container = _container(cls.team, container_number)
            ShipmentContainer.objects.create(shipment=shipment, container=container)
            watch_container(cls.team, container, shipment=shipment)
            name, (latitude, longitude) = cls.places[number]
            _observed_event(
                cls.team,
                container,
                when=timezone.now() - timedelta(hours=index + 1),
                name=name,
                latitude=latitude,
                longitude=longitude,
            )
            cls.containers[number] = container

        TrackingEvent.objects.create(
            team=cls.team,
            provider=make_provider(),
            shipment=Shipment.objects.get(team=cls.team, shipment_number="SHP-HELD"),
            container=cls.containers["SHP-HELD"],
            event_type=TrackingEvent.EventType.CUSTOMS_HOLD,
            event_datetime=timezone.now() - timedelta(hours=6),
            location_name="Rotterdam",
            description="Customs hold",
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _list_places(self, view: str) -> set[str]:
        overview = get_visibility_overview(self.team, VisibilityFilters(view=view))
        return {obj.label for obj in overview.objects}

    def _map_places(self, view: str) -> set[str]:
        response = self.client.get(reverse("visibility:map_data"), {"view": view})
        return {feature["properties"]["location_name"] for feature in response.json()["features"]}

    def test_every_view_has_something_of_its_own_to_show(self):
        """Otherwise the assertions below would pass on three empty sets."""
        for view in VisibilityView.values:
            with self.subTest(view=view):
                self.assertTrue(self._list_places(view))

    def test_the_map_never_draws_more_than_the_list(self):
        """A marker for something not in the list is the failure this prevents.

        Fewer is expected and correct — not everything in the list can be placed.
        """
        for view in VisibilityView.values:
            with self.subTest(view=view):
                expected = {self.places[label][0] for label in self._list_places(view)}
                self.assertTrue(self._map_places(view) <= expected)

    def test_the_exceptions_map_leaves_out_what_is_merely_tracked(self):
        self.assertEqual(self._map_places(VisibilityView.EXCEPTIONS), {"Rotterdam"})

    def test_the_delayed_map_leaves_out_what_is_merely_tracked(self):
        self.assertEqual(self._map_places(VisibilityView.DELAYED), {"Gothenburg"})

    def test_the_tracking_map_shows_all_three(self):
        """All three are being watched — an exception does not stop that being true."""
        self.assertEqual(self._map_places(VisibilityView.TRACKING), {"Shanghai", "Rotterdam", "Gothenburg"})

    def test_the_boards_map_url_carries_the_view_to_the_endpoint(self):
        """Which is how the two stay in step at all: one query string, read twice."""
        response = self.client.get(reverse("visibility:overview"), {"view": "delayed"})
        self.assertContains(response, f'data-scm-map-source="{reverse("visibility:map_data")}?view=delayed"')

    def test_a_container_with_nothing_placeable_is_in_the_list_and_not_on_the_map(self):
        """The honest asymmetry: the list is the fleet, the map is what can be drawn."""
        strip_reported_coordinates(self.team, self.containers["SHP-TRACKED"])

        self.assertIn("SHP-TRACKED", self._list_places(VisibilityView.TRACKING))
        self.assertNotIn("Shanghai", self._map_places(VisibilityView.TRACKING))


@override_settings(STORAGES=TEST_STORAGES)
class TrackingViewQueryCountTest(TestCase):
    """The Tracking view must not cost a query per container.

    It is the default view of the one page that covers a whole fleet, so the read that
    decides what is tracked has to come off data already loaded. It does — the
    workspaces carry every subscription — and this is what stops that regressing into
    a per-container ``exists()``.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("qc@example.com", "qc-team")
        cls.shipment = Shipment.objects.create(
            team=cls.team, shipment_number="SHP-QC", carrier="Maersk", status=Shipment.Status.IN_TRANSIT
        )

    def _add_containers(self, numbers):
        from .factories import with_check_digit

        for number in numbers:
            container = _container(self.team, with_check_digit(number))
            ShipmentContainer.objects.create(shipment=self.shipment, container=container)
            watch_container(self.team, container, shipment=self.shipment)
            make_aggregator_subscription(self.team, container, shipment=self.shipment)

    def _count(self) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            overview = get_visibility_overview(self.team, VisibilityFilters(view=VisibilityView.TRACKING))
            # Touched inside the block: a lazily-derived count would issue its
            # queries here rather than in the builder.
            self.assertTrue(overview.tracking_container_count >= 0)
            list(overview.objects)
        return len(captured)

    def test_doubling_the_containers_does_not_change_the_query_count(self):
        self._add_containers(["MSKU000001", "MSKU000002"])
        before = self._count()

        self._add_containers(["MSKU000003", "MSKU000004"])

        self.assertEqual(self._count(), before, "The Tracking view is issuing queries per container.")
