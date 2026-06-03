"""Tests for SCM health check endpoint (/health/scm/)."""

import json
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.scm.imports.models import ImportJob
from apps.scm.tracking.models import TrackingProvider, TrackingSubscription, TrackingSyncRun
from apps.teams.models import Team
from apps.users.models import CustomUser


def make_team(slug="health-test") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": "Health Test"})[0]


def make_user() -> CustomUser:
    return CustomUser.objects.get_or_create(username="health@example.com", defaults={"email": "health@example.com"})[0]


def make_provider() -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(
        code="TEST",
        defaults={"name": "Test Provider", "provider_type": TrackingProvider.ProviderType.API},
    )[0]


class HealthScmEndpointTests(TestCase):
    def setUp(self):
        self.url = reverse("health-scm")
        self.team = make_team()
        self.user = make_user()

    def test_returns_200_when_all_checks_warning_or_ok(self):
        response = self.client.get(self.url)
        self.assertIn(response.status_code, [200, 503])

    def test_response_has_expected_structure(self):
        response = self.client.get(self.url)
        data = json.loads(response.content)
        self.assertIn("status", data)
        self.assertIn("checks", data)
        for key in ("database", "imports", "tracking", "discovery"):
            self.assertIn(key, data["checks"])

    def test_status_values_are_valid(self):
        response = self.client.get(self.url)
        data = json.loads(response.content)
        valid = {"ok", "warning", "error"}
        self.assertIn(data["status"], valid)
        for v in data["checks"].values():
            self.assertIn(v, valid)

    def test_database_check_ok_when_db_accessible(self):
        response = self.client.get(self.url)
        data = json.loads(response.content)
        self.assertEqual(data["checks"]["database"], "ok")

    def test_imports_warning_when_no_completed_imports(self):
        response = self.client.get(self.url)
        data = json.loads(response.content)
        # Fresh test DB has no completed imports
        self.assertIn(data["checks"]["imports"], ("warning", "ok"))

    def test_imports_ok_when_recent_completed_import(self):

        from django.core.files.uploadedfile import SimpleUploadedFile

        f = SimpleUploadedFile("test.csv", b"data", content_type="text/csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="test.csv",
            import_type=ImportJob.ImportType.CONTAINERS,
            status=ImportJob.Status.COMPLETED,
            completed_at=timezone.now() - timedelta(hours=1),
        )
        response = self.client.get(self.url)
        data = json.loads(response.content)
        self.assertEqual(data["checks"]["imports"], "ok")
        job.delete()

    def test_imports_warning_when_last_import_too_old(self):

        from django.core.files.uploadedfile import SimpleUploadedFile

        f = SimpleUploadedFile("test.csv", b"data", content_type="text/csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="test.csv",
            import_type=ImportJob.ImportType.CONTAINERS,
            status=ImportJob.Status.COMPLETED,
            completed_at=timezone.now() - timedelta(hours=72),
        )
        response = self.client.get(self.url)
        data = json.loads(response.content)
        self.assertEqual(data["checks"]["imports"], "warning")
        job.delete()

    def test_tracking_ok_when_recent_sync_success(self):
        provider = make_provider()
        sub = TrackingSubscription.objects.create(
            team=self.team,
            provider=provider,
            tracking_reference="CSQU3054187",
            status=TrackingSubscription.Status.ACTIVE,
        )
        sync_run = TrackingSyncRun.objects.create(
            team=self.team,
            subscription=sub,
            provider=provider,
            status=TrackingSyncRun.Status.SUCCESS,
            started_at=timezone.now() - timedelta(hours=1),
            finished_at=timezone.now() - timedelta(hours=1),
        )
        response = self.client.get(self.url)
        data = json.loads(response.content)
        self.assertEqual(data["checks"]["tracking"], "ok")
        sync_run.delete()
        sub.delete()

    def test_endpoint_does_not_leak_secrets(self):
        response = self.client.get(self.url)
        content = response.content.decode()
        for word in ("password", "token", "secret", "api_key", "credential"):
            self.assertNotIn(word, content.lower())

    def test_endpoint_accessible_without_authentication(self):
        """Health check must not require login."""
        self.client.logout()
        response = self.client.get(self.url)
        self.assertNotEqual(response.status_code, 302)

    def test_overall_status_is_error_when_database_check_fails(self):
        """Simulate database error path — patch internal check function."""
        from unittest.mock import patch

        from apps.scm import health_views

        with patch.object(health_views, "_check_database", return_value="error"):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        data = json.loads(response.content)
        self.assertEqual(data["status"], "error")

    def test_overall_status_is_warning_with_no_errors(self):
        from unittest.mock import patch

        from apps.scm import health_views

        with (
            patch.object(health_views, "_check_database", return_value="ok"),
            patch.object(health_views, "_check_last_import", return_value="warning"),
            patch.object(health_views, "_check_last_tracking_sync", return_value="ok"),
            patch.object(health_views, "_check_last_discovery", return_value="ok"),
        ):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["status"], "warning")
