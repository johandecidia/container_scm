"""Settings → Tracking: which carriers are offered, and that no secret ever leaves.

The credential tests are the important ones. A stored key must not appear in the
page, in the form that replaces it, or in the response to any action on it — and
replacing one half of a client id/secret pair must not silently blank the other.
"""

from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse

from apps.scm.integrations.credentials import get_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.scm.integrations.services import connect_carrier_integration
from apps.scm.team_settings.tracking_selectors import (
    get_carrier_settings_rows,
    get_usable_carrier_codes,
)
from apps.teams.models import Team
from apps.teams.roles import ROLE_ADMIN, ROLE_MEMBER
from apps.users.models import CustomUser

MAERSK_KEY = "maersk-consumer-key-should-never-be-rendered"
HAPAG_SECRET = "hapag-client-secret-should-never-be-rendered"


def _user(email: str) -> CustomUser:
    return CustomUser.objects.create(username=email, email=email)


class ConnectableCarrierTest(TestCase):
    """Only carriers with a working adapter and a live configuration are offered."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="MCR", slug="mcr-carriers")

    def test_the_three_carriers_with_real_clients_are_offered(self):
        codes = {row.provider_code for row in get_carrier_settings_rows(self.team)}
        self.assertEqual(codes, {"maersk", "cma_cgm", "hapag_lloyd"})

    def test_registered_stubs_are_not_offered(self):
        codes = {row.provider_code for row in get_carrier_settings_rows(self.team)}
        for stub in ("msc", "cosco", "one", "evergreen", "hmm", "yang_ming", "zim"):
            self.assertNotIn(stub, codes)

    def test_an_unconnected_carrier_reports_no_credentials_and_no_health(self):
        row = next(r for r in get_carrier_settings_rows(self.team) if r.provider_code == "maersk")
        self.assertFalse(row.is_connected)
        self.assertFalse(row.is_active)
        self.assertFalse(row.has_credentials)
        self.assertFalse(row.is_usable)
        self.assertIsNone(row.last_success_at)

    def test_credential_fields_follow_the_carriers_auth_style(self):
        rows = {r.provider_code: r for r in get_carrier_settings_rows(self.team)}
        self.assertEqual(rows["maersk"].credential_fields, ("api_key",))
        self.assertEqual(rows["cma_cgm"].credential_fields, ("api_key",))
        self.assertEqual(rows["hapag_lloyd"].credential_fields, ("client_id", "client_secret"))


class ConnectCarrierServiceTest(TestCase):
    """`connect_carrier_integration` leaves a carrier that is actually routable."""

    def setUp(self):
        self.team = Team.objects.create(name="MCR", slug="mcr-connect")

    def test_connecting_creates_an_active_carrier_integration_with_the_shipped_config(self):
        integration = connect_carrier_integration(self.team, "maersk", {"api_key": MAERSK_KEY})
        self.assertEqual(integration.team, self.team)
        self.assertEqual(integration.provider_family, Integration.ProviderFamily.CARRIER)
        self.assertTrue(integration.is_active)
        self.assertEqual(integration.config["base_url"], "https://api.maersk.com")

    def test_the_key_is_encrypted_and_not_stored_in_config(self):
        integration = connect_carrier_integration(self.team, "maersk", {"api_key": MAERSK_KEY})
        credential = IntegrationCredential.objects.get(integration=integration)
        self.assertNotIn(MAERSK_KEY, credential.encrypted_data)
        self.assertNotIn(MAERSK_KEY, str(integration.config))
        self.assertEqual(get_integration_credentials(integration), {"api_key": MAERSK_KEY})

    def test_a_tracking_provider_row_is_created_for_subscriptions_to_point_at(self):
        from apps.scm.tracking.models import TrackingProvider

        connect_carrier_integration(self.team, "maersk", {"api_key": MAERSK_KEY})
        self.assertTrue(TrackingProvider.objects.filter(code="maersk").exists())

    def test_replacing_one_half_of_a_pair_keeps_the_other(self):
        connect_carrier_integration(self.team, "hapag_lloyd", {"client_id": "id-one", "client_secret": HAPAG_SECRET})
        integration = connect_carrier_integration(self.team, "hapag_lloyd", {"client_secret": "rotated"})
        self.assertEqual(get_integration_credentials(integration), {"client_id": "id-one", "client_secret": "rotated"})

    def test_a_carrier_without_a_live_configuration_is_refused(self):
        with self.assertRaises(ValueError):
            connect_carrier_integration(self.team, "msc", {"api_key": "x"})

    def test_a_credential_key_the_auth_style_never_reads_is_refused(self):
        with self.assertRaises(ValueError):
            connect_carrier_integration(self.team, "maersk", {"client_secret": "x"})

    def test_reconnecting_does_not_create_a_second_integration(self):
        connect_carrier_integration(self.team, "maersk", {"api_key": MAERSK_KEY})
        connect_carrier_integration(self.team, "maersk", {"api_key": "rotated"})
        self.assertEqual(Integration.objects.filter(team=self.team, provider_code="maersk").count(), 1)


class TrackingSettingsViewTest(TestCase):
    """The page, the credential form and the actions on them."""

    def setUp(self):
        self.team = Team.objects.create(name="MCR", slug="mcr-tracking-views")
        self.admin = _user("admin@mcr.test")
        self.member = _user("member@mcr.test")
        self.team.members.add(self.admin, through_defaults={"role": ROLE_ADMIN})
        self.team.members.add(self.member, through_defaults={"role": ROLE_MEMBER})
        self.client = Client()
        self.client.force_login(self.admin)

    def test_a_non_admin_cannot_reach_any_tracking_settings_route(self):
        client = Client()
        client.force_login(self.member)
        routes = [
            ("get", reverse("team_settings:tracking")),
            ("get", reverse("team_settings:carrier_credentials", args=["maersk"])),
            ("post", reverse("team_settings:carrier_test_connection", args=["maersk"])),
            ("post", reverse("team_settings:carrier_toggle", args=["maersk"])),
        ]
        for method, url in routes:
            with self.subTest(url=url):
                self.assertEqual(getattr(client, method)(url).status_code, 404)

    def test_the_page_lists_the_connectable_carriers(self):
        response = self.client.get(reverse("team_settings:tracking"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Maersk")
        self.assertContains(response, "Hapag-Lloyd")
        self.assertContains(response, "CMA CGM")

    def test_an_unregistered_carrier_is_a_404(self):
        response = self.client.get(reverse("team_settings:carrier_credentials", args=["not-a-carrier"]))
        self.assertEqual(response.status_code, 404)

    def test_a_registered_stub_carrier_cannot_be_configured(self):
        response = self.client.get(reverse("team_settings:carrier_credentials", args=["msc"]))
        self.assertEqual(response.status_code, 404)

    def test_posting_credentials_connects_the_carrier(self):
        response = self.client.post(
            reverse("team_settings:carrier_credentials", args=["maersk"]),
            {"api_key": MAERSK_KEY, "test_connection_reference": "mrku1234567"},
        )
        self.assertEqual(response.status_code, 302)
        integration = Integration.objects.get(team=self.team, provider_code="maersk")
        self.assertEqual(get_integration_credentials(integration), {"api_key": MAERSK_KEY})
        self.assertEqual(integration.config["test_connection_reference"], "MRKU1234567")

    def test_a_saved_credential_form_sends_the_browser_back_to_the_page(self):
        # The form is in a modal: closing it is the redirect, not a panel swap.
        response = self.client.post(
            reverse("team_settings:carrier_credentials", args=["maersk"]),
            {"api_key": MAERSK_KEY},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], reverse("team_settings:tracking"))

    def test_a_rejected_credential_form_comes_back_as_the_form(self):
        response = self.client.post(
            reverse("team_settings:carrier_credentials", args=["maersk"]),
            {},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HX-Redirect", response)
        # The modal, not the panel it sits over.
        self.assertContains(response, "modal-box")
        self.assertNotContains(response, 'id="settings-tracking"')

    def test_the_stored_key_is_never_rendered_back(self):
        self.client.post(reverse("team_settings:carrier_credentials", args=["maersk"]), {"api_key": MAERSK_KEY})
        for url in (
            reverse("team_settings:tracking"),
            reverse("team_settings:carrier_credentials", args=["maersk"]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, MAERSK_KEY)

    def test_the_page_says_credentials_are_stored_without_showing_them(self):
        self.client.post(reverse("team_settings:carrier_credentials", args=["maersk"]), {"api_key": MAERSK_KEY})
        response = self.client.get(reverse("team_settings:tracking"))
        self.assertContains(response, "••••••••")
        self.assertNotContains(response, MAERSK_KEY)

    def test_an_empty_credential_form_for_an_unconnected_carrier_is_rejected(self):
        self.client.post(reverse("team_settings:carrier_credentials", args=["maersk"]), {})
        self.assertFalse(Integration.objects.filter(team=self.team, provider_code="maersk").exists())

    def test_a_connected_carrier_can_be_deactivated_and_reactivated(self):
        connect_carrier_integration(self.team, "maersk", {"api_key": MAERSK_KEY})
        toggle = reverse("team_settings:carrier_toggle", args=["maersk"])

        self.client.post(toggle)
        integration = Integration.objects.get(team=self.team, provider_code="maersk")
        self.assertFalse(integration.is_active)
        # Deactivation keeps the credentials — it takes the carrier out of service
        # rather than deleting the setup.
        self.assertEqual(get_integration_credentials(integration), {"api_key": MAERSK_KEY})

        self.client.post(toggle)
        integration.refresh_from_db()
        self.assertTrue(integration.is_active)

    def test_toggling_a_carrier_that_is_not_connected_is_a_404(self):
        response = self.client.post(reverse("team_settings:carrier_toggle", args=["maersk"]))
        self.assertEqual(response.status_code, 404)

    def test_a_successful_connection_test_is_recorded_on_the_integration(self):
        connect_carrier_integration(
            self.team, "maersk", {"api_key": MAERSK_KEY}, test_connection_reference="MRKU1234567"
        )
        with mock.patch(
            "apps.scm.integrations.carriers.maersk.client.MaerskClient.test_connection",
            return_value={"success": True, "message": "Connected to Maersk."},
        ):
            response = self.client.post(reverse("team_settings:carrier_test_connection", args=["maersk"]))
        self.assertEqual(response.status_code, 302)
        integration = Integration.objects.get(team=self.team, provider_code="maersk")
        self.assertIsNotNone(integration.last_success_at)
        self.assertIsNotNone(integration.last_tested_at)
        self.assertEqual(integration.status, Integration.Status.ACTIVE)

    def test_a_failing_connection_test_records_the_error_without_the_key(self):
        connect_carrier_integration(
            self.team, "maersk", {"api_key": MAERSK_KEY}, test_connection_reference="MRKU1234567"
        )
        with mock.patch(
            "apps.scm.integrations.carriers.maersk.client.MaerskClient.test_connection",
            side_effect=RuntimeError("401 Unauthorized"),
        ):
            self.client.post(reverse("team_settings:carrier_test_connection", args=["maersk"]))
        integration = Integration.objects.get(team=self.team, provider_code="maersk")
        self.assertEqual(integration.status, Integration.Status.ERROR)
        self.assertIn("401", integration.last_error_message)
        self.assertNotIn(MAERSK_KEY, integration.last_error_message)


class TrackingSettingsIsolationTest(TestCase):
    """One team's carrier configuration is invisible and untouchable from another."""

    def setUp(self):
        self.team = Team.objects.create(name="Ours", slug="ours-tracking")
        self.other_team = Team.objects.create(name="Theirs", slug="theirs-tracking")
        self.admin = _user("ours@mcr.test")
        self.team.members.add(self.admin, through_defaults={"role": ROLE_ADMIN})
        connect_carrier_integration(self.other_team, "maersk", {"api_key": "theirs-key"})
        self.client = Client()
        self.client.force_login(self.admin)

    def test_another_teams_integration_does_not_appear_as_connected(self):
        row = next(r for r in get_carrier_settings_rows(self.team) if r.provider_code == "maersk")
        self.assertFalse(row.is_connected)
        self.assertFalse(row.has_credentials)

    def test_another_teams_integration_cannot_be_toggled(self):
        response = self.client.post(reverse("team_settings:carrier_toggle", args=["maersk"]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Integration.objects.get(team=self.other_team, provider_code="maersk").is_active)

    def test_saving_credentials_creates_this_teams_own_integration(self):
        self.client.post(reverse("team_settings:carrier_credentials", args=["maersk"]), {"api_key": "ours-key"})
        ours = Integration.objects.get(team=self.team, provider_code="maersk")
        theirs = Integration.objects.get(team=self.other_team, provider_code="maersk")
        self.assertNotEqual(ours.pk, theirs.pk)
        self.assertEqual(get_integration_credentials(theirs), {"api_key": "theirs-key"})

    def test_usable_codes_are_scoped_to_the_team(self):
        self.assertEqual(get_usable_carrier_codes(self.team), set())
        self.assertEqual(get_usable_carrier_codes(self.other_team), {"maersk"})
