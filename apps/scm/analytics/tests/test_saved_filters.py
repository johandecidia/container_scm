"""Tests for SavedFilter model, selectors, and HTMX views."""

import json

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.analytics.models import SavedFilter
from apps.scm.analytics.selectors import get_saved_filter, get_saved_filters
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


class SavedFilterSelectorTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("filter@example.com", "filter-team")
        cls.other_user, cls.other_team = _make_user_and_team("other-filter@example.com", "other-filter-team")
        cls.f1 = SavedFilter.objects.create(
            team=cls.team,
            user=cls.user,
            name="My Filter",
            view_key=SavedFilter.ViewKey.SHIPMENTS,
            params={"status": "IN_TRANSIT"},
        )

    def test_get_saved_filters_by_view_key(self):
        qs = get_saved_filters(self.team, self.user, SavedFilter.ViewKey.SHIPMENTS)
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first(), self.f1)

    def test_get_saved_filters_wrong_view_key_returns_empty(self):
        qs = get_saved_filters(self.team, self.user, SavedFilter.ViewKey.CONTAINERS)
        self.assertEqual(qs.count(), 0)

    def test_team_isolation(self):
        SavedFilter.objects.create(
            team=self.other_team,
            user=self.other_user,
            name="Other Filter",
            view_key=SavedFilter.ViewKey.SHIPMENTS,
        )
        qs = get_saved_filters(self.team, self.user, SavedFilter.ViewKey.SHIPMENTS)
        self.assertEqual(qs.count(), 1)

    def test_user_isolation(self):
        SavedFilter.objects.create(
            team=self.team,
            user=self.other_user,
            name="Other User Filter",
            view_key=SavedFilter.ViewKey.SHIPMENTS,
        )
        qs = get_saved_filters(self.team, self.user, SavedFilter.ViewKey.SHIPMENTS)
        self.assertEqual(qs.count(), 1)

    def test_get_saved_filter_by_pk(self):
        result = get_saved_filter(self.team, self.user, self.f1.pk)
        self.assertEqual(result, self.f1)

    def test_get_saved_filter_wrong_team_returns_none(self):
        result = get_saved_filter(self.other_team, self.user, self.f1.pk)
        self.assertIsNone(result)


@override_settings(STORAGES=_TEST_STORAGES)
class SavedFilterCreateViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("create-filter@example.com", "create-filter-team")

    def _client(self):
        c = Client()
        c.login(username=self.user.username, password="pass")
        return c

    def test_requires_login(self):
        c = Client()
        response = c.post(
            reverse("analytics:saved_filter_create"),
            {
                "name": "Test",
                "view_key": "shipments",
                "params": "{}",
            },
        )
        self.assertIn(response.status_code, [302, 403])

    def test_requires_post(self):
        c = self._client()
        response = c.get(reverse("analytics:saved_filter_create"))
        self.assertEqual(response.status_code, 405)

    def test_creates_filter(self):
        c = self._client()
        c.post(
            reverse("analytics:saved_filter_create"),
            {
                "name": "My Shipments Filter",
                "view_key": "shipments",
                "params": json.dumps({"status": "IN_TRANSIT"}),
            },
        )
        self.assertEqual(SavedFilter.objects.filter(team=self.team, user=self.user).count(), 1)
        f = SavedFilter.objects.get(team=self.team, user=self.user)
        self.assertEqual(f.name, "My Shipments Filter")
        self.assertEqual(f.params, {"status": "IN_TRANSIT"})

    def test_invalid_view_key_does_not_create(self):
        c = self._client()
        c.post(
            reverse("analytics:saved_filter_create"),
            {
                "name": "Bad Filter",
                "view_key": "invalid_key",
                "params": "{}",
            },
        )
        self.assertEqual(SavedFilter.objects.filter(team=self.team, user=self.user).count(), 0)

    def test_returns_partial_html(self):
        c = self._client()
        response = c.post(
            reverse("analytics:saved_filter_create"),
            {
                "name": "Quick Filter",
                "view_key": "shipments",
                "params": "{}",
            },
        )
        self.assertEqual(response.status_code, 200)


@override_settings(STORAGES=_TEST_STORAGES)
class SavedFilterDeleteViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("del-filter@example.com", "del-filter-team")
        cls.other_user, cls.other_team = _make_user_and_team("del-other@example.com", "del-other-team")

    def setUp(self):
        self.saved_filter = SavedFilter.objects.create(
            team=self.team,
            user=self.user,
            name="To Delete",
            view_key=SavedFilter.ViewKey.CONTAINERS,
        )

    def _client(self, user=None):
        c = Client()
        c.login(username=(user or self.user).username, password="pass")
        return c

    def test_requires_post(self):
        c = self._client()
        response = c.get(reverse("analytics:saved_filter_delete", kwargs={"pk": self.saved_filter.pk}))
        self.assertEqual(response.status_code, 405)

    def test_deletes_own_filter(self):
        c = self._client()
        response = c.post(reverse("analytics:saved_filter_delete", kwargs={"pk": self.saved_filter.pk}))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(SavedFilter.objects.filter(pk=self.saved_filter.pk).exists())

    def test_cannot_delete_other_teams_filter(self):
        other_filter = SavedFilter.objects.create(
            team=self.other_team,
            user=self.other_user,
            name="Other",
            view_key=SavedFilter.ViewKey.CONTAINERS,
        )
        c = self._client()
        response = c.post(reverse("analytics:saved_filter_delete", kwargs={"pk": other_filter.pk}))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(SavedFilter.objects.filter(pk=other_filter.pk).exists())
