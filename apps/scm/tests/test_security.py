"""Security hardening tests for SCM views (8.5).

Tests:
- Cross-tenant data isolation (team A cannot access team B data)
- Unauthenticated users are redirected / blocked
- GET requests cannot change state
- Integration credentials are not exposed in plain text
"""

from django.test import Client, TestCase
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment
from apps.teams.models import Team
from apps.users.models import CustomUser

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OWNER = "CSQ"
CAT = "U"
SERIAL = "305418"
CHECK = calculate_check_digit(OWNER, CAT, SERIAL)


def make_team(slug: str, name: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": name})[0]


def make_user(email: str) -> CustomUser:
    user = CustomUser.objects.get_or_create(username=email, defaults={"email": email})[0]
    user.set_password("testpass123")
    user.save()
    return user


def make_equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def make_container(team: Team) -> Container:
    eq = make_equipment_type()
    return Container.objects.create(
        team=team,
        owner_code=OWNER,
        category_id=CAT,
        serial_number=SERIAL,
        check_digit=CHECK,
        equipment_type=eq,
    )


def make_shipment(team: Team) -> Shipment:
    return Shipment.objects.create(team=team, shipment_number="SEC-SHIP-001")


# ---------------------------------------------------------------------------
# Unauthenticated access — must redirect, not 200 or 500
# ---------------------------------------------------------------------------


class UnauthenticatedAccessTests(TestCase):
    def setUp(self):
        self.team = make_team("sec-unauth", "Security Unauth")
        self.client = Client()

    def test_container_list_redirects_unauthenticated(self):
        response = self.client.get(reverse("containers:list"))
        self.assertIn(response.status_code, [302, 301])

    def test_shipment_list_redirects_unauthenticated(self):
        response = self.client.get(reverse("shipments:list"))
        self.assertIn(response.status_code, [302, 301])


# ---------------------------------------------------------------------------
# Cross-tenant isolation — team A user cannot access team B objects
# ---------------------------------------------------------------------------


class CrossTenantIsolationTests(TestCase):
    def setUp(self):
        self.team_a = make_team("sec-team-a", "Team A")
        self.team_b = make_team("sec-team-b", "Team B")
        self.user_a = make_user("user_a@example.com")
        self.user_b = make_user("user_b@example.com")

        # Link user_a to team_a, user_b to team_b
        from apps.teams.models import Membership

        Membership.objects.get_or_create(team=self.team_a, user=self.user_a, defaults={"role": "member"})
        Membership.objects.get_or_create(team=self.team_b, user=self.user_b, defaults={"role": "member"})

        self.container_b = make_container(self.team_b)
        self.shipment_b = make_shipment(self.team_b)

    def _login_as_a(self):
        client = Client()
        client.login(username="user_a@example.com", password="testpass123")
        # Set the session to use team_a as the default
        session = client.session
        session["current_team_id"] = self.team_a.pk
        session.save()
        return client

    def test_container_detail_cross_tenant_returns_404(self):
        client = self._login_as_a()
        # team A user tries to access team B container by ID
        url = reverse("containers:detail", args=[self.container_b.pk])
        response = client.get(url)
        # Should be 404, not 200
        self.assertEqual(response.status_code, 404)

    def test_shipment_detail_cross_tenant_returns_404(self):
        client = self._login_as_a()
        url = reverse("shipments:detail", args=[self.shipment_b.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# GET requests cannot change state
# ---------------------------------------------------------------------------


class GetCannotChangeStateTests(TestCase):
    def setUp(self):
        self.team = make_team("sec-get", "Security GET Test")
        self.user = make_user("sec_get@example.com")

        from apps.teams.models import Membership

        Membership.objects.get_or_create(team=self.team, user=self.user, defaults={"role": "member"})

        self.client = Client()
        self.client.login(username="sec_get@example.com", password="testpass123")
        session = self.client.session
        session["current_team_id"] = self.team.pk
        session.save()

        self.shipment = make_shipment(self.team)

    def test_shipment_cancel_via_get_does_not_change_state(self):
        """GET to cancel URL shows confirmation page but does NOT cancel the shipment."""
        original_status = self.shipment.status
        url = reverse("shipments:cancel", args=[self.shipment.pk])
        self.client.get(url)
        # Shipment status must be unchanged after GET
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, original_status)


# ---------------------------------------------------------------------------
# Integration credentials masking
# ---------------------------------------------------------------------------


class CredentialMaskingTests(TestCase):
    def test_mask_secret_returns_masked_string(self):
        from apps.scm.integrations.credentials import mask_secret

        result = mask_secret("sk_live_abc123secret")
        self.assertNotIn("abc123secret", result)
        self.assertIn("***", result)

    def test_mask_secret_handles_short_values(self):
        from apps.scm.integrations.credentials import mask_secret

        result = mask_secret("abc")
        # Short values should be fully masked
        self.assertIsInstance(result, str)
        self.assertNotEqual(result, "abc")

    def test_mask_secret_handles_empty_string(self):
        from apps.scm.integrations.credentials import mask_secret

        result = mask_secret("")
        self.assertIsInstance(result, str)
