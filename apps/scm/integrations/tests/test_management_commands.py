"""Tests for the SCM management commands. No live calls are made."""

import json
import os
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from apps.scm.integrations.credentials import get_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.scm.procurement.models import PurchaseOrder
from apps.teams.models import Team


class FakeCommandResponse:
    """Canned HTTP response for the Maersk command tests."""

    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeCommandSession:
    """Records requests and replays canned responses; makes no network call."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"url": url, "headers": headers or {}, "params": params or {}})
        return self.responses.pop(0) if self.responses else FakeCommandResponse(200, {"events": []})


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


class MaerskSetupCommandTest(TestCase):
    """Connecting a team to Maersk writes config in the clear and the key encrypted."""

    KEY = "setup-command-consumer-key"

    def setUp(self):
        self.team = Team.objects.create(name="maersk-cmd", slug="maersk-cmd")

    def _run(self, **kwargs):
        out = StringIO()
        with mock.patch.dict(os.environ, {"MAERSK_CONSUMER_KEY": self.KEY}):
            call_command("setup_maersk_integration", "--team", self.team.slug, stdout=out, **kwargs)
        return out.getvalue()

    def test_it_creates_a_carrier_integration_with_the_public_config(self):
        self._run()
        integration = Integration.objects.get(team=self.team, provider_code="maersk")
        self.assertEqual(integration.provider_family, Integration.ProviderFamily.CARRIER)
        self.assertTrue(integration.is_active)
        self.assertEqual(integration.config["tracking_path"], "/track-and-trace/public-events")
        self.assertEqual(integration.config["api_key_header_name"], "consumer-key")
        self.assertEqual(integration.config["extra_headers"]["API-Version"], "1")
        self.assertEqual(integration.config["reference_params"]["container_number"], "equipmentReference")

    def test_the_key_is_stored_encrypted_and_never_in_the_config(self):
        self._run()
        integration = Integration.objects.get(team=self.team, provider_code="maersk")
        self.assertEqual(get_integration_credentials(integration), {"api_key": self.KEY})
        self.assertNotIn(self.KEY, json.dumps(integration.config))
        self.assertNotIn(self.KEY, integration.credential.encrypted_data)

    def test_the_key_is_never_printed(self):
        self.assertNotIn(self.KEY, self._run())

    def test_it_is_idempotent(self):
        self._run()
        self._run()
        self.assertEqual(Integration.objects.filter(team=self.team, provider_code="maersk").count(), 1)

    def test_a_missing_environment_variable_is_refused(self):
        from django.core.management.base import CommandError

        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(CommandError):
            call_command("setup_maersk_integration", "--team", self.team.slug)
        self.assertFalse(Integration.objects.filter(team=self.team, provider_code="maersk").exists())

    def test_an_unknown_team_is_refused(self):
        from django.core.management.base import CommandError

        with mock.patch.dict(os.environ, {"MAERSK_CONSUMER_KEY": self.KEY}), self.assertRaises(CommandError):
            call_command("setup_maersk_integration", "--team", "no-such-team")

    def test_keep_config_leaves_a_customised_config_alone(self):
        self._run()
        integration = Integration.objects.get(team=self.team, provider_code="maersk")
        integration.config = {**integration.config, "min_poll_interval_minutes": 120}
        integration.save(update_fields=["config"])
        self._run(**{"keep_config": True})
        integration.refresh_from_db()
        self.assertEqual(integration.config["min_poll_interval_minutes"], 120)


class MaerskTrackingCommandTest(TestCase):
    """The live-verification command reports events without leaking the key."""

    KEY = "tracking-command-consumer-key"
    PAYLOAD = {
        "events": [
            {
                "eventID": "CMD-EVT-1",
                "eventType": "EQUIPMENT",
                "eventClassifierCode": "ACT",
                "equipmentEventTypeCode": "LOAD",
                "eventDateTime": "2026-03-10T08:00:00Z",
                "equipmentReference": "TRDU9258963",
                "location": {"locationName": "Port of Felixstowe", "UNLocationCode": "GBFXT"},
            },
            {
                "eventID": "CMD-EVT-2",
                "eventType": "TRANSPORT",
                "eventClassifierCode": "EST",
                "transportEventTypeCode": "ARRI",
                "eventDateTime": "2026-03-25T14:00:00Z",
                "equipmentReference": "TRDU9258963",
                "location": {"locationName": "Port of Rotterdam", "UNLocationCode": "NLRTM"},
            },
        ]
    }

    def setUp(self):
        self.team = Team.objects.create(name="maersk-track-cmd", slug="maersk-track-cmd")
        with mock.patch.dict(os.environ, {"MAERSK_CONSUMER_KEY": self.KEY}):
            call_command("setup_maersk_integration", "--team", self.team.slug, stdout=StringIO())
        self.integration = Integration.objects.get(team=self.team, provider_code="maersk")

    def _run(self, session, reference="TRDU9258963"):
        from apps.scm.integrations.carriers.maersk.client import MaerskClient

        out = StringIO()
        client = MaerskClient(self.integration, session=session)
        # Patch the name the command actually resolves, not the factory module's —
        # otherwise the command builds a real client and calls Maersk for real.
        with mock.patch(
            "apps.scm.integrations.management.commands.test_maersk_tracking.build_carrier_client",
            return_value=client,
        ):
            call_command("test_maersk_tracking", reference, "--team", self.team.slug, stdout=out)
        return out.getvalue()

    def test_it_reports_the_event_count_and_the_latest_event(self):
        output = self._run(FakeCommandSession([FakeCommandResponse(200, self.PAYLOAD)]))
        self.assertIn("returned 2 event(s)", output)
        self.assertIn("Port of Rotterdam", output)

    def test_it_asks_by_equipment_reference(self):
        session = FakeCommandSession([FakeCommandResponse(200, self.PAYLOAD)])
        self._run(session)
        self.assertEqual(session.requests[0]["params"], {"equipmentReference": "TRDU9258963"})
        self.assertEqual(session.requests[0]["headers"]["consumer-key"], self.KEY)

    def test_it_never_prints_the_consumer_key(self):
        self.assertNotIn(self.KEY, self._run(FakeCommandSession([FakeCommandResponse(200, self.PAYLOAD)])))

    def test_no_data_is_reported_as_no_data(self):
        self.assertIn("no data", self._run(FakeCommandSession([FakeCommandResponse(404)])).lower())

    def test_an_authentication_failure_is_a_command_error_without_the_key(self):
        from django.core.management.base import CommandError

        session = FakeCommandSession([FakeCommandResponse(401), FakeCommandResponse(401)])
        with self.assertRaises(CommandError) as ctx:
            self._run(session)
        self.assertNotIn(self.KEY, str(ctx.exception))

    def test_a_team_without_the_integration_is_told_how_to_configure_it(self):
        from django.core.management.base import CommandError

        other = Team.objects.create(name="maersk-track-none", slug="maersk-track-none")
        with self.assertRaises(CommandError) as ctx:
            call_command("test_maersk_tracking", "TRDU9258963", "--team", other.slug, stdout=StringIO())
        self.assertIn("setup_maersk_integration", str(ctx.exception))
