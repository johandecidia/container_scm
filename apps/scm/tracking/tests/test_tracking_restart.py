"""Starting a container again after Stop released its provider subscription.

Stop is external-first: a Vizion reference is unsubscribed and a Traqo shipment untracked
before the watch is cancelled. Start used to flip every cancelled watch back to ACTIVE and
then refresh — which re-creates nothing for Vizion, because Vizion is never polled. The
result read ACTIVE locally with nothing subscribed at the provider.

The invariant asserted here:

    a watch whose provider resource was released becomes live again only through
    activation, and only once a provider actually answered

and its counterpart: a ``PAUSED`` watch still holds its provider resource, so restarting
it is a resume and creates nothing new.

Every provider is injected; activation, the sync engine and the real Traqo client run.
"""

import json
from unittest import mock

from django.test import TestCase, override_settings

from apps.scm.containers.utils import calculate_check_digit
from apps.scm.integrations.carriers.exceptions import CarrierServerError
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.integrations.traqo.client import TraqoClient
from apps.scm.integrations.vizion import PROVIDER_CODE as VIZION_PROVIDER_CODE
from apps.scm.integrations.vizion import PROVIDER_NAME as VIZION_PROVIDER_NAME
from apps.scm.tracking import activation as activation_module
from apps.scm.tracking.activation import UNAVAILABLE, ActivationResult
from apps.scm.tracking.lifecycle import (
    STARTED,
    STOP_INCOMPLETE,
    STOPPED,
    get_live_container_subscriptions,
    is_container_tracked,
    start_container_tracking,
    stop_container_tracking,
)
from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
from apps.scm.tracking.models import CarrierSource, TrackingEvent, TrackingSubscription
from apps.scm.tracking.sources import (
    STOP_RELEASED,
    ProviderStopOutcome,
    holds_provider_subscription,
    non_carrier_provider_codes,
)
from apps.scm.tracking.tests.test_manual_refresh import FakeResponse
from apps.scm.tracking.tests.test_tracking_lifecycle import (
    TRAQO_FIXTURES,
    FakeVizionClient,
    _container,
    _traqo_payload,
)
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "restart"}}
TRAQO_LIVE = {"CACHES": _LOCMEM, "TRAQO_ENABLED": True, "TRAQO_API_KEY": "restart-key"}
QUOTA_402 = json.loads((TRAQO_FIXTURES / "sandbox_402_shipment_limit_reached.json").read_text())


class TraqoSession:
    """Answers Traqo's container GET and shipment DELETE, recording every request."""

    def __init__(self, container_number, *, get_response=None, get_error=None, delete_error=None):
        self.get_response = get_response or FakeResponse(200, _traqo_payload(container_number, "ONEY"))
        self.get_error = get_error
        self.delete_error = delete_error
        self.requests: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"method": "GET", "url": url, "params": params or {}})
        if self.get_error is not None:
            raise self.get_error
        return self.get_response

    def delete(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"method": "DELETE", "url": url, "params": params or {}})
        if self.delete_error is not None:
            return FakeResponse(502, {"success": False, "message": "upstream"})
        return FakeResponse(200, {"success": True, "deleted": True})

    @property
    def gets(self) -> list[dict]:
        return [request for request in self.requests if request["method"] == "GET"]


def _traqo_client(session):
    return TraqoClient(base_url="https://traqocontainer.com/api/v1", api_key="k", session=session)


def _patch_traqo(session):
    return mock.patch(
        "apps.scm.integrations.traqo.service.TraqoClient.from_settings",
        side_effect=lambda **kwargs: _traqo_client(session),
    )


def _spy_activation():
    """Wrap the real activation so a test can see it was the path taken."""
    return mock.patch.object(
        activation_module, "activate_tracking_route", wraps=activation_module.activate_tracking_route
    )


def _no_discovery():
    """Fail loudly if a restart spent a lookup, a probe or a paid identification."""
    return (
        mock.patch("apps.scm.integrations.carriers.carrier_resolution._default_traqo_lookup"),
        mock.patch("apps.scm.integrations.carriers.carrier_resolution._default_traqo_probe"),
        mock.patch("apps.scm.integrations.carriers.carrier_resolution._default_vizion_identify"),
    )


# ---------------------------------------------------------------------------
# Vizion — the Codex finding
# ---------------------------------------------------------------------------


@override_settings(**TRAQO_LIVE, VIZION_ENABLED=True, VIZION_API_KEY="restart-key")
class VizionRestartAfterReleaseTest(TestCase):
    """ACTIVE → Stop (unsubscribed at Vizion) → Start."""

    def setUp(self):
        self.team = Team.objects.create(name="vizion-restart", slug="vizion-restart")
        self.container = _container(self.team, owner_code="BBC", serial="327307", check_digit=0)
        self.vizion_watch = get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=VIZION_PROVIDER_CODE,
            provider_name=VIZION_PROVIDER_NAME,
            carrier_code="one",
            carrier_name="ONE",
            carrier_source=CarrierSource.VIZION_ACI,
            provider_reference="vizion-ref-1",
        )
        self.vizion = FakeVizionClient()
        self.vizion.create_reference = mock.Mock(side_effect=AssertionError("a restart must not buy a reference"))
        with mock.patch("apps.scm.integrations.vizion.service.VizionClient.from_settings", return_value=self.vizion):
            result = stop_container_tracking(team=self.team, container=self.container)
        self.assertEqual(result.state, STOPPED)
        self.assertEqual(self.vizion.released, ["vizion-ref-1"])

    def _start(self, session):
        lookup, probe, identify = _no_discovery()
        with _spy_activation() as activate, _patch_traqo(session), lookup, probe, identify as vizion_identify:
            result = start_container_tracking(team=self.team, container=self.container)
        vizion_identify.assert_not_called()
        return result, activate

    def test_restart_establishes_a_new_provider_subscription_through_activation(self):
        session = TraqoSession(self.container.container_id)

        result, activate = self._start(session)

        activate.assert_called_once()
        # The provider was actually asked to take the container on, with the sealine
        # routing publishes for the carrier the released watch recorded.
        self.assertEqual([request["params"].get("sealine") for request in session.gets], ["ONEY"])
        self.assertEqual(result.state, STARTED)
        self.assertTrue(result.tracked)
        live = get_live_container_subscriptions(team=self.team, container=self.container)
        self.assertEqual([subscription.provider.code for subscription in live], [TRAQO_PROVIDER_CODE])
        self.assertEqual(live[0].status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(live[0].carrier_code, "one")
        self.assertEqual(live[0].provider_reference, "ONEY")
        self.assertTrue(TrackingEvent.objects.filter(subscription=live[0]).exists())

    def test_the_released_vizion_watch_is_not_resurrected(self):
        self._start(TraqoSession(self.container.container_id))

        self.vizion_watch.refresh_from_db()
        self.assertEqual(self.vizion_watch.status, TrackingSubscription.Status.CANCELLED)

    def test_a_failed_restart_leaves_nothing_active(self):
        session = TraqoSession(self.container.container_id, get_error=CarrierServerError("502 from Traqo"))

        result, _activate = self._start(session)

        self.assertFalse(result.tracked)
        self.assertFalse(is_container_tracked(team=self.team, container=self.container))
        self.vizion_watch.refresh_from_db()
        self.assertEqual(self.vizion_watch.status, TrackingSubscription.Status.CANCELLED)
        self.assertFalse(
            TrackingSubscription.objects.filter(container=self.container, provider__code=TRAQO_PROVIDER_CODE).exists()
        )

    def test_a_restart_refused_for_quota_leaves_nothing_active_and_says_nothing_about_the_account(self):
        session = TraqoSession(self.container.container_id, get_response=FakeResponse(402, QUOTA_402))

        result, _activate = self._start(session)

        self.assertFalse(result.tracked)
        self.assertFalse(is_container_tracked(team=self.team, container=self.container))
        for fragment in ("20 of 20", "professional", "limit", "slot"):
            self.assertNotIn(fragment, str(result.message))

    @override_settings(TRAQO_ENABLED=False, TRAQO_API_KEY="")
    def test_with_no_provider_able_to_take_it_the_container_stays_untracked(self):
        """The exact Codex reproduction: nothing is subscribed, so nothing may read ACTIVE."""
        session = TraqoSession(self.container.container_id)

        result, activate = self._start(session)

        activate.assert_called_once()
        self.assertEqual(session.requests, [])
        self.assertFalse(result.tracked)
        self.assertFalse(is_container_tracked(team=self.team, container=self.container))
        self.vizion_watch.refresh_from_db()
        self.assertEqual(self.vizion_watch.status, TrackingSubscription.Status.CANCELLED)


# ---------------------------------------------------------------------------
# Traqo
# ---------------------------------------------------------------------------


@override_settings(**TRAQO_LIVE)
class TraqoRestartAfterReleaseTest(TestCase):
    """ACTIVE → Stop (untracked at Traqo) → Start, through Traqo's own activation."""

    def setUp(self):
        self.team = Team.objects.create(name="traqo-restart", slug="traqo-restart")
        self.container = _container(self.team, owner_code="BBC", serial="327307", check_digit=0)
        self.watch = get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=TRAQO_PROVIDER_CODE,
            provider_name="Traqo Ocean",
            carrier_code="one",
            carrier_name="ONE",
            carrier_source=CarrierSource.TRAQO_PROBE,
            provider_reference="ONEY",
        )
        stop_session = TraqoSession(self.container.container_id)
        with _patch_traqo(stop_session):
            self.assertEqual(stop_container_tracking(team=self.team, container=self.container).state, STOPPED)
        self.assertEqual([request["method"] for request in stop_session.requests], ["DELETE"])

    def _start(self, session):
        lookup, probe, identify = _no_discovery()
        with _spy_activation() as activate, _patch_traqo(session), lookup as traqo_lookup, probe, identify:
            result = start_container_tracking(team=self.team, container=self.container)
        traqo_lookup.assert_not_called()
        return result, activate

    def test_restart_re_adds_the_shipment_at_traqo_and_revives_the_same_watch(self):
        session = TraqoSession(self.container.container_id)

        result, activate = self._start(session)

        activate.assert_called_once()
        self.assertEqual([request["params"].get("sealine") for request in session.gets], ["ONEY"])
        self.assertTrue(result.tracked)
        self.assertEqual(TrackingSubscription.objects.filter(container=self.container).count(), 1)
        self.watch.refresh_from_db()
        self.assertEqual(self.watch.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(self.watch.provider_reference, "ONEY")

    def test_a_failed_restart_leaves_the_watch_cancelled(self):
        session = TraqoSession(self.container.container_id, get_error=CarrierServerError("502 from Traqo"))

        result, _activate = self._start(session)

        self.assertFalse(result.tracked)
        self.watch.refresh_from_db()
        self.assertEqual(self.watch.status, TrackingSubscription.Status.CANCELLED)
        self.assertFalse(is_container_tracked(team=self.team, container=self.container))

    def test_a_quota_refusal_leaves_the_watch_cancelled_rather_than_active(self):
        """A refresh would have recorded this as SKIPPED — which reads ACTIVE."""
        session = TraqoSession(self.container.container_id, get_response=FakeResponse(402, QUOTA_402))

        result, _activate = self._start(session)

        self.assertFalse(result.tracked)
        self.watch.refresh_from_db()
        self.assertEqual(self.watch.status, TrackingSubscription.Status.CANCELLED)
        self.assertNotIn("20 of 20", self.watch.last_error_message)


# ---------------------------------------------------------------------------
# PAUSED still holds its provider resource
# ---------------------------------------------------------------------------


@override_settings(**TRAQO_LIVE, VIZION_ENABLED=True, VIZION_API_KEY="restart-key")
class PausedWatchRestartTest(TestCase):
    """A failed release leaves the provider resource in place, so restarting resumes it."""

    def setUp(self):
        self.team = Team.objects.create(name="paused-restart", slug="paused-restart")
        self.container = _container(self.team, owner_code="BBC", serial="327307", check_digit=0)

    def _watch(self, provider_code, provider_reference):
        return get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=provider_code,
            provider_name=provider_code,
            carrier_code="one",
            carrier_name="ONE",
            provider_reference=provider_reference,
        )

    def test_a_paused_vizion_watch_is_resumed_without_a_new_reference(self):
        watch = self._watch(VIZION_PROVIDER_CODE, "vizion-ref-1")
        failing = FakeVizionClient(error=CarrierServerError("502 from Vizion"))
        failing.create_reference = mock.Mock()
        with mock.patch("apps.scm.integrations.vizion.service.VizionClient.from_settings", return_value=failing):
            self.assertEqual(stop_container_tracking(team=self.team, container=self.container).state, STOP_INCOMPLETE)

            with _spy_activation() as activate:
                start_container_tracking(team=self.team, container=self.container)

        activate.assert_not_called()
        failing.create_reference.assert_not_called()
        self.assertEqual(TrackingSubscription.objects.filter(container=self.container).count(), 1)
        watch.refresh_from_db()
        self.assertEqual(watch.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(watch.provider_reference, "vizion-ref-1")

    def test_a_paused_traqo_watch_is_resumed_with_one_ordinary_fetch(self):
        watch = self._watch(TRAQO_PROVIDER_CODE, "ONEY")
        session = TraqoSession(self.container.container_id, delete_error=True)
        with _patch_traqo(session):
            self.assertEqual(stop_container_tracking(team=self.team, container=self.container).state, STOP_INCOMPLETE)
            watch.refresh_from_db()
            self.assertEqual(watch.status, TrackingSubscription.Status.PAUSED)

            with _spy_activation() as activate:
                result = start_container_tracking(team=self.team, container=self.container)

        activate.assert_not_called()
        self.assertTrue(result.tracked)
        self.assertEqual([request["params"].get("sealine") for request in session.gets], ["ONEY"])
        self.assertEqual(TrackingSubscription.objects.filter(container=self.container).count(), 1)
        watch.refresh_from_db()
        self.assertEqual(watch.status, TrackingSubscription.Status.ACTIVE)


# ---------------------------------------------------------------------------
# The invariant, for every provider that holds a subscription of its own
# ---------------------------------------------------------------------------


@override_settings(CACHES=_LOCMEM)
class ReleasedWatchInvariantTest(TestCase):
    """No released watch becomes live unless activation succeeded.

    Stated over the registry rather than per provider, so a third provider that declares
    an external stop is held to it without anyone remembering to add a test.
    """

    def setUp(self):
        self.team = Team.objects.create(name="restart-invariant", slug="restart-invariant")

    def test_the_registry_declares_the_providers_this_covers(self):
        holding = {code for code in non_carrier_provider_codes() if holds_provider_subscription(code)}

        self.assertEqual(holding, {TRAQO_PROVIDER_CODE, VIZION_PROVIDER_CODE})
        self.assertFalse(holds_provider_subscription("maersk"))

    def test_a_released_watch_is_never_made_live_when_activation_fails(self):
        holding = [code for code in non_carrier_provider_codes() if holds_provider_subscription(code)]
        for index, provider_code in enumerate(holding):
            with self.subTest(provider=provider_code):
                serial = f"92589{index}"
                container = _container(self.team, serial=serial, check_digit=calculate_check_digit("TRD", "U", serial))
                watch = get_or_create_container_subscription(
                    team=self.team,
                    container=container,
                    provider_code=provider_code,
                    provider_name=provider_code,
                    carrier_code="one",
                    carrier_name="ONE",
                    provider_reference="handle",
                )
                with mock.patch(
                    "apps.scm.tracking.sources.release_provider_subscription",
                    return_value=ProviderStopOutcome(state=STOP_RELEASED),
                ):
                    stop_container_tracking(team=self.team, container=container)

                with mock.patch.object(
                    activation_module,
                    "activate_tracking_route",
                    return_value=ActivationResult(state=UNAVAILABLE),
                ) as activate:
                    result = start_container_tracking(team=self.team, container=container)

                activate.assert_called_once()
                self.assertFalse(result.tracked)
                self.assertEqual(get_live_container_subscriptions(team=self.team, container=container), [])
                watch.refresh_from_db()
                self.assertEqual(watch.status, TrackingSubscription.Status.CANCELLED)
