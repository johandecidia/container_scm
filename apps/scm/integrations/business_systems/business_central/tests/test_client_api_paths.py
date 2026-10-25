"""Allowlisted Business Central API paths and pagination safety.

All HTTP is mocked (requests.get); OAuth is replaced with a fake. No live calls.
"""

import inspect
import re
from unittest import mock

from django.test import TestCase

from apps.scm.integrations.business_systems.business_central import client as client_module
from apps.scm.integrations.business_systems.business_central.client import (
    API_ANALYTICS,
    API_STANDARD,
    BusinessCentralClient,
)
from apps.scm.integrations.business_systems.business_central.exceptions import (
    BusinessCentralConfigurationError,
    BusinessCentralResponseError,
)
from apps.scm.integrations.business_systems.business_central.tests.test_client import (
    _CONFIG,
    API_ROOT,
    COMPANY_ID,
    TENANT_ID,
    _FakeAuth,
    _http,
)
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.teams.models import Team

ANALYTICS_ROOT = f"https://api.businesscentral.dynamics.com/v2.0/{TENANT_ID}/Production/api/microsoft/analytics/v1.0"
_ENDPOINT = f"companies({COMPANY_ID})/itemLedgerEntries"


class BusinessCentralApiPathTest(TestCase):
    def setUp(self):
        team = Team.objects.create(name="bc-api-paths", slug="bc-api-paths")
        integration = Integration.objects.create(
            team=team,
            name="BC",
            provider_code="business_central",
            provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
            config=_CONFIG,
        )
        set_integration_credentials(
            integration, IntegrationCredential.AuthType.OAUTH2, {"client_id": "cid", "client_secret": "csecret"}
        )
        self.client_ = BusinessCentralClient(integration=integration)
        self.client_._auth = _FakeAuth()
        self.cfg = self.client_._require_config()

    # ---- URL building ---------------------------------------------------

    def test_standard_root_is_unchanged(self):
        self.assertEqual(self.cfg.api_root, API_ROOT)
        self.assertEqual(self.cfg.api_root_for(API_STANDARD), API_ROOT)

    def test_analytics_root(self):
        self.assertEqual(self.cfg.api_root_for(API_ANALYTICS), ANALYTICS_ROOT)
        self.assertEqual(self.client_._build_url(_ENDPOINT, api=API_ANALYTICS), f"{ANALYTICS_ROOT}/{_ENDPOINT}")

    def test_unknown_api_is_refused(self):
        for api in ("microsoft/automation/v2.0", "../../other", "", "beta"):
            with self.subTest(api=api), self.assertRaises(BusinessCentralConfigurationError):
                self.cfg.api_root_for(api)

    def test_absolute_endpoint_is_refused(self):
        for endpoint in ("https://evil.example/x", "//evil.example/x"):
            with self.subTest(endpoint=endpoint), self.assertRaises(BusinessCentralConfigurationError):
                self.client_._build_url(endpoint)

    def test_purchase_order_calls_unchanged(self):
        with mock.patch("requests.get", return_value=_http(200, {"value": []})) as get:
            self.client_.fetch_purchase_orders()
        self.assertEqual(get.call_args.args[0], f"{API_ROOT}/companies({COMPANY_ID})/purchaseOrders")

    # ---- pagination -----------------------------------------------------

    def _fetch(self, next_link, api=API_ANALYTICS):
        page1 = _http(200, {"value": [{"id": "1"}], "@odata.nextLink": next_link})
        page2 = _http(200, {"value": [{"id": "2"}]})
        with mock.patch("requests.get", side_effect=[page1, page2]) as get:
            result = self.client_._fetch_all_pages(_ENDPOINT, api=api)
        return result, get

    def test_analytics_first_page_url(self):
        with mock.patch("requests.get", return_value=_http(200, {"value": []})) as get:
            self.client_._fetch_all_pages(_ENDPOINT, api=API_ANALYTICS)
        self.assertEqual(get.call_args.args[0], f"{ANALYTICS_ROOT}/{_ENDPOINT}")

    def test_next_link_within_analytics_api_is_followed(self):
        next_link = f"{ANALYTICS_ROOT}/{_ENDPOINT}?$skiptoken=abc"
        result, get = self._fetch(next_link)
        self.assertEqual([r["id"] for r in result], ["1", "2"])
        self.assertEqual(get.call_args_list[1].args[0], next_link)

    def test_next_link_casing_from_business_central_is_accepted(self):
        next_link = f"{ANALYTICS_ROOT.replace('Production', 'production')}/{_ENDPOINT}?$skiptoken=abc"
        result, _get = self._fetch(next_link)
        self.assertEqual(len(result), 2)

    def test_next_link_to_external_host_is_blocked(self):
        for next_link in (
            "https://evil.example/v2.0/t/e/api/microsoft/analytics/v1.0/x",
            "https://api.businesscentral.dynamics.com.evil.example/v2.0/t/e/api/v2.0/x",
            "http://api.businesscentral.dynamics.com/v2.0/t/e/api/v2.0/x",
        ):
            with self.subTest(next_link=next_link), self.assertRaises(BusinessCentralResponseError):
                self._fetch(next_link)

    def test_next_link_into_another_api_is_blocked(self):
        cases = [
            (API_ANALYTICS, f"{API_ROOT}/{_ENDPOINT}?$skiptoken=abc"),
            (API_STANDARD, f"{ANALYTICS_ROOT}/{_ENDPOINT}?$skiptoken=abc"),
            (API_ANALYTICS, f"{ANALYTICS_ROOT}/../../v2.0/{_ENDPOINT}"),
            (API_STANDARD, f"https://api.businesscentral.dynamics.com/v2.0/{TENANT_ID}/Production/ODataV4/x"),
        ]
        for api, next_link in cases:
            with self.subTest(api=api, next_link=next_link), self.assertRaises(BusinessCentralResponseError):
                self._fetch(next_link, api=api)

    # ---- read-only ------------------------------------------------------

    def test_only_get_is_used(self):
        with (
            mock.patch("requests.get", return_value=_http(200, {"value": []})) as get,
            mock.patch("requests.post") as post,
            mock.patch("requests.put") as put,
            mock.patch("requests.patch") as patch,
            mock.patch("requests.delete") as delete,
            mock.patch("requests.request") as request,
        ):
            self.client_.fetch_purchase_orders()
            self.client_._fetch_all_pages(_ENDPOINT, api=API_ANALYTICS)
        self.assertEqual(get.call_count, 2)
        for method in (post, put, patch, delete, request):
            method.assert_not_called()

    def test_client_source_has_no_write_methods(self):
        source = inspect.getsource(client_module)
        self.assertIsNone(re.search(r"requests\.(post|put|patch|delete|request)\b", source))
        self.assertIsNone(re.search(r"\.(post|put|patch|delete)\(", source))
