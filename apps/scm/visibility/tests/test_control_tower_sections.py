"""The Control Tower's presentation: collapsible sections, status cards, theme-aware map.

What is worth asserting server-side. The sections are native <details> rendered
closed, and their badges are the lists' own lengths, so a badge can be checked
against the read model that built the list. The map's light/dark switch happens in
the browser, but which maps are *offered* a dark style is decided here — only the
Control Tower's — and that is what keeps every other map unchanged.
"""

from __future__ import annotations

import re
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.models import TrackingEvent
from apps.scm.visibility.selectors import get_visibility_overview, parse_visibility_filters
from apps.teams.models import Team
from apps.users.models import CustomUser

from .factories import TEST_STORAGES, make_container, make_provider, make_user_and_team, watch_container

SECTIONS = ("arrivals", "attention", "view")


@override_settings(STORAGES=TEST_STORAGES)
class ControlTowerSectionsTest(TestCase):
    """One shipment on customs hold arriving this week, one delayed, both watched."""

    user: CustomUser
    team: Team

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("sections@example.com", "sections-team")
        cls.held = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-HELD",
            carrier="MSC",
            status=Shipment.Status.IN_TRANSIT,
            eta=timezone.localdate() + timedelta(days=3),
            original_eta=timezone.localdate() + timedelta(days=3),
        )
        cls.delayed = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-LATE",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            eta=timezone.localdate() + timedelta(days=12),
            original_eta=timezone.localdate() + timedelta(days=4),
        )
        cls.container = make_container(cls.team)
        late_container = make_container(cls.team, "MSKU0000006")
        ShipmentContainer.objects.create(shipment=cls.held, container=cls.container)
        ShipmentContainer.objects.create(shipment=cls.delayed, container=late_container)
        TrackingEvent.objects.create(
            team=cls.team,
            provider=make_provider(),
            shipment=cls.held,
            container=cls.container,
            event_type=TrackingEvent.EventType.CUSTOMS_HOLD,
            event_datetime=timezone.now() - timedelta(hours=6),
            location_name="Gothenburg",
            description="Customs hold",
        )
        for shipment, container in ((cls.held, cls.container), (cls.delayed, late_container)):
            watch_container(cls.team, container, shipment=shipment)

    def setUp(self):
        self.client.force_login(self.user)

    def html(self, **params) -> str:
        return self.client.get(reverse("visibility:overview"), params).content.decode()

    def badge(self, html: str, section: str) -> int:
        match = re.search(
            rf'data-scm-section="{section}".*?badge-neutral ml-auto tabular-nums">(\d+)<', html, flags=re.DOTALL
        )
        if match is None:
            self.fail(f"no badge for {section}")
        return int(match.group(1))

    def expected(self, **params) -> dict[str, int]:
        overview = get_visibility_overview(self.team, parse_visibility_filters(params))
        return {
            "arrivals": len(overview.arriving_soon),
            "attention": len(overview.needs_attention),
            "view": len(overview.objects),
        }

    # -- accordions --------------------------------------------------------

    def test_the_three_sections_render_collapsed(self):
        tags = re.findall(r"<details[^>]*data-scm-section=[^>]*>", self.html())
        self.assertEqual(len(tags), 3)
        for tag in tags:
            with self.subTest(tag=tag):
                self.assertNotRegex(tag, r"\bopen\b")

    def test_each_badge_is_its_lists_own_length(self):
        html = self.html()
        expected = self.expected()
        self.assertEqual(expected, {"arrivals": 1, "attention": 2, "view": 2})
        for section in SECTIONS:
            with self.subTest(section=section):
                self.assertEqual(self.badge(html, section), expected[section])

    def test_the_badges_follow_the_boards_filters(self):
        for params in ({"carrier": "MSC"}, {"view": "exceptions"}, {"view": "delayed"}, {"search": "nothing"}):
            with self.subTest(params=params):
                html, expected = self.html(**params), self.expected(**params)
                self.assertEqual({section: self.badge(html, section) for section in SECTIONS}, expected)

    def test_the_selected_view_section_is_titled_after_the_view(self):
        self.assertRegex(self.html(view="delayed"), r'(?s)data-scm-section="view".*?<h2[^>]*>Delayed</h2>')

    def test_another_teams_board_counts_nothing_of_ours(self):
        other_user, _team = make_user_and_team("sections-other@example.com", "sections-other")
        self.client.force_login(other_user)
        html = self.html()
        for section in SECTIONS:
            with self.subTest(section=section):
                self.assertEqual(self.badge(html, section), 0)
        self.assertNotIn("SHP-HELD", html)

    def test_the_sections_content_and_links_are_still_there(self):
        html = self.html()
        self.assertIn("Customs hold at Gothenburg", html)
        self.assertIn(reverse("shipments:detail", args=[self.held.pk]), html)
        self.assertIn(f'href="{reverse("visibility:arrivals")}" class="text-xs link', html)
        self.assertIn(f'href="{reverse("visibility:exceptions")}" class="text-xs link', html)

    def test_an_htmx_swap_brings_the_sections_back_with_their_names(self):
        """The names are what map.js reopens the sections by after a swap."""
        params = {"view": "exceptions"}
        response = self.client.get(reverse("visibility:overview"), params, headers={"hx-request": "true"})
        html, expected = response.content.decode(), self.expected(**params)
        for section in SECTIONS:
            with self.subTest(section=section):
                self.assertIn(f'data-scm-section="{section}"', html)
                self.assertEqual(self.badge(html, section), expected[section])
        self.assertEqual(expected["view"], 1)

    # -- status cards ------------------------------------------------------

    def test_the_six_status_cards_share_the_forecast_tiles_surface(self):
        html = self.html()
        overview_url, arrivals_url = reverse("visibility:overview"), reverse("visibility:arrivals")
        hrefs = [
            f"{overview_url}?view=tracking",
            reverse("shipments:list"),
            arrivals_url,
            f"{arrivals_url}?window=30&amp;state=arrived",
            f"{overview_url}?view=delayed",
            f"{overview_url}?view=exceptions",
        ]
        for href in hrefs:
            with self.subTest(href=href):
                self.assertIn(f'<a href="{href}" class="stat scm-glass', html)
        # And the forecast tiles wear the same one.
        self.assertIn(f'<a href="{arrivals_url}?window=7" class="stat scm-glass', html)

    def test_arriving_card_and_next_7_days_tile_keep_their_own_counts(self):
        """Rows on one, containers on the other — shown side by side, never merged."""
        ShipmentContainer.objects.create(shipment=self.held, container=make_container(self.team, "MSKU0000011"))
        response = self.client.get(reverse("visibility:overview"))
        self.assertEqual(len(response.context["overview"].arriving_soon), 1)
        self.assertEqual(response.context["forecast"].next_7_days, 2)

    # -- map ---------------------------------------------------------------

    @override_settings(MAPBOX_PUBLIC_TOKEN="pk.test-token")
    def test_the_control_tower_map_is_offered_the_default_dark_style(self):
        self.assertIn('data-mapbox-dark-style="mapbox://styles/mapbox/dark-v11"', self.html())

    @override_settings(
        MAPBOX_PUBLIC_TOKEN="pk.test-token",
        MAPBOX_STYLE_URL="mapbox://styles/acme/light",
        MAPBOX_CONTROL_TOWER_DARK_STYLE_URL="mapbox://styles/acme/dark",
    )
    def test_the_control_tower_map_reads_both_configured_styles(self):
        html = self.html()
        self.assertIn('data-mapbox-style="mapbox://styles/acme/light"', html)
        self.assertIn('data-mapbox-dark-style="mapbox://styles/acme/dark"', html)

    @override_settings(MAPBOX_PUBLIC_TOKEN="pk.test-token")
    def test_other_maps_are_not_offered_a_dark_style(self):
        """Without one, map.js never watches the theme and keeps the single style."""
        html = self.client.get(reverse("containers:detail", args=[self.container.pk])).content.decode()
        self.assertIn("data-scm-map", html)
        self.assertNotIn("data-mapbox-dark-style", html)
