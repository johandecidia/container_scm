"""A tracked container with no shipment is a first-class object.

Standalone tracking is normal in this business — a box is bought, the carrier
publishes events against its number, and no shipment record exists yet or ever.
Everything visibility can say about a container on a shipment it must also say
about one on its own: status, ETA, ETA source, vessel, voyage, position, last
event and next check. A missing Shipment FK is not a reason to show blanks.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.tracking.models import TrackingEvent, TrackingSubscription
from apps.scm.tracking.positions import PositionType
from apps.scm.visibility.geojson import overview_feature_collection
from apps.scm.visibility.read_models import JourneyState, ObjectKind
from apps.scm.visibility.selectors import get_container_visibility, list_visibility_objects

from .factories import (
    TEST_STORAGES,
    ingest_maersk_events,
    make_container,
    make_user_and_team,
    payload_in_transit,
)


class StandaloneContainerTest(TestCase):
    """A container mid-voyage, tracked on its own, with everything still to come."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("solo@example.com", "solo-team")
        cls.container = make_container(cls.team)
        cls.subscription = ingest_maersk_events(cls.team, cls.container, payload=payload_in_transit())
        cls.subscription.last_synced_at = timezone.now() - timedelta(minutes=18)
        cls.subscription.next_sync_at = timezone.now() + timedelta(minutes=42)
        cls.subscription.save(update_fields=["last_synced_at", "next_sync_at"])

    def setUp(self):
        self.object = get_container_visibility(self.team, self.container)

    def test_it_has_no_shipment(self):
        """The premise: everything below is true without one."""
        self.assertIsNone(self.object.shipment)

    def test_it_has_a_current_status_from_the_carrier(self):
        self.assertEqual(self.object.current_status, "Gate In")

    def test_a_forecast_departure_does_not_make_it_in_transit(self):
        """The only departure in this snapshot is estimated, so the box has not sailed."""
        self.assertEqual(self.object.journey_state, JourneyState.NOT_DEPARTED)

    def test_it_has_an_eta(self):
        self.assertIsNotNone(self.object.current_eta)

    def test_the_eta_is_attributed_to_carrier_tracking(self):
        """With no shipment to carry a planned date, the source must say so."""
        self.assertEqual(self.object.eta_source, "tracking")

    def test_the_eta_keeps_the_carriers_hour_precision(self):
        self.assertIsNotNone(self.object.current_eta_at)

    def test_it_has_a_vessel_and_voyage(self):
        self.assertEqual(self.object.vessel_name, "JEBEL ALI")
        self.assertEqual(self.object.voyage_number, "623W")
        self.assertEqual(self.object.vessel_imo, "9525936")

    def test_it_has_a_carrier(self):
        self.assertEqual(self.object.carrier_name, "Maersk")

    def test_it_has_a_position(self):
        self.assertIsNotNone(self.object.position)
        self.assertTrue(self.object.position.has_coordinates)

    def test_it_has_a_latest_event(self):
        self.assertIsNotNone(self.object.latest_event)
        self.assertIsNotNone(self.object.last_event_at)

    def test_it_reports_when_the_carrier_was_last_asked(self):
        self.assertIsNotNone(self.object.last_synced_at)

    def test_it_reports_the_next_scheduled_check(self):
        self.assertIsNotNone(self.object.next_check_at)

    def test_it_reports_the_carrier_side_tracking_state(self):
        self.assertEqual(self.object.tracking_state, TrackingSubscription.TrackingStatus.TRACKING)

    def test_it_appears_in_the_overview_as_a_container_object(self):
        objects = list_visibility_objects(self.team)
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0].kind, ObjectKind.CONTAINER)

    def test_it_appears_on_the_overview_map(self):
        features = overview_feature_collection(list_visibility_objects(self.team))["features"]
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["properties"]["container_number"], self.container.container_id)


class StandaloneContainerWithoutCoordinatesTest(TestCase):
    """No coordinates is a state to handle, not a crash and not a fake point."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("nocoord@example.com", "nocoord-team")
        cls.container = make_container(cls.team)
        ingest_maersk_events(cls.team, cls.container)
        TrackingEvent.objects.filter(team=cls.team).update(location_latitude=None, location_longitude=None)

    def test_the_object_still_exists_with_a_status(self):
        obj = get_container_visibility(self.team, self.container)
        self.assertTrue(obj.current_status)

    def test_the_position_is_reported_without_coordinates(self):
        position = get_container_visibility(self.team, self.container).position
        self.assertIsNotNone(position)
        self.assertFalse(position.has_coordinates)
        self.assertEqual(position.position_type, PositionType.FACILITY)

    def test_it_is_absent_from_the_map_rather_than_placed_at_zero_zero(self):
        features = overview_feature_collection(list_visibility_objects(self.team))["features"]
        self.assertEqual(features, [])


@override_settings(STORAGES=TEST_STORAGES)
class StandaloneContainerPagesTest(TestCase):
    """The pages a standalone container reaches."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("solo-page@example.com", "solo-page-team")
        cls.container = make_container(cls.team)
        ingest_maersk_events(cls.team, cls.container)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_container_detail_page_renders_without_a_shipment(self):
        response = self.client.get(reverse("containers:detail", args=[self.container.pk]))
        self.assertEqual(response.status_code, 200)

    def test_the_container_detail_page_shows_a_journey_map_card(self):
        response = self.client.get(reverse("containers:detail", args=[self.container.pk]))
        self.assertContains(response, "Journey")

    def test_the_container_map_data_endpoint_returns_its_events(self):
        response = self.client.get(reverse("visibility:container_map_data", args=[self.container.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["features"])

    def test_a_container_with_no_events_returns_an_empty_collection_not_an_error(self):
        empty = make_container(self.team, number="MSKU0000006")
        response = self.client.get(reverse("visibility:container_map_data", args=[empty.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"type": "FeatureCollection", "features": []})
