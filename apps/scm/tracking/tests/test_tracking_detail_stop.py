"""Stop tracking from the tracking detail page, through the tracking lifecycle.

The page used to offer "Cancel", which marked the watch CANCELLED locally and asked no
provider anything — leaving a Vizion reference subscribed or a Traqo shipment tracked
behind a row claiming it was over. It now runs the same stop the container list and the
workspace run, scoped to the one watch on the page:

    provider released   → CANCELLED
    provider refused    → PAUSED, with the sanitised message
    nothing to release  → CANCELLED, and no provider is called
"""

from unittest import mock

from django.contrib.messages import get_messages
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.audit_log.models import SCMAuditLog
from apps.scm.integrations.carriers.exceptions import CarrierServerError
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.integrations.vizion import PROVIDER_CODE as VIZION_PROVIDER_CODE
from apps.scm.integrations.vizion import PROVIDER_NAME as VIZION_PROVIDER_NAME
from apps.scm.tracking import activation as activation_module
from apps.scm.tracking.lifecycle import is_container_tracked, start_container_tracking
from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
from apps.scm.tracking.models import CarrierSource, TrackingSubscription
from apps.scm.tracking.services import create_tracking_subscription
from apps.scm.tracking.tests.test_tracking_lifecycle import FakeVizionClient, _container
from apps.scm.tracking.tests.test_tracking_restart import TraqoSession, _patch_traqo
from apps.teams.models import Team
from apps.users.models import CustomUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "detail-stop"}}
_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
PROVIDERS_LIVE = {
    "CACHES": _LOCMEM,
    "STORAGES": _STORAGES,
    "TRAQO_ENABLED": True,
    "TRAQO_API_KEY": "detail-stop-key",
    "VIZION_ENABLED": True,
    "VIZION_API_KEY": "detail-stop-key",
}


@override_settings(**PROVIDERS_LIVE)
class TrackingDetailStopTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="detail-stop", slug="detail-stop")
        self.user = CustomUser.objects.create_user(username="member@detail-stop.test", password="pass")
        self.team.members.add(self.user, through_defaults={"role": "member"})
        self.client = Client()
        self.client.force_login(self.user)
        self.container = _container(self.team, owner_code="BBC", serial="327307", check_digit=0)

    def _watch(self, provider_code, provider_name, *, provider_reference="", carrier_source=""):
        return get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=provider_code,
            provider_name=provider_name,
            carrier_code="one",
            carrier_name="ONE",
            carrier_source=carrier_source,
            provider_reference=provider_reference,
        )

    def _stop(self, watch, *, vizion=None):
        vizion = vizion or FakeVizionClient()
        with mock.patch("apps.scm.integrations.vizion.service.VizionClient.from_settings", return_value=vizion):
            response = self.client.post(reverse("tracking:stop", kwargs={"pk": watch.pk}))
        watch.refresh_from_db()
        return response, vizion

    def test_the_page_offers_stop_tracking_rather_than_cancel(self):
        watch = self._watch(VIZION_PROVIDER_CODE, VIZION_PROVIDER_NAME, provider_reference="vizion-ref-1")

        response = self.client.get(reverse("tracking:detail", kwargs={"pk": watch.pk}))

        self.assertContains(response, "Stop tracking")
        self.assertContains(response, reverse("tracking:stop", kwargs={"pk": watch.pk}))
        self.assertNotContains(response, ">Cancel<")

    def test_an_external_subscription_is_released_before_the_watch_is_cancelled(self):
        watch = self._watch(VIZION_PROVIDER_CODE, VIZION_PROVIDER_NAME, provider_reference="vizion-ref-1")

        response, vizion = self._stop(watch)

        self.assertRedirects(response, reverse("tracking:detail", kwargs={"pk": watch.pk}))
        self.assertEqual(vizion.released, ["vizion-ref-1"])
        self.assertEqual(watch.status, TrackingSubscription.Status.CANCELLED)
        self.assertIn("Tracking stopped", [str(m) for m in get_messages(response.wsgi_request)][0])

    def test_a_traqo_shipment_is_untracked_before_the_watch_is_cancelled(self):
        watch = self._watch(TRAQO_PROVIDER_CODE, "Traqo Ocean", provider_reference="ONEY")
        session = TraqoSession(self.container.container_id)

        with _patch_traqo(session):
            self.client.post(reverse("tracking:stop", kwargs={"pk": watch.pk}))

        watch.refresh_from_db()
        self.assertEqual([request["method"] for request in session.requests], ["DELETE"])
        self.assertEqual(watch.status, TrackingSubscription.Status.CANCELLED)

    def test_a_refused_release_leaves_the_watch_paused_with_a_sanitised_message(self):
        watch = self._watch(VIZION_PROVIDER_CODE, VIZION_PROVIDER_NAME, provider_reference="vizion-ref-1")
        refusing = FakeVizionClient(error=CarrierServerError("502 SECRET_PROVIDER_DETAIL_123 billing.example/internal"))

        response, _vizion = self._stop(watch, vizion=refusing)

        self.assertEqual(watch.status, TrackingSubscription.Status.PAUSED)
        self.assertEqual(watch.last_error_message, CarrierServerError.safe_message_template)
        messages = [m for m in get_messages(response.wsgi_request)]
        self.assertEqual(messages[0].level_tag, "warning")
        page = self.client.get(reverse("tracking:detail", kwargs={"pk": watch.pk}))
        self.assertNotContains(page, "SECRET_PROVIDER_DETAIL_123")
        self.assertNotContains(page, "billing.example/internal")

    def test_a_watch_with_nothing_external_is_cancelled_without_a_provider_call(self):
        watch = self._watch("maersk", "Maersk", carrier_source=CarrierSource.DIRECT_API)

        with (
            mock.patch("apps.scm.integrations.traqo.service.TraqoClient.from_settings") as traqo,
            mock.patch("apps.scm.integrations.vizion.service.VizionClient.from_settings") as vizion,
        ):
            self.client.post(reverse("tracking:stop", kwargs={"pk": watch.pk}))

        traqo.assert_not_called()
        vizion.assert_not_called()
        watch.refresh_from_db()
        self.assertEqual(watch.status, TrackingSubscription.Status.CANCELLED)

    def test_a_watch_without_a_container_can_be_stopped(self):
        """Booking and bill-of-lading watches have no container to stop by."""
        from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider

        provider = get_or_create_tracking_provider(carrier_code="maersk", carrier_name="Maersk")
        watch = create_tracking_subscription(self.team, provider, "BOOKING-123")

        self.client.post(reverse("tracking:stop", kwargs={"pk": watch.pk}))

        watch.refresh_from_db()
        self.assertEqual(watch.status, TrackingSubscription.Status.CANCELLED)

    def test_only_the_watch_on_the_page_is_stopped(self):
        vizion_watch = self._watch(VIZION_PROVIDER_CODE, VIZION_PROVIDER_NAME, provider_reference="vizion-ref-1")
        other = self._watch("maersk", "Maersk", carrier_source=CarrierSource.DIRECT_API)

        self._stop(vizion_watch)

        other.refresh_from_db()
        self.assertEqual(other.status, TrackingSubscription.Status.ACTIVE)

    def test_an_already_stopped_watch_asks_no_provider(self):
        watch = self._watch(VIZION_PROVIDER_CODE, VIZION_PROVIDER_NAME, provider_reference="vizion-ref-1")
        self._stop(watch)

        _response, vizion = self._stop(watch)

        self.assertEqual(vizion.released, [])
        self.assertEqual(watch.status, TrackingSubscription.Status.CANCELLED)

    def test_the_stop_is_audited(self):
        watch = self._watch(VIZION_PROVIDER_CODE, VIZION_PROVIDER_NAME, provider_reference="vizion-ref-1")

        self._stop(watch)

        entry = SCMAuditLog.objects.get(team=self.team, action=SCMAuditLog.Action.TRACKING_STOPPED)
        self.assertEqual(entry.object_type, "TrackingSubscription")
        self.assertEqual(entry.object_id, str(watch.pk))
        self.assertEqual(entry.actor, self.user)

    def test_a_get_changes_nothing(self):
        watch = self._watch(VIZION_PROVIDER_CODE, VIZION_PROVIDER_NAME, provider_reference="vizion-ref-1")

        self.client.get(reverse("tracking:stop", kwargs={"pk": watch.pk}))

        watch.refresh_from_db()
        self.assertEqual(watch.status, TrackingSubscription.Status.ACTIVE)

    def test_restart_after_a_detail_page_stop_goes_through_activation(self):
        """TRACK-FIX's invariant holds whichever page the stop came from."""
        watch = self._watch(
            VIZION_PROVIDER_CODE,
            VIZION_PROVIDER_NAME,
            provider_reference="vizion-ref-1",
            carrier_source=CarrierSource.VIZION_ACI,
        )
        self._stop(watch)
        session = TraqoSession(self.container.container_id)

        with (
            mock.patch.object(
                activation_module, "activate_tracking_route", wraps=activation_module.activate_tracking_route
            ) as activate,
            _patch_traqo(session),
        ):
            result = start_container_tracking(team=self.team, container=self.container)

        activate.assert_called_once()
        self.assertTrue(result.tracked)
        self.assertEqual([request["params"].get("sealine") for request in session.gets], ["ONEY"])
        watch.refresh_from_db()
        self.assertEqual(watch.status, TrackingSubscription.Status.CANCELLED)
        self.assertTrue(is_container_tracked(team=self.team, container=self.container))
