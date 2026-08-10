"""Tests for procurement views."""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _make_po(team, po_number="PO-001", external_id="bc-v-po-001") -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=external_id,
        po_number=po_number,
        supplier_no="SUP-001",
        supplier_name="View Supplier",
        status="open",
    )


@override_settings(STORAGES=_TEST_STORAGES)
class PurchaseOrderListViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="View Team", slug="view-team-proc")
        cls.user = CustomUser.objects.create_user(username="proc-list@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_list_requires_login(self):
        client = Client()
        response = client.get(reverse("procurement:purchase_order_list"))
        self.assertIn(response.status_code, [302, 403])

    def test_list_loads_for_logged_in_user(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("procurement:purchase_order_list"))
        self.assertEqual(response.status_code, 200)

    def test_user_without_team_gets_404(self):
        teamless = CustomUser.objects.create_user(username="proc-noteam@example.com", password="pass")
        client = Client()
        client.force_login(teamless)
        response = client.get(reverse("procurement:purchase_order_list"))
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class PurchaseOrderDetailViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Detail Team", slug="detail-team-proc")
        cls.user = CustomUser.objects.create_user(username="proc-detail@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.po = _make_po(cls.team)
        PurchaseOrderLine.objects.create(
            team=cls.team,
            purchase_order=cls.po,
            external_id="bc-v-line-1",
            line_no="10000",
            item_no="ITEM-V01",
            description="Yoga mat",
            ordered_qty=100,
            shipped_qty=40,
            received_qty=10,
        )

    def _url(self):
        return reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": self.po.pk})

    def test_detail_loads(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_detail_shows_po_number(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(self._url())
        self.assertContains(response, "PO-001")

    def test_detail_shows_supplier(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(self._url())
        self.assertContains(response, "View Supplier")

    def test_detail_shows_fulfillment_numbers(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(self._url())
        self.assertEqual(response.status_code, 200)
        fulfillment = response.context["fulfillment"]
        self.assertEqual(fulfillment["ordered_qty"], 100)
        self.assertEqual(fulfillment["shipped_qty"], 40)
        self.assertEqual(fulfillment["received_qty"], 10)
        self.assertEqual(fulfillment["remaining_qty"], 90)

    def test_detail_other_team_gives_404(self):
        other_team = Team.objects.create(name="Other", slug="other-proc")
        other_po = _make_po(other_team, po_number="PO-OTHER", external_id="bc-other-po")
        url = reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": other_po.pk})
        client = Client()
        client.force_login(self.user)
        response = client.get(url)
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class PurchaseOrderListEmptyStateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Empty PO Team", slug="empty-po-team")
        cls.user = CustomUser.objects.create_user(username="proc-empty@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_list_empty_state_returns_200(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("procurement:purchase_order_list"))
        self.assertEqual(response.status_code, 200)
        # no POs created for this team — page_obj should be empty
        po_rows = response.context["po_rows"]
        self.assertEqual(len(po_rows), 0)


@override_settings(STORAGES=_TEST_STORAGES)
class PurchaseOrderListContentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Content PO Team", slug="content-po-team")
        cls.other_team = Team.objects.create(name="Other PO Team", slug="other-content-po-team")
        cls.user = CustomUser.objects.create_user(username="proc-content@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.po = _make_po(cls.team, po_number="PO-LIST-001", external_id="bc-list-po-001")
        _make_po(cls.other_team, po_number="PO-OTHER-LIST", external_id="bc-other-list-po")

    def test_list_shows_own_po_number(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("procurement:purchase_order_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PO-LIST-001")

    def test_list_does_not_show_other_team_po(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("procurement:purchase_order_list"))
        self.assertNotContains(response, "PO-OTHER-LIST")

    def test_anonymous_user_is_redirected(self):
        client = Client()
        response = client.get(reverse("procurement:purchase_order_list"))
        self.assertIn(response.status_code, [302, 403])
        if response.status_code == 302:
            self.assertIn("/login/", response.get("Location", ""))
