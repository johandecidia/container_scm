"""A sync that fails in an unexpected way stores a safe sentence, never the exception.

``SyncOutcome.error_message`` is written to ``TrackingSyncRun.error_message`` and to
``TrackingSubscription.last_error_message``, and both are rendered on team-facing pages:
the tracking detail page (status and sync history) and the container workspace. Typed
carrier errors already go through ``safe_message``; these are the paths that did not —
an unexpected exception, a parser crash, an unknown carrier and a stub parser.

The technical text is still recorded: in the log, with the traceback, and for a parser
error on the raw payload it describes, which is internal diagnostics and never rendered.
"""

from unittest import mock

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.integrations.carriers.exceptions import (
    CarrierConfigurationError,
    CarrierError,
    CarrierInvalidResponseError,
    CarrierNotImplementedError,
)
from apps.scm.integrations.carriers.registry import UnknownCarrierError
from apps.scm.tracking.models import TrackingRawPayload, TrackingSubscription, TrackingSyncRun
from apps.scm.tracking.sync import sync_tracking_subscription
from apps.scm.tracking.tests.test_sync_engine import FakeClient, FakeParser, _provider, _subscription, _sync
from apps.scm.tracking.tests.test_tracking_lifecycle import _container
from apps.teams.models import Team
from apps.users.models import CustomUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "sync-sanitise"}}
_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
SENSITIVE = "SECRET_PROVIDER_DETAIL_123 billing.example/internal 20 of 20"
FRAGMENTS = ("SECRET_PROVIDER_DETAIL_123", "billing.example/internal", "20 of 20")


@override_settings(CACHES=_LOCMEM, STORAGES=_STORAGES)
class UnexpectedSyncFailureSanitisationTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="sync-sanitise", slug="sync-sanitise")
        self.container = _container(self.team)
        self.subscription = _subscription(
            self.team, _provider(), container=self.container, tracking_reference=self.container.container_id
        )

    def _assert_safe(self, run: TrackingSyncRun, expected: str):
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.last_error_message, expected)
        self.assertEqual(run.error_message, expected)
        for fragment in FRAGMENTS:
            self.assertNotIn(fragment, self.subscription.last_error_message)
            self.assertNotIn(fragment, run.error_message)

    def test_an_unexpected_exception_stores_the_generic_sentence_and_logs_the_traceback(self):
        with self.assertLogs("apps.scm.tracking.sync", level="ERROR") as logs:
            run = _sync(self.subscription, client=FakeClient(error=RuntimeError(SENSITIVE)))

        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.UNEXPECTED)
        self._assert_safe(run, CarrierError.safe_message_template)
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.FAILED)
        logged = "\n".join(logs.output)
        self.assertIn("SECRET_PROVIDER_DETAIL_123", logged)
        self.assertIn("Traceback", logged)

    def test_a_parser_crash_stores_the_sanitised_sentence_and_keeps_detail_on_the_raw_payload(self):
        with self.assertLogs("apps.scm.tracking.sync", level="WARNING") as logs:
            run = _sync(self.subscription, parser=FakeParser(error=ValueError(SENSITIVE)))

        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.PARSE_ERROR)
        self._assert_safe(run, CarrierInvalidResponseError.safe_message_template)
        self.assertIn("SECRET_PROVIDER_DETAIL_123", TrackingRawPayload.objects.get(team=self.team).error_message)
        self.assertIn("SECRET_PROVIDER_DETAIL_123", "\n".join(logs.output))

    def test_a_stub_parser_stores_its_safe_message(self):
        with self.assertLogs("apps.scm.tracking.sync", level="WARNING") as logs:
            run = _sync(self.subscription, parser=FakeParser(error=CarrierNotImplementedError(SENSITIVE)))

        self._assert_safe(run, CarrierNotImplementedError.safe_message_template)
        self.assertIn("SECRET_PROVIDER_DETAIL_123", "\n".join(logs.output))

    def test_an_unknown_carrier_stores_the_not_configured_sentence(self):
        with (
            mock.patch(
                "apps.scm.integrations.carriers.factory.build_carrier_client",
                side_effect=UnknownCarrierError(SENSITIVE),
            ),
            self.assertLogs("apps.scm.tracking.sync", level="WARNING") as logs,
        ):
            run = sync_tracking_subscription(self.subscription)

        self._assert_safe(run, CarrierConfigurationError.safe_message_template)
        self.assertIn("SECRET_PROVIDER_DETAIL_123", "\n".join(logs.output))

    def test_team_facing_pages_show_the_safe_sentence_and_no_exception_text(self):
        """Tracking detail (status + sync history) and the container workspace, per role."""
        with self.assertLogs("apps.scm.tracking.sync", level="ERROR"):
            _sync(self.subscription, client=FakeClient(error=RuntimeError(SENSITIVE)))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.FAILED)

        for role in ("member", "admin"):
            with self.subTest(role=role):
                user = CustomUser.objects.create_user(username=f"{role}@sync-sanitise.test", password="pass")
                self.team.members.add(user, through_defaults={"role": role})
                client = Client()
                client.force_login(user)

                pages = [
                    client.get(reverse("tracking:detail", kwargs={"pk": self.subscription.pk})),
                    client.get(reverse("containers:detail", kwargs={"container_id": self.container.pk})),
                ]
                for response in pages:
                    self.assertEqual(response.status_code, 200)
                    # The safe sentence is shown where the failure is: status line and
                    # sync history on the one, the sync problem line on the other.
                    self.assertContains(response, CarrierError.safe_message_template)
                    for fragment in FRAGMENTS:
                        self.assertNotContains(response, fragment)
