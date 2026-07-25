"""Tests for the Business Central management commands (dummy mode, no live calls)."""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.scm.integrations.credentials import get_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
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


class MigrateCredentialsCommandTest(TestCase):
    def setUp(self):
        import base64
        import json

        self.team = Team.objects.create(name="migr", slug="migr")
        self.integration = Integration.objects.create(
            team=self.team,
            name="BC",
            provider_code="business_central",
            provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        )
        # A legacy (unprefixed base64-JSON) credential row.
        legacy = base64.b64encode(json.dumps({"client_secret": "legacy-secret"}).encode()).decode()
        self.credential = IntegrationCredential.objects.create(
            team=self.team,
            integration=self.integration,
            auth_type=IntegrationCredential.AuthType.OAUTH2,
            encrypted_data=legacy,
        )

    def test_dry_run_does_not_change(self):
        out = StringIO()
        original = self.credential.encrypted_data
        call_command("migrate_integration_credentials", "--dry-run", stdout=out)
        self.credential.refresh_from_db()
        self.assertEqual(self.credential.encrypted_data, original)
        self.assertIn("dry-run", out.getvalue())
        self.assertNotIn("legacy-secret", out.getvalue())

    def test_migrates_legacy_to_versioned(self):
        out = StringIO()
        call_command("migrate_integration_credentials", stdout=out)
        self.credential.refresh_from_db()
        self.assertTrue(self.credential.encrypted_data.startswith("fernet:v1:"))
        self.assertEqual(get_integration_credentials(self.integration), {"client_secret": "legacy-secret"})
        self.assertNotIn("legacy-secret", out.getvalue())
