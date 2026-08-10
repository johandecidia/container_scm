"""Tests for the live Business Central OData client.

All HTTP is mocked (requests.get) and OAuth is replaced with a fake auth — no
live Business Central account, tokens, or network access.
"""

from unittest import mock

import requests
from django.test import TestCase

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import (
    BusinessCentralAuthenticationError,
    BusinessCentralConnectionError,
    BusinessCentralRateLimitError,
    BusinessCentralResponseError,
)
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential, IntegrationRequestLog
from apps.teams.models import Team

_CONFIG = {
    "tenant_id": "tenant-guid",
    "environment": "Production",
    "company_id": "company-guid",
    "api_version": "v2.0",
    "page_size": 100,
    "max_retries": 2,
    "retry_backoff_seconds": 0.0,
}


class _FakeAuth:
    def __init__(self):
        self.token = "fake-token"
        self.invalidate_calls = 0

    def get_access_token(self):
        return self.token

    def invalidate_token(self):
        self.invalidate_calls += 1
        self.token = "refreshed-token"


def _http(status_code=200, json_data=None, raise_json=False, headers=None):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.headers = headers or {}
    if raise_json:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_data if json_data is not None else {"value": []}
    return resp


class BusinessCentralClientTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="bc-client", slug="bc-client")
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
            {"client_id": "cid", "client_secret": "csecret"},
        )

    def _client(self):
        client = BusinessCentralClient(integration=self.integration)
        client._auth = _FakeAuth()
        return client

    # ---- URL & headers --------------------------------------------------

    def test_base_url_and_headers(self):
        client = self._client()
        with mock.patch("requests.get", return_value=_http(200, {"value": []})) as get:
            client.fetch_purchase_orders()
        url = get.call_args.args[0]
        self.assertEqual(
            url,
            "https://api.businesscentral.dynamics.com/v2.0/tenant-guid/Production/api/v2.0/companies(company-guid)/purchaseOrders",
        )
        headers = get.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer fake-token")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(get.call_args.kwargs["timeout"], 30)

    def test_modified_since_filter(self):
        client = self._client()
        with mock.patch("requests.get", return_value=_http(200, {"value": []})) as get:
            client.fetch_purchase_orders(modified_since="2026-01-01T00:00:00Z")
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["$filter"], "lastModifiedDateTime gt 2026-01-01T00:00:00Z")

    # ---- happy path / value handling ------------------------------------

    def test_fetch_returns_value_list(self):
        client = self._client()
        with mock.patch("requests.get", return_value=_http(200, {"value": [{"id": "1"}, {"id": "2"}]})):
            result = client.fetch_purchase_orders()
        self.assertEqual(len(result), 2)

    def test_empty_value_list(self):
        client = self._client()
        with mock.patch("requests.get", return_value=_http(200, {"value": []})):
            self.assertEqual(client.fetch_purchase_orders(), [])

    def test_missing_value_key(self):
        client = self._client()
        with mock.patch("requests.get", return_value=_http(200, {})):
            self.assertEqual(client.fetch_purchase_orders(), [])

    def test_pagination_follows_next_link(self):
        client = self._client()
        page1 = _http(200, {"value": [{"id": "1"}], "@odata.nextLink": "https://api/next-page"})
        page2 = _http(200, {"value": [{"id": "2"}]})
        with mock.patch("requests.get", side_effect=[page1, page2]) as get:
            result = client.fetch_purchase_orders()
        self.assertEqual([r["id"] for r in result], ["1", "2"])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1].args[0], "https://api/next-page")

    # ---- error mapping --------------------------------------------------

    def test_400_raises_response_error_no_retry(self):
        client = self._client()
        with (
            mock.patch("requests.get", return_value=_http(400)) as get,
            self.assertRaises(BusinessCentralResponseError),
        ):
            client.fetch_purchase_orders()
        self.assertEqual(get.call_count, 1)

    def test_403_raises_response_error(self):
        client = self._client()
        with (
            mock.patch("requests.get", return_value=_http(403)),
            self.assertRaises(BusinessCentralResponseError),
        ):
            client.fetch_purchase_orders()

    def test_404_raises_response_error(self):
        client = self._client()
        with (
            mock.patch("requests.get", return_value=_http(404)),
            self.assertRaises(BusinessCentralResponseError),
        ):
            client.fetch_purchase_orders()

    def test_500_raises_response_error_no_retry(self):
        client = self._client()
        with (
            mock.patch("requests.get", return_value=_http(500)) as get,
            self.assertRaises(BusinessCentralResponseError),
        ):
            client.fetch_purchase_orders()
        self.assertEqual(get.call_count, 1)

    def test_401_refresh_then_success(self):
        client = self._client()
        with mock.patch("requests.get", side_effect=[_http(401), _http(200, {"value": [{"id": "1"}]})]) as get:
            result = client.fetch_purchase_orders()
        self.assertEqual(len(result), 1)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(client._auth.invalidate_calls, 1)

    def test_401_persistent_raises_auth_error_one_refresh(self):
        client = self._client()
        with (
            mock.patch("requests.get", side_effect=[_http(401), _http(401)]) as get,
            self.assertRaises(BusinessCentralAuthenticationError),
        ):
            client.fetch_purchase_orders()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(client._auth.invalidate_calls, 1)

    def test_429_retried_then_rate_limit_error(self):
        client = self._client()
        with (
            mock.patch("apps.scm.integrations.business_systems.business_central.client.time.sleep"),
            mock.patch("requests.get", return_value=_http(429, headers={"Retry-After": "0"})) as get,
            self.assertRaises(BusinessCentralRateLimitError),
        ):
            client.fetch_purchase_orders()
        # initial + max_retries(2) = 3 attempts
        self.assertEqual(get.call_count, 3)

    def test_503_retried_then_response_error(self):
        client = self._client()
        with (
            mock.patch("apps.scm.integrations.business_systems.business_central.client.time.sleep"),
            mock.patch("requests.get", return_value=_http(503)) as get,
            self.assertRaises(BusinessCentralResponseError),
        ):
            client.fetch_purchase_orders()
        self.assertEqual(get.call_count, 3)

    def test_503_recovers_on_retry(self):
        client = self._client()
        with (
            mock.patch("apps.scm.integrations.business_systems.business_central.client.time.sleep"),
            mock.patch("requests.get", side_effect=[_http(503), _http(200, {"value": [{"id": "1"}]})]),
        ):
            result = client.fetch_purchase_orders()
        self.assertEqual(len(result), 1)

    def test_connection_error_retried_then_raises(self):
        client = self._client()
        with (
            mock.patch("apps.scm.integrations.business_systems.business_central.client.time.sleep"),
            mock.patch("requests.get", side_effect=requests.ConnectionError()) as get,
            self.assertRaises(BusinessCentralConnectionError),
        ):
            client.fetch_purchase_orders()
        self.assertEqual(get.call_count, 3)

    def test_invalid_json_raises_response_error(self):
        client = self._client()
        with (
            mock.patch("requests.get", return_value=_http(200, raise_json=True)),
            self.assertRaises(BusinessCentralResponseError),
        ):
            client.fetch_purchase_orders()

    # ---- test_connection & logging --------------------------------------

    def test_test_connection_success(self):
        client = self._client()
        with mock.patch("requests.get", return_value=_http(200, {"value": []})) as get:
            result = client.test_connection()
        self.assertTrue(result["success"])
        self.assertEqual(get.call_args.kwargs["params"], {"$top": 1})

    def test_test_connection_failure_raises(self):
        client = self._client()
        with (
            mock.patch("requests.get", return_value=_http(403)),
            self.assertRaises(BusinessCentralResponseError),
        ):
            client.test_connection()

    def test_request_is_logged_on_success(self):
        client = self._client()
        with mock.patch("requests.get", return_value=_http(200, {"value": []})):
            client.fetch_purchase_orders()
        log = IntegrationRequestLog.objects.filter(integration=self.integration).latest("created_at")
        self.assertTrue(log.success)
        self.assertEqual(log.status_code, 200)
        self.assertEqual(log.provider_code, "business_central")
        self.assertTrue(log.request_id)

    def test_request_is_logged_on_error(self):
        client = self._client()
        with (
            mock.patch("requests.get", return_value=_http(404)),
            self.assertRaises(BusinessCentralResponseError),
        ):
            client.fetch_purchase_orders()
        log = IntegrationRequestLog.objects.filter(integration=self.integration).latest("created_at")
        self.assertFalse(log.success)
        self.assertEqual(log.status_code, 404)

    def test_logged_endpoint_contains_no_token(self):
        client = self._client()
        with mock.patch("requests.get", return_value=_http(200, {"value": []})):
            client.fetch_purchase_orders()
        for log in IntegrationRequestLog.objects.filter(integration=self.integration):
            self.assertNotIn("fake-token", log.endpoint)
            self.assertNotIn("Bearer", log.endpoint)
