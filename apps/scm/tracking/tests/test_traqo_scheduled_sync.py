"""BBCU3273070 once more — now polled by the scheduler, with nobody pressing Refresh.

TRACK-DISCOVERY established the carrier and left the watch in the shape that makes this
possible:

    container           BBCU3273070
    provider            traqo          who supplies the data
    carrier_code        one            who is moving the box
    carrier_source      traqo_probe    how that was established
    provider_reference  ONEY           what a later fetch needs

What is asserted here is that one recorded fact — the sealine — is enough, and that
nothing else is re-derived from it. A poll is one request::

    GET /container/BBCU3273070?sealine=ONEY

and then the *existing* pipeline: raw payload, normalised events through the same
fingerprint and upsert a carrier's go through, Traqo's ETA observation, the subscription's
own state machine and the shared polling cadence.

So the negative assertions carry as much weight as the positive ones. A scheduled poll
must not re-open the question of who is carrying the box:

    carrier resolution           never called
    Traqo free carrier lookup    never called
    Traqo candidate probing      never called
    direct carrier discovery     never called
    Vizion ACI                   never called

Discovery and ongoing tracking are different problems, and a cadence that re-ran discovery
would spend money per cycle to be told what the watch already says.

Nothing below is mocked except the socket and the five discovery entry points that are
asserted never to run: the real dispatcher, the real sync engine, the real Traqo client,
the real mapper and the real ingestion all execute.
"""

import json
import pathlib
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.models import Integration
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.integrations.traqo.client import TraqoClient
from apps.scm.integrations.traqo.scheduled import resolve_watch_sealine
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.models import (
    CarrierSource,
    ETAHistory,
    TrackingEvent,
    TrackingProvider,
    TrackingRawPayload,
    TrackingSubscription,
    TrackingSyncRun,
)
from apps.scm.tracking.selectors import get_due_tracking_subscriptions
from apps.scm.tracking.tasks import dispatch_due_tracking_subscriptions, sync_single_tracking_subscription
from apps.teams.models import Team

FIXTURES = pathlib.Path(__file__).parents[2] / "integrations" / "tests" / "fixtures" / "traqo"

CONTAINER_NUMBER = "BBCU3273070"
ONE_CODE = "one"
ONE_SCAC = "ONEY"

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "traqo-scheduled"}}
TRAQO_LIVE = {"TRAQO_ENABLED": True, "TRAQO_API_KEY": "scheduled-key", "CACHES": _LOCMEM}

_DISPATCH_SYNC = "apps.scm.tracking.tasks.sync_single_tracking_subscription.delay"

# The steps that answer "who is carrying this box". Every one of them must stay untouched
# by a poll of a watch that already knows.
_CARRIER_RESOLUTION = "apps.scm.integrations.carriers.carrier_resolution.resolve_carrier_for_container"
_TRAQO_LOOKUP = "apps.scm.integrations.traqo.discovery.lookup_carrier_for_container"
_TRAQO_PROBE = "apps.scm.integrations.traqo.carrier_probe.probe_candidate_carriers"
_DIRECT_DISCOVERY = "apps.scm.integrations.carriers.carrier_discovery.discover_carrier_for_container"
_VIZION_ACI = "apps.scm.integrations.vizion.discovery.identify_carrier"


def one_payload() -> dict:
    """A Traqo container response for BBCU3273070 under sealine ONEY.

    The recorded sandbox envelope with this container's number and ONE as the sealine, so
    the mapper reading it is the real one reading a real shape.
    """
    payload = json.loads((FIXTURES / "sandbox_container_MRSU6859427.json").read_text())
    data = payload["data"]
    data["reference_number"] = CONTAINER_NUMBER
    data["sealine"] = ONE_SCAC
    data["sealine_name"] = "ONE"
    for event in data.get("events_table") or []:
        event["container_number"] = CONTAINER_NUMBER
    return payload


def payload_with_events(count: int) -> dict:
    """The same envelope carrying ``count`` distinct actual events, oldest first.

    A journey that grows one event at a time is what a poll actually sees, and the
    recorded fixture has three — too few to tell "the new one was added" apart from "they
    were all rewritten". Each event keeps the fixture's real shape and differs only in the
    fields that make it a different event: its index, its day and its place.
    """
    payload = one_payload()
    template = (payload["data"]["events_table"] or [{}])[0]
    payload["data"]["events_table"] = [
        {
            **template,
            "idx": index + 1,
            "timestamp": f"2026-03-{index + 1:02d} 06:00:00",
            "location": f"Mundra Terminal {index + 1}",
        }
        for index in range(count)
    ]
    return payload


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class TraqoSession:
    """Answers for ONEY and 404s for anything else, recording every request.

    ``requests`` is the cost assertion — one entry per Traqo call actually made — and the
    404 is what Traqo returns for a container it has no shipment for under that sealine.
    """

    def __init__(self, payload=None, answering_sealine=ONE_SCAC, error=None):
        self.payload = payload if payload is not None else one_payload()
        self.answering_sealine = answering_sealine
        self.error = error
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        params = params or {}
        self.requests.append({"url": url, "params": params})
        if self.error is not None:
            raise self.error
        if params.get("sealine") != self.answering_sealine:
            return FakeResponse(404, {"success": False, "message": "No shipment found."})
        return FakeResponse(200, self.payload)

    @property
    def sealines_asked(self) -> list[str]:
        return [request["params"].get("sealine") for request in self.requests]

    @property
    def containers_asked(self) -> list[str]:
        return [request["url"].rsplit("/", 1)[-1] for request in self.requests]


def _equipment_type():
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team: Team, owner="BBC", serial="327307", check_digit=0) -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check_digit,
        equipment_type=_equipment_type(),
    )


def _traqo_provider() -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(code=TRAQO_PROVIDER_CODE, defaults={"name": "Traqo Ocean"})[0]


def _traqo_watch(team, container, **kwargs) -> TrackingSubscription:
    """The watch TRACK-DISCOVERY leaves behind: carrier ONE, tracked via Traqo."""
    defaults = {
        "tracking_reference": container.container_id,
        "reference_type": TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
        "carrier_code": ONE_CODE,
        "carrier_name": "ONE",
        "carrier_source": CarrierSource.TRAQO_PROBE,
        "provider_reference": ONE_SCAC,
        "status": TrackingSubscription.Status.ACTIVE,
        "next_sync_at": timezone.now() - timedelta(minutes=1),
    }
    defaults.update(kwargs)
    return TrackingSubscription.objects.create(
        team=team,
        provider=_traqo_provider(),
        container=container,
        **defaults,
    )


class TraqoScheduledSyncTestCase(TestCase):
    """Shared harness: a due Traqo watch, and the real chain over a fake socket."""

    def setUp(self):
        self.team = Team.objects.create(name="traqo-sched", slug="traqo-sched")
        self.container = _container(self.team)
        self.subscription = _traqo_watch(self.team, self.container)
        self.session = TraqoSession()

    def _client(self, session=None) -> TraqoClient:
        return TraqoClient(
            base_url="https://traqocontainer.com/api/v1",
            api_key="scheduled-key",
            session=session or self.session,
        )

    def run_scheduled_sync(self, session=None) -> dict:
        """Drive beat's dispatcher and the real sync task, with a socket-level fake.

        ``from_settings`` is the seam rather than the fetch function: everything from the
        client's URL building and error mapping downwards is the production code.
        """
        client = self._client(session)
        queued: list[int] = []
        with (
            mock.patch.object(TraqoClient, "from_settings", return_value=client),
            mock.patch(_DISPATCH_SYNC, side_effect=lambda pk: queued.append(pk)),
        ):
            dispatch_due_tracking_subscriptions.run()
            results = [sync_single_tracking_subscription.run(pk) for pk in queued]
        return {"queued": queued, "results": results}


@override_settings(**TRAQO_LIVE)
class Bbcu3273070ScheduledPollTest(TraqoScheduledSyncTestCase):
    """The main scenario: an established Traqo watch is polled unattended."""

    # -- the request --------------------------------------------------------

    def test_the_watch_is_due_and_dispatched(self):
        due = list(get_due_tracking_subscriptions(self.team))

        self.assertEqual([sub.pk for sub in due], [self.subscription.pk])

    def test_traqo_is_asked_exactly_once_about_this_container_and_sealine(self):
        outcome = self.run_scheduled_sync()

        self.assertEqual(outcome["queued"], [self.subscription.pk])
        self.assertEqual(len(self.session.requests), 1)
        self.assertEqual(self.session.containers_asked, [CONTAINER_NUMBER])
        self.assertEqual(self.session.sealines_asked, [ONE_SCAC])

    def test_the_sealine_comes_from_the_watch_not_from_a_fresh_decision(self):
        self.assertEqual(resolve_watch_sealine(self.subscription), ONE_SCAC)

    # -- what must not happen ----------------------------------------------

    def test_no_step_of_carrier_discovery_is_re_run(self):
        """The watch knows its carrier. Re-deriving it per cycle would be paid for."""
        with (
            mock.patch(_CARRIER_RESOLUTION) as resolution,
            mock.patch(_TRAQO_LOOKUP) as lookup,
            mock.patch(_TRAQO_PROBE) as probe,
            mock.patch(_DIRECT_DISCOVERY) as direct,
            mock.patch(_VIZION_ACI) as vizion,
        ):
            self.run_scheduled_sync()

        resolution.assert_not_called()
        lookup.assert_not_called()
        probe.assert_not_called()
        direct.assert_not_called()
        vizion.assert_not_called()

    def test_no_carrier_adapter_is_built_for_the_aggregator(self):
        """``provider = traqo`` must not be looked up in the carrier registry."""
        with mock.patch("apps.scm.integrations.carriers.factory.build_carrier_client") as build:
            self.run_scheduled_sync()

        build.assert_not_called()

    # -- what the poll produced --------------------------------------------

    def test_a_sync_run_is_recorded_against_the_traqo_provider(self):
        self.run_scheduled_sync()

        run = TrackingSyncRun.objects.get(team=self.team, subscription=self.subscription)
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.NONE)
        self.assertEqual(run.provider.code, TRAQO_PROVIDER_CODE)
        self.assertEqual(run.metadata.get("sealine"), ONE_SCAC)

    def test_the_raw_payload_is_stored_through_the_same_path(self):
        self.run_scheduled_sync()

        payload = TrackingRawPayload.objects.get(team=self.team, subscription=self.subscription)
        self.assertTrue(payload.parsed_successfully)
        self.assertEqual(payload.provider.code, TRAQO_PROVIDER_CODE)
        self.assertEqual(payload.payload_json["data"]["reference_number"], CONTAINER_NUMBER)

    def test_tracking_events_are_stored_for_the_container(self):
        outcome = self.run_scheduled_sync()

        events = TrackingEvent.objects.filter(team=self.team, container=self.container)
        self.assertEqual(events.count(), outcome["results"][0]["events_created"])
        self.assertTrue(events.exists())
        self.assertTrue(all(event.provider.code == TRAQO_PROVIDER_CODE for event in events))

    def test_the_eta_observation_is_processed_on_the_scheduled_path_too(self):
        """Not just on manual refresh: both paths must derive the same visibility."""
        self.run_scheduled_sync()

        self.assertTrue(
            ETAHistory.objects.filter(team=self.team, container=self.container, source=TRAQO_PROVIDER_CODE).exists()
        )

    def test_the_subscription_is_returned_to_active_and_rescheduled(self):
        before = self.subscription.next_sync_at
        self.run_scheduled_sync()

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)
        self.assertEqual(self.subscription.consecutive_failures, 0)
        self.assertEqual(self.subscription.last_error_message, "")
        self.assertIsNotNone(self.subscription.last_synced_at)
        self.assertIsNotNone(self.subscription.last_event_at)
        self.assertGreater(self.subscription.next_sync_at, before)
        self.assertGreater(self.subscription.next_sync_at, timezone.now())

    def test_it_is_no_longer_due_once_polled(self):
        self.run_scheduled_sync()

        self.assertEqual(list(get_due_tracking_subscriptions(self.team)), [])

    def test_the_carrier_identity_on_the_watch_is_left_alone(self):
        """Traqo supplying the data does not make Traqo the carrier."""
        self.run_scheduled_sync()

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.carrier_code, ONE_CODE)
        self.assertEqual(self.subscription.carrier_name, "ONE")
        self.assertEqual(self.subscription.carrier_source, CarrierSource.TRAQO_PROBE)
        self.assertEqual(self.subscription.provider.code, TRAQO_PROVIDER_CODE)


@override_settings(**TRAQO_LIVE)
class TraqoScheduledIdempotencyTest(TraqoScheduledSyncTestCase):
    """Polling the same payload twice must add nothing."""

    def _make_due(self):
        self.subscription.refresh_from_db()
        self.subscription.next_sync_at = timezone.now() - timedelta(minutes=1)
        self.subscription.save(update_fields=["next_sync_at", "updated_at"])

    def _poll_again(self):
        self._make_due()
        self.session = TraqoSession()
        return self.run_scheduled_sync()

    def test_the_same_payload_creates_no_duplicate_events(self):
        first = self.run_scheduled_sync()
        created = first["results"][0]["events_created"]
        event_ids = set(TrackingEvent.objects.filter(team=self.team).values_list("pk", flat=True))

        second = self._poll_again()

        self.assertEqual(second["results"][0]["events_created"], 0)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), created)
        self.assertEqual(set(TrackingEvent.objects.filter(team=self.team).values_list("pk", flat=True)), event_ids)

    def test_both_polls_are_recorded_as_their_own_runs(self):
        """Each attempt is history, whether or not it learned anything new."""
        self.run_scheduled_sync()
        self._poll_again()

        runs = TrackingSyncRun.objects.filter(team=self.team, subscription=self.subscription)
        self.assertEqual(runs.count(), 2)
        self.assertTrue(all(run.status == TrackingSyncRun.Status.SUCCESS for run in runs))

    def test_the_watch_stays_valid_and_is_rescheduled_again(self):
        self.run_scheduled_sync()
        self._poll_again()

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)
        self.assertGreater(self.subscription.next_sync_at, timezone.now())

    def test_a_new_event_is_the_only_thing_a_later_poll_adds(self):
        """Twelve events, then thirteen: one row created, the other twelve kept."""
        first = self.run_scheduled_sync(session=TraqoSession(payload=payload_with_events(12)))
        self.assertEqual(first["results"][0]["events_created"], 12)
        before = set(
            TrackingEvent.objects.filter(team=self.team, container=self.container).values_list("pk", flat=True)
        )

        self._make_due()
        second = self.run_scheduled_sync(session=TraqoSession(payload=payload_with_events(13)))

        self.assertEqual(second["results"][0]["events_created"], 1)
        after = set(TrackingEvent.objects.filter(team=self.team, container=self.container).values_list("pk", flat=True))
        self.assertEqual(len(after), 13)
        self.assertTrue(before.issubset(after))

    def test_the_newest_event_is_what_the_read_models_see(self):
        """A scheduled poll must reach visibility through ordinary event ingestion."""
        from apps.scm.tracking.selectors import get_latest_meaningful_actual_event

        self.run_scheduled_sync(session=TraqoSession(payload=payload_with_events(6)))
        before = get_latest_meaningful_actual_event(self.team, container=self.container)

        self._make_due()
        self.run_scheduled_sync(session=TraqoSession(payload=payload_with_events(7)))

        after = get_latest_meaningful_actual_event(self.team, container=self.container)
        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertNotEqual(before.pk, after.pk)
        self.assertGreater(after.event_datetime, before.event_datetime)


@override_settings(**TRAQO_LIVE)
class TraqoProviderCarrierSeparationTest(TraqoScheduledSyncTestCase):
    """``provider = traqo`` dispatches Traqo. ``carrier_code = one`` dispatches nothing."""

    def test_the_dispatcher_never_looks_for_a_provider_named_after_the_carrier(self):
        self.run_scheduled_sync()

        self.assertFalse(TrackingProvider.objects.filter(code=ONE_CODE).exists())

    def test_the_one_direct_adapter_is_never_built(self):
        """Even with ONE connected for this team, the technical provider is Traqo."""
        Integration.objects.create(
            team=self.team,
            name="ONE",
            provider_code=ONE_CODE,
            provider_family=Integration.ProviderFamily.CARRIER,
            is_active=True,
        )

        with mock.patch("apps.scm.integrations.carriers.factory.build_carrier_client") as build:
            self.run_scheduled_sync()

        build.assert_not_called()
        self.assertEqual(len(self.session.requests), 1)

    def test_the_events_are_attributed_to_traqo_while_the_carrier_stays_one(self):
        self.run_scheduled_sync()

        event = TrackingEvent.objects.filter(team=self.team, container=self.container).first()
        self.assertEqual(event.provider.code, TRAQO_PROVIDER_CODE)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.carrier_code, ONE_CODE)


@override_settings(**TRAQO_LIVE)
class TraqoSealineRecoveryTest(TraqoScheduledSyncTestCase):
    """A watch created before the sealine was persisted, handled without guessing."""

    def test_a_missing_sealine_is_recovered_from_the_recorded_carrier(self):
        self.subscription.provider_reference = ""
        self.subscription.save(update_fields=["provider_reference", "updated_at"])

        self.run_scheduled_sync()

        self.assertEqual(self.session.sealines_asked, [ONE_SCAC])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.provider_reference, ONE_SCAC)

    def test_a_recovered_sealine_is_written_back_once(self):
        self.subscription.provider_reference = ""
        self.subscription.save(update_fields=["provider_reference", "updated_at"])

        resolve_watch_sealine(self.subscription)
        self.subscription.refresh_from_db()
        with mock.patch("apps.scm.tracking.manual_refresh.record_subscription_carrier") as record:
            self.assertEqual(resolve_watch_sealine(self.subscription), ONE_SCAC)

        record.assert_not_called()

    def test_a_watch_with_no_sealine_and_no_carrier_is_a_configuration_problem(self):
        """Never guessed from the container prefix — that is discovery's question."""
        self.subscription.provider_reference = ""
        self.subscription.carrier_code = ""
        self.subscription.carrier_name = ""
        self.subscription.carrier_source = ""
        self.subscription.save(
            update_fields=["provider_reference", "carrier_code", "carrier_name", "carrier_source", "updated_at"]
        )

        outcome = self.run_scheduled_sync()

        self.assertEqual(self.session.requests, [])
        run = TrackingSyncRun.objects.get(team=self.team, subscription=self.subscription)
        self.assertEqual(run.status, TrackingSyncRun.Status.SKIPPED)
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.NOT_CONFIGURED)
        self.assertEqual(outcome["results"][0]["events_created"], 0)

    def test_a_carrier_traqo_publishes_no_sealine_for_is_not_invented(self):
        self.subscription.provider_reference = ""
        self.subscription.carrier_code = "evergreen"
        self.subscription.save(update_fields=["provider_reference", "carrier_code", "updated_at"])

        self.assertEqual(resolve_watch_sealine(self.subscription), "")


@override_settings(**TRAQO_LIVE)
class TraqoScheduledErrorSemanticsTest(TraqoScheduledSyncTestCase):
    """Traqo's failures map onto the engine's existing outcome vocabulary."""

    def setUp(self):
        super().setUp()
        # Events from an earlier poll, so "the watch keeps what it learned" is testable.
        self.run_scheduled_sync()
        self.baseline_events = TrackingEvent.objects.filter(team=self.team).count()
        self.assertGreater(self.baseline_events, 0)
        self.subscription.refresh_from_db()
        self.subscription.next_sync_at = timezone.now() - timedelta(minutes=1)
        self.subscription.save(update_fields=["next_sync_at", "updated_at"])

    def _latest_run(self) -> TrackingSyncRun:
        return TrackingSyncRun.objects.filter(team=self.team, subscription=self.subscription).latest("started_at", "pk")

    def test_no_data_is_a_success_that_withdraws_nothing(self):
        """A container Traqo temporarily has no shipment for is not a lost carrier."""
        self.run_scheduled_sync(session=TraqoSession(answering_sealine="NOPE"))

        run = self._latest_run()
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertTrue(run.metadata.get("no_data"))
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), self.baseline_events)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(self.subscription.carrier_code, ONE_CODE)
        self.assertEqual(self.subscription.provider_reference, ONE_SCAC)
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)

    def test_a_timeout_is_a_transient_failure_that_backs_off(self):
        import requests

        self.run_scheduled_sync(session=TraqoSession(error=requests.exceptions.Timeout("too slow")))

        run = self._latest_run()
        self.assertEqual(run.status, TrackingSyncRun.Status.FAILED)
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.TIMEOUT)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.FAILED)
        self.assertEqual(self.subscription.consecutive_failures, 1)
        self.assertGreater(self.subscription.next_sync_at, timezone.now())
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), self.baseline_events)

    def test_a_server_error_is_a_transient_failure(self):
        session = TraqoSession()
        session.get = lambda url, headers=None, params=None, timeout=None: FakeResponse(503, {"message": "down"})

        self.run_scheduled_sync(session=session)

        run = self._latest_run()
        self.assertEqual(run.status, TrackingSyncRun.Status.FAILED)
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.SERVER_ERROR)

    def test_a_rejected_credential_is_reported_as_authentication(self):
        session = TraqoSession()
        session.get = lambda url, headers=None, params=None, timeout=None: FakeResponse(401, {"message": "nope"})

        self.run_scheduled_sync(session=session)

        run = self._latest_run()
        self.assertEqual(run.status, TrackingSyncRun.Status.FAILED)
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.AUTHENTICATION)

    def test_an_answer_about_a_different_container_is_never_mapped_onto_this_one(self):
        wrong = one_payload()
        wrong["data"]["reference_number"] = "MSCU1234567"

        self.run_scheduled_sync(session=TraqoSession(payload=wrong))

        run = self._latest_run()
        self.assertEqual(run.status, TrackingSyncRun.Status.FAILED)
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.INVALID_RESPONSE)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), self.baseline_events)


@override_settings(**TRAQO_LIVE)
class TraqoScheduledStateMachineTest(TraqoScheduledSyncTestCase):
    """The existing due-selector rules, with no Traqo exceptions."""

    def _assert_not_due(self, status):
        self.subscription.status = status
        self.subscription.save(update_fields=["status", "updated_at"])

        self.assertEqual(list(get_due_tracking_subscriptions(self.team)), [])

        self.run_scheduled_sync()
        self.assertEqual(self.session.requests, [])

    def test_a_paused_watch_is_not_polled(self):
        self._assert_not_due(TrackingSubscription.Status.PAUSED)

    def test_a_completed_watch_is_not_polled(self):
        self._assert_not_due(TrackingSubscription.Status.COMPLETED)

    def test_a_cancelled_watch_is_not_polled(self):
        self._assert_not_due(TrackingSubscription.Status.CANCELLED)

    def test_a_failed_watch_is_retried(self):
        self.subscription.status = TrackingSubscription.Status.FAILED
        self.subscription.consecutive_failures = 2
        self.subscription.save(update_fields=["status", "consecutive_failures", "updated_at"])

        self.run_scheduled_sync()

        self.assertEqual(len(self.session.requests), 1)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(self.subscription.consecutive_failures, 0)

    def test_a_watch_not_yet_due_is_left_alone(self):
        self.subscription.next_sync_at = timezone.now() + timedelta(hours=2)
        self.subscription.save(update_fields=["next_sync_at", "updated_at"])

        self.run_scheduled_sync()

        self.assertEqual(self.session.requests, [])

    def test_a_concurrent_poll_neither_calls_traqo_nor_records_a_run(self):
        """The engine's own lock, taken for a Traqo watch like any other."""
        from apps.scm.integrations.locks import resource_lock
        from apps.scm.tracking.sync import sync_tracking_subscription

        with (
            resource_lock(f"subscription:{self.subscription.pk}", prefix="tracking_sync_lock"),
            mock.patch.object(TraqoClient, "from_settings", return_value=self._client()),
        ):
            run = sync_tracking_subscription(self.subscription)

        self.assertIsNone(run)
        self.assertEqual(self.session.requests, [])
        self.assertEqual(TrackingSyncRun.objects.filter(subscription=self.subscription).count(), 0)


@override_settings(**TRAQO_LIVE)
class TraqoManualAndScheduledConvergenceTest(TraqoScheduledSyncTestCase):
    """The same provider execution, whoever initiated it."""

    def test_manual_refresh_of_an_established_traqo_watch_actually_fetches(self):
        """It used to report ``not_carrier_polled`` and call nothing."""
        from apps.scm.tracking.manual_refresh import UPDATED, refresh_container_tracking

        with (
            mock.patch.object(TraqoClient, "from_settings", return_value=self._client()),
            mock.patch("apps.scm.tracking.continuation.discover_journey_continuation") as continuation,
        ):
            continuation.return_value = mock.Mock(found=False)
            result = refresh_container_tracking(team=self.team, container=self.container)

        self.assertEqual(result.state, UPDATED)
        self.assertEqual(self.session.sealines_asked, [ONE_SCAC])
        self.assertGreater(result.events_created, 0)

    def test_manual_refresh_does_not_re_open_carrier_discovery(self):
        from apps.scm.tracking.manual_refresh import refresh_container_tracking

        with (
            mock.patch.object(TraqoClient, "from_settings", return_value=self._client()),
            mock.patch("apps.scm.tracking.continuation.discover_journey_continuation") as continuation,
            mock.patch(_CARRIER_RESOLUTION) as resolution,
            mock.patch(_TRAQO_LOOKUP) as lookup,
            mock.patch(_TRAQO_PROBE) as probe,
            mock.patch(_VIZION_ACI) as vizion,
        ):
            continuation.return_value = mock.Mock(found=False)
            refresh_container_tracking(team=self.team, container=self.container)

        resolution.assert_not_called()
        lookup.assert_not_called()
        probe.assert_not_called()
        vizion.assert_not_called()

    def test_manual_and_scheduled_refresh_produce_the_same_state(self):
        from apps.scm.tracking.manual_refresh import refresh_container_tracking

        with (
            mock.patch.object(TraqoClient, "from_settings", return_value=self._client()),
            mock.patch("apps.scm.tracking.continuation.discover_journey_continuation") as continuation,
        ):
            continuation.return_value = mock.Mock(found=False)
            refresh_container_tracking(team=self.team, container=self.container)

        manual_events = TrackingEvent.objects.filter(team=self.team, container=self.container).count()
        manual_eta = ETAHistory.objects.filter(team=self.team, source=TRAQO_PROVIDER_CODE).count()

        self.subscription.refresh_from_db()
        self.subscription.next_sync_at = timezone.now() - timedelta(minutes=1)
        self.subscription.save(update_fields=["next_sync_at", "updated_at"])
        self.run_scheduled_sync(session=TraqoSession())

        # The scheduled poll saw the identical payload, so it added nothing — which is
        # the point: the two paths write through the same ingestion, not two of them.
        self.assertEqual(TrackingEvent.objects.filter(team=self.team, container=self.container).count(), manual_events)
        self.assertEqual(ETAHistory.objects.filter(team=self.team, source=TRAQO_PROVIDER_CODE).count(), manual_eta)


@override_settings(**TRAQO_LIVE)
class TraqoAlongsideADirectSubscriptionTest(TestCase):
    """Multi-source tracking: a Traqo watch must not displace a direct one."""

    def setUp(self):
        self.team = Team.objects.create(name="traqo-multi", slug="traqo-multi")
        self.container = _container(self.team)
        self.shipment = Shipment.objects.create(team=self.team, reference="SH-TRAQO-MULTI")
        ShipmentContainer.objects.create(shipment=self.shipment, container=self.container)

        self.maersk_provider = TrackingProvider.objects.get_or_create(code="maersk", defaults={"name": "Maersk"})[0]
        self.maersk = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.maersk_provider,
            container=self.container,
            shipment=self.shipment,
            tracking_reference=CONTAINER_NUMBER,
            reference_type=TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
            carrier_code="maersk",
            carrier_name="Maersk",
            carrier_source=CarrierSource.DIRECT_API,
            status=TrackingSubscription.Status.ACTIVE,
            next_sync_at=timezone.now() - timedelta(minutes=1),
        )
        self.traqo = _traqo_watch(self.team, self.container, shipment=self.shipment)
        self.session = TraqoSession()

    def test_both_are_due_and_each_is_dispatched_once(self):
        due = {sub.pk for sub in get_due_tracking_subscriptions(self.team)}

        self.assertEqual(due, {self.maersk.pk, self.traqo.pk})

    def test_polling_traqo_leaves_the_direct_subscription_untouched(self):
        client = TraqoClient(base_url="https://traqocontainer.com/api/v1", api_key="k", session=self.session)
        before = (self.maersk.status, self.maersk.next_sync_at, self.maersk.carrier_code)

        with mock.patch.object(TraqoClient, "from_settings", return_value=client):
            sync_single_tracking_subscription.run(self.traqo.pk)

        self.maersk.refresh_from_db()
        self.assertEqual((self.maersk.status, self.maersk.next_sync_at, self.maersk.carrier_code), before)
        self.assertEqual(
            TrackingSubscription.objects.filter(team=self.team, container=self.container).count(),
            2,
        )

    def test_the_traqo_poll_does_not_claim_to_be_the_containers_only_source(self):
        client = TraqoClient(base_url="https://traqocontainer.com/api/v1", api_key="k", session=self.session)

        with mock.patch.object(TraqoClient, "from_settings", return_value=client):
            sync_single_tracking_subscription.run(self.traqo.pk)

        self.traqo.refresh_from_db()
        self.assertEqual(self.traqo.carrier_code, ONE_CODE)
        self.maersk.refresh_from_db()
        self.assertEqual(self.maersk.carrier_code, "maersk")
        # Every event the poll stored is Traqo's; nothing was re-attributed.
        events = TrackingEvent.objects.filter(team=self.team, container=self.container)
        self.assertTrue(events.exists())
        self.assertTrue(all(event.provider.code == TRAQO_PROVIDER_CODE for event in events))


class TraqoScheduledSyncWithoutCredentialsTest(TraqoScheduledSyncTestCase):
    """With Traqo disabled, a poll reports a configuration gap and calls nothing.

    No ``override_settings``: this runs under the suite-wide guard that blanks the
    aggregator credentials, which is exactly the state an installation that has not
    enabled Traqo is in.
    """

    def test_nothing_is_called_and_the_gap_is_reported(self):
        queued: list[int] = []
        with mock.patch(_DISPATCH_SYNC, side_effect=lambda pk: queued.append(pk)):
            dispatch_due_tracking_subscriptions.run()
            [sync_single_tracking_subscription.run(pk) for pk in queued]

        self.assertEqual(queued, [self.subscription.pk])
        run = TrackingSyncRun.objects.get(team=self.team, subscription=self.subscription)
        self.assertEqual(run.status, TrackingSyncRun.Status.SKIPPED)
        self.assertEqual(run.error_type, TrackingSyncRun.ErrorType.NOT_CONFIGURED)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)

    def test_the_watch_is_rescheduled_rather_than_spinning(self):
        sync_single_tracking_subscription.run(self.subscription.pk)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(self.subscription.consecutive_failures, 0)
        self.assertGreater(self.subscription.next_sync_at, timezone.now())
