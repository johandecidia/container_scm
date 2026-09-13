"""Geospatial context on the workspaces, and the sentences the map cannot say.

The Container Workspace has one job LOC-4 gives it that no marker can do: when
there is nothing to plot and a destination is known, the page has to say so in
words. A single destination marker with no explanation reads as the container being
there, which is the error the whole of LOC-4 is arranged to avoid.

The Location Workspace has the mirror job — a place with no coordinates is a normal
state of the master data, and saying so beside a link to the form is more use than
an empty map.
"""

from __future__ import annotations

from django.test import Client, TestCase
from django.urls import reverse

from apps.scm.containers.models import ContainerLocation
from apps.scm.visibility.map_positions import PositionClass

from .factories import make_location, make_user_and_team, place_container_at, set_reported_coordinates
from .map_surface_scenarios import ROTTERDAM, MapSurfaceTestCase


class ContainerWorkspaceMapTest(MapSurfaceTestCase):
    """One box: where it is, where it is going, and no route between them."""

    def _map_data(self):
        return self.client.get(reverse("visibility:container_map_data", args=[self.container.pk])).json()

    def _positions(self):
        return [
            f["properties"]
            for f in self._map_data()["features"]
            if f["properties"].get("object_type") == "map_position"
        ]

    def test_the_page_says_there_is_no_plottable_position_when_there_is_not(self):
        """The sentence that stops one destination marker being read as an arrival."""
        response = self.client.get(reverse("containers:detail", args=[self.container.pk]))
        self.assertContains(response, "No current plottable position")
        self.assertContains(response, "Oceanterminalen")

    def test_the_destination_is_labelled_as_where_it_is_going(self):
        response = self.client.get(reverse("containers:detail", args=[self.container.pk]))
        self.assertContains(response, "Where it is going")

    def test_an_accepted_position_and_a_destination_are_both_drawn(self):
        place_container_at(self.team, self.container, self.rotterdam)

        classes = {position["position_class"] for position in self._positions()}

        self.assertEqual(classes, {PositionClass.PHYSICAL, PositionClass.DESTINATION})

    def test_no_line_is_drawn_between_a_container_and_its_destination(self):
        """A straight line between two places is not a shipping route."""
        place_container_at(self.team, self.container, self.rotterdam)

        for feature in self._map_data()["features"]:
            if feature["geometry"]["type"] == "LineString":
                self.assertFalse(feature["properties"]["is_vessel_track"])
                self.assertIn("event_connection", feature["properties"]["line_type"] + "_event_connection")

    def _flagged_events(self):
        """Journey points the server marked as the container's current one.

        Filtered to events on purpose: a canonical position marker also carries
        is_current — it *is* the current position — so counting both would never
        detect the duplicate this is about.
        """
        return [
            f
            for f in self._map_data()["features"]
            if f["properties"].get("object_type") == "event" and f["properties"].get("is_current")
        ]

    def test_a_canonical_marker_replaces_the_journeys_own_current_halo(self):
        """Two rings claiming "now" at two coordinates is a contradiction on screen."""
        place_container_at(self.team, self.container, self.rotterdam)

        current = [p for p in self._positions() if p["position_class"] == PositionClass.PHYSICAL]

        self.assertTrue(current)
        self.assertEqual(self._flagged_events(), [])

    def test_the_journey_keeps_its_current_halo_when_no_marker_can_be_drawn(self):
        """A current position the map cannot draw leaves the journey's claim standing.

        A carrier gate-in resolved to a depot nobody has recorded a latitude for, so
        there is an accepted position — which outranks everything — and no marker for
        it. The same carrier's events do carry coordinates, so the journey is on the
        map, and its own current point keeps the halo because nothing replaced it.

        The position's source is what makes the journey's current location the
        carrier's point rather than our own: a location derived from a tracking event
        is not an independent observation, so ``build_physical_observation`` declines
        to make one of it.

        The destination marker is present and does not count: it says where the box
        is going, so it takes nothing away from the journey's claim about now.
        """
        from apps.scm.containers.choices import LocationSource

        depot = make_location(self.team, "John Evans Depot")
        set_reported_coordinates(self.team, self.container, ROTTERDAM[0], ROTTERDAM[1])
        place_container_at(self.team, self.container, depot, source=LocationSource.TRACKING_EVENT)

        classes = {p["position_class"] for p in self._positions()}

        self.assertEqual(classes, {PositionClass.DESTINATION})
        self.assertTrue(self._flagged_events())

    def test_a_known_place_without_coordinates_is_stated_rather_than_drawn(self):
        depot = make_location(self.team, "John Evans Depot")
        place_container_at(self.team, self.container, depot)

        response = self.client.get(reverse("containers:detail", args=[self.container.pk]))

        self.assertContains(response, "John Evans Depot")
        self.assertContains(response, "has no coordinates yet")

    def test_there_is_still_only_one_map_on_the_page(self):
        response = self.client.get(reverse("containers:detail", args=[self.container.pk]))
        # A bare attribute, not `data-scm-map=` — see map_card.html.
        self.assertEqual(response.content.decode().count("data-scm-map\n"), 1)


class LocationWorkspaceMapTest(MapSurfaceTestCase):
    """The place itself, and an honest account of a missing coordinate."""

    def test_a_location_with_coordinates_gets_a_map(self):
        response = self.client.get(reverse("containers:location_detail", args=[self.terminal.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-scm-map")
        self.assertContains(response, "Where this is")

    def test_the_marker_carries_the_inventory_count(self):
        place_container_at(self.team, self.container, self.terminal)

        response = self.client.get(reverse("visibility:location_map_data", args=[self.terminal.pk]))

        properties = response.json()["features"][0]["properties"]
        self.assertEqual(properties["container_count"], 1)
        self.assertEqual(properties["place_statement"], "At Oceanterminalen")

    def test_the_marker_count_matches_the_inventory_tab(self):
        """One definition of how full a depot is, not two."""
        place_container_at(self.team, self.container, self.terminal)

        page = self.client.get(reverse("containers:location_detail", args=[self.terminal.pk]))
        marker = self.client.get(reverse("visibility:location_map_data", args=[self.terminal.pk])).json()

        self.assertEqual(
            marker["features"][0]["properties"]["container_count"],
            page.context["workspace"].container_count,
        )

    def test_a_location_without_coordinates_says_so_and_offers_the_fix(self):
        depot = make_location(self.team, "John Evans Depot")

        response = self.client.get(reverse("containers:location_detail", args=[depot.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No coordinates recorded")
        self.assertContains(response, reverse("containers:location_update", args=[depot.pk]))

    def test_a_location_without_coordinates_draws_nothing_rather_than_zero_zero(self):
        depot = make_location(self.team, "John Evans Depot")

        response = self.client.get(reverse("visibility:location_map_data", args=[depot.pk]))

        self.assertEqual(response.json(), {"type": "FeatureCollection", "features": []})

    def test_another_teams_location_map_is_not_found(self):
        _other_user, other_team = make_user_and_team("surface-b@example.com", "surface-team-b")
        theirs = make_location(other_team, "Theirs", latitude="1.0", longitude="1.0")

        response = self.client.get(reverse("visibility:location_map_data", args=[theirs.pk]))

        self.assertEqual(response.status_code, 404)

    def test_the_location_map_requires_login(self):
        response = Client().get(reverse("visibility:location_map_data", args=[self.terminal.pk]))
        self.assertIn(response.status_code, (302, 403))


class ArrivalsMapAffordanceTest(MapSurfaceTestCase):
    """One map, reached from the queue — not a second implementation."""

    def test_the_queue_links_to_the_map_with_the_destination_overlay_on(self):
        response = self.client.get(reverse("visibility:arrivals"))
        self.assertContains(response, f"{reverse('visibility:overview')}?destinations=1")

    def test_the_queue_does_not_embed_a_second_map(self):
        response = self.client.get(reverse("visibility:arrivals"))
        self.assertNotContains(response, "data-scm-map=")


class CoordinateValidationTest(TestCase):
    """Coordinates that are not on the planet are rejected; absent ones are not."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("coords@example.com", "coords-team")

    def _form(self, **overrides):
        from apps.scm.containers.forms import ContainerLocationForm

        data = {"name": "Somewhere", "location_type": "terminal", "is_active": "on", **overrides}
        return ContainerLocationForm(data, team=self.team)

    def test_no_coordinates_at_all_is_valid(self):
        """LOC-1 deliberately did not invent any. Requiring them would force a guess."""
        self.assertTrue(self._form().is_valid(), self._form().errors)

    def test_real_coordinates_are_accepted(self):
        form = self._form(latitude="57.696629", longitude="11.858448")
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_latitude_off_the_planet_is_rejected_against_its_own_field(self):
        form = self._form(latitude="200", longitude="11.858448")
        self.assertFalse(form.is_valid())
        self.assertIn("latitude", form.errors)

    def test_a_longitude_off_the_planet_is_rejected_against_its_own_field(self):
        form = self._form(latitude="57.696629", longitude="-200")
        self.assertFalse(form.is_valid())
        self.assertIn("longitude", form.errors)

    def test_the_poles_and_the_antimeridian_are_valid(self):
        for latitude, longitude in (("90", "180"), ("-90", "-180"), ("0", "0")):
            with self.subTest(latitude=latitude, longitude=longitude):
                form = self._form(latitude=latitude, longitude=longitude)
                self.assertTrue(form.is_valid(), form.errors)

    def test_the_rule_holds_for_writers_that_never_see_a_form(self):
        """Declared on the model, so an importer or a shell session is covered too."""
        from django.core.exceptions import ValidationError

        location = ContainerLocation(team=self.team, name="Nowhere", latitude=95, longitude=0)
        with self.assertRaises(ValidationError) as raised:
            location.full_clean()
        self.assertIn("latitude", raised.exception.error_dict)
