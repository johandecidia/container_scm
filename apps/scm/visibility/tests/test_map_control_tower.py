"""The Control Tower map: what it offers, and what it survives.

The primary operational map. Two things are asserted here that a screenshot cannot:
that the map's own controls and the board's filters stay separate concerns, and that
every part of the page keeps working when Mapbox does not. A Control Tower that 500s
because a token is unset is worse than a Control Tower with no map.
"""

from __future__ import annotations

from django.test import override_settings
from django.urls import reverse

from apps.scm.visibility.map_positions import PositionClass

from .factories import make_location, place_container_at
from .map_surface_scenarios import MapSurfaceTestCase


class ControlTowerMapTest(MapSurfaceTestCase):
    """The primary operational map."""

    def test_the_page_renders_with_the_map_and_its_legend(self):
        response = self.client.get(reverse("visibility:overview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-scm-map")
        for label in ("Physical", "Tracking", "Destination"):
            self.assertContains(response, label)

    def test_the_map_offers_a_position_type_filter(self):
        response = self.client.get(reverse("visibility:overview"))
        self.assertContains(response, 'name="position"')
        self.assertContains(response, f'value="{PositionClass.PHYSICAL}"')
        self.assertContains(response, f'value="{PositionClass.TRACKING}"')

    def test_the_destination_overlay_is_off_by_default(self):
        response = self.client.get(reverse("visibility:overview"))
        self.assertContains(response, 'name="destinations"')
        self.assertNotContains(
            response,
            'name="destinations" value="1" data-scm-map-filter class="checkbox checkbox-xs checkbox-warning" checked',
        )

    def test_arriving_with_the_overlay_asked_for_shows_it_switched_on(self):
        """The Arrivals queue links here with ?destinations=1.

        A toggle that arrived unchecked while the map drew destinations would
        contradict itself.
        """
        response = self.client.get(reverse("visibility:overview"), {"destinations": "1"})
        self.assertContains(response, "checked")
        self.assertTrue(response.context["operational_map"].filters.show_destinations)

    def test_the_board_map_url_carries_the_board_filters(self):
        response = self.client.get(reverse("visibility:overview"), {"exceptions": "1"})
        self.assertContains(response, f'data-scm-map-source="{reverse("visibility:map_data")}?exceptions=1"')

    def test_the_board_map_url_leaves_the_maps_own_parameters_to_the_map(self):
        """Sending them twice would grow a shared link's query string on every click."""
        response = self.client.get(reverse("visibility:overview"), {"destinations": "1", "position": "physical"})
        self.assertNotContains(response, 'data-scm-map-source="/scm/visibility/map-data/?destinations')
        self.assertContains(response, f'data-scm-map-source="{reverse("visibility:map_data")}"')

    def test_the_swapped_board_still_carries_no_map_element(self):
        """The WebGL context must survive a filter change. See the page comment."""
        response = self.client.get(reverse("visibility:overview"), HTTP_HX_REQUEST="true")
        self.assertNotContains(response, "data-scm-map=")
        self.assertContains(response, "data-scm-map-source=")

    def test_the_coverage_line_reports_what_cannot_be_drawn(self):
        response = self.client.get(reverse("visibility:overview"))
        self.assertContains(response, "with no plottable position")

    def test_the_coverage_line_names_a_location_needing_coordinates(self):
        depot = make_location(self.team, "John Evans Depot")
        place_container_at(self.team, self.container, depot)

        response = self.client.get(reverse("visibility:overview"))

        self.assertContains(response, "John Evans Depot")
        self.assertContains(response, "has no coordinates yet")

    def test_a_marker_leads_to_its_location_panel(self):
        place_container_at(self.team, self.container, self.terminal)
        response = self.client.get(reverse("visibility:map_data"))
        panel_url = response.json()["features"][0]["properties"]["panel_url"]

        panel = self.client.get(panel_url)

        self.assertEqual(panel.status_code, 200)
        self.assertContains(panel, "Oceanterminalen")
        self.assertContains(panel, reverse("containers:detail", args=[self.container.pk]))

    def test_a_panel_for_a_place_nothing_matches_any_more_says_so(self):
        """A marker was real when the map drew it. A filter can empty it."""
        response = self.client.get(
            reverse("visibility:map_location_panel", args=[PositionClass.PHYSICAL, self.terminal.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "any more")


class ControlTowerWithoutMapboxTest(MapSurfaceTestCase):
    """No token is a handled state, not an outage."""

    @override_settings(MAPBOX_PUBLIC_TOKEN="")
    def test_the_control_tower_still_renders(self):
        response = self.client.get(reverse("visibility:overview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Map not configured")

    @override_settings(MAPBOX_PUBLIC_TOKEN="")
    def test_the_operational_queues_are_still_there(self):
        response = self.client.get(reverse("visibility:overview"))
        self.assertContains(response, "Needs attention")
        self.assertContains(response, "Upcoming arrivals")

    @override_settings(MAPBOX_PUBLIC_TOKEN="")
    def test_no_map_element_is_emitted_for_the_script_to_find(self):
        response = self.client.get(reverse("visibility:overview"))
        self.assertNotContains(response, "data-mapbox-token")

    @override_settings(MAPBOX_PUBLIC_TOKEN="")
    def test_the_map_data_endpoint_still_answers(self):
        """The token is a browser concern. The data does not depend on it."""
        place_container_at(self.team, self.container, self.terminal)
        response = self.client.get(reverse("visibility:map_data"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["features"])

    @override_settings(MAPBOX_PUBLIC_TOKEN="")
    def test_the_arrivals_and_container_pages_still_render(self):
        for url in (
            reverse("visibility:arrivals"),
            reverse("containers:detail", args=[self.container.pk]),
            reverse("containers:location_detail", args=[self.terminal.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
