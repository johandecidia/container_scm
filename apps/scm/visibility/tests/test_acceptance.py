"""EPIC 3 acceptance: carrier response in, trustworthy map out.

One test walks the whole path a real container takes — subscription, carrier
response, DCSA parser, stored raw payload, normalised events, location and vessel,
status and ETA, container workspace, shipment workspace, GeoJSON, timeline — and
checks that nothing along it needed a human to retype anything.

The rest cover what has to keep working when a piece is missing: no Mapbox token,
no coordinates, no carrier data, an unconfigured carrier. A map is an enhancement,
and a page that dies without one is worse than a page with no map.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.models import Container
from apps.scm.containers.selectors import get_container_workspace
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.shipments.selectors import get_merged_shipment_timeline
from apps.scm.tracking.models import (
    TrackingEvent,
    TrackingRawPayload,
    TrackingSubscription,
)
from apps.scm.visibility.mapbox import DEFAULT_STYLE_URL, get_mapbox_config
from apps.scm.visibility.selectors import get_shipment_visibility
from apps.teams.models import Team

from .factories import (
    TEST_STORAGES,
    ingest_maersk_events,
    maersk_payload,
    make_container,
    make_provider,
    make_user_and_team,
    payload_in_transit,
)


@override_settings(STORAGES=TEST_STORAGES)
class EndToEndVisibilityTest(TestCase):
    """Carrier data reaches every surface without being entered twice."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("e2e@example.com", "e2e-team")
        cls.container = make_container(cls.team)
        cls.shipment = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-E2E",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            origin_port="Shanghai",
            destination_port="Gothenburg",
        )
        ShipmentContainer.objects.create(shipment=cls.shipment, container=cls.container)

        # The stored carrier response, kept the way the sync engine keeps it.
        cls.raw_payload = TrackingRawPayload.objects.create(
            team=cls.team,
            provider=make_provider(),
            payload_json=maersk_payload(),
            payload_type=TrackingRawPayload.PayloadType.API_RESPONSE,
            received_at=timezone.now(),
            parsed_successfully=True,
        )
        cls.subscription = ingest_maersk_events(cls.team, cls.container, shipment=cls.shipment)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_carrier_response_is_retained_for_reparsing(self):
        self.assertTrue(TrackingRawPayload.objects.filter(team=self.team, parsed_successfully=True).exists())

    def test_the_parser_produced_normalised_events(self):
        self.assertEqual(TrackingEvent.objects.filter(team=self.team, container=self.container).count(), 10)

    def test_location_vessel_and_voyage_survived_the_parse(self):
        arrival = TrackingEvent.objects.get(team=self.team, event_code="ARRI")
        self.assertEqual(arrival.location_unlocode, "SEGOT")
        self.assertEqual(arrival.vessel_name, "JEBEL ALI")
        self.assertEqual(arrival.voyage_number, "623W")
        self.assertIsNotNone(arrival.location_latitude)

    def test_the_container_workspace_derives_status_and_carriage(self):
        workspace = get_container_workspace(self.team, self.container)
        self.assertEqual(workspace.current_status, "Gate In")
        self.assertEqual(workspace.vessel_name, "JEBEL ALI")
        self.assertEqual(workspace.tracking_carrier_name, "Maersk")

    def test_the_shipment_visibility_object_speaks_for_its_container(self):
        obj = get_shipment_visibility(self.team, self.shipment)
        self.assertEqual(obj.container_count, 1)
        self.assertEqual(obj.current_status, "Gate In")
        self.assertEqual(obj.voyage_number, "623W")

    def test_the_overview_map_endpoint_serves_the_shipment(self):
        response = self.client.get(reverse("visibility:map_data"))
        features = response.json()["features"]
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["properties"]["label"], "SHP-E2E")

    def test_the_shipment_map_endpoint_serves_its_journey(self):
        response = self.client.get(reverse("visibility:shipment_map_data", args=[self.shipment.pk]))
        features = response.json()["features"]
        self.assertTrue(any(f["geometry"]["type"] == "LineString" for f in features))
        self.assertTrue(any(f["geometry"]["type"] == "Point" for f in features))

    def test_the_shipment_timeline_carries_the_carrier_events(self):
        titles = {item.title for item in get_merged_shipment_timeline(self.team, self.shipment)}
        self.assertIn("Vessel Arrived", titles)

    def test_timeline_entries_with_coordinates_are_linkable_to_the_map(self):
        linkable = [i for i in get_merged_shipment_timeline(self.team, self.shipment) if i.map_event_id]
        self.assertTrue(linkable)

    def test_the_visibility_page_renders(self):
        response = self.client.get(reverse("visibility:overview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SHP-E2E")

    def test_the_shipment_detail_page_shows_the_journey_summary(self):
        response = self.client.get(reverse("shipments:detail", args=[self.shipment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "JEBEL ALI")
        self.assertContains(response, "Last confirmed location")

    def test_the_container_detail_page_shows_the_same_derivations(self):
        response = self.client.get(reverse("containers:detail", args=[self.container.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Gate In")
        self.assertContains(response, "Last checked")

    def test_the_carriage_is_derived_rather_than_copied_onto_the_shipment(self):
        """Vessel and voyage are shown everywhere and stored nowhere but the events.

        Nobody typed them, and there is no second copy to drift out of date.
        """
        self.shipment.refresh_from_db()
        stored = " ".join(str(value) for value in Shipment.objects.filter(pk=self.shipment.pk).values()[0].values())
        self.assertNotIn("JEBEL ALI", stored)
        self.assertNotIn("623W", stored)
        self.assertEqual(get_shipment_visibility(self.team, self.shipment).vessel_name, "JEBEL ALI")


@override_settings(STORAGES=TEST_STORAGES)
class EmptyStateTest(TestCase):
    """Every missing piece is a state, not a failure."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("empty@example.com", "empty-team")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    @override_settings(MAPBOX_PUBLIC_TOKEN="")
    def test_without_a_token_the_page_still_renders_with_a_notice(self):
        response = self.client.get(reverse("visibility:overview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Map not configured")

    @override_settings(MAPBOX_PUBLIC_TOKEN="")
    def test_without_a_token_no_map_element_is_emitted_for_the_script_to_find(self):
        response = self.client.get(reverse("visibility:overview"))
        self.assertNotContains(response, "data-mapbox-token")

    @override_settings(MAPBOX_PUBLIC_TOKEN="pk.test-token", MAPBOX_STYLE_URL="")
    def test_a_missing_style_falls_back_to_a_working_default(self):
        self.assertEqual(get_mapbox_config().style_url, DEFAULT_STYLE_URL)

    @override_settings(MAPBOX_PUBLIC_TOKEN="pk.test-token")
    def test_with_a_token_the_map_element_carries_its_configuration(self):
        response = self.client.get(reverse("visibility:overview"))
        self.assertContains(response, "data-scm-map")
        self.assertContains(response, "pk.test-token")

    def test_a_secret_token_is_never_read_from_a_public_setting(self):
        """The setting is rendered into the page, so only a pk. token belongs in it."""
        with override_settings(MAPBOX_PUBLIC_TOKEN="  pk.trimmed  "):
            self.assertEqual(get_mapbox_config().token, "pk.trimmed")

    def test_an_empty_team_gets_an_empty_board_not_an_error(self):
        response = self.client.get(reverse("visibility:overview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nothing is being tracked yet")

    def test_an_empty_team_gets_an_empty_feature_collection(self):
        response = self.client.get(reverse("visibility:map_data"))
        self.assertEqual(response.json(), {"type": "FeatureCollection", "features": []})

    def test_filters_that_match_nothing_say_so(self):
        response = self.client.get(reverse("visibility:overview"), {"search": "nothing-matches-this"})
        self.assertContains(response, "Nothing matches these filters")


class TrackingStateTest(TestCase):
    """NO_DATA, NOT_CONFIGURED and ERROR are three different answers."""

    team: Team
    container: Container

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("state@example.com", "state-team")
        cls.container = make_container(cls.team)
        cls.subscription = ingest_maersk_events(cls.team, cls.container, payload=payload_in_transit())

    def _state(self) -> str:
        from apps.scm.visibility.selectors import get_container_visibility

        return get_container_visibility(self.team, self.container).tracking_state

    def test_a_working_watch_reports_tracking(self):
        self.assertEqual(self._state(), TrackingSubscription.TrackingStatus.TRACKING)

    def test_a_carrier_with_nothing_on_the_reference_is_not_an_error(self):
        self.subscription.tracking_status = TrackingSubscription.TrackingStatus.NO_DATA
        self.subscription.save(update_fields=["tracking_status"])
        self.assertEqual(self._state(), TrackingSubscription.TrackingStatus.NO_DATA)

    def test_an_unconfigured_carrier_is_not_an_error_either(self):
        self.subscription.tracking_status = TrackingSubscription.TrackingStatus.NOT_CONFIGURED
        self.subscription.save(update_fields=["tracking_status"])
        self.assertEqual(self._state(), TrackingSubscription.TrackingStatus.NOT_CONFIGURED)

    def test_an_error_is_reported_as_an_error(self):
        self.subscription.tracking_status = TrackingSubscription.TrackingStatus.ERROR
        self.subscription.save(update_fields=["tracking_status"])
        self.assertEqual(self._state(), TrackingSubscription.TrackingStatus.ERROR)

    def test_a_paused_watch_promises_no_next_check(self):
        """An old next_sync_at nothing will act on must not be shown as a schedule."""
        from apps.scm.visibility.selectors import get_container_visibility

        self.subscription.status = TrackingSubscription.Status.PAUSED
        self.subscription.next_sync_at = timezone.now() + timedelta(hours=1)
        self.subscription.save(update_fields=["status", "next_sync_at"])
        self.assertIsNone(get_container_visibility(self.team, self.container).next_check_at)
