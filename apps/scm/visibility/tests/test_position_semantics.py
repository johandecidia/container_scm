"""Position quality and forecasts, all the way out to the map.

The tracking layer already refuses to upgrade a terminal coordinate into a GPS fix
or to let a forecast become an observation. These tests check that the visibility
layer carries those refusals through instead of quietly flattening them — which is
the easiest way for a map to start lying, because a dot looks equally confident
wherever the number came from.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from django.test import TestCase

from apps.scm.tracking.models import TrackingEvent
from apps.scm.tracking.positions import PositionType, classify_position, get_latest_container_position
from apps.scm.visibility.geojson import overview_feature_collection
from apps.scm.visibility.read_models import JourneyState
from apps.scm.visibility.selectors import get_container_visibility, list_visibility_objects

from .factories import make_container, make_provider, make_user_and_team

# Gothenburg, Oceanterminalen — the terminal's own coordinates, as DCSA supplies
# them inside the event's location object.
SEGOT_LAT = Decimal("57.696629")
SEGOT_LON = Decimal("11.858448")


class PositionSemanticsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        _user, cls.team = make_user_and_team("pos@example.com", "pos-team")
        cls.container = make_container(cls.team)
        cls.provider = make_provider()
        cls.subscription = _subscribe(cls.team, cls.container, cls.provider)

    def _event(self, **kwargs) -> TrackingEvent:
        defaults = {
            "team": self.team,
            "provider": self.provider,
            "container": self.container,
            "subscription": self.subscription,
            "event_time_type": TrackingEvent.EventTimeType.ACTUAL,
            "event_datetime": datetime(2026, 8, 9, 14, 32, tzinfo=UTC),
            "event_fingerprint": f"pos-{TrackingEvent.objects.count()}",
        }
        return TrackingEvent.objects.create(**{**defaults, **kwargs})

    def test_terminal_coordinates_stay_a_facility(self):
        """SEGOT's coordinates say the box passed through, not that it is there now."""
        event = self._event(
            event_type=TrackingEvent.EventType.GATE_IN,
            location_name="Gothenburg, Oceanterminalen",
            location_unlocode="SEGOT",
            location_latitude=SEGOT_LAT,
            location_longitude=SEGOT_LON,
        )
        self.assertEqual(classify_position(event), PositionType.FACILITY)
        position = get_latest_container_position(self.team, self.container)
        self.assertEqual(position.position_type, PositionType.FACILITY)
        self.assertFalse(position.is_realtime)

    def test_a_facility_reaches_the_map_as_a_facility(self):
        self._event(
            event_type=TrackingEvent.EventType.GATE_IN,
            location_name="Gothenburg, Oceanterminalen",
            location_unlocode="SEGOT",
            location_latitude=SEGOT_LAT,
            location_longitude=SEGOT_LON,
        )
        feature = overview_feature_collection(list_visibility_objects(self.team))["features"][0]
        self.assertEqual(feature["properties"]["position_type"], PositionType.FACILITY)
        self.assertFalse(feature["properties"]["is_realtime"])
        self.assertEqual(feature["properties"]["position_label"], "Gothenburg, Oceanterminalen")

    def test_a_vessel_position_is_not_the_containers_gps(self):
        """Where the ship is, not where the box is once it has been discharged."""
        event = self._event(
            event_type=TrackingEvent.EventType.VESSEL_DEPARTED,
            transport_mode=TrackingEvent.TransportMode.VESSEL,
            vessel_name="JEBEL ALI",
            vessel_imo="9525936",
            location_latitude=Decimal("30.882240"),
            location_longitude=Decimal("121.874470"),
        )
        self.assertEqual(classify_position(event), PositionType.VESSEL)
        feature = overview_feature_collection(list_visibility_objects(self.team))["features"][0]
        self.assertEqual(feature["properties"]["position_type"], PositionType.VESSEL)
        self.assertFalse(feature["properties"]["is_realtime"])

    def test_an_estimated_event_stays_estimated_however_precise_it_looks(self):
        event = self._event(
            event_type=TrackingEvent.EventType.VESSEL_ARRIVED,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
            location_unlocode="SEGOT",
            location_latitude=SEGOT_LAT,
            location_longitude=SEGOT_LON,
        )
        self.assertEqual(classify_position(event), PositionType.ESTIMATED)
        feature = overview_feature_collection(list_visibility_objects(self.team))["features"][0]
        self.assertEqual(feature["properties"]["position_type"], PositionType.ESTIMATED)
        self.assertFalse(feature["properties"]["is_realtime"])

    def test_a_forecast_arrival_does_not_become_the_current_status(self):
        """The core rule: expected is not happened."""
        self._event(
            event_type=TrackingEvent.EventType.LOADED_ON_VESSEL,
            event_datetime=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
            location_unlocode="CNSHA",
        )
        self._event(
            event_type=TrackingEvent.EventType.VESSEL_ARRIVED,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
            event_datetime=datetime(2026, 8, 21, 6, 0, tzinfo=UTC),
            location_unlocode="SEGOT",
        )
        obj = get_container_visibility(self.team, self.container)
        self.assertEqual(obj.current_status, "Loaded on Vessel")
        self.assertNotEqual(obj.current_status, "Vessel Arrived")

    def test_a_forecast_arrival_does_not_bucket_the_object_as_arrived(self):
        self._event(
            event_type=TrackingEvent.EventType.VESSEL_DEPARTED,
            event_datetime=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
            location_unlocode="CNSHA",
        )
        self._event(
            event_type=TrackingEvent.EventType.VESSEL_ARRIVED,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
            event_datetime=datetime(2026, 8, 21, 6, 0, tzinfo=UTC),
            location_unlocode="SEGOT",
        )
        self.assertEqual(get_container_visibility(self.team, self.container).journey_state, JourneyState.IN_TRANSIT)

    def test_a_forecast_arrival_is_offered_as_an_eta_instead(self):
        self._event(
            event_type=TrackingEvent.EventType.VESSEL_ARRIVED,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
            event_datetime=datetime(2026, 8, 21, 6, 0, tzinfo=UTC),
            location_unlocode="SEGOT",
        )
        obj = get_container_visibility(self.team, self.container)
        self.assertEqual(obj.eta_source, "tracking")
        self.assertEqual(obj.current_eta.isoformat(), "2026-08-21")

    def test_an_unclassified_event_does_not_erase_the_last_known_status(self):
        """A code we cannot map costs detail, not the whole answer."""
        self._event(
            event_type=TrackingEvent.EventType.LOADED_ON_VESSEL,
            event_datetime=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
            location_unlocode="CNSHA",
        )
        self._event(
            event_type=TrackingEvent.EventType.UNKNOWN,
            carrier_event_type="EQUIPMENT",
            event_code="DROP",
            event_datetime=datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
            location_unlocode="SEGOT",
        )
        self.assertEqual(get_container_visibility(self.team, self.container).current_status, "Loaded on Vessel")

    def test_a_placeless_document_milestone_does_not_erase_the_last_known_place(self):
        """A bill of lading is released nowhere; the box is still at the terminal."""
        self._event(
            event_type=TrackingEvent.EventType.GATE_IN,
            event_datetime=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
            location_name="Gothenburg, Oceanterminalen",
            location_unlocode="SEGOT",
            location_latitude=SEGOT_LAT,
            location_longitude=SEGOT_LON,
        )
        self._event(
            event_type=TrackingEvent.EventType.UNKNOWN,
            carrier_event_type="SHIPMENT",
            event_code="RELS",
            event_datetime=datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
        )
        position = get_container_visibility(self.team, self.container).position
        self.assertEqual(position.location_unlocode, "SEGOT")
        self.assertEqual(position.position_type, PositionType.FACILITY)

    def test_coordinates_with_no_place_attached_are_a_gps_fix(self):
        """The only case that is a real fix of the container itself."""
        event = self._event(
            event_type=TrackingEvent.EventType.GATE_OUT,
            location_latitude=Decimal("57.700000"),
            location_longitude=Decimal("11.900000"),
        )
        self.assertEqual(classify_position(event), PositionType.GPS)
        feature = overview_feature_collection(list_visibility_objects(self.team))["features"][0]
        self.assertTrue(feature["properties"]["is_realtime"])


def _subscribe(team, container, provider):
    from apps.scm.tracking.models import TrackingSubscription

    return TrackingSubscription.objects.create(
        team=team,
        provider=provider,
        container=container,
        tracking_reference=container.container_id,
        status=TrackingSubscription.Status.ACTIVE,
        tracking_status=TrackingSubscription.TrackingStatus.TRACKING,
    )
