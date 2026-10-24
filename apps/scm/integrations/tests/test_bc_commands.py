"""Tests for bc_test_connection and bc_inspect_purchase_orders (all HTTP mocked)."""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.scm.integrations.business_systems.business_central.tests.test_client import (
    _CONFIG,
    COMPANY_ID,
    _FakeAuth,
    _http,
)
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine
from apps.teams.models import Team

_SECRET = "very-secret-client-secret"
_PO_ID = "5d115c9c-44e3-ea11-bb43-000d3a2feca1"
_PO = {
    "id": _PO_ID,
    "number": "106001",
    "vendorNumber": "30000",
    "vendorName": "CoolWood Technologies",
    "status": "Open",
    "orderDate": "2026-09-14",
    "lastModifiedDateTime": "2026-09-14T08:12:44Z",
}
_LINE = {
    "id": "6f115c9c-44e3-ea11-bb43-000d3a2feca1",
    "sequence": 10000,
    "lineType": "Item",
    "lineObjectNumber": "CON22G1",
    "quantity": 50,
    "directUnitCost": 2140.35,
    "expectedReceiptDate": "2026-11-02",
    "accessKey": "should-not-print",
}


def _connection_responses():
    return [_http(200, {"value": [{"id": COMPANY_ID, "name": "CRONUS"}]}), _http(200, {"value": []})]


class _CommandCase(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="bc-cmd", slug="bc-cmd")
        self.integration = Integration.objects.create(
            team=self.team,
            name="BC Sandbox",
            provider_code="business_central",
            provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
            config=_CONFIG,
        )
        set_integration_credentials(
            self.integration, IntegrationCredential.AuthType.OAUTH2, {"client_id": "cid", "client_secret": _SECRET}
        )
        self.auth_patcher = mock.patch(
            "apps.scm.integrations.business_systems.business_central.client.BusinessCentralClient._build_auth",
            return_value=_FakeAuth(),
        )
        self.auth_patcher.start()
        self.addCleanup(self.auth_patcher.stop)

    def _run(self, name, *args):
        out = StringIO()
        call_command(name, "--integration", str(self.integration.pk), *args, stdout=out)
        return out.getvalue()


class TestConnectionCommandTest(_CommandCase):
    def test_success_prints_each_check(self):
        with mock.patch("requests.get", side_effect=_connection_responses()):
            output = self._run("bc_test_connection")
        self.assertIn("OAuth2 token acquired", output)
        self.assertIn("Company accessible: CRONUS", output)
        self.assertIn("Purchase orders readable", output)
        self.assertIn("Business Central connection: OK", output)
        self.assertNotIn(_SECRET, output)
        self.assertNotIn("fake-token", output)

    def test_permission_failure_is_categorised(self):
        with (
            mock.patch("requests.get", side_effect=[_connection_responses()[0], _http(403)]),
            self.assertRaisesMessage(CommandError, "Missing Business Central permissions"),
        ):
            self._run("bc_test_connection")

    def test_permission_failure_names_request_and_denied_object(self):
        body = {
            "error": {
                "code": "Internal_ServerError",
                "message": "(TableData 38 Purchase Header Read: Base Application)",
            }
        }
        with (
            mock.patch("requests.get", side_effect=[_connection_responses()[0], _http(403, body)]),
            self.assertRaises(CommandError) as ctx,
        ):
            self._run("bc_test_connection")
        self.assertIn(f"GET companies({COMPANY_ID})/purchaseOrders (HTTP 403)", str(ctx.exception))
        self.assertIn('TableData 38 "Purchase Header": Read', str(ctx.exception))

    def test_invalid_environment_is_categorised(self):
        with (
            mock.patch("requests.get", return_value=_http(404)),
            self.assertRaisesMessage(CommandError, "Invalid environment"),
        ):
            self._run("bc_test_connection")

    def test_missing_credentials(self):
        IntegrationCredential.objects.filter(integration=self.integration).delete()
        self.auth_patcher.stop()  # real credential lookup
        with mock.patch("requests.get") as get, self.assertRaisesMessage(CommandError, "Configuration error"):
            self._run("bc_test_connection")
        get.assert_not_called()

    def test_non_bc_integration_rejected(self):
        self.integration.provider_code = "maersk"
        self.integration.save()
        with self.assertRaisesMessage(CommandError, "not a Business Central integration"):
            self._run("bc_test_connection")


class InspectCommandTest(_CommandCase):
    def _inspect(self, *args, lines=None):
        responses = [*_connection_responses(), _http(200, {"value": [_PO]}), _http(200, {"value": lines or [_LINE]})]
        with mock.patch("requests.get", side_effect=responses) as get:
            output = self._run("bc_inspect_purchase_orders", *args)
        return output, get

    def test_summary_output(self):
        output, _ = self._inspect("--limit", "3")
        self.assertIn("Business Central connection: OK", output)
        self.assertIn("Environment: Production", output)
        self.assertIn("Purchase Order: 106001", output)
        self.assertIn(f"BC ID: {_PO_ID}", output)
        self.assertIn("Status: Open (SCM status: released)", output)
        self.assertIn("Item: CON22G1", output)
        self.assertIn("Quantity: 50", output)
        self.assertIn("Expected Receipt Date: 2026-11-02", output)

    def test_limit_constrains_the_api_request(self):
        _, get = self._inspect("--limit", "2", "--line-limit", "4")
        po_call, line_call = get.call_args_list[2], get.call_args_list[3]
        self.assertEqual(po_call.kwargs["params"]["$top"], 2)
        self.assertEqual(line_call.kwargs["params"]["$top"], 4)

    def test_no_procurement_writes(self):
        self._inspect()
        self.assertFalse(PurchaseOrder.objects.exists())
        self.assertFalse(PurchaseOrderLine.objects.exists())

    def test_raw_payload_not_printed_by_default(self):
        output, _ = self._inspect()
        self.assertNotIn("accessKey", output)
        self.assertNotIn("should-not-print", output)

    def test_show_fields_masks_sensitive_names(self):
        output, _ = self._inspect("--show-fields")
        self.assertIn("· lineObjectNumber = 'CON22G1'", output)
        self.assertIn("· accessKey = *** (masked)", output)
        self.assertNotIn("should-not-print", output)

    def test_no_credentials_or_token_in_output(self):
        output, _ = self._inspect("--show-fields")
        self.assertNotIn(_SECRET, output)
        self.assertNotIn("fake-token", output)

    def test_mapping_problem_reported_not_raised(self):
        output, _ = self._inspect(lines=[{**_LINE, "id": ""}])
        self.assertIn("Would NOT import", output)

    def test_limit_bounds_validated(self):
        for value in ("0", "51"):
            with self.subTest(value=value), self.assertRaises(CommandError):
                self._run("bc_inspect_purchase_orders", "--limit", value)

    def test_connection_failure_stops_before_fetch(self):
        with (
            mock.patch("requests.get", return_value=_http(404)) as get,
            self.assertRaisesMessage(CommandError, "Invalid environment"),
        ):
            self._run("bc_inspect_purchase_orders")
        self.assertEqual(get.call_count, 1)


class SetupCommandTest(TestCase):
    _ENV = {"BC_CLIENT_ID": "client-id-value", "BC_CLIENT_SECRET": _SECRET}

    def setUp(self):
        self.team = Team.objects.create(name="bc-setup", slug="bc-setup")

    def _setup(self, *extra, env=None, environment="Sandbox_eligra_261002"):
        out = StringIO()
        args = [
            "--team",
            "bc-setup",
            "--tenant-id",
            "609e5a65-e2b8-49d7-85d4-5cdadf170d85",
            "--environment",
            environment,
            "--company-id",
            COMPANY_ID,
            *extra,
        ]
        with mock.patch.dict("os.environ", env if env is not None else self._ENV, clear=False):
            call_command("setup_business_central_integration", *args, stdout=out)
        return out.getvalue()

    def test_creates_safe_integration_and_encrypted_credentials(self):
        from apps.scm.integrations.credentials import get_integration_credentials

        output = self._setup()
        integration = Integration.objects.get(team=self.team, provider_code="business_central")
        self.assertEqual(integration.provider_family, Integration.ProviderFamily.BUSINESS_SYSTEM)
        self.assertEqual(integration.config["environment"], "Sandbox_eligra_261002")
        self.assertIs(integration.config["sync_enabled"], False)
        self.assertIs(integration.config["scheduled_sync_enabled"], False)
        self.assertNotIn(_SECRET, str(integration.config))
        cred = IntegrationCredential.objects.get(integration=integration)
        self.assertNotIn(_SECRET, cred.encrypted_data)
        self.assertEqual(get_integration_credentials(integration)["client_secret"], _SECRET)
        self.assertNotIn(_SECRET, output)
        self.assertNotIn("client-id-value", output)

    def test_enable_sync_never_enables_schedule(self):
        self._setup("--enable-sync", "--initial-sync-days", "30")
        config = Integration.objects.get(team=self.team).config
        self.assertIs(config["sync_enabled"], True)
        self.assertIs(config["scheduled_sync_enabled"], False)
        self.assertEqual(config["initial_sync_days"], 30)

    def test_rerun_without_secret_keeps_credentials(self):
        from apps.scm.integrations.credentials import get_integration_credentials

        self._setup()
        output = self._setup("--enable-sync", env={"BC_CLIENT_ID": "", "BC_CLIENT_SECRET": ""})
        self.assertIn("Credentials: unchanged", output)
        integration = Integration.objects.get(team=self.team)
        self.assertEqual(get_integration_credentials(integration)["client_secret"], _SECRET)

    def test_missing_credentials_on_create(self):
        with self.assertRaisesMessage(CommandError, "BC_CLIENT_SECRET"):
            self._setup(env={"BC_CLIENT_ID": "x", "BC_CLIENT_SECRET": ""})
        self.assertFalse(Integration.objects.filter(team=self.team).exists())

    def test_refuses_to_repoint_team_to_another_environment(self):
        self._setup()
        with self.assertRaisesMessage(CommandError, "separate Team"):
            self._setup(environment="Production")
        self.assertEqual(Integration.objects.get(team=self.team).config["environment"], "Sandbox_eligra_261002")

    def test_rejects_non_guid_company(self):
        with self.assertRaisesMessage(CommandError, "GUID"):
            call_command(
                "setup_business_central_integration",
                "--team",
                "bc-setup",
                "--tenant-id",
                "t",
                "--environment",
                "Sandbox",
                "--company-id",
                "CRONUS",
            )
