"""Regression tests for BC URL construction, bounded paging and the connection test.

All HTTP is mocked — no live Business Central account, tokens, or network access.
"""

from unittest import mock

import requests
from django.test import SimpleTestCase, TestCase

from apps.scm.integrations.business_systems.business_central import client as client_module
from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient, _resolve_config
from apps.scm.integrations.business_systems.business_central.exceptions import (
    BusinessCentralAuthenticationError,
    BusinessCentralCompanyNotFoundError,
    BusinessCentralConfigurationError,
    BusinessCentralConnectionError,
    BusinessCentralEnvironmentNotFoundError,
    BusinessCentralNotFoundError,
    BusinessCentralPermissionError,
    BusinessCentralResponseError,
)
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential, IntegrationRequestLog
from apps.teams.models import Team

from .test_client import _CONFIG, API_ROOT, COMPANY_ID, TENANT_ID, _FakeAuth, _http

_PO_GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _cfg(**overrides):
    return _resolve_config({**_CONFIG, **overrides})


class UrlConstructionTest(SimpleTestCase):
    def test_standard_api_root(self):
        self.assertEqual(
            _cfg().api_root,
            f"https://api.businesscentral.dynamics.com/v2.0/{TENANT_ID}/Production/api/v2.0",
        )

    def test_sandbox_environment_root(self):
        cfg = _cfg(environment="Sandbox_eligra_261002")
        self.assertEqual(
            cfg.api_root,
            f"https://api.businesscentral.dynamics.com/v2.0/{TENANT_ID}/Sandbox_eligra_261002/api/v2.0",
        )

    def test_api_version_only_changes_api_segment(self):
        # The leading /v2.0 is the service endpoint version, not the API version.
        root = _cfg(api_version="v1.0").api_root
        self.assertTrue(root.startswith(f"https://api.businesscentral.dynamics.com/v2.0/{TENANT_ID}/"))
        self.assertTrue(root.endswith("/api/v1.0"))
        self.assertEqual(root.count("/v2.0"), 1)

    def test_version_never_duplicated(self):
        self.assertEqual(_cfg().api_root.count("/v2.0/"), 1)
        self.assertTrue(_cfg().api_root.endswith("/api/v2.0"))

    def test_environment_is_url_encoded(self):
        root = _cfg(environment="My Sandbox/x").api_root
        self.assertIn("/My%20Sandbox%2Fx/api/", root)

    def test_company_path_uses_canonical_guid(self):
        cfg = _cfg(company_id=COMPANY_ID.upper())
        self.assertEqual(cfg.company_path, f"companies({COMPANY_ID})")

    def test_company_id_must_be_guid(self):
        with self.assertRaises(BusinessCentralConfigurationError):
            _cfg(company_id="CRONUS International")

    def test_missing_required_fields(self):
        with self.assertRaises(BusinessCentralConfigurationError) as ctx:
            _resolve_config({"tenant_id": TENANT_ID})
        self.assertIn("environment", str(ctx.exception))
        self.assertIn("company_id", str(ctx.exception))

    def test_page_size_bounds(self):
        with self.assertRaises(BusinessCentralConfigurationError):
            _cfg(page_size=20001)


class _LiveClientCase(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="bc-conn", slug="bc-conn")
        self.integration = Integration.objects.create(
            team=self.team,
            name="BC",
            provider_code="business_central",
            provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
            config=_CONFIG,
        )
        set_integration_credentials(
            self.integration,
            IntegrationCredential.AuthType.OAUTH2,
            {"client_id": "cid", "client_secret": "super-secret-value"},
        )

    def _client(self):
        client = BusinessCentralClient(integration=self.integration)
        client._auth = _FakeAuth()
        return client


class BoundedFetchTest(_LiveClientCase):
    def test_limit_sent_as_top(self):
        with mock.patch("requests.get", return_value=_http(200, {"value": [{"id": "1"}]})) as get:
            self._client().fetch_purchase_orders(top=3)
        self.assertEqual(get.call_args.kwargs["params"]["$top"], 3)

    def test_no_top_without_limit(self):
        # $top caps the whole result in OData; it must never be used as a page size.
        with mock.patch("requests.get", return_value=_http(200, {"value": []})) as get:
            self._client().fetch_purchase_orders()
        self.assertNotIn("$top", get.call_args.kwargs["params"])

    def test_limit_stops_pagination(self):
        next_link = f"{API_ROOT}/companies({COMPANY_ID})/purchaseOrders?$skiptoken=x"
        page = _http(200, {"value": [{"id": "1"}, {"id": "2"}], "@odata.nextLink": next_link})
        with mock.patch("requests.get", side_effect=[page]) as get:
            result = self._client().fetch_purchase_orders(top=2)
        self.assertEqual(len(result), 2)
        self.assertEqual(get.call_count, 1)

    def test_empty_trailing_page_ends_collection(self):
        next_link = f"{API_ROOT}/companies({COMPANY_ID})/purchaseOrders?$skiptoken=x"
        page1 = _http(200, {"value": [{"id": "1"}], "@odata.nextLink": next_link})
        page2 = _http(200, {"value": [], "@odata.nextLink": next_link + "y"})
        with mock.patch("requests.get", side_effect=[page1, page2]) as get:
            result = self._client().fetch_purchase_orders()
        self.assertEqual(len(result), 1)
        self.assertEqual(get.call_count, 2)

    def test_next_link_outside_api_host_rejected(self):
        page = _http(200, {"value": [{"id": "1"}], "@odata.nextLink": "https://evil.example.com/steal"})
        with (
            mock.patch("requests.get", side_effect=[page]) as get,
            self.assertRaises(BusinessCentralResponseError),
        ):
            self._client().fetch_purchase_orders()
        self.assertEqual(get.call_count, 1)

    def test_select_uses_standard_v2_property_names(self):
        with mock.patch("requests.get", return_value=_http(200, {"value": []})) as get:
            self._client().fetch_purchase_orders()
        select = get.call_args.kwargs["params"]["$select"].split(",")
        self.assertIn("requestedReceiptDate", select)
        self.assertNotIn("expectedReceiptDate", select)  # not a purchaseOrder header property

    def test_lines_url_and_select(self):
        with mock.patch("requests.get", return_value=_http(200, {"value": []})) as get:
            self._client().fetch_purchase_order_lines(_PO_GUID, top=5)
        self.assertEqual(
            get.call_args.args[0],
            f"{API_ROOT}/companies({COMPANY_ID})/purchaseOrders({_PO_GUID})/purchaseOrderLines",
        )
        select = get.call_args.kwargs["params"]["$select"].split(",")
        self.assertIn("lineObjectNumber", select)
        self.assertNotIn("itemNumber", select)
        self.assertNotIn("lastModifiedDateTime", select)  # not a purchaseOrderLine property
        self.assertEqual(get.call_args.kwargs["params"]["$top"], 5)

    def test_lines_reject_non_guid_identifier(self):
        with mock.patch("requests.get") as get, self.assertRaises(BusinessCentralConfigurationError):
            self._client().fetch_purchase_order_lines("PO100245)/../vendors(")
        get.assert_not_called()

    def test_client_never_writes(self):
        with (
            mock.patch("requests.get", return_value=_http(200, {"value": []})),
            mock.patch("requests.post") as post,
            mock.patch("requests.patch") as patch,
            mock.patch("requests.put") as put,
            mock.patch("requests.delete") as delete,
            mock.patch("requests.request") as request,
        ):
            client = self._client()
            client.fetch_purchase_orders()
            client.fetch_purchase_order_lines(_PO_GUID)
        for write in (post, patch, put, delete, request):
            write.assert_not_called()


class ErrorClassificationTest(_LiveClientCase):
    def test_403_is_permission_error_with_code(self):
        body = {"error": {"code": "Authorization_InsufficientPermissions", "message": "detail"}}
        with (
            mock.patch("requests.get", return_value=_http(403, body)),
            self.assertRaises(BusinessCentralPermissionError) as ctx,
        ):
            self._client().fetch_purchase_orders()
        self.assertEqual(ctx.exception.error_code, "Authorization_InsufficientPermissions")
        self.assertIn("Authorization_InsufficientPermissions", str(ctx.exception))
        self.assertNotIn("detail", str(ctx.exception))

    def test_404_is_not_found_error(self):
        with mock.patch("requests.get", return_value=_http(404)), self.assertRaises(BusinessCentralNotFoundError):
            self._client().fetch_purchase_orders()

    def test_unsafe_error_code_not_echoed(self):
        body = {"error": {"code": "<script>Bearer abc</script>"}}
        with (
            mock.patch("requests.get", return_value=_http(400, body)),
            self.assertRaises(BusinessCentralResponseError) as ctx,
        ):
            self._client().fetch_purchase_orders()
        self.assertNotIn("script", str(ctx.exception))
        self.assertEqual(ctx.exception.error_code, "")


def _companies(*ids):
    return _http(200, {"value": [{"id": i, "name": f"Company {n}"} for n, i in enumerate(ids)]})


class ConnectionTestTest(_LiveClientCase):
    def test_success_runs_every_check(self):
        with mock.patch("requests.get", side_effect=[_companies(COMPANY_ID), _http(200, {"value": []})]) as get:
            result = self._client().test_connection()
        self.assertTrue(result["success"])
        self.assertEqual(result["company_name"], "Company 0")
        self.assertEqual(len(result["checks"]), 4)
        self.assertEqual(get.call_args_list[0].args[0], f"{API_ROOT}/companies")
        self.assertEqual(get.call_args_list[1].args[0], f"{API_ROOT}/companies({COMPANY_ID})/purchaseOrders")

    def test_company_match_is_case_insensitive(self):
        with mock.patch("requests.get", side_effect=[_companies(COMPANY_ID.upper()), _http(200, {"value": []})]):
            self.assertTrue(self._client().test_connection()["success"])

    def test_auth_failure(self):
        client = self._client()
        client._auth.get_access_token = mock.Mock(side_effect=BusinessCentralAuthenticationError("Token failed"))
        with mock.patch("requests.get") as get, self.assertRaises(BusinessCentralAuthenticationError):
            client.test_connection()
        get.assert_not_called()

    def test_token_accepted_but_bc_rejects(self):
        with (
            mock.patch("requests.get", side_effect=[_http(401), _http(401)]),
            self.assertRaises(BusinessCentralAuthenticationError) as ctx,
        ):
            self._client().test_connection()
        self.assertIn("registered in Business Central", str(ctx.exception))

    def test_invalid_environment(self):
        with (
            mock.patch("requests.get", return_value=_http(404)),
            self.assertRaises(BusinessCentralEnvironmentNotFoundError) as ctx,
        ):
            self._client().test_connection()
        self.assertIn("Production", str(ctx.exception))

    def test_invalid_company(self):
        other = "99999999-9999-9999-9999-999999999999"
        with (
            mock.patch("requests.get", side_effect=[_companies(other)]),
            self.assertRaises(BusinessCentralCompanyNotFoundError),
        ):
            self._client().test_connection()

    def test_missing_purchase_order_permission(self):
        with (
            mock.patch("requests.get", side_effect=[_companies(COMPANY_ID), _http(403)]),
            self.assertRaises(BusinessCentralPermissionError),
        ):
            self._client().test_connection()

    def test_403_reports_failed_endpoint_and_denied_object(self):
        message = (
            "Sorry, the current permissions prevented the action. "
            "(TableData 5496 Purchase Order Entity Buffer Read: Base Application) Bearer fake-token super-secret-value"
        )
        body = {"error": {"code": "Internal_ServerError", "message": message}}
        with (
            mock.patch("requests.get", side_effect=[_companies(COMPANY_ID), _http(403, body)]),
            self.assertRaises(BusinessCentralPermissionError) as ctx,
        ):
            self._client().test_connection()
        exc = ctx.exception
        self.assertEqual(exc.endpoint, f"companies({COMPANY_ID})/purchaseOrders")
        self.assertEqual(exc.permission["object_id"], "5496")
        self.assertEqual(exc.permission["permission"], "Read")
        # The exception text — shown to everyone and stored on the integration — is unchanged.
        self.assertNotIn("Purchase Order Entity Buffer", str(exc))
        self.assertNotIn("fake-token", str(exc.permission))

        log = IntegrationRequestLog.objects.get(integration=self.integration, status_code=403)
        self.assertIn(f"GET companies({COMPANY_ID})/purchaseOrders (HTTP 403)", log.error_message)
        self.assertIn('TableData 5496 "Purchase Order Entity Buffer": Read', log.error_message)
        self.assertNotIn("fake-token", log.error_message)
        self.assertNotIn("super-secret-value", log.error_message)

    def test_network_error(self):
        with (
            mock.patch.object(client_module.time, "sleep"),
            mock.patch("requests.get", side_effect=requests.ConnectionError("host 10.0.0.1 unreachable")),
            self.assertRaises(BusinessCentralConnectionError) as ctx,
        ):
            self._client().test_connection()
        self.assertNotIn("10.0.0.1", str(ctx.exception))

    def test_unexpected_response(self):
        with (
            mock.patch("requests.get", return_value=_http(200, raise_json=True)),
            self.assertRaises(BusinessCentralResponseError),
        ):
            self._client().test_connection()

    def test_no_secret_or_token_in_errors_or_logs(self):
        with (
            mock.patch("requests.get", return_value=_http(403)),
            self.assertRaises(BusinessCentralPermissionError) as e,
        ):
            self._client().test_connection()
        self.assertNotIn("super-secret-value", str(e.exception))
        self.assertNotIn("fake-token", str(e.exception))
        for log in IntegrationRequestLog.objects.filter(integration=self.integration):
            for field in (log.endpoint, log.error_message):
                self.assertNotIn("fake-token", field)
                self.assertNotIn("super-secret-value", field)

    def test_dummy_client(self):
        self.assertTrue(BusinessCentralClient(use_dummy=True).test_connection()["success"])
