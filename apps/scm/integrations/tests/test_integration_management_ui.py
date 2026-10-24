"""Settings → Integrations: create, edit, test and (de)activate a Business Central integration.

All Business Central / Entra HTTP is mocked — no network access, no real credentials.
"""

from unittest import mock

import requests
from django.test import TestCase
from django.urls import reverse

from apps.scm.integrations.credentials import get_integration_credentials, set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.scm.procurement.models import PurchaseOrder
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser

TENANT_ID = "11111111-2222-3333-4444-555555555555"
COMPANY_ID = "66666666-7777-8888-9999-000000000000"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SECRET = "super-secret-value-123"
TOKEN = "eyJ-access-token-value"

_AUTH_POST = "apps.scm.integrations.business_systems.business_central.auth.requests.post"
_CLIENT_GET = "apps.scm.integrations.business_systems.business_central.client.requests.get"
_BACKOFF = "apps.scm.integrations.business_systems.business_central.client.BusinessCentralClient._backoff"


def _response(status_code: int, payload: dict) -> mock.Mock:
    response = mock.Mock(status_code=status_code, headers={})
    response.json.return_value = payload
    return response


def _token_ok():
    return _response(200, {"access_token": TOKEN, "expires_in": 3600})


def _form_data(**overrides) -> dict:
    data = {
        "name": "BC Production",
        "provider": "business_central",
        "tenant_id": TENANT_ID,
        "environment": "Production",
        "company_id": COMPANY_ID,
        "client_id": CLIENT_ID,
        "client_secret": SECRET,
        "api_version": "v2.0",
        "is_active": "on",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def _user(team: Team, email: str, role: str = "admin") -> CustomUser:
    user = CustomUser.objects.create_user(username=email, email=email, password="pw")
    Membership.objects.create(team=team, user=user, role=role)
    return user


def _bc_integration(team: Team, **config) -> Integration:
    integration = Integration.objects.create(
        team=team,
        name="BC",
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        status=Integration.Status.PENDING,
        is_active=True,
        config={
            "tenant_id": TENANT_ID,
            "environment": "Production",
            "company_id": COMPANY_ID,
            "api_version": "v2.0",
            "sync_enabled": True,
            **config,
        },
    )
    set_integration_credentials(
        integration, IntegrationCredential.AuthType.OAUTH2, {"client_id": CLIENT_ID, "client_secret": SECRET}
    )
    return integration


class _LoginMixin:
    def _login(self, user, team):
        self.client.force_login(user)
        session = self.client.session
        session["team"] = team.pk
        session.save()


class CreateIntegrationTest(_LoginMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="create", slug="create")
        cls.admin = _user(cls.team, "create-admin@example.com")

    def setUp(self):
        self._login(self.admin, self.team)

    def test_empty_state_shows_add_button(self):
        resp = self.client.get(reverse("integrations:list"))
        self.assertContains(resp, "No Business Central integrations configured.")
        self.assertContains(resp, "Add Integration")
        self.assertContains(resp, reverse("integrations:create"))

    def test_form_defaults(self):
        resp = self.client.get(reverse("integrations:create"), HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 200)
        form = resp.context["form"]
        self.assertEqual(form["api_version"].value(), "v2.0")
        self.assertEqual(form["provider"].value(), "business_central")
        self.assertTrue(form["is_active"].value())
        for field in ("name", "tenant_id", "environment", "company_id", "client_id", "client_secret"):
            self.assertFalse(form[field].value())

    def test_create_stores_config_and_encrypted_credentials(self):
        resp = self.client.post(reverse("integrations:create"), _form_data(), HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["HX-Retarget"], "#integrations-panel")
        self.assertContains(resp, 'id="modal-container" hx-swap-oob="true"')
        self.assertContains(resp, "BC Production")
        self.assertNotContains(resp, SECRET)

        integration = Integration.objects.get(team=self.team, provider_code="business_central")
        self.assertEqual(integration.provider_family, Integration.ProviderFamily.BUSINESS_SYSTEM)
        self.assertEqual(integration.status, Integration.Status.PENDING)
        self.assertTrue(integration.is_active)
        self.assertEqual(integration.config["tenant_id"], TENANT_ID)
        self.assertEqual(integration.config["environment"], "Production")
        self.assertEqual(integration.config["company_id"], COMPANY_ID)
        self.assertEqual(integration.config["api_version"], "v2.0")
        # Safe default: saving never enables syncing.
        self.assertFalse(integration.config["sync_enabled"])
        self.assertFalse(integration.config["scheduled_sync_enabled"])
        # Secrets only in the encrypted credential row.
        self.assertNotIn(SECRET, str(integration.config))
        credential = IntegrationCredential.objects.get(integration=integration)
        self.assertTrue(credential.encrypted_data.startswith("fernet:v1:"))
        self.assertNotIn(SECRET, credential.encrypted_data)
        self.assertEqual(credential.team, self.team)
        self.assertEqual(get_integration_credentials(integration), {"client_id": CLIENT_ID, "client_secret": SECRET})

    def test_create_inactive(self):
        self.client.post(reverse("integrations:create"), _form_data(is_active=None), HTTP_HX_REQUEST="true")
        self.assertFalse(Integration.objects.get(team=self.team).is_active)

    def test_validation_errors_render_inline_without_saving(self):
        cases = {
            "company_id": "CRONUS",
            "tenant_id": "not a tenant",
            "client_id": "abc",
            "environment": "bad env!",
            "api_version": "2",
            "client_secret": "",
            "name": "",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                resp = self.client.post(
                    reverse("integrations:create"), _form_data(**{field: value}), HTTP_HX_REQUEST="true"
                )
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, "modal-open")
                self.assertContains(resp, "text-error")
                self.assertFalse(resp.has_header("HX-Retarget"))
                self.assertNotContains(resp, SECRET)
        self.assertFalse(Integration.objects.filter(team=self.team).exists())

    def test_tenant_domain_accepted(self):
        self.client.post(
            reverse("integrations:create"), _form_data(tenant_id="Contoso.onmicrosoft.com"), HTTP_HX_REQUEST="true"
        )
        self.assertEqual(Integration.objects.get(team=self.team).config["tenant_id"], "contoso.onmicrosoft.com")

    def test_second_integration_rejected_and_add_button_hidden(self):
        _bc_integration(self.team)
        resp = self.client.post(reverse("integrations:create"), _form_data(), HTTP_HX_REQUEST="true")
        self.assertContains(resp, "already has a Business Central integration")
        self.assertEqual(Integration.objects.filter(team=self.team).count(), 1)
        self.assertNotContains(self.client.get(reverse("integrations:list")), "Add Integration")

    def test_non_htmx_post_redirects(self):
        resp = self.client.post(reverse("integrations:create"), _form_data())
        self.assertRedirects(resp, reverse("integrations:list"))


class EditIntegrationTest(_LoginMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="edit", slug="edit")
        cls.admin = _user(cls.team, "edit-admin@example.com")

    def setUp(self):
        self.integration = _bc_integration(self.team)
        self._login(self.admin, self.team)

    def test_edit_form_prefills_settings_but_never_the_secret(self):
        resp = self.client.get(reverse("integrations:update", args=[self.integration.pk]), HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, TENANT_ID)
        self.assertContains(resp, COMPANY_ID)
        self.assertContains(resp, CLIENT_ID)
        self.assertNotContains(resp, SECRET)
        self.assertNotContains(resp, IntegrationCredential.objects.get(integration=self.integration).encrypted_data)
        self.assertContains(resp, "Leave blank to keep the current secret.")

    def test_edit_with_blank_secret_keeps_stored_secret(self):
        resp = self.client.post(
            reverse("integrations:update", args=[self.integration.pk]),
            _form_data(name="Renamed", environment="Sandbox", client_secret=""),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp["HX-Retarget"], "#integrations-panel")
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.name, "Renamed")
        self.assertEqual(self.integration.config["environment"], "Sandbox")
        # Existing sync settings are preserved, not reset.
        self.assertTrue(self.integration.config["sync_enabled"])
        self.assertEqual(get_integration_credentials(self.integration)["client_secret"], SECRET)

    def test_edit_replaces_secret_when_supplied(self):
        before = IntegrationCredential.objects.get(integration=self.integration).encrypted_data
        self.client.post(
            reverse("integrations:update", args=[self.integration.pk]),
            _form_data(client_secret="rotated-secret-456"),
            HTTP_HX_REQUEST="true",
        )
        credential = IntegrationCredential.objects.get(integration=self.integration)
        self.assertNotEqual(credential.encrypted_data, before)
        self.assertNotIn("rotated-secret-456", credential.encrypted_data)
        self.assertEqual(get_integration_credentials(self.integration)["client_secret"], "rotated-secret-456")
        self.assertEqual(IntegrationCredential.objects.filter(integration=self.integration).count(), 1)

    def test_secret_required_when_none_stored(self):
        IntegrationCredential.objects.filter(integration=self.integration).delete()
        resp = self.client.post(
            reverse("integrations:update", args=[self.integration.pk]),
            _form_data(client_secret=""),
            HTTP_HX_REQUEST="true",
        )
        self.assertContains(resp, "text-error")
        self.assertFalse(IntegrationCredential.objects.filter(integration=self.integration).exists())

    def test_list_shows_integration_without_secrets(self):
        resp = self.client.get(reverse("integrations:list"))
        for text in ("BC", "Business Central", "Production", "Pending", "Active", "Edit", "Test connection"):
            self.assertContains(resp, text)
        self.assertNotContains(resp, SECRET)
        self.assertNotContains(resp, "Add Integration")


class ActivationTest(_LoginMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="toggle", slug="toggle")
        cls.admin = _user(cls.team, "toggle-admin@example.com")

    def setUp(self):
        self.integration = _bc_integration(self.team)
        self._login(self.admin, self.team)

    def test_toggle_requires_post(self):
        self.assertEqual(self.client.get(reverse("integrations:toggle", args=[self.integration.pk])).status_code, 405)

    def test_deactivate_then_activate(self):
        url = reverse("integrations:toggle", args=[self.integration.pk])
        resp = self.client.post(url, HTTP_HX_REQUEST="true")
        self.assertContains(resp, "deactivated")
        self.assertContains(resp, 'id="integrations-panel"')
        self.integration.refresh_from_db()
        self.assertFalse(self.integration.is_active)
        # Credentials and config are kept.
        self.assertEqual(get_integration_credentials(self.integration)["client_secret"], SECRET)

        self.client.post(url, HTTP_HX_REQUEST="true")
        self.integration.refresh_from_db()
        self.assertTrue(self.integration.is_active)


class ConnectionTestTest(_LoginMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="conn", slug="conn")
        cls.admin = _user(cls.team, "conn-admin@example.com")

    def setUp(self):
        self.integration = _bc_integration(self.team)
        self.url = reverse("integrations:test_connection", args=[self.integration.pk])
        self._login(self.admin, self.team)

    def _post(self, token_response, get_side_effect):
        with (
            mock.patch(_AUTH_POST, return_value=token_response) as post,
            mock.patch(_CLIENT_GET, side_effect=get_side_effect) as get,
            mock.patch(_BACKOFF),
        ):
            resp = self.client.post(self.url, HTTP_HX_REQUEST="true")
        return resp, post, get

    def _assert_no_secrets(self, resp):
        self.assertNotContains(resp, SECRET)
        self.assertNotContains(resp, TOKEN)
        self.integration.refresh_from_db()
        self.assertNotIn(SECRET, self.integration.last_error_message)
        self.assertNotIn(TOKEN, self.integration.last_error_message)

    def test_successful_connection(self):
        companies = _response(200, {"value": [{"id": COMPANY_ID, "name": "CRONUS"}]})
        purchase_orders = _response(200, {"value": []})
        resp, post, get = self._post(_token_ok(), [companies, purchase_orders])

        self.assertContains(resp, "Connected to Business Central")
        self.assertContains(resp, "alert-success")
        # Client-credentials flow against the configured tenant.
        self.assertIn(TENANT_ID, post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "client_credentials")
        # Read-only: only GETs, against the configured environment.
        self.assertTrue(all("/Production/api/v2.0/" in c.args[0] for c in get.call_args_list))
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.status, Integration.Status.ACTIVE)
        self.assertIsNotNone(self.integration.last_success_at)
        self.assertIsNotNone(self.integration.last_tested_at)
        self._assert_no_secrets(resp)

    def test_successful_connection_does_not_import_data(self):
        companies = _response(200, {"value": [{"id": COMPANY_ID, "name": "CRONUS"}]})
        purchase_orders = _response(200, {"value": [{"id": "x"}]})
        self._post(_token_ok(), [companies, purchase_orders])
        self.assertFalse(self.integration.sync_runs.exists())
        self.assertFalse(PurchaseOrder.objects.filter(team=self.team).exists())

    def test_authentication_failure(self):
        resp, _, get = self._post(_response(401, {"error": "invalid_client", "error_description": SECRET}), [])
        self.assertContains(resp, "Connection test failed")
        self.assertContains(resp, "Authentication failure")
        self.assertContains(resp, "alert-error")
        get.assert_not_called()
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.status, Integration.Status.ERROR)
        self._assert_no_secrets(resp)

    def test_invalid_company_id(self):
        companies = _response(200, {"value": [{"id": "99999999-9999-9999-9999-999999999999", "name": "Other"}]})
        resp, _, _ = self._post(_token_ok(), [companies])
        self.assertContains(resp, "Invalid company")
        self.assertContains(resp, COMPANY_ID)
        self._assert_no_secrets(resp)

    def test_missing_permissions(self):
        forbidden = _response(403, {"error": {"code": "Authorization_InsufficientPermissions", "message": TOKEN}})
        resp, _, _ = self._post(_token_ok(), [forbidden])
        self.assertContains(resp, "Missing Business Central permissions")
        self._assert_no_secrets(resp)

    def test_missing_permissions_shows_admin_diagnostic(self):
        companies = _response(200, {"value": [{"id": COMPANY_ID, "name": "CRONUS"}]})
        message = (
            "Sorry, the current permissions prevented the action. "
            f"(TableData 5496 Purchase Order Entity Buffer Read: Base Application) {TOKEN} {SECRET}"
        )
        forbidden = _response(403, {"error": {"code": "Internal_ServerError", "message": message}})
        resp, _, _ = self._post(_token_ok(), [companies, forbidden])

        self.assertContains(resp, "Missing Business Central permissions")
        self.assertContains(resp, f"GET companies({COMPANY_ID})/purchaseOrders (HTTP 403)")
        self.assertContains(resp, "TableData 5496 &quot;Purchase Order Entity Buffer&quot;: Read (Base Application)")
        self._assert_no_secrets(resp)
        # The stored error (visible outside this admin action) keeps the general message.
        self.assertNotIn("Purchase Order Entity Buffer", self.integration.last_error_message)

    def test_timeout(self):
        resp, _, _ = self._post(_token_ok(), requests.Timeout("boom"))
        self.assertContains(resp, "Network error")
        self._assert_no_secrets(resp)

    def test_missing_credentials(self):
        IntegrationCredential.objects.filter(integration=self.integration).delete()
        resp, post, _ = self._post(_token_ok(), [])
        self.assertContains(resp, "Configuration error")
        post.assert_not_called()

    def test_non_htmx_from_detail_redirects_back(self):
        companies = _response(200, {"value": [{"id": COMPANY_ID, "name": "CRONUS"}]})
        with (
            mock.patch(_AUTH_POST, return_value=_token_ok()),
            mock.patch(_CLIENT_GET, side_effect=[companies, _response(200, {"value": []})]),
        ):
            resp = self.client.post(self.url, {"next": "detail"})
        self.assertRedirects(resp, reverse("integrations:detail", args=[self.integration.pk]))


class PermissionsAndIsolationTest(_LoginMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="iso", slug="iso")
        cls.admin = _user(cls.team, "iso-admin@example.com")
        cls.member = _user(cls.team, "iso-member@example.com", role="member")
        cls.other_team = Team.objects.create(name="iso-other", slug="iso-other")
        cls.other_admin = _user(cls.other_team, "iso-other@example.com")
        cls.integration = _bc_integration(cls.team)

    def _write_requests(self):
        pk = self.integration.pk
        return [
            ("get", reverse("integrations:create")),
            ("post", reverse("integrations:create")),
            ("get", reverse("integrations:update", args=[pk])),
            ("post", reverse("integrations:update", args=[pk])),
            ("post", reverse("integrations:test_connection", args=[pk])),
            ("post", reverse("integrations:toggle", args=[pk])),
        ]

    def test_member_gets_404_everywhere(self):
        self._login(self.member, self.team)
        with mock.patch(_AUTH_POST) as post:
            for method, url in self._write_requests():
                with self.subTest(method=method, url=url):
                    resp = getattr(self.client, method)(url, _form_data())
                    self.assertEqual(resp.status_code, 404)
        post.assert_not_called()
        self.assertEqual(Integration.objects.filter(team=self.team).count(), 1)

    def test_anonymous_redirected(self):
        for method, url in self._write_requests():
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(self.client, method)(url).status_code, 302)

    def test_other_team_admin_cannot_touch_integration(self):
        self._login(self.other_admin, self.other_team)
        pk = self.integration.pk
        with mock.patch(_AUTH_POST) as post:
            for url in (
                reverse("integrations:update", args=[pk]),
                reverse("integrations:test_connection", args=[pk]),
                reverse("integrations:toggle", args=[pk]),
            ):
                with self.subTest(url=url):
                    self.assertEqual(self.client.post(url, _form_data(name="Hijacked")).status_code, 404)
            self.assertEqual(self.client.get(reverse("integrations:update", args=[pk])).status_code, 404)
        post.assert_not_called()
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.name, "BC")
        self.assertTrue(self.integration.is_active)

    def test_other_team_creates_its_own_integration(self):
        self._login(self.other_admin, self.other_team)
        list_resp = self.client.get(reverse("integrations:list"))
        self.assertNotContains(list_resp, reverse("integrations:detail", args=[self.integration.pk]))
        self.assertContains(list_resp, "Add Integration")

        self.client.post(reverse("integrations:create"), _form_data(name="Other BC"), HTTP_HX_REQUEST="true")
        created = Integration.objects.get(team=self.other_team)
        self.assertEqual(IntegrationCredential.objects.get(integration=created).team, self.other_team)
        # The first team's integration and credentials are untouched.
        self.assertEqual(get_integration_credentials(self.integration)["client_secret"], SECRET)
        self.assertEqual(self.integration.name, Integration.objects.get(pk=self.integration.pk).name)
