"""Team isolation across every visibility endpoint.

Three of these endpoints take an object id straight out of the URL, which is the
classic way a multi-tenant map leaks: the lookup filters on the id and forgets the
team, and changing a number in the address bar returns somebody else's fleet.
A failure in this module is a release blocker, not a bug.
"""

from __future__ import annotations

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.visibility.geojson import overview_feature_collection
from apps.scm.visibility.selectors import (
    get_container_journey_events,
    get_shipment_journey_events,
    list_visibility_objects,
)

from .factories import TEST_STORAGES, ingest_maersk_events, make_container, make_user_and_team


class VisibilityTeamFixture(TestCase):
    """Two teams, each with a tracked container and a shipment carrying it."""

    @classmethod
    def setUpTestData(cls):
        cls.user_a, cls.team_a = make_user_and_team("iso-a@example.com", "iso-team-a")
        cls.user_b, cls.team_b = make_user_and_team("iso-b@example.com", "iso-team-b")

        cls.container_a = make_container(cls.team_a)
        cls.container_b = make_container(cls.team_b)
        cls.shipment_a = Shipment.objects.create(
            team=cls.team_a, shipment_number="SHP-A", carrier="Maersk", status=Shipment.Status.IN_TRANSIT
        )
        cls.shipment_b = Shipment.objects.create(
            team=cls.team_b, shipment_number="SHP-B", carrier="Maersk", status=Shipment.Status.IN_TRANSIT
        )
        ShipmentContainer.objects.create(shipment=cls.shipment_a, container=cls.container_a)
        ShipmentContainer.objects.create(shipment=cls.shipment_b, container=cls.container_b)
        ingest_maersk_events(cls.team_a, cls.container_a, shipment=cls.shipment_a)
        ingest_maersk_events(cls.team_b, cls.container_b, shipment=cls.shipment_b)

    def client_for(self, user) -> Client:
        client = Client()
        client.force_login(user)
        return client


class SelectorIsolationTest(VisibilityTeamFixture):
    """The selectors themselves, without a view in the way."""

    def test_an_overview_lists_only_its_own_teams_objects(self):
        labels = {obj.label for obj in list_visibility_objects(self.team_a)}
        self.assertIn("SHP-A", labels)
        self.assertNotIn("SHP-B", labels)

    def test_shipment_journey_events_are_scoped_to_the_team(self):
        """Another team's shipment id returns nothing, not that shipment's events."""
        self.assertEqual(get_shipment_journey_events(self.team_a, self.shipment_b), [])

    def test_container_journey_events_are_scoped_to_the_team(self):
        self.assertEqual(get_container_journey_events(self.team_a, self.container_b), [])

    def test_overview_geojson_carries_only_one_teams_features(self):
        features = overview_feature_collection(list_visibility_objects(self.team_a))["features"]
        self.assertTrue(features)
        for feature in features:
            self.assertEqual(feature["properties"]["object_id"], self.shipment_a.pk)


@override_settings(STORAGES=TEST_STORAGES)
class EndpointIsolationTest(VisibilityTeamFixture):
    """The HTTP surface, where an id in the URL is the attack."""

    def test_overview_requires_login(self):
        response = Client().get(reverse("visibility:overview"))
        self.assertIn(response.status_code, (302, 403))

    def test_map_data_requires_login(self):
        response = Client().get(reverse("visibility:map_data"))
        self.assertIn(response.status_code, (302, 403))

    def test_overview_renders_for_a_member(self):
        response = self.client_for(self.user_a).get(reverse("visibility:overview"))
        self.assertEqual(response.status_code, 200)

    def test_overview_map_data_returns_only_the_callers_team(self):
        response = self.client_for(self.user_a).get(reverse("visibility:map_data"))
        self.assertEqual(response.status_code, 200)
        for feature in response.json()["features"]:
            self.assertEqual(feature["properties"]["object_id"], self.shipment_a.pk)

    def test_shipment_map_data_of_another_team_is_not_found(self):
        response = self.client_for(self.user_a).get(reverse("visibility:shipment_map_data", args=[self.shipment_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_container_map_data_of_another_team_is_not_found(self):
        response = self.client_for(self.user_a).get(
            reverse("visibility:container_map_data", args=[self.container_b.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_object_panel_of_another_teams_shipment_is_not_found(self):
        response = self.client_for(self.user_a).get(
            reverse("visibility:object_panel", args=["shipment", self.shipment_b.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_object_panel_of_another_teams_container_is_not_found(self):
        response = self.client_for(self.user_a).get(
            reverse("visibility:object_panel", args=["container", self.container_b.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_an_unknown_object_kind_is_not_found(self):
        response = self.client_for(self.user_a).get(
            reverse("visibility:object_panel", args=["vessel", self.shipment_a.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_own_shipment_map_data_is_served(self):
        response = self.client_for(self.user_a).get(reverse("visibility:shipment_map_data", args=[self.shipment_a.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["features"])

    def test_own_container_map_data_is_served(self):
        response = self.client_for(self.user_a).get(
            reverse("visibility:container_map_data", args=[self.container_a.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["features"])

    def test_a_user_without_a_team_gets_404(self):
        from apps.users.models import CustomUser

        teamless = CustomUser.objects.create_user(username="teamless-vis@example.com", password="pass")
        client = Client()
        client.force_login(teamless)
        self.assertEqual(client.get(reverse("visibility:overview")).status_code, 404)
