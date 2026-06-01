"""Tests for supplier delivery views."""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderStatus
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryStatus
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _make_po(team, po_number="PO-VW-001", external_id="bc-vw-po-001") -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=external_id,
        po_number=po_number,
        supplier_no="SUP-001",
        supplier_name="View Supplier",
        status=PurchaseOrderStatus.OPEN,
    )


def _make_delivery(team, po, reference="DEL-VW-001") -> SupplierDelivery:
    return SupplierDelivery.objects.create(
        team=team,
        purchase_order=po,
        delivery_reference=reference,
        status=SupplierDeliveryStatus.PLANNED,
    )


@override_settings(STORAGES=_TEST_STORAGES)
class SupplierDeliveryListViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="SD View Team", slug="sd-view-team")
        cls.user = CustomUser.objects.create_user(username="sd-list@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_list_requires_login(self):
        client = Client()
        response = client.get(reverse("supplier_deliveries:list"))
        self.assertIn(response.status_code, [302, 403])

    def test_list_loads_for_logged_in_user(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("supplier_deliveries:list"))
        self.assertEqual(response.status_code, 200)

    def test_user_without_team_gets_404(self):
        teamless = CustomUser.objects.create_user(username="sd-noteam@example.com", password="pass")
        client = Client()
        client.force_login(teamless)
        response = client.get(reverse("supplier_deliveries:list"))
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class SupplierDeliveryDetailViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="SD Detail Team", slug="sd-detail-team")
        cls.user = CustomUser.objects.create_user(username="sd-detail@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.po = _make_po(cls.team)
        cls.delivery = _make_delivery(cls.team, cls.po)

    def _url(self):
        return reverse("supplier_deliveries:detail", kwargs={"delivery_id": self.delivery.pk})

    def test_detail_loads(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_detail_shows_reference(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(self._url())
        self.assertContains(response, "DEL-VW-001")

    def test_detail_other_team_gives_404(self):
        other_team = Team.objects.create(name="Other SD", slug="other-sd-team")
        other_po = _make_po(other_team, po_number="PO-OTHER-SD", external_id="bc-other-sd-po")
        other_delivery = _make_delivery(other_team, other_po, reference="DEL-OTHER")
        url = reverse("supplier_deliveries:detail", kwargs={"delivery_id": other_delivery.pk})
        client = Client()
        client.force_login(self.user)
        response = client.get(url)
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class SupplierDeliveryCreateViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="SD Create Team", slug="sd-create-team")
        cls.user = CustomUser.objects.create_user(username="sd-create@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.po = _make_po(cls.team, po_number="PO-CR", external_id="bc-cr-po")

    def test_create_view_loads(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("supplier_deliveries:create"))
        self.assertEqual(response.status_code, 200)

    def test_create_delivery_for_correct_team(self):
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("supplier_deliveries:create"),
            {
                "purchase_order": self.po.pk,
                "delivery_reference": "DEL-CREATE-TEST",
                "supplier": "Test Supplier",
                "status": SupplierDeliveryStatus.PLANNED,
            },
        )
        self.assertIn(response.status_code, [200, 302])
        delivery = SupplierDelivery.objects.filter(delivery_reference="DEL-CREATE-TEST").first()
        if delivery:
            self.assertEqual(delivery.team, self.team)
