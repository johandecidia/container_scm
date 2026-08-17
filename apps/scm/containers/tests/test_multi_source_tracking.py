"""Tests for a container that has more than one verified tracking source.

The workspace, the panel and the journey map all used to speak for whichever
subscription happened to be newest. What is tested here is that they now speak for
all of them at once: every source is listed, every timeline entry says who reported
it, freshness spans every watch, and the place the container is *now* comes from the
derived current location rather than from the newest carrier event.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.choices import LocationSource, LocationType
from apps.scm.containers.models import Container, ContainerLocation, EquipmentType
from apps.scm.containers.selectors import get_container_workspace
from apps.scm.tracking.journey import PHYSICAL_SOURCE_CODE, CurrentLocationBasis
from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingSubscription
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "multi-source"}}
_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

PANEL_TEMPLATE = "scm/containers/partials/container_tracking_panel.html"

DISCHARGED_AT_BORN = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)
AT_GOTHENBURG = datetime(2026, 8, 16, 9, 30, tzinfo=UTC)

CONTAINER_NUMBER = "MCUU2009300"


@override_settings(CACHES=_LOCMEM, STORAGES=_TEST_STORAGES)
class MultiSourceTestBase(TestCase):
    team_slug: str

    def setUp(self):
        self.team = Team.objects.create(name=self.team_slug, slug=self.team_slug)
        self.user = CustomUser.objects.create_user(username=f"{self.team_slug}@example.com", password="pass")
        self.team.members.add(self.user, through_defaults={"role": ROLE_MEMBER})
        self.client_ = Client()
        self.client_.force_login(self.user)

        self.cma = _provider("cma_cgm", "CMA CGM")
        self.cosco = _provider("cosco", "COSCO Shipping")
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
            "event_fingerprint": f"{provider.code}-{event_type}-{when}-{kwargs.get('location_name', '')}",
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

    def physically_at(self, name, when, **kwargs):
        location = ContainerLocation.objects.create(
            team=self.team,
            name=name,
            location_type=kwargs.pop("location_type", LocationType.DEPOT),
            **kwargs,
        )
        self.container.current_location = location
        self.container.last_location_update = when
        self.container.location_source = LocationSource.MANUAL
        self.container.save()
        return location

    def born_then_gothenburg(self):
        """The journey from the brief: CMA to Born, then physically in Gothenburg."""
        self.subscription(self.cma)
        self.event(
            self.cma,
            TrackingEvent.EventType.DISCHARGED,
            DISCHARGED_AT_BORN,
            location_name="Born",
            location_unlocode="NLBON",
            location_latitude=Decimal("50.887"),
            location_longitude=Decimal("5.808"),
        )
        self.physically_at("Oceanterminalen", AT_GOTHENBURG, city="Gothenburg", country="Sweden")

    def workspace(self):
        return get_container_workspace(self.team, self.container)

    def detail(self):
        return self.client_.get(reverse("containers:detail", args=[self.container.pk]))

    def map_data(self):
        return self.client_.get(reverse("visibility:container_map_data", args=[self.container.pk])).json()


class WorkspaceSpeaksForEverySourceTest(MultiSourceTestBase):
    team_slug = "multi-source-workspace"

    def test_all_sources_are_listed_not_just_the_newest_watch(self):
        self.subscription(self.cma)
        self.subscription(self.cosco)

        self.assertEqual(self.workspace().tracking_source_names, ["CMA CGM", "COSCO Shipping"])
        self.assertTrue(self.workspace().has_multiple_tracking_sources)

    def test_the_physical_record_is_listed_beside_the_carriers(self):
        self.born_then_gothenburg()

        sources = self.workspace().tracking_sources

        self.assertEqual([source.code for source in sources], ["cma_cgm", PHYSICAL_SOURCE_CODE])

    def test_a_single_source_container_still_names_one_carrier(self):
        """The list column and the carrier filter can only show one."""
        self.subscription(self.cma)

        self.assertEqual(self.workspace().tracking_carrier_name, "CMA CGM")

    def test_freshness_spans_every_watch(self):
        recent = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)
        self.subscription(self.cma, last_synced_at=datetime(2026, 8, 10, 6, 0, tzinfo=UTC))
        self.subscription(self.cosco, last_synced_at=recent)

        self.assertEqual(self.workspace().last_refreshed_at, recent)

    def test_the_next_check_is_the_soonest_across_live_watches(self):
        soonest = datetime(2026, 8, 17, 7, 0, tzinfo=UTC)
        self.subscription(self.cma, next_sync_at=datetime(2026, 8, 18, 7, 0, tzinfo=UTC))
        self.subscription(self.cosco, next_sync_at=soonest)

        self.assertEqual(self.workspace().next_check_at, soonest)

    def test_a_finished_watch_does_not_promise_a_next_check(self):
        self.subscription(
            self.cma,
            status=TrackingSubscription.Status.COMPLETED,
            next_sync_at=datetime(2026, 8, 18, 7, 0, tzinfo=UTC),
        )

        self.assertIsNone(self.workspace().next_check_at)

    def test_the_current_location_can_be_the_physical_one(self):
        self.born_then_gothenburg()

        workspace = self.workspace()

        self.assertEqual(workspace.derived_current_location.basis, CurrentLocationBasis.PHYSICAL)
        self.assertEqual(workspace.current_position.label, "Oceanterminalen")
        # And the carrier's own last position is still available, unchanged.
        self.assertEqual(workspace.position.label, "Born")

    def test_the_gap_is_derived_on_the_workspace(self):
        self.born_then_gothenburg()

        self.assertTrue(self.workspace().has_tracking_gap)
        self.assertEqual(self.workspace().tracking_gap.from_location, "Born")

    def test_a_bulk_workspace_reports_no_journey_rather_than_a_partial_one(self):
        from apps.scm.containers.workspace import get_container_workspaces

        self.born_then_gothenburg()

        workspace = get_container_workspaces(self.team, [self.container])[self.container.pk]

        self.assertIsNone(workspace.journey)
        self.assertEqual(workspace.tracking_sources, [])
        self.assertIsNone(workspace.tracking_gap)
        # The carrier position is still there — that is what a bulk workspace is for.
        self.assertEqual(workspace.position.label, "Born")

    def test_the_timeline_holds_both_providers_events(self):
        self.subscription(self.cma)
        self.subscription(self.cosco)
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_name="Born")
        self.event(self.cosco, TrackingEvent.EventType.GATE_OUT, AT_GOTHENBURG, location_name="Gothenburg")

        timeline = self.workspace().journey_timeline

        self.assertEqual([point.source.name for point in timeline], ["COSCO Shipping", "CMA CGM"])


class TrackingPanelShowsEverySourceTest(MultiSourceTestBase):
    team_slug = "multi-source-panel"

    def test_the_panel_lists_the_tracking_sources(self):
        self.subscription(self.cma)
        self.subscription(self.cosco)

        response = self.detail()

        self.assertContains(response, "Tracking sources")
        self.assertContains(response, "CMA CGM")
        self.assertContains(response, "COSCO Shipping")

    def test_every_timeline_entry_names_its_source(self):
        self.subscription(self.cma)
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_name="Born")

        self.assertContains(self.detail(), "Source: CMA CGM")

    def test_a_second_source_for_the_same_event_is_shown_beside_the_first(self):
        self.subscription(self.cma)
        self.subscription(self.cosco)
        for provider, offset in ((self.cma, 0), (self.cosco, 3)):
            self.event(
                provider,
                TrackingEvent.EventType.DISCHARGED,
                DISCHARGED_AT_BORN + timedelta(minutes=offset),
                location_name="Born",
                location_unlocode="NLBON",
            )

        response = self.detail()

        self.assertContains(response, "Source: CMA CGM")
        self.assertContains(response, "also COSCO Shipping")

    def test_the_physical_observation_appears_on_the_timeline(self):
        self.born_then_gothenburg()

        response = self.detail()

        self.assertContains(response, "Oceanterminalen")
        self.assertContains(response, "At depot")
        self.assertContains(response, "Source: MCR")

    def test_a_gap_is_called_out(self):
        self.born_then_gothenburg()

        response = self.detail()

        self.assertContains(response, "Journey gap detected")
        self.assertContains(response, "Born → Oceanterminalen")
        self.assertContains(response, "No tracking source currently explains")

    def test_no_gap_means_no_alarm(self):
        self.subscription(self.cma)
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_name="Born")

        self.assertNotContains(self.detail(), "Journey gap detected")

    def test_several_providers_render_without_error(self):
        self.subscription(self.cma)
        self.subscription(self.cosco)
        self.event(self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, location_name="Born")
        self.event(self.cosco, TrackingEvent.EventType.GATE_OUT, AT_GOTHENBURG, location_name="Gothenburg")
        self.physically_at("Oceanterminalen", AT_GOTHENBURG + timedelta(days=1), city="Gothenburg")

        response = self.detail()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, PANEL_TEMPLATE)

    def test_the_refresh_response_carries_the_same_journey(self):
        self.born_then_gothenburg()

        response = self.client_.post(
            reverse("containers:refresh_tracking", args=[self.container.pk]),
            **{"HTTP_HX_REQUEST": "true"},
        )

        self.assertTemplateUsed(response, PANEL_TEMPLATE)
        self.assertContains(response, "Journey gap detected")
        self.assertContains(response, "Source: MCR")


class JourneyMapUsesEverySourceTest(MultiSourceTestBase):
    team_slug = "multi-source-map"

    def _located(self, provider, event_type, when, name, unlocode, latitude, longitude):
        return self.event(
            provider,
            event_type,
            when,
            location_name=name,
            location_unlocode=unlocode,
            location_latitude=Decimal(latitude),
            location_longitude=Decimal(longitude),
        )

    def test_both_providers_contribute_points(self):
        self._located(
            self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, "Born", "NLBON", "50.887", "5.808"
        )
        self._located(
            self.cosco, TrackingEvent.EventType.GATE_OUT, AT_GOTHENBURG, "Gothenburg", "SEGOT", "57.708", "11.974"
        )

        points = [f for f in self.map_data()["features"] if f["geometry"]["type"] == "Point"]

        self.assertEqual({point["properties"]["source_name"] for point in points}, {"CMA CGM", "COSCO Shipping"})

    def test_each_point_says_who_reported_it(self):
        self._located(
            self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, "Born", "NLBON", "50.887", "5.808"
        )

        point = next(f for f in self.map_data()["features"] if f["geometry"]["type"] == "Point")

        self.assertEqual(point["properties"]["source_name"], "CMA CGM")
        self.assertEqual(point["properties"]["source_label"], "CMA CGM")
        self.assertEqual(point["properties"]["source_kind"], "carrier")

    def test_a_corroborated_point_names_both_sources(self):
        self._located(
            self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, "Born", "NLBON", "50.887", "5.808"
        )
        self._located(
            self.cosco,
            TrackingEvent.EventType.DISCHARGED,
            DISCHARGED_AT_BORN + timedelta(minutes=2),
            "Born",
            "NLBON",
            "50.887",
            "5.808",
        )

        points = [f for f in self.map_data()["features"] if f["geometry"]["type"] == "Point"]

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["properties"]["source_label"], "CMA CGM · COSCO Shipping")

    def test_the_current_marker_follows_the_derived_current_location(self):
        self._located(
            self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, "Born", "NLBON", "50.887", "5.808"
        )
        self._located(
            self.cosco, TrackingEvent.EventType.GATE_OUT, AT_GOTHENBURG, "Gothenburg", "SEGOT", "57.708", "11.974"
        )

        current = [f for f in self.map_data()["features"] if f["properties"].get("is_current")]

        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["properties"]["position_label"], "Gothenburg")

    def test_nothing_is_marked_current_when_the_container_is_somewhere_unmappable(self):
        """A physical observation has no coordinates, so no point may claim to be it."""
        self.born_then_gothenburg()

        features = self.map_data()["features"]

        self.assertTrue(features)
        self.assertFalse([f for f in features if f["properties"].get("is_current")])

    def test_a_forecast_leg_is_still_drawn_as_a_forecast(self):
        self._located(
            self.cma, TrackingEvent.EventType.DISCHARGED, DISCHARGED_AT_BORN, "Born", "NLBON", "50.887", "5.808"
        )
        self.event(
            self.cma,
            TrackingEvent.EventType.VESSEL_ARRIVED,
            AT_GOTHENBURG,
            location_name="Gothenburg",
            location_unlocode="SEGOT",
            location_latitude=Decimal("57.708"),
            location_longitude=Decimal("11.974"),
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )

        lines = [f for f in self.map_data()["features"] if f["geometry"]["type"] == "LineString"]

        self.assertTrue(any(line["properties"]["is_forecast"] for line in lines))
        self.assertFalse(any(line["properties"]["is_vessel_track"] for line in lines))

    def test_another_teams_container_is_not_reachable(self):
        other_team = Team.objects.create(name="multi-source-map-other", slug="multi-source-map-other")
        other_container = _container(other_team, CONTAINER_NUMBER)

        response = self.client_.get(reverse("visibility:container_map_data", args=[other_container.pk]))

        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
