"""Starting and stopping a container's tracking.

The lifecycle is a layer over machinery that already had tests — carrier resolution,
provider routing, activation, the sync engine — so what is asserted here is only what
is new, and most of it is about what does *not* happen:

    starting a tracked container        calls no provider and creates no second watch
    restarting a stopped container      re-runs no discovery: the watch is resumed
    stopping                            keeps every event, payload and position
    stopping a Vizion watch             releases the reference that is being billed
    a failed release                    leaves the watch retryable and not tracking
    stopping a Traqo or direct watch    makes no remote call, because none exists

Every provider is injected. Nothing below the lifecycle is mocked out: activation, the
sync engine and the real Traqo client all run, with a fake session instead of a socket.
"""

import json
import pathlib
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scm.audit_log.models import SCMAuditLog
from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.workspace import get_container_workspace
from apps.scm.integrations.carriers.exceptions import CarrierNoDataError, CarrierServerError
from apps.scm.integrations.models import Integration
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.integrations.traqo import carrier_probe
from apps.scm.integrations.traqo import discovery as traqo_discovery
from apps.scm.integrations.traqo.client import TraqoClient
from apps.scm.integrations.vizion import PROVIDER_CODE as VIZION_PROVIDER_CODE
from apps.scm.integrations.vizion import PROVIDER_NAME as VIZION_PROVIDER_NAME
from apps.scm.tracking.lifecycle import (
    ALREADY_ACTIVE,
    ALREADY_STOPPED,
    STARTED,
    STOP_INCOMPLETE,
    STOPPED,
    is_container_tracked,
    start_container_tracking,
    stop_container_tracking,
)
from apps.scm.tracking.manual_refresh import SUCCESS, WARNING, get_or_create_container_subscription
from apps.scm.tracking.models import (
    CarrierSource,
    TrackingEvent,
    TrackingRawPayload,
    TrackingSubscription,
)
from apps.scm.tracking.selectors import get_due_tracking_subscriptions
from apps.scm.tracking.sources import (
    STOP_FAILED,
    STOP_NOT_CONFIGURED,
    STOP_NOT_REQUIRED,
    release_provider_subscription,
)
from apps.scm.tracking.tests.test_manual_refresh import (
    PAYLOAD,
    FakeResponse,
    FakeSession,
    _maersk_integration,
    _normalised_event,
)
from apps.teams.models import Team
from apps.users.models import CustomUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "lifecycle"}}
TRAQO_FIXTURES = pathlib.Path(__file__).parents[2] / "integrations" / "tests" / "fixtures" / "traqo"


def _equipment_type():
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team, owner_code="TRD", serial="925896", check_digit=3):
    return Container.objects.create(
        team=team,
        owner_code=owner_code,
        category_id="U",
        serial_number=serial,
        check_digit=check_digit,
        equipment_type=_equipment_type(),
    )


# ---------------------------------------------------------------------------
# Start and stop, over a real direct carrier
# ---------------------------------------------------------------------------


@override_settings(CACHES=_LOCMEM)
class ContainerTrackingLifecycleTest(TestCase):
    """The whole cycle on a container Maersk can answer for."""

    def setUp(self):
        self.team = Team.objects.create(name="lifecycle", slug="lifecycle")
        self.user = CustomUser.objects.create_user(username="admin@lifecycle.test", email="admin@lifecycle.test")
        self.container = _container(self.team)
        self.integration = _maersk_integration(self.team)

    def _start(self):
        """Start tracking with Maersk answering through an injected session."""
        from apps.scm.integrations.carriers.maersk.client import MaerskClient

        session = FakeSession([FakeResponse(200, PAYLOAD)])
        client = MaerskClient(self.integration, session=session)
        with mock.patch(
            "apps.scm.integrations.carriers.factory.build_carrier_client",
            return_value=client,
        ):
            result = start_container_tracking(team=self.team, container=self.container, actor=self.user)
        return result, session

    def test_starting_an_untracked_container_creates_a_live_watch(self):
        result, _session = self._start()

        self.assertEqual(result.state, STARTED)
        self.assertTrue(result.tracked)
        subscription = TrackingSubscription.objects.get(team=self.team, container=self.container)
        self.assertEqual(subscription.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(subscription.provider.code, "maersk")
        self.assertTrue(is_container_tracked(team=self.team, container=self.container))

    def test_starting_uses_the_existing_activation_path_and_stores_its_events(self):
        """No second ingestion path: the events land through the ordinary sync."""
        self._start()

        self.assertEqual(TrackingEvent.objects.filter(team=self.team, container=self.container).count(), 2)
        self.assertTrue(TrackingRawPayload.objects.filter(team=self.team).exists())

    def test_starting_an_already_tracked_container_creates_no_second_watch(self):
        self._start()

        # No session is injected, so any provider call at all would fail loudly.
        second = start_container_tracking(team=self.team, container=self.container, actor=self.user)

        self.assertEqual(second.state, ALREADY_ACTIVE)
        self.assertTrue(second.tracked)
        self.assertEqual(TrackingSubscription.objects.filter(team=self.team, container=self.container).count(), 1)

    def test_starting_an_already_tracked_container_calls_no_provider(self):
        self._start()
        events_before = TrackingEvent.objects.filter(team=self.team).count()

        with mock.patch("apps.scm.integrations.carriers.factory.build_carrier_client") as build:
            start_container_tracking(team=self.team, container=self.container, actor=self.user)

        build.assert_not_called()
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), events_before)

    def test_stopping_cancels_the_watch(self):
        self._start()

        result = stop_container_tracking(team=self.team, container=self.container, actor=self.user)

        self.assertEqual(result.state, STOPPED)
        self.assertEqual(result.level, SUCCESS)
        self.assertFalse(result.tracked)
        subscription = TrackingSubscription.objects.get(team=self.team, container=self.container)
        self.assertEqual(subscription.status, TrackingSubscription.Status.CANCELLED)
        self.assertFalse(is_container_tracked(team=self.team, container=self.container))

    def test_stopping_takes_the_container_out_of_the_schedulers_queue(self):
        """The scheduler contract: it reads status, and knows nothing about why."""
        self._start()
        subscription = TrackingSubscription.objects.get(team=self.team, container=self.container)
        subscription.next_sync_at = timezone.now() - timedelta(hours=1)
        subscription.save(update_fields=["next_sync_at"])
        self.assertIn(subscription, list(get_due_tracking_subscriptions(self.team)))

        stop_container_tracking(team=self.team, container=self.container, actor=self.user)

        self.assertEqual(list(get_due_tracking_subscriptions(self.team)), [])
        subscription.refresh_from_db()
        self.assertIsNone(subscription.next_sync_at)

    def test_stopping_keeps_every_event_payload_and_position(self):
        self._start()
        events = set(
            TrackingEvent.objects.filter(team=self.team, container=self.container).values_list("pk", flat=True)
        )
        payloads = set(TrackingRawPayload.objects.filter(team=self.team).values_list("pk", flat=True))
        self.assertTrue(events)

        stop_container_tracking(team=self.team, container=self.container, actor=self.user)

        self.assertEqual(
            set(TrackingEvent.objects.filter(team=self.team, container=self.container).values_list("pk", flat=True)),
            events,
        )
        self.assertEqual(set(TrackingRawPayload.objects.filter(team=self.team).values_list("pk", flat=True)), payloads)

    def test_a_stopped_container_still_shows_its_journey_in_the_workspace(self):
        self._start()
        stop_container_tracking(team=self.team, container=self.container, actor=self.user)

        workspace = get_container_workspace(team=self.team, container=self.container)

        self.assertFalse(workspace.has_live_tracking)
        self.assertEqual(len(workspace.timeline), 2)
        self.assertIsNotNone(workspace.journey)
        self.assertTrue(workspace.journey.points)

    def test_stopping_an_already_stopped_container_is_a_success(self):
        self._start()
        stop_container_tracking(team=self.team, container=self.container, actor=self.user)

        again = stop_container_tracking(team=self.team, container=self.container, actor=self.user)

        self.assertEqual(again.state, ALREADY_STOPPED)
        self.assertFalse(again.tracked)

    def test_stopping_a_container_that_was_never_tracked_is_a_success(self):
        fresh = _container(self.team, serial="925897", check_digit=9)

        result = stop_container_tracking(team=self.team, container=fresh, actor=self.user)

        self.assertEqual(result.state, ALREADY_STOPPED)
        self.assertFalse(TrackingSubscription.objects.filter(container=fresh).exists())

    def test_starting_again_after_stopping_resumes_the_same_watch(self):
        self._start()
        original = TrackingSubscription.objects.get(team=self.team, container=self.container)
        stop_container_tracking(team=self.team, container=self.container, actor=self.user)

        result, _session = self._start()

        self.assertTrue(result.tracked)
        self.assertEqual(TrackingSubscription.objects.filter(team=self.team, container=self.container).count(), 1)
        original.refresh_from_db()
        self.assertEqual(original.status, TrackingSubscription.Status.ACTIVE)
        self.assertTrue(is_container_tracked(team=self.team, container=self.container))

    def test_restarting_re_runs_no_carrier_discovery(self):
        """The resumed watch already records the carrier, so nothing is re-proved.

        This is the reason Start resumes before it fetches. Without it, resolution
        would find no verified source, fall through to discovery, and spend a sweep —
        possibly a paid identification — to re-learn what the row already says.
        """
        self._start()
        stop_container_tracking(team=self.team, container=self.container, actor=self.user)

        with mock.patch("apps.scm.integrations.carriers.carrier_discovery.discover_carrier_for_container") as discover:
            self._start()

        discover.assert_not_called()

    def test_start_and_stop_are_recorded_in_the_audit_trail(self):
        self._start()
        stop_container_tracking(team=self.team, container=self.container, actor=self.user)

        actions = list(
            SCMAuditLog.objects.filter(team=self.team, object_id=str(self.container.pk))
            .order_by("created_at")
            .values_list("action", flat=True)
        )
        self.assertIn(SCMAuditLog.Action.TRACKING_STARTED, actions)
        self.assertIn(SCMAuditLog.Action.TRACKING_STOPPED, actions)
        stopped = SCMAuditLog.objects.filter(team=self.team, action=SCMAuditLog.Action.TRACKING_STOPPED).first()
        self.assertEqual(stopped.actor, self.user)


# ---------------------------------------------------------------------------
# Team scoping
# ---------------------------------------------------------------------------


@override_settings(CACHES=_LOCMEM)
class LifecycleTeamScopingTest(TestCase):
    """Two teams, the same container number, and nothing crossing between them."""

    def setUp(self):
        self.team = Team.objects.create(name="scope-a", slug="scope-a")
        self.other = Team.objects.create(name="scope-b", slug="scope-b")
        self.container = _container(self.team)
        self.other_container = _container(self.other)
        self.subscription = self._watch(self.team, self.container)
        self.other_subscription = self._watch(self.other, self.other_container)

    def _watch(self, team, container):
        return get_or_create_container_subscription(
            team=team,
            container=container,
            provider_code="maersk",
            provider_name="Maersk",
            carrier_code="maersk",
            carrier_name="Maersk",
            carrier_source=CarrierSource.DIRECT_API,
        )

    def test_stopping_one_teams_container_leaves_the_others_alone(self):
        stop_container_tracking(team=self.team, container=self.container)

        self.subscription.refresh_from_db()
        self.other_subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.CANCELLED)
        self.assertEqual(self.other_subscription.status, TrackingSubscription.Status.ACTIVE)

    def test_a_watch_is_only_found_through_its_own_team(self):
        """Passing the wrong team finds nothing to stop rather than another tenant's watch."""
        result = stop_container_tracking(team=self.other, container=self.container)

        self.assertEqual(result.state, ALREADY_STOPPED)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)

    def test_tracked_state_is_read_per_team(self):
        self.assertTrue(is_container_tracked(team=self.team, container=self.container))
        self.assertFalse(is_container_tracked(team=self.other, container=self.container))


# ---------------------------------------------------------------------------
# Provider behaviour on stop
# ---------------------------------------------------------------------------


class FakeVizionClient:
    """Answers a reference release, or refuses to."""

    def __init__(self, error=None):
        self.error = error
        self.released: list[str] = []

    def deactivate_reference(self, reference_id):
        self.released.append(reference_id)
        if self.error is not None:
            raise self.error
        return {"message": "Reference unsubscribed successfully."}


@override_settings(CACHES=_LOCMEM, VIZION_ENABLED=True, VIZION_API_KEY="stop-key")
class VizionStopTest(TestCase):
    """A Vizion reference is billed until it is released, so Stop has to release it."""

    def setUp(self):
        self.team = Team.objects.create(name="vizion-stop", slug="vizion-stop")
        self.container = _container(self.team, owner_code="CPW", serial="258829", check_digit=7)
        self.subscription = get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=VIZION_PROVIDER_CODE,
            provider_name=VIZION_PROVIDER_NAME,
            carrier_code="one",
            carrier_name="ONE",
            carrier_source=CarrierSource.VIZION_ACI,
            provider_reference="vizion-ref-1",
        )

    def _stop(self, client):
        with mock.patch(
            "apps.scm.integrations.vizion.service.VizionClient.from_settings",
            return_value=client,
        ):
            return stop_container_tracking(team=self.team, container=self.container)

    def test_stopping_releases_the_reference_at_vizion(self):
        client = FakeVizionClient()

        result = self._stop(client)

        self.assertEqual(client.released, ["vizion-ref-1"])
        self.assertEqual(result.state, STOPPED)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.CANCELLED)

    def test_a_reference_vizion_no_longer_holds_counts_as_released(self):
        """404 is the state a release is trying to reach, which makes Stop repeatable."""
        result = self._stop(FakeVizionClient(error=CarrierNoDataError("404")))

        self.assertEqual(result.state, STOPPED)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.CANCELLED)

    def test_a_failed_release_leaves_the_watch_paused_rather_than_cancelled(self):
        result = self._stop(FakeVizionClient(error=CarrierServerError("502 from Vizion")))

        self.assertEqual(result.state, STOP_INCOMPLETE)
        self.assertEqual(result.level, WARNING)
        self.assertFalse(result.tracked)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.PAUSED)

    def test_a_failed_release_still_stops_the_tracking(self):
        """Refusing to stop locally would keep polling *and* keep paying."""
        self._stop(FakeVizionClient(error=CarrierServerError("502 from Vizion")))

        self.assertFalse(is_container_tracked(team=self.team, container=self.container))
        self.assertEqual(list(get_due_tracking_subscriptions(self.team)), [])

    def test_a_failed_release_records_why_on_the_watch(self):
        self._stop(FakeVizionClient(error=CarrierServerError("502 from Vizion")))

        self.subscription.refresh_from_db()
        self.assertIn("CarrierServerError", self.subscription.last_error_message)

    def test_pressing_stop_again_retries_the_release(self):
        self._stop(FakeVizionClient(error=CarrierServerError("502 from Vizion")))
        retry = FakeVizionClient()

        result = self._stop(retry)

        self.assertEqual(retry.released, ["vizion-ref-1"])
        self.assertEqual(result.state, STOPPED)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.CANCELLED)

    def test_a_watch_with_no_reference_needs_no_release(self):
        self.subscription.provider_reference = ""
        self.subscription.save(update_fields=["provider_reference"])

        outcome = release_provider_subscription(self.subscription)

        self.assertEqual(outcome.state, STOP_NOT_REQUIRED)

    @override_settings(VIZION_ENABLED=False, VIZION_API_KEY="")
    def test_an_unreachable_vizion_is_not_treated_as_a_retryable_failure(self):
        """Nobody can release a reference without a credential, so the stop is recorded.

        Reported as ``NOT_CONFIGURED`` rather than ``FAILED``: treating it as retryable
        would leave the container permanently unstoppable in an installation where
        Vizion has been switched off.
        """
        outcome = release_provider_subscription(self.subscription)
        self.assertEqual(outcome.state, STOP_NOT_CONFIGURED)

        result = stop_container_tracking(team=self.team, container=self.container)

        self.assertEqual(result.state, STOPPED)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.CANCELLED)

    def test_a_provider_error_never_escapes_as_an_exception(self):
        with mock.patch(
            "apps.scm.integrations.vizion.service.VizionClient.from_settings",
            side_effect=RuntimeError("boom"),
        ):
            outcome = release_provider_subscription(self.subscription)

        self.assertEqual(outcome.state, STOP_FAILED)
        self.assertIn("RuntimeError", outcome.detail)


@override_settings(CACHES=_LOCMEM)
class NoRemoteStopTest(TestCase):
    """Traqo and the direct carriers publish nothing to withdraw, and none is invented."""

    def setUp(self):
        self.team = Team.objects.create(name="no-remote-stop", slug="no-remote-stop")
        self.container = _container(self.team)

    def _watch(self, provider_code, provider_name, provider_reference=""):
        return get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=provider_code,
            provider_name=provider_name,
            carrier_code="one" if provider_code == TRAQO_PROVIDER_CODE else provider_code,
            carrier_name=provider_name,
            carrier_source=CarrierSource.TRAQO_LOOKUP,
            provider_reference=provider_reference,
        )

    def test_a_traqo_watch_needs_no_remote_release(self):
        subscription = self._watch(TRAQO_PROVIDER_CODE, "Traqo Ocean", provider_reference="ONEY")

        outcome = release_provider_subscription(subscription)

        self.assertEqual(outcome.state, STOP_NOT_REQUIRED)

    def test_stopping_a_traqo_watch_makes_no_http_call_and_cancels_it(self):
        self._watch(TRAQO_PROVIDER_CODE, "Traqo Ocean", provider_reference="ONEY")

        with mock.patch("apps.scm.integrations.traqo.client.TraqoClient.from_settings") as build:
            result = stop_container_tracking(team=self.team, container=self.container)

        build.assert_not_called()
        self.assertEqual(result.state, STOPPED)
        self.assertEqual(
            TrackingSubscription.objects.get(team=self.team, container=self.container).status,
            TrackingSubscription.Status.CANCELLED,
        )

    def test_a_direct_carrier_watch_needs_no_remote_release(self):
        subscription = self._watch("maersk", "Maersk")

        outcome = release_provider_subscription(subscription)

        self.assertEqual(outcome.state, STOP_NOT_REQUIRED)

    def test_stopping_a_direct_watch_builds_no_carrier_client(self):
        self._watch("maersk", "Maersk")

        with mock.patch("apps.scm.integrations.carriers.factory.build_carrier_client") as build:
            stop_container_tracking(team=self.team, container=self.container)

        build.assert_not_called()

    def test_every_live_source_is_stopped_not_just_the_newest(self):
        """A container can be watched by several providers, and Stop means all of them."""
        self._watch("maersk", "Maersk")
        self._watch(TRAQO_PROVIDER_CODE, "Traqo Ocean", provider_reference="MAEU")

        result = stop_container_tracking(team=self.team, container=self.container)

        self.assertEqual(result.state, STOPPED)
        statuses = set(
            TrackingSubscription.objects.filter(team=self.team, container=self.container).values_list(
                "status", flat=True
            )
        )
        self.assertEqual(statuses, {TrackingSubscription.Status.CANCELLED})

    def test_a_completed_leg_is_history_and_is_left_alone(self):
        completed = self._watch("maersk", "Maersk")
        completed.status = TrackingSubscription.Status.COMPLETED
        completed.save(update_fields=["status"])

        result = stop_container_tracking(team=self.team, container=self.container)

        self.assertEqual(result.state, ALREADY_STOPPED)
        completed.refresh_from_db()
        self.assertEqual(completed.status, TrackingSubscription.Status.COMPLETED)


# ---------------------------------------------------------------------------
# Starting through Traqo
# ---------------------------------------------------------------------------


def _traqo_payload(container_number: str, sealine: str) -> dict:
    payload = json.loads((TRAQO_FIXTURES / "sandbox_container_MRSU6859427.json").read_text())
    data = payload["data"]
    data["reference_number"] = container_number
    data["sealine"] = sealine
    data["sealine_name"] = "ONE"
    for event in data.get("events_table") or []:
        event["container_number"] = container_number
    return payload


class TraqoOnlySession:
    """Answers for one sealine and 404s for every other, recording each request."""

    def __init__(self, payload, answering_sealine):
        self.payload = payload
        self.answering_sealine = answering_sealine
        self.requests: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None):
        params = params or {}
        self.requests.append({"url": url, "params": params})
        if params.get("sealine") != self.answering_sealine:
            return FakeResponse(404, {"success": False, "message": "No shipment found."})
        return FakeResponse(200, self.payload)


@override_settings(CACHES=_LOCMEM, TRAQO_ENABLED=True, TRAQO_API_KEY="lifecycle-key")
class TraqoStartTest(TestCase):
    """Start routes through the existing chain, aggregator included.

    BBCU3273070 is the container that motivated candidate probing: Traqo's free lookup
    does not recognise it, and Traqo tracks it perfectly once told ``sealine=ONEY``.
    Starting it must therefore reach Traqo through resolution and routing — not through
    anything the lifecycle invented.
    """

    def setUp(self):
        self.team = Team.objects.create(name="traqo-start", slug="traqo-start")
        self.container = _container(self.team, owner_code="BBC", serial="327307", check_digit=0)
        # A connected carrier that is not ONE, so "the direct sweep was never reached"
        # is a meaningful assertion rather than an empty one.
        Integration.objects.create(
            team=self.team,
            name="cosco",
            provider_code="cosco",
            provider_family=Integration.ProviderFamily.CARRIER,
            is_active=True,
        )
        self.session = TraqoOnlySession(_traqo_payload(self.container.container_id, "ONEY"), "ONEY")

    def _traqo_client(self):
        return TraqoClient(base_url="https://traqocontainer.com/api/v1", api_key="k", session=self.session)

    def _start(self):
        """Start with Traqo's lookup blind and its container endpoint injected.

        The two aggregator calls are replaced at the seams the resolution chain already
        provides for it; everything from the probe's verification rules down through
        routing, activation and the tracking write path runs for real.
        """

        def _lookup(container_number, **kwargs):
            return traqo_discovery.TraqoCarrierDiscovery(
                container_number=container_number,
                status=traqo_discovery.NOT_FOUND,
                reason="No shipment found for this number.",
            )

        def _probe(*, container_number, preferred_carrier_codes=(), exclude_carrier_codes=frozenset()):
            candidates = carrier_probe.build_traqo_probe_candidates(
                container_number=container_number,
                preferred_carrier_codes=preferred_carrier_codes,
                exclude_carrier_codes=exclude_carrier_codes,
            )
            return carrier_probe.probe_candidate_carriers(
                container_number=container_number,
                candidates=candidates,
                client=self._traqo_client(),
            )

        with (
            mock.patch(
                "apps.scm.integrations.carriers.carrier_resolution._default_traqo_lookup",
                side_effect=_lookup,
            ),
            mock.patch(
                "apps.scm.integrations.carriers.carrier_resolution._default_traqo_probe",
                side_effect=_probe,
            ),
            mock.patch(
                "apps.scm.integrations.traqo.service.TraqoClient.from_settings",
                side_effect=lambda **kwargs: self._traqo_client(),
            ),
        ):
            return start_container_tracking(team=self.team, container=self.container)

    def test_starting_routes_to_traqo_and_records_the_sealine_that_answered(self):
        """Only routing and activation can produce this shape, and it is theirs."""
        result = self._start()

        self.assertTrue(result.tracked)
        subscription = TrackingSubscription.objects.get(team=self.team, container=self.container)
        self.assertEqual(subscription.provider.code, TRAQO_PROVIDER_CODE)
        self.assertEqual(subscription.carrier_code, "one")
        self.assertEqual(subscription.carrier_source, CarrierSource.TRAQO_PROBE)
        self.assertEqual(subscription.provider_reference, "ONEY")
        self.assertEqual(subscription.status, TrackingSubscription.Status.ACTIVE)

    def test_the_direct_sweep_is_never_reached_once_traqo_has_answered(self):
        with mock.patch("apps.scm.integrations.carriers.factory.build_carrier_client") as build:
            self._start()

        build.assert_not_called()

    def test_start_obeys_an_override_that_cannot_be_asked_rather_than_substituting_one(self):
        """Routing's no-fallback rule holds on the Start path too.

        A container set to a provider that cannot answer for its carrier must not be
        quietly tracked through the one routing would otherwise have chosen — which is
        the whole reason an explicit choice does not fall back.
        """
        self.container.tracking_provider_override = "maersk"
        self.container.save(update_fields=["tracking_provider_override"])

        result = self._start()

        self.assertFalse(result.tracked)
        self.assertFalse(TrackingSubscription.objects.filter(team=self.team, container=self.container).exists())
        self.assertFalse(is_container_tracked(team=self.team, container=self.container))

    def test_stopping_the_traqo_watch_keeps_its_events(self):
        self._start()
        events = TrackingEvent.objects.filter(team=self.team, container=self.container).count()
        self.assertTrue(events)

        stop_container_tracking(team=self.team, container=self.container)

        self.assertEqual(TrackingEvent.objects.filter(team=self.team, container=self.container).count(), events)

    def test_restarting_asks_traqo_only_about_the_sealine_it_recorded(self):
        """A restart is one ordinary fetch, not a fresh probe across candidates."""
        self._start()
        stop_container_tracking(team=self.team, container=self.container)
        calls_before = len(self.session.requests)

        self._start()

        asked = [request["params"].get("sealine") for request in self.session.requests[calls_before:]]
        self.assertEqual(asked, ["ONEY"])


# ---------------------------------------------------------------------------
# Events are never withdrawn by a stop
# ---------------------------------------------------------------------------


@override_settings(CACHES=_LOCMEM)
class StopKeepsHistoryTest(TestCase):
    """Stated on its own, because it is the guarantee the whole feature rests on."""

    def setUp(self):
        self.team = Team.objects.create(name="stop-history", slug="stop-history")
        self.container = _container(self.team)
        self.subscription = get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code="maersk",
            provider_name="Maersk",
            carrier_code="maersk",
            carrier_name="Maersk",
            carrier_source=CarrierSource.DIRECT_API,
        )
        from apps.scm.tracking.ingestion import persist_normalised_events

        persist_normalised_events(
            team=self.team,
            provider=self.subscription.provider,
            events=[_normalised_event(self.container.container_id)],
            subscription=self.subscription,
            container=self.container,
        )

    def test_the_watch_survives_the_stop_so_its_events_keep_their_source(self):
        event_count = TrackingEvent.objects.filter(team=self.team, container=self.container).count()
        self.assertTrue(event_count)

        stop_container_tracking(team=self.team, container=self.container)

        self.assertTrue(TrackingSubscription.objects.filter(pk=self.subscription.pk).exists())
        self.assertEqual(
            TrackingEvent.objects.filter(
                team=self.team, container=self.container, subscription=self.subscription
            ).count(),
            event_count,
        )

    def test_the_carrier_evidence_on_the_watch_is_not_cleared(self):
        stop_container_tracking(team=self.team, container=self.container)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.carrier_code, "maersk")
        self.assertEqual(self.subscription.carrier_source, CarrierSource.DIRECT_API)
