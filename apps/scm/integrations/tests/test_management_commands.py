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
        # otherwise the command builds a real client and calls Maersk for real. The
        # Maersk command is a thin alias, so the name lives in test_carrier_tracking.
        with mock.patch(
            "apps.scm.integrations.management.commands.test_carrier_tracking.build_carrier_client",
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


class CmaCgmSetupCommandTest(TestCase):
    """Connecting a team to CMA CGM writes config in the clear and the key encrypted."""

    KEY = "test-cma-api-key"
    CONTAINER = "CMAU1234564"

    def setUp(self):
        self.team = Team.objects.create(name="cma-cmd", slug="cma-cmd")

    def _run(self, *args, **kwargs):
        out = StringIO()
        with mock.patch.dict(os.environ, {"CMA_CGM_API_KEY": self.KEY}):
            call_command("setup_cma_cgm_integration", "--team", self.team.slug, *args, stdout=out, **kwargs)
        return out.getvalue()

    def _integration(self) -> Integration:
        return Integration.objects.get(team=self.team, provider_code="cma_cgm")

    def test_it_creates_a_carrier_integration_with_the_public_config(self):
        self._run()
        integration = self._integration()
        self.assertEqual(integration.provider_family, Integration.ProviderFamily.CARRIER)
        self.assertEqual(integration.api_style, Integration.ApiStyle.DCSA)
        self.assertTrue(integration.is_active)
        self.assertEqual(integration.config["base_url"], "https://apis.cma-cgm.net")
        self.assertEqual(integration.config["tracking_path"], "/operation/trackandtrace/v1/events")
        self.assertEqual(integration.config["api_key_header_name"], "keyId")

    def test_it_maps_every_supported_reference_kind(self):
        self._run()
        reference_params = self._integration().config["reference_params"]
        self.assertEqual(reference_params["container_number"], "equipmentReference")
        self.assertEqual(reference_params["booking_number"], "carrierBookingReference")
        self.assertEqual(reference_params["bill_of_lading_number"], "transportDocumentReference")

    def test_it_configures_cursor_pagination(self):
        self._run()
        pagination = self._integration().config["pagination"]
        self.assertEqual(pagination["cursor_param"], "cursor")
        self.assertEqual(pagination["next_page_header"], "Next-Page")
        self.assertEqual(pagination["limit_param"], "limit")
        self.assertEqual(pagination["page_size"], 100)
        self.assertEqual(pagination["max_pages"], 20)

    def test_the_key_is_stored_encrypted_and_never_in_the_config(self):
        self._run()
        integration = self._integration()
        self.assertEqual(get_integration_credentials(integration), {"api_key": self.KEY})
        self.assertNotIn(self.KEY, json.dumps(integration.config))
        self.assertNotIn(self.KEY, integration.credential.encrypted_data)

    def test_the_key_is_never_printed(self):
        self.assertNotIn(self.KEY, self._run())

    def test_it_creates_the_tracking_provider(self):
        from apps.scm.tracking.models import TrackingProvider

        self._run()
        self.assertTrue(TrackingProvider.objects.filter(code="cma_cgm").exists())

    def test_it_is_idempotent(self):
        self._run()
        self._run()
        self.assertEqual(Integration.objects.filter(team=self.team, provider_code="cma_cgm").count(), 1)

    def test_no_test_reference_is_configured_by_default(self):
        """A reference known to the account is not shipped in code."""
        output = self._run()
        self.assertNotIn("test_connection_reference", self._integration().config)
        self.assertIn("No test_connection_reference", output)

    def test_a_test_reference_can_be_supplied(self):
        self._run("--test-reference", self.CONTAINER)
        self.assertEqual(self._integration().config["test_connection_reference"], self.CONTAINER)

    def test_a_configured_test_reference_survives_a_key_rotation(self):
        self._run("--test-reference", self.CONTAINER)
        self._run()
        self.assertEqual(self._integration().config["test_connection_reference"], self.CONTAINER)

    def test_keep_config_leaves_a_customised_config_alone(self):
        self._run()
        integration = self._integration()
        integration.config = {**integration.config, "min_poll_interval_minutes": 120}
        integration.save(update_fields=["config"])
        self._run(**{"keep_config": True})
        integration.refresh_from_db()
        self.assertEqual(integration.config["min_poll_interval_minutes"], 120)

    def test_a_missing_environment_variable_is_refused(self):
        from django.core.management.base import CommandError

        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(CommandError):
            call_command("setup_cma_cgm_integration", "--team", self.team.slug)
        self.assertFalse(Integration.objects.filter(team=self.team, provider_code="cma_cgm").exists())

    def test_an_unknown_team_is_refused(self):
        from django.core.management.base import CommandError

        with mock.patch.dict(os.environ, {"CMA_CGM_API_KEY": self.KEY}), self.assertRaises(CommandError):
            call_command("setup_cma_cgm_integration", "--team", "no-such-team")


class CarrierTrackingCommandTest(TestCase):
    """The generic live-verification command works for any implemented carrier."""

    KEY = "test-cma-api-key"
    CONTAINER = "CMAU1234564"
    PAYLOAD = [
        {
            "eventID": "CMA-CMD-1",
            "eventType": "EQUIPMENT",
            "eventClassifierCode": "ACT",
            "equipmentEventTypeCode": "LOAD",
            "eventDateTime": "2026-03-10T08:00:00Z",
            "equipmentReference": "CMAU1234564",
            "eventLocation": {"locationName": "Port of Shanghai", "UNLocationCode": "CNSHA"},
        },
        {
            "eventID": "CMA-CMD-2",
            "eventType": "TRANSPORT",
            "eventClassifierCode": "EST",
            "transportEventTypeCode": "ARRI",
            "eventDateTime": "2026-03-25T14:00:00Z",
            "transportCall": {"location": {"locationName": "Port of Le Havre", "UNLocationCode": "FRLEH"}},
        },
    ]

    def setUp(self):
        self.team = Team.objects.create(name="cma-track-cmd", slug="cma-track-cmd")
        with mock.patch.dict(os.environ, {"CMA_CGM_API_KEY": self.KEY}):
            call_command(
                "setup_cma_cgm_integration",
                "--team",
                self.team.slug,
                "--test-reference",
                self.CONTAINER,
                stdout=StringIO(),
            )
        self.integration = Integration.objects.get(team=self.team, provider_code="cma_cgm")

    def _run(self, session, reference=None, *args):
        from apps.scm.integrations.carriers.cma_cgm.client import CmaCgmClient

        out = StringIO()
        client = CmaCgmClient(self.integration, session=session)
        # Patch the name the command actually resolves, so no live call is made.
        with mock.patch(
            "apps.scm.integrations.management.commands.test_carrier_tracking.build_carrier_client",
            return_value=client,
        ):
            call_command(
                "test_carrier_tracking",
                reference or self.CONTAINER,
                "--provider",
                "cma_cgm",
                "--team",
                self.team.slug,
                *args,
                stdout=out,
            )
        return out.getvalue()

    def test_it_reports_the_event_count_and_the_latest_event(self):
        output = self._run(FakeCommandSession([FakeCommandResponse(200, self.PAYLOAD)]))
        self.assertIn("CMA CGM returned 2 event(s)", output)
        self.assertIn("Port of Le Havre", output)

    def test_it_reports_the_first_event(self):
        output = self._run(FakeCommandSession([FakeCommandResponse(200, self.PAYLOAD)]))
        self.assertIn("First event", output)

    def test_it_asks_by_equipment_reference_with_the_key_id_header(self):
        session = FakeCommandSession([FakeCommandResponse(200, self.PAYLOAD)])
        self._run(session)
        self.assertEqual(session.requests[0]["params"]["equipmentReference"], self.CONTAINER)
        self.assertEqual(session.requests[0]["headers"]["keyId"], self.KEY)

    def test_it_can_ask_by_booking_number(self):
        session = FakeCommandSession([FakeCommandResponse(200, [])])
        self._run(session, "CMA-BKG-987654", "--by", "booking")
        self.assertEqual(session.requests[0]["params"]["carrierBookingReference"], "CMA-BKG-987654")

    def test_it_never_prints_the_api_key(self):
        self.assertNotIn(self.KEY, self._run(FakeCommandSession([FakeCommandResponse(200, self.PAYLOAD)])))

    def test_no_data_is_reported_as_no_data(self):
        self.assertIn("no data", self._run(FakeCommandSession([FakeCommandResponse(404)])).lower())

    def test_an_empty_result_is_reported_as_zero_events(self):
        self.assertIn("returned 0 event(s)", self._run(FakeCommandSession([FakeCommandResponse(200, [])])))

    def test_an_authentication_failure_is_a_command_error_without_the_key(self):
        from django.core.management.base import CommandError

        session = FakeCommandSession([FakeCommandResponse(401), FakeCommandResponse(401)])
        with self.assertRaises(CommandError) as ctx:
            self._run(session)
        self.assertNotIn(self.KEY, str(ctx.exception))

    def test_a_team_without_the_integration_is_told_how_to_configure_it(self):
        from django.core.management.base import CommandError

        other = Team.objects.create(name="cma-track-none", slug="cma-track-none")
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "test_carrier_tracking",
                self.CONTAINER,
                "--provider",
                "cma_cgm",
                "--team",
                other.slug,
                stdout=StringIO(),
            )
        self.assertIn("setup_cma_cgm_integration", str(ctx.exception))

    def test_an_unknown_provider_is_refused(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command(
                "test_carrier_tracking",
                self.CONTAINER,
                "--provider",
                "not-a-carrier",
                "--team",
                self.team.slug,
                stdout=StringIO(),
            )
