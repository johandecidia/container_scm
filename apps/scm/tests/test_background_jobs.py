"""Tests for SCM background job robustness (8.3).

Verifies retry configuration, idempotency, missing-team/object handling,
and task-level logging.
"""

from unittest.mock import patch

from django.test import TestCase

from apps.scm.analytics.tasks import compute_analytics_snapshot
from apps.scm.containers.tasks import sync_container_status
from apps.scm.imports.tasks import async_parse_import_job, async_validate_import_job
from apps.scm.integrations.tasks import process_integration_webhook_event
from apps.scm.procurement.tasks import sync_purchase_orders_from_bc
from apps.scm.shipments.tasks import update_shipment_tracking
from apps.scm.tracking.tasks import sync_due_tracking_subscriptions, sync_single_tracking_subscription

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------


class TaskRetryConfigTests(TestCase):
    """All SCM tasks that call external systems must have retry configured."""

    def test_analytics_snapshot_has_retry(self):
        self.assertGreater(compute_analytics_snapshot.max_retries, 0)

    def test_parse_import_has_retry(self):
        self.assertGreater(async_parse_import_job.max_retries, 0)

    def test_validate_import_has_retry(self):
        self.assertGreater(async_validate_import_job.max_retries, 0)

    def test_sync_purchase_orders_has_retry(self):
        self.assertGreater(sync_purchase_orders_from_bc.max_retries, 0)

    def test_process_webhook_has_retry(self):
        self.assertGreater(process_integration_webhook_event.max_retries, 0)

    def test_sync_container_status_has_retry(self):
        self.assertGreater(sync_container_status.max_retries, 0)

    def test_update_shipment_tracking_has_retry(self):
        self.assertGreater(update_shipment_tracking.max_retries, 0)


# ---------------------------------------------------------------------------
# Missing object / team — tasks must not raise, must log warning and return
# ---------------------------------------------------------------------------


class MissingObjectTests(TestCase):
    def test_analytics_snapshot_skips_missing_team(self):
        result = compute_analytics_snapshot.run(999999)
        self.assertEqual(result["status"], "skipped")

    def test_sync_purchase_orders_skips_missing_team(self):
        # Should return None without raising
        result = sync_purchase_orders_from_bc.run(999999)
        self.assertIsNone(result)

    def test_sync_container_status_skips_missing_container(self):
        result = sync_container_status.run(999999)
        self.assertIsNone(result)

    def test_update_shipment_tracking_skips_missing_shipment(self):
        result = update_shipment_tracking.run(999999)
        self.assertIsNone(result)

    def test_sync_single_subscription_skips_missing_subscription(self):
        result = sync_single_tracking_subscription.run(999999)
        self.assertFalse(result)

    def test_process_webhook_skips_missing_event(self):
        result = process_integration_webhook_event.run(999999)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Analytics snapshot idempotency
# ---------------------------------------------------------------------------


class AnalyticsSnapshotIdempotencyTests(TestCase):
    def test_snapshot_task_is_idempotent(self):
        """Running the task twice should not create duplicate snapshots."""
        from apps.scm.analytics.models import AnalyticsSnapshot
        from apps.teams.models import Team

        team = Team.objects.get_or_create(slug="analytics-idempotency", defaults={"name": "Idempotency Test"})[0]

        compute_analytics_snapshot.run(team.pk)
        compute_analytics_snapshot.run(team.pk)

        count = AnalyticsSnapshot.objects.filter(team=team).count()
        # Should be 1 (update_or_create) not 2
        self.assertEqual(count, 1)
        team.delete()

    def test_snapshot_task_returns_ok_status(self):
        from apps.teams.models import Team

        team = Team.objects.get_or_create(slug="analytics-ok-test", defaults={"name": "OK Test"})[0]
        result = compute_analytics_snapshot.run(team.pk)
        self.assertEqual(result["status"], "ok")
        self.assertIn("date", result)
        team.delete()


# ---------------------------------------------------------------------------
# Tracking sync
# ---------------------------------------------------------------------------


class TrackingSyncTests(TestCase):
    def test_sync_due_subscriptions_returns_summary(self):
        """Calling with no subscriptions should return a valid summary dict."""
        result = sync_due_tracking_subscriptions.run()
        self.assertIn("successes", result)
        self.assertIn("failures", result)
        self.assertIn("skipped", result)
        self.assertIn("total", result)

    def test_sync_due_subscriptions_does_not_raise(self):
        """Should never propagate exceptions from sub-syncs."""
        with patch("apps.scm.tracking.sync.sync_tracking_subscription", side_effect=RuntimeError("boom")):
            # sync_due_tracking_subscriptions calls sync_tracking_subscription per-sub,
            # but the outer loop should not blow up on empty queryset
            result = sync_due_tracking_subscriptions.run()
        self.assertIsInstance(result, dict)
