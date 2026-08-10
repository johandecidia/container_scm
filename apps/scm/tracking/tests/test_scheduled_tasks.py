"""Tests for the scheduled tracking and discovery tasks, and for raw payload
retention.

Dispatchers must pass IDs (never model instances), stay team-isolated, and never
silently truncate their work.
"""

from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scm.containers.discovery import add_planned_container
from apps.scm.containers.models import PlannedContainer, PlannedContainerStatus
from apps.scm.containers.tasks import (
    discover_planned_containers_for_team,
    dispatch_planned_container_discovery,
    expire_stale_planned_containers,
)
from apps.scm.integrations.tasks import (
    dispatch_shipment_container_discovery_task,
    sync_enabled_business_central_integrations_task,
)
from apps.scm.shipments.models import Shipment
from apps.scm.tracking.models import TrackingProvider, TrackingRawPayload, TrackingSubscription
from apps.scm.tracking.retention import archive_old_raw_payloads, delete_expired_raw_payloads
from apps.scm.tracking.tasks import (
    apply_tracking_raw_payload_retention,
    dispatch_due_tracking_subscriptions,
)
from apps.teams.models import Team

MRKU_1 = "MRKU1234563"
MRKU_2 = "MRKU2345685"

_DISPATCH_SYNC = "apps.scm.tracking.tasks.sync_single_tracking_subscription.delay"
_DISPATCH_DISCOVERY = "apps.scm.containers.tasks.discover_planned_containers_for_team.delay"
_DISPATCH_SHIPMENT = "apps.scm.integrations.tasks.discover_containers_for_open_shipments_task.delay"


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _provider(code: str = "maersk") -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(code=code, defaults={"name": code})[0]


def _subscription(team: Team, provider: TrackingProvider, **kwargs) -> TrackingSubscription:
    defaults = {"tracking_reference": MRKU_1}
    defaults.update(kwargs)
    return TrackingSubscription.objects.create(team=team, provider=provider, **defaults)


def _payload(team: Team, provider: TrackingProvider, *, age_days: int, **kwargs) -> TrackingRawPayload:
    payload = TrackingRawPayload.objects.create(
        team=team,
        provider=provider,
        payload_json={"events": [{"eventID": "E1"}]},
        payload_hash="hash-" + str(age_days),
        received_at=timezone.now() - timedelta(days=age_days),
        **kwargs,
    )
    return payload


class TrackingDispatcherTest(TestCase):
    """The scheduled dispatcher queues one task per due subscription, by ID."""

    def setUp(self):
        self.team = _team("dispatch-tracking-team")
        self.provider = _provider()

    def test_due_subscriptions_are_queued_by_id(self):
        subscription = _subscription(self.team, self.provider)
        with mock.patch(_DISPATCH_SYNC) as delay:
            result = dispatch_due_tracking_subscriptions.run()
        delay.assert_called_once_with(subscription.pk)
        self.assertEqual(result["queued"], 1)
        self.assertFalse(result["capped"])

    def test_tasks_receive_ids_not_model_instances(self):
        _subscription(self.team, self.provider)
        with mock.patch(_DISPATCH_SYNC) as delay:
            dispatch_due_tracking_subscriptions.run()
        for call in delay.call_args_list:
            self.assertIsInstance(call.args[0], int)

    def test_subscriptions_not_yet_due_are_not_queued(self):
        _subscription(self.team, self.provider, next_sync_at=timezone.now() + timedelta(hours=2))
        with mock.patch(_DISPATCH_SYNC) as delay:
            result = dispatch_due_tracking_subscriptions.run()
        delay.assert_not_called()
        self.assertEqual(result["queued"], 0)

    def test_paused_subscriptions_are_not_queued(self):
        _subscription(self.team, self.provider, status=TrackingSubscription.Status.PAUSED)
        with mock.patch(_DISPATCH_SYNC) as delay:
            dispatch_due_tracking_subscriptions.run()
        delay.assert_not_called()

    def test_dispatch_covers_every_team(self):
        other_team = _team("dispatch-tracking-other-team")
        _subscription(self.team, self.provider)
        _subscription(other_team, self.provider, tracking_reference=MRKU_2)
        with mock.patch(_DISPATCH_SYNC) as delay:
            result = dispatch_due_tracking_subscriptions.run()
        self.assertEqual(result["queued"], 2)
        self.assertEqual(delay.call_count, 2)

    def test_cap_is_reported_not_hidden(self):
        """A truncated dispatch must not look like a complete one."""
        _subscription(self.team, self.provider)
        _subscription(self.team, self.provider, tracking_reference=MRKU_2)
        with mock.patch(_DISPATCH_SYNC) as delay:
            result = dispatch_due_tracking_subscriptions.run(limit=1)
        self.assertEqual(delay.call_count, 1)
        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["due"], 2)
        self.assertTrue(result["capped"])

    def test_default_limit_comes_from_settings(self):
        self.assertIsNotNone(getattr(settings, "SCM_TRACKING_DISPATCH_LIMIT", None))


class PlannedContainerDispatcherTest(TestCase):
    def setUp(self):
        self.team = _team("dispatch-planned-team")

    def test_teams_with_work_are_queued_once(self):
        add_planned_container(self.team, MRKU_1)
        add_planned_container(self.team, MRKU_2)
        with mock.patch(_DISPATCH_DISCOVERY) as delay:
            result = dispatch_planned_container_discovery.run()
        delay.assert_called_once_with(self.team.pk)
        self.assertEqual(result["queued"], 1)

    def test_teams_without_work_are_not_queued(self):
        with mock.patch(_DISPATCH_DISCOVERY) as delay:
            result = dispatch_planned_container_discovery.run()
        delay.assert_not_called()
        self.assertEqual(result["queued"], 0)

    def test_each_team_gets_its_own_task(self):
        other = _team("dispatch-planned-other-team")
        add_planned_container(self.team, MRKU_1)
        add_planned_container(other, MRKU_1)
        with mock.patch(_DISPATCH_DISCOVERY) as delay:
            result = dispatch_planned_container_discovery.run()
        self.assertEqual(result["queued"], 2)
        self.assertEqual({call.args[0] for call in delay.call_args_list}, {self.team.pk, other.pk})

    def test_per_team_task_loads_the_team_itself(self):
        add_planned_container(self.team, MRKU_1)
        summary = discover_planned_containers_for_team.run(self.team.pk)
        self.assertEqual(summary["checked"], 1)

    def test_missing_team_is_skipped_not_raised(self):
        summary = discover_planned_containers_for_team.run(999999)
        self.assertEqual(summary["checked"], 0)

    def test_expiry_task_expires_exhausted_numbers(self):
        planned = add_planned_container(self.team, MRKU_1, max_attempts=1)
        PlannedContainer.objects.filter(pk=planned.pk).update(attempts=1)
        self.assertEqual(expire_stale_planned_containers.run(), 1)
        planned.refresh_from_db()
        self.assertEqual(planned.status, PlannedContainerStatus.EXPIRED)


class ShipmentDiscoveryDispatcherTest(TestCase):
    def setUp(self):
        self.team = _team("dispatch-shipment-team")

    def _shipment(self, team, **kwargs):
        defaults = {"shipment_number": f"SHP-{team.slug}", "carrier": "Maersk", "carrier_booking_reference": "BKG-1"}
        defaults.update(kwargs)
        return Shipment.objects.create(team=team, **defaults)

    def test_team_with_candidate_shipment_is_queued(self):
        self._shipment(self.team)
        with mock.patch(_DISPATCH_SHIPMENT) as delay:
            result = dispatch_shipment_container_discovery_task.run()
        delay.assert_called_once_with(self.team.pk)
        self.assertEqual(result["queued"], 1)

    def test_two_candidate_shipments_queue_their_team_once(self):
        """DISTINCT must collapse to one task per team, not one per candidate shipment."""
        self._shipment(self.team, shipment_number="SHP-D1", carrier_booking_reference="BKG-D1")
        self._shipment(self.team, shipment_number="SHP-D2", carrier_booking_reference="BKG-D2")
        with mock.patch(_DISPATCH_SHIPMENT) as delay:
            result = dispatch_shipment_container_discovery_task.run()
        self.assertEqual([call.args[0] for call in delay.call_args_list], [self.team.pk])
        self.assertEqual(result["queued"], 1)

    def test_shipment_without_carrier_does_not_queue_its_team(self):
        self._shipment(self.team, carrier="")
        with mock.patch(_DISPATCH_SHIPMENT) as delay:
            result = dispatch_shipment_container_discovery_task.run()
        delay.assert_not_called()
        self.assertEqual(result["queued"], 0)

    def test_delivered_shipment_does_not_queue_its_team(self):
        self._shipment(self.team, status=Shipment.Status.DELIVERED)
        with mock.patch(_DISPATCH_SHIPMENT) as delay:
            dispatch_shipment_container_discovery_task.run()
        delay.assert_not_called()


class BusinessCentralDispatchToggleTest(TestCase):
    """Epic 1's scheduled sync can be paused by configuration, not by deletion."""

    @override_settings(SCM_BUSINESS_CENTRAL_DISPATCH_ENABLED=False)
    def test_disabled_dispatcher_queues_nothing(self):
        with mock.patch("apps.scm.integrations.tasks.sync_business_central_purchase_orders_task.delay") as delay:
            result = sync_enabled_business_central_integrations_task.run()
        delay.assert_not_called()
        self.assertTrue(result["disabled"])
        self.assertEqual(result["queued"], 0)

    @override_settings(SCM_BUSINESS_CENTRAL_DISPATCH_ENABLED=True)
    def test_enabled_dispatcher_still_runs(self):
        result = sync_enabled_business_central_integrations_task.run()
        self.assertFalse(result["disabled"])


class RawPayloadRetentionTest(TestCase):
    """Retention archives the body and keeps the audit record."""

    def setUp(self):
        self.team = _team("retention-team")
        self.provider = _provider()

    def test_old_payload_body_is_archived(self):
        payload = _payload(self.team, self.provider, age_days=200)
        self.assertEqual(archive_old_raw_payloads(days=90), 1)
        payload.refresh_from_db()
        self.assertTrue(payload.is_archived)
        self.assertEqual(payload.payload_json, {"_archived": True})

    def test_archiving_keeps_the_audit_metadata(self):
        """The record is often the only evidence of what the carrier said."""
        payload = _payload(self.team, self.provider, age_days=200, parsed_successfully=True)
        archive_old_raw_payloads(days=90)
        payload.refresh_from_db()
        self.assertEqual(TrackingRawPayload.objects.filter(pk=payload.pk).count(), 1)
        self.assertEqual(payload.payload_hash, "hash-200")
        self.assertTrue(payload.parsed_successfully)
        self.assertIsNotNone(payload.received_at)
        self.assertGreater(payload.payload_bytes, 0)

    def test_recent_payload_is_untouched(self):
        payload = _payload(self.team, self.provider, age_days=5)
        self.assertEqual(archive_old_raw_payloads(days=90), 0)
        payload.refresh_from_db()
        self.assertFalse(payload.is_archived)
        self.assertIn("events", payload.payload_json)

    def test_archiving_is_idempotent(self):
        _payload(self.team, self.provider, age_days=200)
        self.assertEqual(archive_old_raw_payloads(days=90), 1)
        self.assertEqual(archive_old_raw_payloads(days=90), 0)

    def test_zero_days_disables_archiving(self):
        payload = _payload(self.team, self.provider, age_days=500)
        self.assertEqual(archive_old_raw_payloads(days=0), 0)
        payload.refresh_from_db()
        self.assertFalse(payload.is_archived)

    def test_deletion_is_off_by_default(self):
        _payload(self.team, self.provider, age_days=5000)
        self.assertEqual(delete_expired_raw_payloads(), 0)
        self.assertEqual(TrackingRawPayload.objects.filter(team=self.team).count(), 1)

    def test_deletion_runs_only_when_a_window_is_configured(self):
        _payload(self.team, self.provider, age_days=5000)
        self.assertEqual(delete_expired_raw_payloads(days=365), 1)
        self.assertEqual(TrackingRawPayload.objects.filter(team=self.team).count(), 0)

    def test_retention_can_be_scoped_to_a_team(self):
        other = _team("retention-other-team")
        mine = _payload(self.team, self.provider, age_days=200)
        theirs = _payload(other, self.provider, age_days=200)
        archive_old_raw_payloads(days=90, team=self.team)
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertTrue(mine.is_archived)
        self.assertFalse(theirs.is_archived)

    @override_settings(
        SCM_TRACKING_RAW_PAYLOAD_RETENTION_DAYS=30,
        SCM_TRACKING_RAW_PAYLOAD_DELETE_DAYS=0,
    )
    def test_scheduled_task_uses_configured_windows(self):
        _payload(self.team, self.provider, age_days=60)
        result = apply_tracking_raw_payload_retention.run()
        self.assertEqual(result, {"archived": 1, "deleted": 0})

    @override_settings(SCM_TRACKING_RAW_PAYLOAD_RETENTION_DAYS=0)
    def test_scheduled_task_respects_disabled_retention(self):
        _payload(self.team, self.provider, age_days=1000)
        result = apply_tracking_raw_payload_retention.run()
        self.assertEqual(result["archived"], 0)


class ScheduledTaskRegistrationTest(TestCase):
    """Every dispatcher this feature relies on must actually be scheduled."""

    def test_tracking_and_discovery_tasks_are_scheduled(self):
        scheduled = {entry["task"] for entry in settings.SCHEDULED_TASKS.values()}
        for task_path in (
            "apps.scm.tracking.tasks.dispatch_due_tracking_subscriptions",
            "apps.scm.containers.tasks.dispatch_planned_container_discovery",
            "apps.scm.containers.tasks.expire_stale_planned_containers",
            "apps.scm.integrations.tasks.dispatch_shipment_container_discovery_task",
            "apps.scm.tracking.tasks.apply_tracking_raw_payload_retention",
        ):
            with self.subTest(task=task_path):
                self.assertIn(task_path, scheduled)

    def test_scheduled_tasks_are_importable(self):
        """A schedule entry pointing at a missing task fails silently in production."""
        import importlib

        for entry in settings.SCHEDULED_TASKS.values():
            task_path = entry["task"]
            if not task_path.startswith("apps.scm."):
                continue
            module_path, _, attribute = task_path.rpartition(".")
            with self.subTest(task=task_path):
                module = importlib.import_module(module_path)
                self.assertTrue(hasattr(module, attribute), f"{task_path} does not exist")
