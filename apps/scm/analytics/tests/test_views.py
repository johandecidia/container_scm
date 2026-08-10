"""Tests for analytics dashboard view."""

import datetime

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.analytics.models import AnalyticsSnapshot
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _make_user_and_team(username: str, slug: str):
    team = Team.objects.create(name=slug, slug=slug)
    user = CustomUser.objects.create_user(username=username, password="pass")
    team.members.add(user, through_defaults={"role": ROLE_MEMBER})
    return user, team


@override_settings(STORAGES=_TEST_STORAGES)
class AnalyticsDashboardPermissionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("analytics@example.com", "analytics-team")

    def test_requires_login(self):
        client = Client()
        response = client.get(reverse("analytics:dashboard"))
        self.assertIn(response.status_code, [302, 403])

    def test_logged_in_user_gets_200(self):
        client = Client()
        client.login(username="analytics@example.com", password="pass")
        response = client.get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 200)


@override_settings(STORAGES=_TEST_STORAGES)
class AnalyticsDashboardContentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("content@example.com", "content-team")
        cls.other_user, cls.other_team = _make_user_and_team("other@example.com", "other-content-team")

    def _client(self, user):
        c = Client()
        c.login(username=user.username, password="pass")
        return c

    def test_shows_snapshot_kpis(self):
        AnalyticsSnapshot.objects.create(
            team=self.team,
            date=datetime.date(2026, 1, 10),
            total_shipments=42,
        )
        response = self._client(self.user).get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "42")

    def test_does_not_show_other_team_data(self):
        AnalyticsSnapshot.objects.create(
            team=self.other_team,
            date=datetime.date(2026, 1, 10),
            total_shipments=99,
        )
        response = self._client(self.user).get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "99")

    def test_renders_empty_state_when_no_snapshot(self):
        response = self._client(self.user).get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 200)
        # Template renders an empty-state message, not a crash
        self.assertNotContains(response, "Error")


@override_settings(STORAGES=_TEST_STORAGES)
class AnalyticsDashboardContextTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("ctx@example.com", "ctx-analytics-team")

    def _client(self):
        c = Client()
        c.login(username="ctx@example.com", password="pass")
        return c

    def test_live_stats_in_context(self):
        response = self._client().get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("live_stats", response.context)

    def test_snapshot_in_context(self):
        response = self._client().get(reverse("analytics:dashboard"))
        self.assertIn("snapshot", response.context)

    def test_transit_stats_in_context(self):
        response = self._client().get(reverse("analytics:dashboard"))
        self.assertIn("transit_stats", response.context)

    def test_carrier_data_in_context(self):
        response = self._client().get(reverse("analytics:dashboard"))
        self.assertIn("carrier_data", response.context)

    def test_container_stats_in_context(self):
        response = self._client().get(reverse("analytics:dashboard"))
        self.assertIn("container_stats", response.context)

    def test_supplier_data_in_context(self):
        response = self._client().get(reverse("analytics:dashboard"))
        self.assertIn("supplier_data", response.context)

    def test_date_filter_does_not_crash(self):
        response = self._client().get(
            reverse("analytics:dashboard"),
            {"date_from": "2026-01-01", "date_to": "2026-06-01"},
        )
        self.assertEqual(response.status_code, 200)


@override_settings(STORAGES=_TEST_STORAGES)
class AnalyticsSearchTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("search@example.com", "search-analytics-team")

    def _client(self):
        c = Client()
        c.login(username="search@example.com", password="pass")
        return c

    def test_search_endpoint_returns_200(self):
        response = self._client().get(reverse("analytics:search"), {"q": "test"})
        self.assertEqual(response.status_code, 200)

    def test_search_requires_login(self):
        client = Client()
        response = client.get(reverse("analytics:search"), {"q": "test"})
        self.assertIn(response.status_code, [302, 403])

    def test_search_with_empty_query_returns_200(self):
        response = self._client().get(reverse("analytics:search"), {"q": ""})
        self.assertEqual(response.status_code, 200)

    def test_search_htmx_returns_partial(self):
        response = self._client().get(reverse("analytics:search"), {"q": "test"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)


@override_settings(STORAGES=_TEST_STORAGES)
class AnalyticsDashboardZeroStateTest(TestCase):
    """Analytics dashboard shows zero counts (not blank/crash) when no data exists."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("zero@example.com", "zero-analytics-team")

    def _client(self):
        c = Client()
        c.login(username="zero@example.com", password="pass")
        return c

    def test_live_stats_zero_when_no_shipments(self):
        response = self._client().get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 200)
        live_stats = response.context["live_stats"]
        self.assertEqual(live_stats["active_shipments"], 0)
        self.assertEqual(live_stats["delayed_shipments"], 0)
        self.assertEqual(live_stats["open_purchase_orders"], 0)

    def test_container_stats_zero_when_no_containers(self):
        response = self._client().get(reverse("analytics:dashboard"))
        container_stats = response.context["container_stats"]
        self.assertEqual(container_stats["total"], 0)
        self.assertEqual(container_stats["in_transit"], 0)

    def test_transit_stats_empty_when_no_delivered_shipments(self):
        response = self._client().get(reverse("analytics:dashboard"))
        transit_stats = response.context["transit_stats"]
        self.assertEqual(transit_stats["count"], 0)
        self.assertIsNone(transit_stats["avg_days"])

    def test_analytics_user_without_team_gets_404(self):
        teamless = CustomUser.objects.create_user(username="zero-noteam@example.com", password="pass")
        client = Client()
        client.force_login(teamless)
        response = client.get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 404)
