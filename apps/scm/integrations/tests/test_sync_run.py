"""Tests for the IntegrationSyncRun model."""

from django.test import TestCase

from apps.scm.integrations.models import Integration, IntegrationSyncRun
from apps.teams.models import Team


class IntegrationSyncRunModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="syncrun", slug="syncrun")
        cls.integration = Integration.objects.create(
            team=cls.team,
            name="BC",
            provider_code="business_central",
            provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        )

    def _run(self, **kwargs):
        return IntegrationSyncRun.objects.create(team=self.team, integration=self.integration, **kwargs)

    def test_defaults(self):
        run = self._run()
        self.assertEqual(run.status, IntegrationSyncRun.Status.PENDING)
        self.assertEqual(run.trigger_type, IntegrationSyncRun.TriggerType.SCHEDULED)
        self.assertEqual(run.resource_type, IntegrationSyncRun.ResourceType.PURCHASE_ORDERS)
        self.assertEqual(run.records_fetched, 0)
        self.assertEqual(run.records_created, 0)
        self.assertEqual(run.records_updated, 0)
        self.assertEqual(run.records_unchanged, 0)
        self.assertEqual(run.records_failed, 0)
        self.assertIsNone(run.started_at)
        self.assertIsNone(run.finished_at)
        self.assertIsNone(run.watermark_from)
        self.assertIsNone(run.watermark_to)
        self.assertEqual(run.metadata, {})

    def test_relations(self):
        run = self._run()
        self.assertEqual(run.team, self.team)
        self.assertEqual(run.integration, self.integration)
        self.assertIn(run, self.integration.sync_runs.all())

    def test_str(self):
        run = self._run(status=IntegrationSyncRun.Status.COMPLETED)
        self.assertIn("completed", str(run))
        self.assertIn("purchase_orders", str(run))

    def test_ordering_newest_first(self):
        first = self._run()
        second = self._run()
        runs = list(IntegrationSyncRun.objects.all())
        self.assertEqual(runs[0], second)
        self.assertEqual(runs[1], first)

    def test_status_values(self):
        for status in [
            IntegrationSyncRun.Status.PENDING,
            IntegrationSyncRun.Status.RUNNING,
            IntegrationSyncRun.Status.COMPLETED,
            IntegrationSyncRun.Status.PARTIALLY_COMPLETED,
            IntegrationSyncRun.Status.FAILED,
        ]:
            run = self._run(status=status)
            self.assertEqual(run.status, status)

    def test_cascade_delete_with_integration(self):
        run = self._run()
        run_pk = run.pk
        self.integration.delete()
        self.assertFalse(IntegrationSyncRun.objects.filter(pk=run_pk).exists())
