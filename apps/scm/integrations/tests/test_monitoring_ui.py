"""Tests for the Business Central integration monitoring UI."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from apps.scm.integrations.models import Integration, IntegrationCredential, IntegrationSyncRun
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser


def _member(team, email):
    user = CustomUser.objects.create_user(username=email, email=email, password="pw")
    Membership.objects.create(team=team, user=user, role="admin")
    return user


def _bc_integration(team, name="BC"):
    return Integration.objects.create(
        team=team,
        name=name,
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        is_active=True,
        config={"environment": "Production", "company_id": "company-guid", "sync_enabled": True},
    )


class MonitoringUITest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="mon", slug="mon")
        cls.user = _member(cls.team, "mon@example.com")
        cls.integration = _bc_integration(cls.team)

    def _login(self, user=None, team=None):
        user = user or self.user
        team = team or self.team
        self.client.force_login(user)
        session = self.client.session
        session["team"] = team.pk
        session.save()

    def test_list_view(self):
        self._login()
        resp = self.client.get(reverse("integrations:list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "BC")

    def test_detail_shows_health_no_credentials(self):
        IntegrationCredential.objects.create(
            team=self.team,
            integration=self.integration,
            auth_type=IntegrationCredential.AuthType.OAUTH2,
            encrypted_data="fernet:v1:secretblob",
        )
        self._login()
        resp = self.client.get(reverse("integrations:detail", args=[self.integration.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Production")
        self.assertContains(resp, "company-guid")
        # Never leak the stored credential blob.
        self.assertNotContains(resp, "secretblob")

    def test_login_required(self):
        resp = self.client.get(reverse("integrations:detail", args=[self.integration.pk]))
        self.assertIn(resp.status_code, (302, 404))

    def test_other_team_cannot_access(self):
        other = Team.objects.create(name="mon-other", slug="mon-other")
        other_user = _member(other, "o@example.com")
        self._login(user=other_user, team=other)
        # Manipulated id pointing at another team's integration → 404.
        resp = self.client.get(reverse("integrations:detail", args=[self.integration.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_sync_now_requires_post(self):
        self._login()
        resp = self.client.get(reverse("integrations:sync_now", args=[self.integration.pk]))
        self.assertEqual(resp.status_code, 405)

    def test_sync_now_queues_task(self):
        self._login()
        with mock.patch("apps.scm.integrations.views.sync_business_central_purchase_orders_task.delay") as delay:
            resp = self.client.post(reverse("integrations:sync_now", args=[self.integration.pk]))
        self.assertEqual(resp.status_code, 302)
        delay.assert_called_once_with(self.integration.id, "manual")

    def test_sync_now_blocked_when_running(self):
        IntegrationSyncRun.objects.create(
            team=self.team,
            integration=self.integration,
            resource_type=IntegrationSyncRun.ResourceType.PURCHASE_ORDERS,
            status=IntegrationSyncRun.Status.RUNNING,
        )
        self._login()
        with mock.patch("apps.scm.integrations.views.sync_business_central_purchase_orders_task.delay") as delay:
            self.client.post(reverse("integrations:sync_now", args=[self.integration.pk]))
        delay.assert_not_called()

    def test_test_connection_queues_task(self):
        self._login()
        with mock.patch("apps.scm.integrations.views.test_business_central_connection_task.delay") as delay:
            resp = self.client.post(reverse("integrations:test_connection", args=[self.integration.pk]))
        self.assertEqual(resp.status_code, 302)
        delay.assert_called_once_with(self.integration.id)

    def test_other_team_cannot_trigger_sync(self):
        other = Team.objects.create(name="mon-x", slug="mon-x")
        other_user = _member(other, "x@example.com")
        self._login(user=other_user, team=other)
        with mock.patch("apps.scm.integrations.views.sync_business_central_purchase_orders_task.delay") as delay:
            resp = self.client.post(reverse("integrations:sync_now", args=[self.integration.pk]))
        self.assertEqual(resp.status_code, 404)
        delay.assert_not_called()
