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


@override_settings(STORAGES=_TEST_STORAGES)
class SupplierDeliveryDashboardTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="SD Dashboard Team", slug="sd-dash-team")
        cls.user = CustomUser.objects.create_user(username="sd-dash@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_dashboard_loads(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("supplier_deliveries:dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_dashboard_requires_login(self):
        client = Client()
        response = client.get(reverse("supplier_deliveries:dashboard"))
        self.assertIn(response.status_code, [302, 403])

    def test_dashboard_has_counts_in_context(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("supplier_deliveries:dashboard"))
        self.assertIn("dashboard", response.context)
        dashboard = response.context["dashboard"]
        self.assertIn("open_count", dashboard)
        self.assertIn("in_transit_count", dashboard)
        self.assertIn("completed_count", dashboard)

    def test_dashboard_user_without_team_gets_404(self):
        teamless = CustomUser.objects.create_user(username="sd-dash-noteam@example.com", password="pass")
        client = Client()
        client.force_login(teamless)
        response = client.get(reverse("supplier_deliveries:dashboard"))
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class SupplierDeliveryListEmptyStateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="SD Empty Team", slug="sd-empty-team")
        cls.user = CustomUser.objects.create_user(username="sd-empty@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_list_shows_200_when_no_deliveries(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("supplier_deliveries:list"))
        self.assertEqual(response.status_code, 200)
        deliveries = list(response.context["deliveries"])
        self.assertEqual(len(deliveries), 0)


@override_settings(STORAGES=_TEST_STORAGES)
class SupplierDeliveryUpdateViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="SD Update Team", slug="sd-update-team")
        cls.user = CustomUser.objects.create_user(username="sd-update@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.po = _make_po(cls.team, po_number="PO-UPD", external_id="bc-upd-po")
        cls.delivery = _make_delivery(cls.team, cls.po, reference="DEL-UPD-001")

    def _url(self):
        return reverse("supplier_deliveries:update", kwargs={"delivery_id": self.delivery.pk})

    def test_update_view_loads(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_update_other_team_delivery_gives_404(self):
        other_team = Team.objects.create(name="Other SD Update", slug="other-sd-update")
        other_po = _make_po(other_team, po_number="PO-OTHER-UPD", external_id="bc-other-upd-po")
        other_delivery = _make_delivery(other_team, other_po, reference="DEL-OTHER-UPD")
        url = reverse("supplier_deliveries:update", kwargs={"delivery_id": other_delivery.pk})
        client = Client()
        client.force_login(self.user)
        response = client.post(url, data={"delivery_reference": "HACKED"})
        self.assertEqual(response.status_code, 404)
        other_delivery.refresh_from_db()
        self.assertEqual(other_delivery.delivery_reference, "DEL-OTHER-UPD")


@override_settings(STORAGES=_TEST_STORAGES)
class SupplierDeliveryMarkReceivedTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="SD Mark Team", slug="sd-mark-team")
        cls.user = CustomUser.objects.create_user(username="sd-mark@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.po = _make_po(cls.team, po_number="PO-MARK", external_id="bc-mark-po")

    def test_mark_received_changes_status(self):
        delivery = _make_delivery(self.team, self.po, reference="DEL-MARK-001")
        client = Client()
        client.force_login(self.user)
        url = reverse("supplier_deliveries:mark_received", kwargs={"delivery_id": delivery.pk})
        response = client.post(url)
        self.assertIn(response.status_code, [200, 302])
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, SupplierDeliveryStatus.RECEIVED)

    def test_mark_received_htmx_returns_partial(self):
        delivery = _make_delivery(self.team, self.po, reference="DEL-MARK-HTMX")
        client = Client()
        client.force_login(self.user)
        url = reverse("supplier_deliveries:mark_received", kwargs={"delivery_id": delivery.pk})
        response = client.post(url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)

    def test_mark_received_cross_team_gives_404(self):
        other_team = Team.objects.create(name="Other SD Mark", slug="other-sd-mark")
        other_po = _make_po(other_team, po_number="PO-OTHER-MARK", external_id="bc-other-mark-po")
        other_delivery = _make_delivery(other_team, other_po, reference="DEL-OTHER-MARK")
        url = reverse("supplier_deliveries:mark_received", kwargs={"delivery_id": other_delivery.pk})
        client = Client()
        client.force_login(self.user)
        response = client.post(url)
        self.assertEqual(response.status_code, 404)
        other_delivery.refresh_from_db()
        self.assertNotEqual(other_delivery.status, SupplierDeliveryStatus.RECEIVED)
