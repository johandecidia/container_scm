"""Tests for read-only enforcement of Business Central purchase orders (UI + admin)."""

from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.scm.procurement.admin import PurchaseOrderAdmin, PurchaseOrderLineAdmin
from apps.scm.procurement.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderSource,
)
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser


def _member(team, email):
    user = CustomUser.objects.create_user(username=email, email=email, password="pw")
    Membership.objects.create(team=team, user=user, role="admin")
    return user


def _po(team, source, po_number="PO1", external_id=None):
    return PurchaseOrder.objects.create(
        team=team,
        external_id=external_id or f"{source}-{po_number}",
        po_number=po_number,
        source_system=source,
    )


class ReadOnlyFrontEndTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="ro", slug="ro")
        cls.user = _member(cls.team, "ro@example.com")
        cls.bc_po = _po(cls.team, PurchaseOrderSource.BUSINESS_CENTRAL, "BC-1")
        cls.manual_po = _po(cls.team, PurchaseOrderSource.MANUAL, "MAN-1")

    def _login(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["team"] = self.team.pk
        session.save()

    def test_list_view_works_and_shows_badge(self):
        self._login()
        resp = self.client.get(reverse("procurement:purchase_order_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Managed by Business Central")

    def test_detail_bc_shows_badge(self):
        self._login()
        resp = self.client.get(reverse("procurement:purchase_order_detail", args=[self.bc_po.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Managed by Business Central")

    def test_detail_manual_has_no_badge(self):
        self._login()
        resp = self.client.get(reverse("procurement:purchase_order_detail", args=[self.manual_po.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Managed by Business Central")

    def test_other_team_cannot_view_bc_po(self):
        other_team = Team.objects.create(name="ro-other", slug="ro-other")
        other_user = _member(other_team, "other@example.com")
        self.client.force_login(other_user)
        session = self.client.session
        session["team"] = other_team.pk
        session.save()
        resp = self.client.get(reverse("procurement:purchase_order_detail", args=[self.bc_po.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_manual_create_makes_manual_source(self):
        self._login()
        resp = self.client.post(
            reverse("procurement:purchase_order_create"),
            {"po_number": "NEW-1", "supplier_no": "S", "supplier_name": "Sup", "status": "open", "currency": "USD"},
        )
        self.assertEqual(resp.status_code, 302)
        created = PurchaseOrder.objects.get(team=self.team, po_number="NEW-1")
        self.assertEqual(created.source_system, PurchaseOrderSource.MANUAL)


class ReadOnlyAdminTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="ro-admin", slug="ro-admin")
        cls.bc_po = _po(cls.team, PurchaseOrderSource.BUSINESS_CENTRAL, "BC-1")
        cls.manual_po = _po(cls.team, PurchaseOrderSource.MANUAL, "MAN-1")
        cls.bc_line = PurchaseOrderLine.objects.create(
            team=cls.team, purchase_order=cls.bc_po, external_id="l1", line_no="1", item_no="A"
        )

    def setUp(self):
        self.factory = RequestFactory()
        self.superuser = CustomUser.objects.create_superuser(
            username="su@example.com", email="su@example.com", password="pw"
        )
        self.po_admin = PurchaseOrderAdmin(PurchaseOrder, AdminSite())
        self.line_admin = PurchaseOrderLineAdmin(PurchaseOrderLine, AdminSite())

    def _req(self):
        request = self.factory.get("/")
        request.user = self.superuser
        return request

    def test_bc_po_fields_readonly(self):
        ro = self.po_admin.get_readonly_fields(self._req(), obj=self.bc_po)
        self.assertIn("po_number", ro)
        self.assertIn("status", ro)
        self.assertIn("source_system", ro)

    def test_manual_po_fields_editable(self):
        ro = self.po_admin.get_readonly_fields(self._req(), obj=self.manual_po)
        self.assertNotIn("po_number", ro)

    def test_bc_po_cannot_be_deleted(self):
        self.assertFalse(self.po_admin.has_delete_permission(self._req(), obj=self.bc_po))

    def test_manual_po_can_be_deleted(self):
        self.assertTrue(self.po_admin.has_delete_permission(self._req(), obj=self.manual_po))

    def test_bc_line_readonly_and_undeletable(self):
        ro = self.line_admin.get_readonly_fields(self._req(), obj=self.bc_line)
        self.assertIn("ordered_qty", ro)
        self.assertFalse(self.line_admin.has_delete_permission(self._req(), obj=self.bc_line))
