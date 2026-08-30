"""Tests for the tracking sync engine: outcome classification, locking, raw payload
handling and polling schedule.

Every carrier call is a fake injected through the carrier factory; no test opens a
socket.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scm.integrations.carriers.base import BaseCarrierClient, BaseCarrierParser, CarrierCapability
from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
from apps.scm.integrations.carriers.exceptions import (
    CarrierAuthenticationError,
    CarrierConfigurationError,
    CarrierInvalidResponseError,
    CarrierNoDataError,
    CarrierNotImplementedError,
    CarrierRateLimitError,
    CarrierServerError,
    CarrierTimeoutError,
)
from apps.scm.integrations.carriers.registry import UnknownCarrierError
from apps.scm.shipments.models import Shipment
from apps.scm.tracking import polling
from apps.scm.tracking.models import (
    TrackingEvent,
    TrackingProvider,
    TrackingRawPayload,
    TrackingSubscription,
    TrackingSyncRun,
)
from apps.scm.tracking.selectors import get_due_tracking_subscriptions
from apps.scm.tracking.sync import sync_due_tracking_subscriptions, sync_tracking_subscription
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "tracking-sync"}}
_EVENT_TIME = datetime(2024, 3, 10, 8, 0, tzinfo=UTC)

_ErrorType = TrackingSyncRun.ErrorType


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _provider(code: str = "maersk") -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(code=code, defaults={"name": code})[0]


def _subscription(team: Team, provider: TrackingProvider, **kwargs) -> TrackingSubscription:
    defaults = {
        "tracking_reference": "MRKU1234567",
        "reference_type": TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
    }
    defaults.update(kwargs)
    return TrackingSubscription.objects.create(team=team, provider=provider, **defaults)


def _event(**kwargs) -> NormalisedTrackingEvent:
    defaults: dict[str, Any] = {
        "event_type": "EQUIPMENT",
        "event_classifier": "ACT",
        "event_code": "LOAD",
        "event_datetime": _EVENT_TIME,
        "location_name": "Port of Felixstowe",
        "location_unlocode": "GBFXT",
        "container_number": "MRKU1234567",
        "raw_event_id": "EVT-1",
    }
    defaults.update(kwargs)
    return NormalisedTrackingEvent(**defaults)


class FakeClient(BaseCarrierClient):
    """Carrier client that returns a canned payload or raises a canned error."""

    provider_code = "maersk"
    capabilities = CarrierCapability(
        supports_pull=True,
        supports_tracking_by_container=True,
        supports_tracking_by_bl=True,
        supports_tracking_by_booking=True,
    )

    def __init__(self, integration=None, *, payload=None, error=None):
        super().__init__(integration)
        self.payload = payload if payload is not None else {"events": []}
        self.error = error
        self.calls: list[dict] = []

    def fetch_tracking(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.payload


class FakeParser(BaseCarrierParser):
    """Parser that returns canned normalised events or raises."""

    provider_code = "maersk"

    def __init__(self, events=None, error=None):
        self.events = events or []
        self.error = error

    def parse_tracking_events(self, raw_payload):
        if self.error is not None:
            raise self.error
        return list(self.events)


def _patch_adapters(client, parser):
    """Inject a fake client and parser into the sync engine's factory calls."""
    return (
        mock.patch("apps.scm.integrations.carriers.factory.build_carrier_client", return_value=client),
        mock.patch("apps.scm.integrations.carriers.factory.build_carrier_parser", return_value=parser),
    )


def _sync(subscription, *, client=None, parser=None):
    client = client or FakeClient()
    parser = parser or FakeParser()
    client_patch, parser_patch = _patch_adapters(client, parser)
    with client_patch, parser_patch:
        return sync_tracking_subscription(subscription)


@override_settings(CACHES=_LOCMEM)
class SuccessfulSyncTest(TestCase):
    """A carrier that answers with events produces a success and stored events."""

    def setUp(self):
        self.team = _team("sync-ok-team")
        self.provider = _provider()
        self.subscription = _subscription(self.team, self.provider)

    def test_sync_run_is_successful(self):
        run = _sync(self.subscription, parser=FakeParser(events=[_event()]))
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.error_type, _ErrorType.NONE)
        self.assertEqual(run.events_created, 1)

    def test_events_are_persisted_and_linked(self):
        _sync(self.subscription, parser=FakeParser(events=[_event()]))
        event = TrackingEvent.objects.get(team=self.team)
        self.assertEqual(event.subscription_id, self.subscription.pk)
        self.assertIsNotNone(event.raw_payload_id)

    def test_raw_payload_is_marked_parsed_only_after_parsing(self):
        _sync(self.subscription, parser=FakeParser(events=[_event()]))
        payload = TrackingRawPayload.objects.get(team=self.team)
        self.assertTrue(payload.parsed_successfully)
        self.assertEqual(payload.payload_type, TrackingRawPayload.PayloadType.API_RESPONSE)

    def test_subscription_becomes_active_and_tracking(self):
        _sync(self.subscription, parser=FakeParser(events=[_event()]))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)
        self.assertIsNotNone(self.subscription.last_event_at)

    def test_failure_counter_is_reset_on_success(self):
        self.subscription.consecutive_failures = 4
        self.subscription.save()
        _sync(self.subscription, parser=FakeParser(events=[_event()]))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.consecutive_failures, 0)
        self.assertEqual(self.subscription.last_error_message, "")

    def test_next_sync_is_scheduled(self):
        _sync(self.subscription, parser=FakeParser(events=[_event()]))
        self.subscription.refresh_from_db()
        self.assertIsNotNone(self.subscription.next_sync_at)
        self.assertGreater(self.subscription.next_sync_at, timezone.now())

    def test_the_reference_is_sent_as_the_matching_keyword(self):
        client = FakeClient()
        _sync(self.subscription, client=client, parser=FakeParser(events=[_event()]))
        self.assertEqual(client.calls, [{"container_number": "MRKU1234567"}])

    def test_bill_of_lading_subscription_sends_bl_keyword(self):
        subscription = _subscription(
            self.team,
            self.provider,
            tracking_reference="MAEU-BL-1",
            reference_type=TrackingSubscription.ReferenceType.BILL_OF_LADING,
        )
        client = FakeClient()
        _sync(subscription, client=client)
        self.assertEqual(client.calls, [{"bill_of_lading_number": "MAEU-BL-1"}])

    def test_repeated_sync_of_same_payload_creates_no_duplicates(self):
        parser = FakeParser(events=[_event()])
        _sync(self.subscription, parser=parser)
        run = _sync(self.subscription, parser=parser)
        self.assertEqual(run.events_created, 0)
        self.assertEqual(run.events_updated, 1)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 1)


@override_settings(CACHES=_LOCMEM)
class NoDataSyncTest(TestCase):
    """A carrier with nothing to report is a success, not a failure."""

    def setUp(self):
        self.team = _team("sync-nodata-team")
        self.provider = _provider()
        self.subscription = _subscription(self.team, self.provider)

    def test_empty_event_list_is_a_success_with_no_data_status(self):
        run = _sync(self.subscription, client=FakeClient(payload={"events": []}))
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.events_created, 0)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.NO_DATA)

    def test_no_data_error_is_a_success_not_an_error(self):
        run = _sync(self.subscription, client=FakeClient(error=CarrierNoDataError("404 not found")))
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.error_type, _ErrorType.NONE)
        self.assertTrue(run.metadata.get("no_data"))

    def test_no_data_does_not_increment_failures(self):
        _sync(self.subscription, client=FakeClient(error=CarrierNoDataError("404")))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.consecutive_failures, 0)
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)

    def test_no_data_schedules_another_poll(self):
        _sync(self.subscription, client=FakeClient(error=CarrierNoDataError("404")))
        self.subscription.refresh_from_db()
        self.assertIsNotNone(self.subscription.next_sync_at)

    def test_no_data_creates_no_events(self):
        _sync(self.subscription, client=FakeClient(error=CarrierNoDataError("404")))
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)


@override_settings(CACHES=_LOCMEM)
class SkippedSyncTest(TestCase):
    """An adapter that was never called must not look like a successful empty sync."""

    def setUp(self):
        self.team = _team("sync-skip-team")
        self.provider = _provider()
        self.subscription = _subscription(self.team, self.provider)

    def test_unimplemented_client_is_skipped_not_successful(self):
        run = _sync(self.subscription, client=FakeClient(error=CarrierNotImplementedError("stub")))
        self.assertEqual(run.status, TrackingSyncRun.Status.SKIPPED)
        self.assertEqual(run.error_type, _ErrorType.NOT_IMPLEMENTED)

    def test_unimplemented_client_stores_no_payload_and_no_events(self):
        _sync(self.subscription, client=FakeClient(error=CarrierNotImplementedError("stub")))
        self.assertEqual(TrackingRawPayload.objects.filter(team=self.team).count(), 0)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)

    def test_unimplemented_client_sets_not_configured_status(self):
        _sync(self.subscription, client=FakeClient(error=CarrierNotImplementedError("stub")))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.NOT_CONFIGURED)

    def test_skip_does_not_increment_failure_counter(self):
        """A carrier we never called must not accumulate failures."""
        _sync(self.subscription, client=FakeClient(error=CarrierNotImplementedError("stub")))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.consecutive_failures, 0)
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)

    def test_missing_configuration_is_skipped(self):
        run = _sync(self.subscription, client=FakeClient(error=CarrierConfigurationError("no credentials")))
        self.assertEqual(run.status, TrackingSyncRun.Status.SKIPPED)
        self.assertEqual(run.error_type, _ErrorType.NOT_CONFIGURED)

    def test_unimplemented_parser_is_skipped_but_keeps_the_payload(self):
        run = _sync(
            self.subscription,
            client=FakeClient(payload={"events": [{"eventID": "X"}]}),
            parser=FakeParser(error=CarrierNotImplementedError("parser stub")),
        )
        self.assertEqual(run.status, TrackingSyncRun.Status.SKIPPED)
        payload = TrackingRawPayload.objects.get(team=self.team)
        self.assertFalse(payload.parsed_successfully)

    def test_unknown_carrier_is_skipped(self):
        with mock.patch(
            "apps.scm.integrations.carriers.factory.build_carrier_client",
            side_effect=UnknownCarrierError("nope"),
        ):
            run = sync_tracking_subscription(self.subscription)
        self.assertEqual(run.status, TrackingSyncRun.Status.SKIPPED)
        self.assertEqual(run.error_type, _ErrorType.NOT_CONFIGURED)

    def test_manual_subscription_is_not_polled(self):
        manual = _subscription(
            self.team,
            self.provider,
            reference_type=TrackingSubscription.ReferenceType.MANUAL,
            tracking_reference="manual-1",
        )
        run = _sync(manual)
        self.assertEqual(run.status, TrackingSyncRun.Status.SKIPPED)

    def test_a_provider_this_poller_does_not_drive_is_told_apart_from_a_broken_one(self):
        """Traqo is a working provider the carrier poller is simply not the caller for."""
        traqo = _subscription(self.team, _provider("traqo"), tracking_reference="CPWU2588297")

        run = sync_tracking_subscription(traqo)

        self.assertEqual(run.status, TrackingSyncRun.Status.SKIPPED)
        self.assertEqual(run.error_type, _ErrorType.NOT_CARRIER_POLLED)

    def test_skipping_it_leaves_the_subscription_tracking(self):
        traqo = _subscription(
            self.team,
            _provider("traqo"),
            tracking_reference="CPWU2588297",
            tracking_status=TrackingSubscription.TrackingStatus.TRACKING,
        )

        sync_tracking_subscription(traqo)

        traqo.refresh_from_db()
        # NOT_CONFIGURED here would tell the UI the container cannot be tracked, when
        # its events are already stored and correct.
        self.assertEqual(traqo.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)
        self.assertEqual(traqo.status, TrackingSubscription.Status.ACTIVE)
        self.assertEqual(traqo.consecutive_failures, 0)

    def test_skipping_it_records_no_error_against_the_subscription(self):
        traqo = _subscription(self.team, _provider("traqo"), tracking_reference="CPWU2588297")

        run = sync_tracking_subscription(traqo)

        traqo.refresh_from_db()
        self.assertEqual(traqo.last_error_message, "")
        # The explanation lives on the run, where it is information rather than a fault.
        self.assertIn("refresh_with", run.metadata)

    def test_it_never_reaches_the_carrier_factory(self):
        traqo = _subscription(self.team, _provider("traqo"), tracking_reference="CPWU2588297")

        with mock.patch("apps.scm.integrations.carriers.factory.build_carrier_client") as build:
            sync_tracking_subscription(traqo)

        build.assert_not_called()

    def test_the_scheduled_poller_does_not_queue_it_at_all(self):
        _subscription(self.team, _provider("traqo"), tracking_reference="CPWU2588297")

        due = list(get_due_tracking_subscriptions(self.team))

        self.assertEqual([sub.pk for sub in due], [self.subscription.pk])

    def test_a_misconfigured_carrier_is_still_reported_as_not_configured(self):
        """The safe skip must not become a way for real carrier faults to go quiet."""
        with mock.patch(
            "apps.scm.integrations.carriers.factory.build_carrier_client",
            side_effect=UnknownCarrierError("nope"),
        ):
            run = sync_tracking_subscription(self.subscription)

        self.subscription.refresh_from_db()
        self.assertEqual(run.error_type, _ErrorType.NOT_CONFIGURED)
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.NOT_CONFIGURED)

    def test_skipped_run_schedules_a_later_retry(self):
        _sync(self.subscription, client=FakeClient(error=CarrierNotImplementedError("stub")))
        self.subscription.refresh_from_db()
        self.assertIsNotNone(self.subscription.next_sync_at)
        # Not configured is polled rarely — well beyond the in-transit interval.
        self.assertGreater(
            self.subscription.next_sync_at,
            timezone.now() + timedelta(minutes=polling.INTERVAL_IN_TRANSIT),
        )


@override_settings(CACHES=_LOCMEM)
class FailedSyncTest(TestCase):
    """Each failure kind is classified so it can be told apart and acted on."""

    def setUp(self):
        self.team = _team("sync-fail-team")
        self.provider = _provider()
        self.subscription = _subscription(self.team, self.provider)

    def _run_with_error(self, error) -> TrackingSyncRun:
        return _sync(self.subscription, client=FakeClient(error=error))

    def test_authentication_error_is_classified(self):
        run = self._run_with_error(CarrierAuthenticationError("401 Unauthorized"))
        self.assertEqual(run.status, TrackingSyncRun.Status.FAILED)
        self.assertEqual(run.error_type, _ErrorType.AUTHENTICATION)

    def test_timeout_is_classified(self):
        run = self._run_with_error(CarrierTimeoutError("timed out"))
        self.assertEqual(run.error_type, _ErrorType.TIMEOUT)

    def test_server_error_is_classified(self):
        run = self._run_with_error(CarrierServerError("503", status_code=503))
        self.assertEqual(run.error_type, _ErrorType.SERVER_ERROR)

    def test_rate_limit_is_classified(self):
        run = self._run_with_error(CarrierRateLimitError("429", retry_after=60))
        self.assertEqual(run.error_type, _ErrorType.RATE_LIMIT)

    def test_invalid_response_is_classified(self):
        run = self._run_with_error(CarrierInvalidResponseError("not json"))
        self.assertEqual(run.error_type, _ErrorType.INVALID_RESPONSE)

    def test_failure_marks_subscription_failed_and_counts(self):
        self._run_with_error(CarrierAuthenticationError("401"))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.FAILED)
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.ERROR)
        self.assertEqual(self.subscription.consecutive_failures, 1)
        self.assertIn("401", self.subscription.last_error_message)

    def test_failure_creates_no_events(self):
        self._run_with_error(CarrierTimeoutError("timeout"))
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 0)

    def test_rate_limit_respects_retry_after(self):
        self._run_with_error(CarrierRateLimitError("429", retry_after=7200))
        self.subscription.refresh_from_db()
        # Retry-After is a lower bound: never come back sooner than asked.
        self.assertGreaterEqual(self.subscription.next_sync_at, timezone.now() + timedelta(seconds=7100))

    def test_repeated_failures_back_off(self):
        for _ in range(3):
            self._run_with_error(CarrierServerError("503"))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.consecutive_failures, 3)
        self.assertGreater(
            self.subscription.next_sync_at,
            timezone.now() + timedelta(minutes=polling.BACKOFF_BASE_MINUTES),
        )

    def test_parser_error_keeps_payload_unparsed_and_fails(self):
        run = _sync(
            self.subscription,
            client=FakeClient(payload={"events": [{"eventID": "X"}]}),
            parser=FakeParser(error=ValueError("bad shape")),
        )
        self.assertEqual(run.status, TrackingSyncRun.Status.FAILED)
        self.assertEqual(run.error_type, _ErrorType.PARSE_ERROR)
        payload = TrackingRawPayload.objects.get(team=self.team)
        self.assertFalse(payload.parsed_successfully)
        self.assertIn("bad shape", payload.error_message)

    def test_unexpected_error_is_classified_and_run_is_closed(self):
        run = self._run_with_error(RuntimeError("something odd"))
        self.assertEqual(run.status, TrackingSyncRun.Status.FAILED)
        self.assertEqual(run.error_type, _ErrorType.UNEXPECTED)
        self.assertIsNotNone(run.finished_at)

    def test_empty_reference_is_rejected(self):
        subscription = _subscription(self.team, self.provider, tracking_reference="   ")
        run = _sync(subscription)
        self.assertEqual(run.status, TrackingSyncRun.Status.FAILED)
        self.assertEqual(run.error_type, _ErrorType.UNSUPPORTED_REFERENCE)


@override_settings(CACHES=_LOCMEM)
class SyncLockingTest(TestCase):
    """The same subscription cannot be synced twice at once."""

    def setUp(self):
        self.team = _team("sync-lock-team")
        self.provider = _provider()
        self.subscription = _subscription(self.team, self.provider)

    def test_second_concurrent_sync_is_refused(self):
        """A nested sync of the same subscription returns None and records no run."""
        outer_client = FakeClient()
        inner_result = {}

        def fetch_then_try_again(**kwargs):
            inner_result["run"] = _sync(self.subscription)
            return {"events": []}

        outer_client.fetch_tracking = fetch_then_try_again
        run = _sync(self.subscription, client=outer_client)

        self.assertIsNotNone(run)
        self.assertIsNone(inner_result["run"], "The second concurrent sync must be refused")
        self.assertEqual(TrackingSyncRun.objects.filter(subscription=self.subscription).count(), 1)

    def test_different_subscriptions_sync_independently(self):
        other = _subscription(self.team, self.provider, tracking_reference="MRKU7654321")
        inner = {}

        client = FakeClient()

        def fetch_then_sync_other(**kwargs):
            inner["run"] = _sync(other)
            return {"events": []}

        client.fetch_tracking = fetch_then_sync_other
        _sync(self.subscription, client=client)
        self.assertIsNotNone(inner["run"])

    def test_status_alone_does_not_block_a_sync(self):
        """Locking, not the SYNCING status, is what prevents double runs."""
        self.subscription.status = TrackingSubscription.Status.SYNCING
        self.subscription.save()
        run = _sync(self.subscription)
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)


@override_settings(CACHES=_LOCMEM)
class TerminalShipmentTest(TestCase):
    """Watching stops once the shipment is finished."""

    def setUp(self):
        self.team = _team("sync-terminal-team")
        self.provider = _provider()

    def test_delivered_shipment_completes_the_subscription(self):
        shipment = Shipment.objects.create(
            team=self.team, shipment_number="SHP-TERM-1", status=Shipment.Status.DELIVERED
        )
        subscription = _subscription(self.team, self.provider, shipment=shipment)
        _sync(subscription)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, TrackingSubscription.Status.COMPLETED)

    def test_in_transit_shipment_keeps_the_subscription_active(self):
        shipment = Shipment.objects.create(
            team=self.team, shipment_number="SHP-TERM-2", status=Shipment.Status.IN_TRANSIT
        )
        subscription = _subscription(self.team, self.provider, shipment=shipment)
        _sync(subscription)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, TrackingSubscription.Status.ACTIVE)

    def test_completed_subscription_is_not_due(self):
        shipment = Shipment.objects.create(
            team=self.team, shipment_number="SHP-TERM-3", status=Shipment.Status.DELIVERED
        )
        subscription = _subscription(self.team, self.provider, shipment=shipment)
        _sync(subscription)
        self.assertNotIn(subscription, get_due_tracking_subscriptions(self.team))


@override_settings(CACHES=_LOCMEM)
class DueSubscriptionSelectionTest(TestCase):
    """Due detection must not starve a subscription whose worker died."""

    def setUp(self):
        self.team = _team("sync-due-team")
        self.provider = _provider()

    def test_new_subscription_is_due(self):
        subscription = _subscription(self.team, self.provider)
        self.assertIn(subscription, get_due_tracking_subscriptions(self.team))

    def test_future_next_sync_is_not_due(self):
        subscription = _subscription(self.team, self.provider, next_sync_at=timezone.now() + timedelta(hours=1))
        self.assertNotIn(subscription, get_due_tracking_subscriptions(self.team))

    def test_paused_subscription_is_not_due(self):
        subscription = _subscription(self.team, self.provider, status=TrackingSubscription.Status.PAUSED)
        self.assertNotIn(subscription, get_due_tracking_subscriptions(self.team))

    def test_failed_subscription_is_retried_when_due(self):
        subscription = _subscription(self.team, self.provider, status=TrackingSubscription.Status.FAILED)
        self.assertIn(subscription, get_due_tracking_subscriptions(self.team))

    def test_recently_syncing_subscription_is_not_picked_up_again(self):
        subscription = _subscription(self.team, self.provider, status=TrackingSubscription.Status.SYNCING)
        self.assertNotIn(subscription, get_due_tracking_subscriptions(self.team))

    def test_stale_syncing_subscription_is_recovered(self):
        """A crashed sync must not leave the subscription stuck forever."""
        subscription = _subscription(self.team, self.provider, status=TrackingSubscription.Status.SYNCING)
        stale = timezone.now() - timedelta(minutes=polling.INTERVAL_NOT_CONFIGURED)
        TrackingSubscription.objects.filter(pk=subscription.pk).update(updated_at=stale)
        self.assertIn(subscription, get_due_tracking_subscriptions(self.team))

    def test_other_teams_subscriptions_are_not_returned(self):
        other_team = _team("sync-due-other-team")
        mine = _subscription(self.team, self.provider)
        theirs = _subscription(other_team, self.provider)
        due = list(get_due_tracking_subscriptions(self.team))
        self.assertIn(mine, due)
        self.assertNotIn(theirs, due)


@override_settings(CACHES=_LOCMEM)
class SyncBatchTest(TestCase):
    """The batch dispatcher summarises outcomes and never dies on one subscription."""

    def setUp(self):
        self.team = _team("sync-batch-team")
        self.provider = _provider()

    def test_summary_counts_success_and_skip(self):
        _subscription(self.team, self.provider, tracking_reference="MRKU1234567")
        _subscription(self.team, self.provider, tracking_reference="MRKU7654321")
        client_patch, parser_patch = _patch_adapters(FakeClient(), FakeParser())
        with client_patch, parser_patch:
            summary = sync_due_tracking_subscriptions()
        self.assertEqual(summary["total"], summary["successes"] + summary["failures"] + summary["skipped"])
        self.assertGreaterEqual(summary["successes"], 2)

    def test_one_broken_subscription_does_not_stop_the_batch(self):
        _subscription(self.team, self.provider, tracking_reference="MRKU1234567")
        with mock.patch(
            "apps.scm.tracking.sync.sync_tracking_subscription",
            side_effect=RuntimeError("boom"),
        ):
            summary = sync_due_tracking_subscriptions()
        self.assertIsInstance(summary, dict)
        self.assertGreaterEqual(summary["failures"], 1)

    def test_empty_queue_returns_zero_summary(self):
        summary = sync_due_tracking_subscriptions()
        self.assertEqual(summary["total"], summary["successes"] + summary["failures"] + summary["skipped"])


class PollingPolicyTest(TestCase):
    """Interval selection follows the shipment's actual state."""

    def setUp(self):
        self.team = _team("polling-team")
        self.provider = _provider()

    def test_before_first_event_is_polled_slowly(self):
        subscription = _subscription(self.team, self.provider)
        self.assertEqual(polling.base_interval_minutes(subscription), polling.INTERVAL_BEFORE_FIRST_EVENT)

    def test_in_transit_is_polled_normally(self):
        subscription = _subscription(self.team, self.provider, last_event_at=timezone.now())
        self.assertEqual(polling.base_interval_minutes(subscription), polling.INTERVAL_IN_TRANSIT)

    def test_after_arrival_is_polled_less_often(self):
        shipment = Shipment.objects.create(
            team=self.team, shipment_number="SHP-POLL-1", actual_arrival_at=timezone.now()
        )
        subscription = _subscription(self.team, self.provider, shipment=shipment, last_event_at=timezone.now())
        self.assertEqual(polling.base_interval_minutes(subscription), polling.INTERVAL_AFTER_ARRIVAL)

    def test_not_configured_is_polled_rarely(self):
        subscription = _subscription(
            self.team,
            self.provider,
            tracking_status=TrackingSubscription.TrackingStatus.NOT_CONFIGURED,
        )
        self.assertEqual(polling.base_interval_minutes(subscription), polling.INTERVAL_NOT_CONFIGURED)

    def test_explicit_override_wins(self):
        subscription = _subscription(self.team, self.provider, sync_interval_minutes=5)
        self.assertEqual(polling.base_interval_minutes(subscription), 5)

    def test_carrier_minimum_interval_is_respected(self):
        subscription = _subscription(self.team, self.provider, sync_interval_minutes=1)
        scheduled = polling.next_sync_at(subscription, integration_config={"min_poll_interval_minutes": 120})
        self.assertGreaterEqual(scheduled, timezone.now() + timedelta(minutes=110))

    def test_backoff_grows_with_consecutive_failures(self):
        early = _subscription(self.team, self.provider, consecutive_failures=1, last_event_at=timezone.now())
        late = _subscription(self.team, self.provider, consecutive_failures=6, last_event_at=timezone.now())
        self.assertGreater(
            polling.next_sync_at(late),
            polling.next_sync_at(early),
        )

    def test_backoff_is_capped(self):
        subscription = _subscription(self.team, self.provider, consecutive_failures=50)
        scheduled = polling.next_sync_at(subscription)
        self.assertLess(scheduled, timezone.now() + timedelta(minutes=polling.MAX_BACKOFF_MINUTES * 1.5))

    def test_jitter_spreads_identical_subscriptions(self):
        """Two identical subscriptions must not retry at the same instant."""
        subscription = _subscription(self.team, self.provider, last_event_at=timezone.now())
        times = {polling.next_sync_at(subscription) for _ in range(20)}
        self.assertGreater(len(times), 1)
