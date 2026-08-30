"""What the primary navigation offers, and what it deliberately no longer does.

UX-1 moved Container SCM's navigation from "which Django app do you want to open"
to "what is my work". Two halves of that are worth locking down:

* the operational entries are present, and the technical capabilities — tracking,
  discovery, AI chat, supplier deliveries — are not;
* every route that left the menu still answers. The menu changed; nothing was
  deleted. A test is the only thing that keeps "hidden" from quietly becoming
  "gone" in a later cleanup.
"""

from __future__ import annotations

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.visibility.tests.factories import TEST_STORAGES, make_user_and_team


@override_settings(STORAGES=TEST_STORAGES)
class NavigationFixture(TestCase):
    """One team member, and the shell as they see it."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("nav@example.com", "nav-team")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def nav_html(self) -> str:
        """The rendered application shell, taken from a page that uses it."""
        response = self.client.get(reverse("visibility:overview"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class PrimaryNavigationTest(NavigationFixture):
    def test_the_operational_groups_are_offered(self):
        html = self.nav_html()
        for label in ("Control Tower", "Exceptions", "Arrivals", "Containers", "Shipments"):
            with self.subTest(label=label):
                self.assertIn(label, html)

    def test_purchase_orders_locations_and_analytics_are_offered(self):
        html = self.nav_html()
        self.assertIn(reverse("procurement:purchase_order_list"), html)
        self.assertIn(reverse("containers:location_list"), html)
        self.assertIn(reverse("analytics:dashboard"), html)

    def test_control_tower_is_the_visibility_overview(self):
        """One page. The label changed; the implementation did not move."""
        self.assertIn(reverse("visibility:overview"), self.nav_html())

    def test_exceptions_and_arrivals_are_control_tower_filters(self):
        """No new work-queue pages in UX-1 — the existing filter parameters do it."""
        html = self.nav_html()
        overview = reverse("visibility:overview")
        self.assertIn(f"{overview}?exceptions=1", html)
        self.assertIn(f"{overview}?eta=7", html)

    def test_tracking_is_no_longer_in_the_navigation(self):
        self.assertNotIn(reverse("tracking:list"), self.nav_html())

    def test_discovery_is_no_longer_in_the_navigation(self):
        self.assertNotIn(reverse("containers:discovery_dashboard"), self.nav_html())

    def test_ai_chat_is_no_longer_in_the_navigation(self):
        self.assertNotIn(reverse("chat:chat_home"), self.nav_html())

    def test_supplier_deliveries_is_no_longer_a_primary_entry(self):
        self.assertNotIn(reverse("supplier_deliveries:list"), self.nav_html())

    def test_imports_and_integrations_are_still_reachable_from_the_shell(self):
        """Moved to Settings, not removed: they are how data gets in."""
        html = self.nav_html()
        self.assertIn(reverse("imports:list"), html)
        self.assertIn(reverse("integrations:list"), html)

    def test_the_navigation_renders_on_mobile_too(self):
        """One include feeds both the desktop sidebar and the mobile drawer.

        Counted on a group label rather than a URL: the Control Tower's own KPI
        cards link to the same filtered URLs the menu does, so a URL count would
        measure the page as well as the shell.
        """
        html = self.nav_html()
        for group in ("Control", "Supply Chain", "Network", "Insights", "Settings"):
            with self.subTest(group=group):
                self.assertEqual(html.count(f"<span>{group}</span>"), 2)


class HomeRoutingTest(NavigationFixture):
    def test_the_authenticated_team_home_leads_to_the_control_tower(self):
        response = self.client.get(reverse("web_team:home", args=[self.team.slug]))
        self.assertRedirects(response, reverse("visibility:overview"))

    def test_the_site_root_leads_to_the_control_tower_through_the_team(self):
        response = self.client.get(reverse("web:home"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[-1][0], reverse("visibility:overview"))

    def test_the_visibility_url_still_works_directly(self):
        """Bookmarks and links from before UX-1 must not break."""
        self.assertEqual(self.client.get(reverse("visibility:overview")).status_code, 200)


class RoutesKeptAfterLeavingTheMenuTest(NavigationFixture):
    """Move and hide before delete. Every one of these still answers."""

    def test_routes_removed_from_the_menu_still_resolve(self):
        for name in (
            "tracking:list",
            "containers:discovery_dashboard",
            "imports:list",
            "integrations:list",
            "supplier_deliveries:list",
            "supplier_deliveries:dashboard",
            "chat:chat_home",
        ):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200, f"{name} stopped working")
