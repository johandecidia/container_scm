"""Item ledger entry fetching through the Analytics API.

All HTTP is mocked (requests.get); OAuth is replaced with a fake. No live calls.
"""

from unittest import mock

import requests
from django.test import SimpleTestCase, TestCase

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import (
    BusinessCentralConfigurationError,
    BusinessCentralConnectionError,
    BusinessCentralPermissionError,
    BusinessCentralResponseError,
)
from apps.scm.integrations.business_systems.business_central.tests.test_client import (
    _CONFIG,
    COMPANY_ID,
    TENANT_ID,
    _FakeAuth,
    _http,
)
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.teams.models import Team

ANALYTICS_ROOT = f"https://api.businesscentral.dynamics.com/v2.0/{TENANT_ID}/Production/api/microsoft/analytics/v1.0"
ENDPOINT_URL = f"{ANALYTICS_ROOT}/companies({COMPANY_ID})/itemLedgerEntries"


class ItemLedgerClientTest(TestCase):
    def setUp(self):
        team = Team.objects.create(name="bc-ile-client", slug="bc-ile-client")
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

    def _pages(self, **kwargs):
        return list(self.client_.iter_item_ledger_entry_pages(**kwargs))

    def test_endpoint_order_and_no_filter_by_default(self):
        with mock.patch("requests.get", return_value=_http(200, {"value": [{"entryNo": 1}]})) as get:
            pages = self._pages()
        self.assertEqual(pages, [[{"entryNo": 1}]])
        self.assertEqual(get.call_args.args[0], ENDPOINT_URL)
        self.assertEqual(get.call_args.kwargs["params"], {"$orderby": "entryNo asc"})

    def test_cursor_prefix_and_serial_filters(self):
        with mock.patch("requests.get", return_value=_http(200, {"value": []})) as get:
            self._pages(after_entry_no=92057, item_no_prefixes=["CON", "MAFI-CW"], serial_only=True)
        self.assertEqual(
            get.call_args.kwargs["params"]["$filter"],
            "entryNo gt 92057 and (startswith(itemNo,'CON') or startswith(itemNo,'MAFI-CW')) and serialNo ne ''",
        )

    def test_unsafe_prefix_is_refused_before_any_request(self):
        for prefix in ("CON') or true or ('", "", "A" * 21, "CON X"):
            with (
                self.subTest(prefix=prefix),
                mock.patch("requests.get") as get,
                self.assertRaises(BusinessCentralConfigurationError),
            ):
                self._pages(item_no_prefixes=[prefix])
            get.assert_not_called()

    def test_non_integer_cursor_is_refused(self):
        with self.assertRaises(BusinessCentralConfigurationError):
            self._pages(after_entry_no="5 or 1 eq 1")

    def test_pages_follow_next_link_lazily(self):
        next_link = f"{ENDPOINT_URL}?$orderby=entryNo asc&aid=FIN&$skiptoken=2"
        page1 = _http(200, {"value": [{"entryNo": 1}, {"entryNo": 2}], "@odata.nextLink": next_link})
        page2 = _http(200, {"value": [{"entryNo": 3}]})
        with mock.patch("requests.get", side_effect=[page1, page2]) as get:
            pages = self.client_.iter_item_ledger_entry_pages()
            self.assertEqual(next(pages), [{"entryNo": 1}, {"entryNo": 2}])
            self.assertEqual(get.call_count, 1)  # the next page is only requested when asked for
            self.assertEqual(next(pages), [{"entryNo": 3}])
        self.assertEqual(get.call_args_list[1].args[0], next_link)

    def test_next_link_outside_analytics_api_is_blocked(self):
        bad = f"https://api.businesscentral.dynamics.com/v2.0/{TENANT_ID}/Production/api/v2.0/companies({COMPANY_ID})/x"
        page1 = _http(200, {"value": [{"entryNo": 1}], "@odata.nextLink": bad})
        with mock.patch("requests.get", return_value=page1), self.assertRaises(BusinessCentralResponseError):
            self._pages()

    def test_permission_error(self):
        resp = _http(403, {"error": {"code": "Authorization_InsufficientPermissions", "message": "no"}})
        with mock.patch("requests.get", return_value=resp), self.assertRaises(BusinessCentralPermissionError):
            self._pages()

    def test_timeout_is_retried_then_raised(self):
        with (
            mock.patch("requests.get", side_effect=requests.Timeout()) as get,
            mock.patch("time.sleep"),
            self.assertRaises(BusinessCentralConnectionError),
        ):
            self._pages()
        self.assertEqual(get.call_count, _CONFIG["max_retries"] + 1)

    def test_invalid_response_is_refused(self):
        for resp in (_http(200, raise_json=True), _http(200, {"value": {"entryNo": 1}})):
            with mock.patch("requests.get", return_value=resp), self.assertRaises(BusinessCentralResponseError):
                self._pages()

    def test_unsupported_odata_operation_is_a_response_error(self):
        resp = _http(501, {"error": {"code": "BadRequest_MethodNotImplemented"}})
        with mock.patch("requests.get", return_value=resp), self.assertRaises(BusinessCentralResponseError):
            self._pages()


class DummyItemLedgerClientTest(SimpleTestCase):
    def _entry_nos(self, **kwargs):
        pages = list(BusinessCentralClient(use_dummy=True).iter_item_ledger_entry_pages(**kwargs))
        return [r["entryNo"] for page in pages for r in page], len(pages)

    def test_fixture_is_paged_in_ascending_order(self):
        nos, pages = self._entry_nos()
        self.assertEqual(nos, sorted(nos))
        self.assertEqual(len(nos), 11)
        self.assertEqual(pages, 4)

    def test_filters_match_the_live_semantics(self):
        self.assertTrue(all(n > 92100 for n in self._entry_nos(after_entry_no=92100)[0]))
        self.assertNotIn(92200, self._entry_nos(item_no_prefixes=["CON", "MAFI"])[0])
        self.assertNotIn(92061, self._entry_nos(serial_only=True)[0])
