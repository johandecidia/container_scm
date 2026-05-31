"""Tests for tracking services."""

from django.test import TestCase
from django.utils import timezone

from apps.scm.tracking.models import (
    TrackingEvent,
    TrackingProvider,
    TrackingSubscription,
    TrackingSyncRun,
)
from apps.scm.tracking.services import (
    cancel_tracking_subscription,
    complete_tracking_subscription,
    create_sync_run,
    create_tracking_subscription,
    deduplicate_tracking_event,
    finish_sync_run_failed,
    finish_sync_run_success,
    pause_tracking_subscription,
    resume_tracking_subscription,
    store_raw_payload,
    update_subscription_sync_state,
    upsert_tracking_event,
)
from apps.teams.models import Team


def _team(slug):
    return Team.objects.create(name=slug, slug=slug)


def _provider(code="SVC_PROV"):
    return TrackingProvider.objects.create(
        code=code,
        name=f"Provider {code}",
        provider_type=TrackingProvider.ProviderType.MANUAL,
    )


class CreateTrackingSubscriptionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("svc-create-track-team")
        cls.provider = _provider("SVC_CREATE_PROV")

    def test_creates_subscription(self):
        sub = create_tracking_subscription(self.team, self.provider, "MSKU1234567")
        self.assertIsNotNone(sub.pk)
        self.assertEqual(sub.team, self.team)
        self.assertEqual(sub.provider, self.provider)
        self.assertEqual(sub.tracking_reference, "MSKU1234567")

    def test_default_status_active(self):
        sub = create_tracking_subscription(self.team, self.provider, "MSKU9999991")
        self.assertEqual(sub.status, TrackingSubscription.Status.ACTIVE)


class PauseResumeTrackingSubscriptionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("svc-pause-team")
        cls.provider = _provider("SVC_PAUSE_PROV")

    def setUp(self):
        self.sub = create_tracking_subscription(self.team, self.provider, "PAUSE-REF")

    def test_pause(self):
        pause_tracking_subscription(self.sub)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, TrackingSubscription.Status.PAUSED)

    def test_resume(self):
        pause_tracking_subscription(self.sub)
        resume_tracking_subscription(self.sub)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, TrackingSubscription.Status.ACTIVE)

    def test_complete(self):
        complete_tracking_subscription(self.sub)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, TrackingSubscription.Status.COMPLETED)

    def test_cancel(self):
        cancel_tracking_subscription(self.sub)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, TrackingSubscription.Status.CANCELLED)


class StoreRawPayloadTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("svc-raw-team")
        cls.provider = _provider("SVC_RAW_PROV")

    def test_stores_payload(self):
        payload = {"data": "test"}
        raw = store_raw_payload(self.team, self.provider, payload)
        self.assertIsNotNone(raw.pk)
        self.assertEqual(raw.payload_json, payload)

    def test_payload_hash_set(self):
        raw = store_raw_payload(self.team, self.provider, {"a": 1})
        self.assertTrue(len(raw.payload_hash) == 64)  # SHA-256 hex


class UpsertTrackingEventTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("svc-upsert-team")
        cls.provider = _provider("SVC_UPSERT_PROV")
        cls.sub = TrackingSubscription.objects.create(
            team=cls.team, provider=cls.provider, tracking_reference="UPSERT-REF"
        )

    def test_creates_new_event(self):
        event, created = upsert_tracking_event(
            team=self.team,
            provider=self.provider,
            event_type=TrackingEvent.EventType.GATE_IN,
            event_datetime=timezone.now(),
            source_event_id="SRC-001",
        )
        self.assertTrue(created)
        self.assertIsNotNone(event.pk)

    def test_deduplicates_by_source_event_id(self):
        dt = timezone.now()
        upsert_tracking_event(
            team=self.team,
            provider=self.provider,
            event_type=TrackingEvent.EventType.GATE_IN,
            event_datetime=dt,
            source_event_id="SRC-DEDUP-001",
        )
        _event2, created2 = upsert_tracking_event(
            team=self.team,
            provider=self.provider,
            event_type=TrackingEvent.EventType.GATE_IN,
            event_datetime=dt,
            source_event_id="SRC-DEDUP-001",
        )
        self.assertFalse(created2)
        self.assertEqual(TrackingEvent.objects.filter(source_event_id="SRC-DEDUP-001", team=self.team).count(), 1)

    def test_deduplicates_fallback_without_source_event_id(self):
        dt = timezone.now()
        upsert_tracking_event(
            team=self.team,
            provider=self.provider,
            event_type=TrackingEvent.EventType.DISCHARGED,
            event_datetime=dt,
            subscription=self.sub,
        )
        _event2, created2 = upsert_tracking_event(
            team=self.team,
            provider=self.provider,
            event_type=TrackingEvent.EventType.DISCHARGED,
            event_datetime=dt,
            subscription=self.sub,
        )
        self.assertFalse(created2)


class DeduplicateTrackingEventTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("svc-dedup-team")
        cls.provider = _provider("SVC_DEDUP_PROV")

    def test_returns_true_when_duplicate_by_source_id(self):
        TrackingEvent.objects.create(
            team=self.team,
            provider=self.provider,
            event_type=TrackingEvent.EventType.GATE_OUT,
            source_event_id="DEDUP-SRC-99",
        )
        self.assertTrue(deduplicate_tracking_event(self.team, self.provider, source_event_id="DEDUP-SRC-99"))

    def test_returns_false_when_no_duplicate(self):
        self.assertFalse(deduplicate_tracking_event(self.team, self.provider, source_event_id="NONEXISTENT-SRC"))


class UpdateSubscriptionSyncStateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("svc-syncstate-team")
        cls.provider = _provider("SVC_SS_PROV")

    def setUp(self):
        self.sub = create_tracking_subscription(self.team, self.provider, "SS-REF")

    def test_success_resets_failure_counter(self):
        self.sub.consecutive_failures = 3
        self.sub.save()
        update_subscription_sync_state(self.sub, success=True)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.consecutive_failures, 0)
        self.assertEqual(self.sub.status, TrackingSubscription.Status.ACTIVE)

    def test_failure_increments_counter(self):
        update_subscription_sync_state(self.sub, success=False, error_message="timeout")
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.consecutive_failures, 1)
        self.assertEqual(self.sub.status, TrackingSubscription.Status.FAILED)

    def test_repeated_failures_accumulate(self):
        update_subscription_sync_state(self.sub, success=False, error_message="err1")
        update_subscription_sync_state(self.sub, success=False, error_message="err2")
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.consecutive_failures, 2)


class SyncRunServiceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("svc-run-team")
        cls.provider = _provider("SVC_RUN_PROV")
        cls.sub = TrackingSubscription.objects.create(
            team=cls.team, provider=cls.provider, tracking_reference="RUN-REF"
        )

    def test_create_sync_run_marks_syncing(self):
        run = create_sync_run(self.team, self.sub, self.provider)
        self.assertIsNotNone(run.pk)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, TrackingSubscription.Status.SYNCING)

    def test_finish_success(self):
        run = create_sync_run(self.team, self.sub, self.provider)
        finish_sync_run_success(run, events_created=5, events_updated=2)
        run.refresh_from_db()
        self.assertEqual(run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(run.events_created, 5)
        self.assertEqual(run.events_updated, 2)

    def test_finish_failed(self):
        run = TrackingSyncRun.objects.create(team=self.team, provider=self.provider, subscription=self.sub)
        finish_sync_run_failed(run, error_message="network error")
        run.refresh_from_db()
        self.assertEqual(run.status, TrackingSyncRun.Status.FAILED)
        self.assertEqual(run.error_message, "network error")
