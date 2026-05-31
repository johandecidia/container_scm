"""Tests for tracking views and team isolation."""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.tracking.models import TrackingProvider, TrackingSubscription
from apps.scm.tracking.services import create_tracking_subscription
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _provider(code="VIEW_PROV"):
    return TrackingProvider.objects.create(
        code=code,
        name=f"Provider {code}",
        provider_type=TrackingProvider.ProviderType.MANUAL,
    )


def _make_user_and_team(username, team_slug):
    team = Team.objects.create(name=team_slug, slug=team_slug)
    user = CustomUser.objects.create_user(username=username, password="pass")
    team.members.add(user, through_defaults={"role": ROLE_MEMBER})
    return user, team


@override_settings(STORAGES=_TEST_STORAGES)
class TrackingListPermissionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("track-list@example.com", "track-list-team")

    def test_list_requires_login(self):
        client = Client()
        response = client.get(reverse("tracking:list"))
        self.assertIn(response.status_code, [302, 403])

    def test_logged_in_user_can_see_list(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("tracking:list"))
        self.assertEqual(response.status_code, 200)

    def test_user_without_team_gets_404(self):
        teamless = CustomUser.objects.create_user(username="teamless-track@example.com", password="pass")
        client = Client()
        client.force_login(teamless)
        response = client.get(reverse("tracking:list"))
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class TrackingTeamIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("iso-track@example.com", "iso-track-team")
        cls.other_user, cls.other_team = _make_user_and_team("other-iso-track@example.com", "other-iso-track-team")
        cls.provider = _provider("ISO_PROV")
        cls.own_sub = create_tracking_subscription(cls.team, cls.provider, "OWN-TRACK-REF")
        cls.other_sub = create_tracking_subscription(cls.other_team, cls.provider, "OTHER-TRACK-REF")

    def test_list_shows_own_subscriptions(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("tracking:list"))
        self.assertEqual(response.status_code, 200)
        subscriptions = list(response.context["subscriptions"])
        self.assertIn(self.own_sub, subscriptions)
        self.assertNotIn(self.other_sub, subscriptions)

    def test_detail_other_team_gives_404(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("tracking:detail", kwargs={"pk": self.other_sub.pk})
        response = client.get(url)
        self.assertIn(response.status_code, [404, 403])

    def test_pause_other_team_subscription_gives_404(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("tracking:pause", kwargs={"pk": self.other_sub.pk})
        response = client.post(url)
        self.assertIn(response.status_code, [404, 403])
        self.other_sub.refresh_from_db()
        self.assertNotEqual(self.other_sub.status, TrackingSubscription.Status.PAUSED)

    def test_cancel_other_team_subscription_gives_404(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("tracking:cancel", kwargs={"pk": self.other_sub.pk})
        response = client.post(url)
        self.assertIn(response.status_code, [404, 403])
        self.other_sub.refresh_from_db()
        self.assertNotEqual(self.other_sub.status, TrackingSubscription.Status.CANCELLED)


@override_settings(STORAGES=_TEST_STORAGES)
class TrackingDetailTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("detail-track@example.com", "detail-track-team")
        cls.provider = _provider("DETAIL_PROV")
        cls.sub = create_tracking_subscription(cls.team, cls.provider, "DETAIL-TRACK-REF")

    def test_detail_page_loads(self):
        client = Client()
        client.force_login(self.user)
        url = reverse("tracking:detail", kwargs={"pk": self.sub.pk})
        response = client.get(url)
        self.assertEqual(response.status_code, 200)


@override_settings(STORAGES=_TEST_STORAGES)
class TrackingPauseResumeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("pr-track@example.com", "pr-track-team")
        cls.provider = _provider("PR_PROV")

    def setUp(self):
        self.sub = create_tracking_subscription(self.team, self.provider, f"PR-TRACK-{self.__class__.__name__}")

    def test_pause_changes_status(self):
        client = Client()
        client.force_login(self.user)
        client.post(reverse("tracking:pause", kwargs={"pk": self.sub.pk}))
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, TrackingSubscription.Status.PAUSED)

    def test_resume_changes_status(self):
        client = Client()
        client.force_login(self.user)
        client.post(reverse("tracking:pause", kwargs={"pk": self.sub.pk}))
        client.post(reverse("tracking:resume", kwargs={"pk": self.sub.pk}))
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, TrackingSubscription.Status.ACTIVE)
