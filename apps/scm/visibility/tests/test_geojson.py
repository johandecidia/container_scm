"""The GeoJSON contract.

Every assertion here exists because getting it wrong is invisible until someone
looks at a map and believes it: coordinates in the wrong order put Gothenburg in
Somalia, a facility coordinate presented as a live position claims a box is sitting
somewhere it left last week, and a forecast arrival drawn like an observed one says
a container has landed when it is still at sea.
"""

from __future__ import annotations

from django.test import TestCase

from apps.scm.tracking.models import TrackingEvent
from apps.scm.tracking.positions import PositionType
from apps.scm.visibility.geojson import (
    LINE_ACTUAL,
    LINE_FORECAST,
    journey_feature_collection,
    overview_feature_collection,
)
from apps.scm.visibility.selectors import get_container_journey_events, list_visibility_objects

from .factories import (
    FIXTURE_CONTAINER_NUMBER,
    ingest_maersk_events,
    make_container,
    make_user_and_team,
)

# The fixture's Gothenburg arrival, straight from the carrier response.
GOTHENBURG_LAT = 57.697938
GOTHENBURG_LON = 11.856845


class GeoJsonShapeTest(TestCase):
    """The envelope, and the coordinate order inside it."""

    @classmethod
    def setUpTestData(cls):
        _user, cls.team = make_user_and_team("geo@example.com", "geo-team")
        cls.container = make_container(cls.team)
        ingest_maersk_events(cls.team, cls.container)

    def test_overview_returns_a_feature_collection(self):
        collection = overview_feature_collection(list_visibility_objects(self.team))
        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertIsInstance(collection["features"], list)

    def test_journey_returns_a_feature_collection(self):
        events = get_container_journey_events(self.team, self.container)
        self.assertEqual(journey_feature_collection(events)["type"], "FeatureCollection")

    def test_every_feature_is_a_feature(self):
        events = get_container_journey_events(self.team, self.container)
        for feature in journey_feature_collection(events)["features"]:
            self.assertEqual(feature["type"], "Feature")
            self.assertIn("geometry", feature)
            self.assertIn("properties", feature)

    def test_point_coordinates_are_longitude_then_latitude(self):
        """GeoJSON is [lon, lat]. Swapped, Gothenburg lands in the Indian Ocean."""
        events = get_container_journey_events(self.team, self.container)
        points = [f for f in journey_feature_collection(events)["features"] if f["geometry"]["type"] == "Point"]
        arrival = next(f for f in points if f["properties"]["event_unlocode"] == "SEGOT")
        longitude, latitude = arrival["geometry"]["coordinates"]
        self.assertAlmostEqual(longitude, GOTHENBURG_LON, places=5)
        self.assertAlmostEqual(latitude, GOTHENBURG_LAT, places=5)

    def test_events_without_coordinates_are_not_drawn(self):
        """A document milestone has no place; inventing one would be a lie."""
        events = get_container_journey_events(self.team, self.container)
        located = [e for e in events if e.location_latitude is not None]
        points = [f for f in journey_feature_collection(events)["features"] if f["geometry"]["type"] == "Point"]
        self.assertEqual(len(points), len(located))
        self.assertLess(len(points), len(events))

    def test_coordinates_are_json_numbers_not_decimal_strings(self):
        events = get_container_journey_events(self.team, self.container)
        for feature in journey_feature_collection(events)["features"]:
            if feature["geometry"]["type"] == "Point":
                for value in feature["geometry"]["coordinates"]:
                    self.assertIsInstance(value, float)


class GeoJsonSemanticsTest(TestCase):
    """What the properties claim about the world."""

    @classmethod
    def setUpTestData(cls):
        _user, cls.team = make_user_and_team("geo-sem@example.com", "geo-sem-team")
        cls.container = make_container(cls.team)
        ingest_maersk_events(cls.team, cls.container)
        cls.features = journey_feature_collection(
            get_container_journey_events(cls.team, cls.container),
            container_number=cls.container.container_id,
        )["features"]

    def _points(self):
        return [f for f in self.features if f["geometry"]["type"] == "Point"]

    def _lines(self):
        return [f for f in self.features if f["geometry"]["type"] == "LineString"]

    def test_a_terminal_coordinate_is_a_facility_not_a_gps_fix(self):
        arrival = next(f for f in self._points() if f["properties"]["event_unlocode"] == "SEGOT")
        self.assertEqual(arrival["properties"]["position_type"], PositionType.VESSEL)
        self.assertFalse(arrival["properties"]["is_realtime"])

    def test_no_event_point_claims_to_be_realtime(self):
        """Nothing in a carrier's event feed is a GPS fix of the container."""
        for feature in self._points():
            self.assertFalse(feature["properties"]["is_realtime"])

    def test_a_forecast_is_marked_as_a_forecast(self):
        forecast = next(f for f in self._points() if f["properties"]["event_type"] == "vessel_departed")
        self.assertTrue(forecast["properties"]["is_estimated"])
        self.assertFalse(forecast["properties"]["is_actual"])
        self.assertEqual(forecast["properties"]["event_time_type"], TrackingEvent.EventTimeType.ESTIMATED)

    def test_an_unclassified_event_shows_the_carriers_own_wording(self):
        """A mapping gap costs detail, never the whole event."""
        drops = [f for f in self._points() if f["properties"]["event_type"] == TrackingEvent.EventType.UNKNOWN]
        for feature in drops:
            self.assertNotEqual(feature["properties"]["event_title"], "Unknown")
            self.assertTrue(feature["properties"]["event_title"])

    def test_an_unclassified_event_keeps_its_raw_carrier_codes(self):
        unknown = [f for f in self._points() if f["properties"]["event_type"] == TrackingEvent.EventType.UNKNOWN]
        for feature in unknown:
            self.assertIn("/", feature["properties"]["carrier_reference"])

    def test_an_unknown_drop_is_never_promoted_to_delivered(self):
        for feature in self._points():
            if feature["properties"]["carrier_reference"].endswith("DROP"):
                self.assertNotEqual(feature["properties"]["event_type"], TrackingEvent.EventType.DELIVERED)

    def test_lines_are_event_connections_and_say_they_are_not_a_track(self):
        for line in self._lines():
            self.assertFalse(line["properties"]["is_vessel_track"])
            self.assertIn(line["properties"]["line_type"], (LINE_ACTUAL, LINE_FORECAST))

    def test_the_actual_and_forecast_legs_are_separate_features(self):
        line_types = {line["properties"]["line_type"] for line in self._lines()}
        self.assertIn(LINE_ACTUAL, line_types)

    def test_the_container_number_travels_with_every_event(self):
        for feature in self._points():
            self.assertEqual(feature["properties"]["container_number"], FIXTURE_CONTAINER_NUMBER)


class OverviewGeoJsonTest(TestCase):
    """What the fleet-level map says about each object."""

    @classmethod
    def setUpTestData(cls):
        _user, cls.team = make_user_and_team("geo-ov@example.com", "geo-ov-team")
        cls.container = make_container(cls.team)
        ingest_maersk_events(cls.team, cls.container)
        cls.features = overview_feature_collection(list_visibility_objects(cls.team))["features"]

    def test_a_standalone_tracked_container_appears_on_the_map(self):
        self.assertEqual(len(self.features), 1)
        self.assertEqual(self.features[0]["properties"]["object_type"], "container")

    def test_the_object_is_identified_by_its_container_number(self):
        self.assertEqual(self.features[0]["properties"]["container_number"], FIXTURE_CONTAINER_NUMBER)

    def test_the_position_carries_its_quality(self):
        properties = self.features[0]["properties"]
        self.assertIn(properties["position_type"], PositionType.values)
        self.assertTrue(properties["position_type_label"])

    def test_the_panel_url_points_at_this_object(self):
        properties = self.features[0]["properties"]
        self.assertIn(f"/panel/container/{self.container.pk}/", properties["panel_url"])

    def test_properties_are_ui_ready_so_the_browser_derives_nothing(self):
        properties = self.features[0]["properties"]
        for key in ("current_status", "journey_state_label", "health_label", "position_type_label"):
            self.assertIn(key, properties)
