"""Tests for the Business Central management commands (dummy mode, no live calls)."""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.scm.integrations.models import Integration
from apps.scm.procurement.models import PurchaseOrder
from apps.teams.models import Team


class BcManagementCommandTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="cmd", slug="cmd")
        self.integration = Integration.objects.create(
            team=self.team,
            name="BC",
            provider_code="business_central",
            provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
            is_active=True,
            config={"sync_enabled": True},
        )

    def test_sync_command_dummy(self):
        out = StringIO()
        call_command("bc_sync_purchase_orders", "--integration", str(self.integration.pk), "--dummy", stdout=out)
        self.assertIn("completed", out.getvalue())
        self.assertEqual(PurchaseOrder.objects.filter(team=self.team).count(), 2)

    def test_sync_command_unknown_integration(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("bc_sync_purchase_orders", "--integration", "999999", "--dummy")
