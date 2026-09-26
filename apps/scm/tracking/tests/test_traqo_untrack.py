"""Stopping a Traqo watch, at Traqo as well as here.

TRACK-ADMIN-1 recorded that Traqo published no untrack. That was wrong — or has since
become wrong — and ``DELETE /api/v1/shipments/:id`` is real: verified against the
keyless sandbox, whose 404 body is reproduced below.

What is asserted here is the contract, not the transport: the handle we address the call
with, the three outcomes, and that a failed release leaves something retryable behind.
The lifecycle itself (external-first, CANCELLED on success, PAUSED on failure) is already
covered by ``test_tracking_lifecycle.py`` and needed no change to gain a second provider
— which is the point of the capability being per source.
"""

from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.integrations.carriers.exceptions import (
    CarrierNoDataError,
    CarrierServerError,
    CarrierUnsupportedReferenceError,
)
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.integrations.traqo.client import TraqoClient
from apps.scm.integrations.traqo.service import release_traqo_shipment
from apps.scm.tracking.lifecycle import (
    ALREADY_STOPPED,
    STOP_INCOMPLETE,
    STOPPED,
    is_container_tracked,
    start_container_tracking,
    stop_container_tracking,
)
from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
from apps.scm.tracking.models import CarrierSource, TrackingEvent, TrackingSubscription
from apps.scm.tracking.selectors import get_due_tracking_subscriptions
from apps.scm.tracking.sources import (
    STOP_FAILED,
    STOP_NOT_CONFIGURED,
    STOP_NOT_REQUIRED,
    STOP_RELEASED,
    release_provider_subscription,
)
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "traqo-untrack"}}
TRAQO_LIVE = {"CACHES": _LOCMEM, "TRAQO_ENABLED": True, "TRAQO_API_KEY": "untrack-key"}

CONTAINER_NUMBER = "TRDU9258963"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class DeleteSession:
    """A session that answers DELETE, recording the method and URL it was given."""

    def __init__(self, response=None):
        self.response = response or FakeResponse(200, {"success": True, "deleted": True, "shipment_id": "SHP-1042"})
        self.requests: list[dict] = []

    def delete(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"method": "DELETE", "url": url, "params": params or {}})
        return self.response

    # The shared transport dispatches by method name; a GET here would be a bug worth
    # failing on rather than quietly answering.
    def get(self, url, headers=None, params=None, timeout=None):  # pragma: no cover
        raise AssertionError(f"untracking must not issue a GET (asked {url})")


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team, owner="TRD", serial="925896") -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit(owner, "U", serial),
        equipment_type=_equipment_type(),
    )


@override_settings(**TRAQO_LIVE)
class TraqoUntrackClientTest(TestCase):
    """The request Traqo would actually receive."""

    def _client(self, session):
        return TraqoClient(base_url="https://traqocontainer.com/api/v1", api_key="k", session=session)

    def test_untracking_issues_a_delete_to_the_shipments_endpoint(self):
        session = DeleteSession()

        self._client(session).untrack_shipment(CONTAINER_NUMBER)

        self.assertEqual(len(session.requests), 1)
        request = session.requests[0]
        self.assertEqual(request["method"], "DELETE")
        self.assertEqual(request["url"], f"https://traqocontainer.com/api/v1/shipments/{CONTAINER_NUMBER}")

    def test_the_sandbox_path_segment_is_honoured(self):
        session = DeleteSession()
        client = TraqoClient(base_url="https://traqocontainer.com/api/v1", sandbox=True, session=session)

        client.untrack_shipment(CONTAINER_NUMBER)

        self.assertEqual(
            session.requests[0]["url"],
            f"https://traqocontainer.com/api/v1/sandbox/shipments/{CONTAINER_NUMBER}",
        )

    def test_a_shipment_traqo_does_not_hold_is_no_data(self):
        """The live 404 body, which the shared transport turns into CarrierNoDataError."""
        session = DeleteSession(
            FakeResponse(404, {"success": False, "statusCode": 404, "message": "Shipment not found in your account."})
        )

        with self.assertRaises(CarrierNoDataError):
            self._client(session).untrack_shipment(CONTAINER_NUMBER)

    def test_an_empty_reference_is_refused_before_a_request_is_spent(self):
        session = DeleteSession()

        with self.assertRaises(CarrierUnsupportedReferenceError):
            self._client(session).untrack_shipment("")

        self.assertEqual(session.requests, [])


@override_settings(**TRAQO_LIVE)
class TraqoReleaseOutcomeTest(TestCase):
    """The four answers the lifecycle branches on."""

    def setUp(self):
        self.team = Team.objects.create(name="release", slug="traqo-release")
        self.container = _container(self.team)
        self.subscription = _traqo_watch(self.team, self.container)

    def test_a_successful_delete_is_released(self):
        client = mock.Mock(untrack_shipment=mock.Mock(return_value={"success": True, "deleted": True}))

        outcome = release_traqo_shipment(self.subscription, client=client)

        self.assertEqual(outcome.state, STOP_RELEASED)
        client.untrack_shipment.assert_called_once_with(CONTAINER_NUMBER)

    def test_a_404_is_released_because_that_is_the_destination(self):
        client = mock.Mock(untrack_shipment=mock.Mock(side_effect=CarrierNoDataError("404")))

        outcome = release_traqo_shipment(self.subscription, client=client)

        self.assertEqual(outcome.state, STOP_RELEASED)
        self.assertIn("no longer holds", outcome.detail)

    def test_a_transient_failure_is_retryable(self):
        client = mock.Mock(untrack_shipment=mock.Mock(side_effect=CarrierServerError("502")))

        outcome = release_traqo_shipment(self.subscription, client=client)

        self.assertEqual(outcome.state, STOP_FAILED)
        self.assertIn("CarrierServerError", outcome.detail)

    def test_a_watch_with_no_reference_needs_no_release(self):
        self.subscription.tracking_reference = ""
        self.subscription.save(update_fields=["tracking_reference"])

        self.assertEqual(release_traqo_shipment(self.subscription).state, STOP_NOT_REQUIRED)

    @override_settings(TRAQO_ENABLED=False, TRAQO_API_KEY="")
    def test_an_unreachable_traqo_is_not_a_retryable_failure(self):
        outcome = release_traqo_shipment(self.subscription)

        self.assertEqual(outcome.state, STOP_NOT_CONFIGURED)

    def test_the_handle_is_the_container_number_not_the_sealine(self):
        """``provider_reference`` holds the sealine the scheduled sync needs."""
        self.assertEqual(self.subscription.provider_reference, "ONEY")
        client = mock.Mock(untrack_shipment=mock.Mock(return_value={}))

        release_traqo_shipment(self.subscription, client=client)

        client.untrack_shipment.assert_called_once_with(CONTAINER_NUMBER)

    def test_the_provider_is_reached_through_the_shared_capability(self):
        """No branch per provider in the lifecycle — the source declares its own stop."""
        with mock.patch(
            "apps.scm.integrations.traqo.service.release_traqo_shipment",
            return_value=mock.Mock(state=STOP_RELEASED, detail=""),
        ) as release:
            outcome = release_provider_subscription(self.subscription)

        release.assert_called_once()
        self.assertEqual(outcome.state, STOP_RELEASED)


def _traqo_watch(team, container, *, provider_reference="ONEY") -> TrackingSubscription:
    return get_or_create_container_subscription(
        team=team,
        container=container,
        provider_code=TRAQO_PROVIDER_CODE,
        provider_name="Traqo Ocean",
        carrier_code="one",
        carrier_name="ONE (Ocean Network Express)",
        carrier_source=CarrierSource.TRAQO_PROBE,
        provider_reference=provider_reference,
    )


@override_settings(**TRAQO_LIVE)
class TraqoStopTest(TestCase):
    """Stop, end to end, for a container watched through Traqo."""

    def setUp(self):
        self.team = Team.objects.create(name="stop", slug="traqo-stop")
        self.container = _container(self.team)
        self.subscription = _traqo_watch(self.team, self.container)
        from apps.scm.tracking.ingestion import persist_normalised_events
        from apps.scm.tracking.tests.test_manual_refresh import _normalised_event

        persist_normalised_events(
            team=self.team,
            provider=self.subscription.provider,
            events=[_normalised_event(self.container.container_id)],
            subscription=self.subscription,
            container=self.container,
        )

    def _stop(self, *, error=None):
        client = mock.Mock(
            untrack_shipment=mock.Mock(side_effect=error) if error else mock.Mock(return_value={"success": True})
        )
        with mock.patch(
            "apps.scm.integrations.traqo.service.TraqoClient.from_settings",
            return_value=client,
        ):
            return stop_container_tracking(team=self.team, container=self.container), client

    def test_stopping_untracks_at_traqo_and_cancels_locally(self):
        result, client = self._stop()

        self.assertEqual(result.state, STOPPED)
        client.untrack_shipment.assert_called_once_with(CONTAINER_NUMBER)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.CANCELLED)

    def test_stopping_clears_the_next_sync_and_empties_the_queue(self):
        self.subscription.next_sync_at = timezone.now() - timedelta(hours=1)
        self.subscription.save(update_fields=["next_sync_at"])
        self.assertIn(self.subscription, list(get_due_tracking_subscriptions(self.team)))

        self._stop()

        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.next_sync_at)
        self.assertEqual(list(get_due_tracking_subscriptions(self.team)), [])

    def test_stopping_keeps_the_tracking_history(self):
        events = set(
            TrackingEvent.objects.filter(team=self.team, container=self.container).values_list("pk", flat=True)
        )
        self.assertTrue(events)

        self._stop()

        self.assertEqual(
            set(TrackingEvent.objects.filter(team=self.team, container=self.container).values_list("pk", flat=True)),
            events,
        )

    def test_a_404_from_traqo_still_stops_the_watch(self):
        result, _client = self._stop(error=CarrierNoDataError("404"))

        self.assertEqual(result.state, STOPPED)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.CANCELLED)

    def test_a_failed_untrack_parks_the_watch_and_keeps_the_sealine(self):
        result, _client = self._stop(error=CarrierServerError("502"))

        self.assertEqual(result.state, STOP_INCOMPLETE)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.PAUSED)
        # The handle a retry needs, and the sealine the watch needs if it is restarted.
        self.assertEqual(self.subscription.provider_reference, "ONEY")
        self.assertEqual(self.subscription.tracking_reference, CONTAINER_NUMBER)

    def test_a_failed_untrack_still_stops_the_polling(self):
        self._stop(error=CarrierServerError("502"))

        self.assertFalse(is_container_tracked(team=self.team, container=self.container))
        self.assertEqual(list(get_due_tracking_subscriptions(self.team)), [])

    def test_pressing_stop_again_retries_the_untrack(self):
        self._stop(error=CarrierServerError("502"))

        result, client = self._stop()

        client.untrack_shipment.assert_called_once_with(CONTAINER_NUMBER)
        self.assertEqual(result.state, STOPPED)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.CANCELLED)

    def test_stopping_an_already_stopped_container_calls_traqo_no_further_times(self):
        """Idempotent, and free: there is nothing left to release."""
        self._stop()

        result, client = self._stop()

        self.assertEqual(result.state, ALREADY_STOPPED)
        client.untrack_shipment.assert_not_called()

    def test_an_old_watch_without_a_sealine_can_still_be_untracked(self):
        """Backward compatibility: the handle never was ``provider_reference``."""
        self.subscription.provider_reference = ""
        self.subscription.save(update_fields=["provider_reference"])

        result, client = self._stop()

        client.untrack_shipment.assert_called_once_with(CONTAINER_NUMBER)
        self.assertEqual(result.state, STOPPED)

    def test_restarting_after_a_stop_goes_back_to_traqo_rather_than_only_resuming(self):
        """The shipment was untracked, so a restart has to re-add it — not flip the status.

        Covered end to end, including failure and quota, in ``test_tracking_restart.py``.
        """
        self._stop()

        with mock.patch("apps.scm.tracking.activation.activate_tracking_route") as activate:
            activate.return_value = mock.Mock(sync_run=None, state="unavailable", route=None)
            start_container_tracking(team=self.team, container=self.container)

        activate.assert_called_once()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.CANCELLED)


# The 402 the live sandbox returns, reproduced from ``integrations/traqo/errors.py``. Its
# message and ``data`` describe our account — plan, allowance, billing link — and none of
# it may reach a team.
QUOTA_402 = {
    "success": False,
    "statusCode": 402,
    "message": "Shipment limit reached (20 of 20). Add more slots or upgrade your professional plan.",
    "data": {
        "error": "shipment_limit_reached",
        "limit": 20,
        "used": 20,
        "plan": "professional",
        "manageUrl": "https://traqocontainer.com/billing",
    },
}
_ACCOUNT_FRAGMENTS = ("20 of 20", "professional", "slots", "billing", "limit", "Traqo", "402")


@override_settings(**TRAQO_LIVE)
class TraqoStopErrorSanitisationTest(TestCase):
    """A failed untrack keeps Traqo's own words in the log, never on the watch.

    ``last_error_message`` is rendered on team-facing pages. The Traqo account's plan and
    usage are superuser-only, so a stop that fails with them must store only the error's
    ``safe_message`` — the same boundary a failed fetch goes through.
    """

    def setUp(self):
        self.team = Team.objects.create(name="stop-leak", slug="traqo-stop-leak")
        self.container = _container(self.team)
        self.subscription = _traqo_watch(self.team, self.container)

    def _stop_with_402(self):
        session = DeleteSession(FakeResponse(402, QUOTA_402))
        client = TraqoClient(base_url="https://traqocontainer.com/api/v1", api_key="k", session=session)
        with mock.patch("apps.scm.integrations.traqo.service.TraqoClient.from_settings", return_value=client):
            return stop_container_tracking(team=self.team, container=self.container)

    def test_the_provider_detail_never_reaches_last_error_message(self):
        from apps.scm.integrations.carriers.exceptions import CarrierProviderQuotaError

        with self.assertLogs("apps.scm.integrations.traqo.service", level="WARNING"):
            result = self._stop_with_402()

        self.assertEqual(result.state, STOP_INCOMPLETE)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.PAUSED)
        self.assertEqual(self.subscription.last_error_message, CarrierProviderQuotaError.safe_message_template)
        for fragment in _ACCOUNT_FRAGMENTS:
            self.assertNotIn(fragment, self.subscription.last_error_message)

    def test_the_provider_detail_stays_in_the_log(self):
        with self.assertLogs("apps.scm.integrations.traqo.service", level="WARNING") as logs:
            self._stop_with_402()

        record = next(record for record in logs.records if "Untracking Traqo shipment" in record.getMessage())
        self.assertIn("Shipment limit reached (20 of 20)", record.getMessage())
        self.assertEqual(record.provider_detail.get("plan"), "professional")
        self.assertEqual(record.provider_detail.get("used"), 20)

    def test_the_outcome_keeps_detail_for_the_log_and_a_safe_sentence_for_the_team(self):
        session = DeleteSession(FakeResponse(402, QUOTA_402))
        client = TraqoClient(base_url="https://traqocontainer.com/api/v1", api_key="k", session=session)

        outcome = release_traqo_shipment(self.subscription, client=client)

        self.assertEqual(outcome.state, STOP_FAILED)
        self.assertIn("20 of 20", outcome.detail)
        self.assertNotIn("20 of 20", outcome.safe_message)
        self.assertNotIn("professional", outcome.safe_message)

    def test_team_admin_pages_show_no_account_detail(self):
        """The container workspace and the tracking detail page, as a team admin sees them."""
        from django.test import Client
        from django.urls import reverse

        from apps.users.models import CustomUser

        self._stop_with_402()
        admin = CustomUser.objects.create_user(username="admin@traqo-stop-leak.test", password="pass")
        self.team.members.add(admin, through_defaults={"role": "admin"})
        client = Client()
        client.force_login(admin)

        pages = [
            client.get(reverse("containers:detail", kwargs={"container_id": self.container.pk})),
            client.get(reverse("tracking:detail", kwargs={"pk": self.subscription.pk})),
        ]
        for response in pages:
            self.assertEqual(response.status_code, 200)
            for fragment in ("20 of 20", "professional", "traqocontainer.com/billing", "shipment_limit_reached"):
                self.assertNotContains(response, fragment)
