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
