"""Tests for permanently deleting a purchase order from the list view.

Deleting is allowed for orders SCM owns and refused for orders Business Central
owns. The refusal is enforced in the service, so the tests below check the service
as well as both request shapes that reach it — a guard that only lives in one
branch of one view is not a guard.
"""

from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.procurement.models import (
    PurchaseOrder,
    PurchaseOrderEvent,
    PurchaseOrderEventType,
    PurchaseOrderLine,
    PurchaseOrderSource,
)
from apps.scm.procurement.services import delete_purchase_order
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryLine
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser


def _member(team, email):
    user = CustomUser.objects.create_user(username=email, email=email, password="pw")
    Membership.objects.create(team=team, user=user, role="admin")
    return user


class PurchaseOrderDeleteTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="del", slug="del")
        self.user = _member(self.team, "del@example.com")
        self.po = PurchaseOrder.objects.create(
            team=self.team,
            external_id="manual-1",
            po_number="PO-DEL-1",
            source_system=PurchaseOrderSource.MANUAL,
        )
        self.line = PurchaseOrderLine.objects.create(
            team=self.team, purchase_order=self.po, external_id="l1", line_no="1", item_no="A"
        )
        self.event = PurchaseOrderEvent.objects.create(
            purchase_order=self.po, event_type=PurchaseOrderEventType.CREATED
        )
        equipment_type = EquipmentType.objects.get_or_create(
            iso_code="22G1",
            defaults={"category": "GP", "length_ft": 20, "description": "20GP"},
        )[0]
        self.container = Container.objects.create(
            team=self.team,
            owner_code="MSC",
            category_id="U",
            serial_number="123456",
            check_digit=6,
            equipment_type=equipment_type,
        )
        self.delivery = SupplierDelivery.objects.create(
            team=self.team, purchase_order=self.po, delivery_reference="D-1"
        )
        self.delivery_line = SupplierDeliveryLine.objects.create(
            team=self.team,
            delivery=self.delivery,
            purchase_order_line=self.line,
            container=self.container,
        )

    def _login(self, user=None, team=None):
        self.client.force_login(user or self.user)
        session = self.client.session
        session["team"] = (team or self.team).pk
        session.save()

    @property
    def _url(self):
        return reverse("procurement:purchase_order_delete", args=[self.po.pk])

    def test_list_shows_delete_button(self):
        self._login()
        resp = self.client.get(reverse("procurement:purchase_order_list"))
        self.assertContains(resp, f"po-row-{self.po.pk}")
        self.assertContains(resp, self._url)

    def test_delete_removes_po_and_related_records(self):
        self._login()
        resp = self.client.post(self._url)
        self.assertRedirects(resp, reverse("procurement:purchase_order_list"))
        self.assertFalse(PurchaseOrder.objects.filter(pk=self.po.pk).exists())
        self.assertFalse(PurchaseOrderLine.objects.filter(pk=self.line.pk).exists())
        self.assertFalse(PurchaseOrderEvent.objects.filter(pk=self.event.pk).exists())
        self.assertFalse(SupplierDelivery.objects.filter(pk=self.delivery.pk).exists())
        self.assertFalse(SupplierDeliveryLine.objects.filter(pk=self.delivery_line.pk).exists())

    def test_delete_keeps_the_container(self):
        self._login()
        self.client.post(self._url)
        self.assertTrue(Container.objects.filter(pk=self.container.pk).exists())

    def test_htmx_delete_returns_empty_body_to_remove_the_row(self):
        self._login()
        resp = self.client.delete(self._url, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"")
        self.assertFalse(PurchaseOrder.objects.filter(pk=self.po.pk).exists())

    def test_get_does_not_delete(self):
        self._login()
        resp = self.client.get(self._url)
        self.assertEqual(resp.status_code, 405)
        self.assertTrue(PurchaseOrder.objects.filter(pk=self.po.pk).exists())

    def test_other_team_cannot_delete(self):
        other_team = Team.objects.create(name="del-other", slug="del-other")
        other_user = _member(other_team, "other-del@example.com")
        self._login(other_user, other_team)
        resp = self.client.post(self._url)
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(PurchaseOrder.objects.filter(pk=self.po.pk).exists())

    def test_anonymous_cannot_delete(self):
        resp = self.client.post(self._url)
        self.assertNotEqual(resp.status_code, 200)
        self.assertTrue(PurchaseOrder.objects.filter(pk=self.po.pk).exists())


class BusinessCentralPurchaseOrderDeleteTest(TestCase):
    """A Business Central order cannot be deleted from SCM, by any route.

    BC is master for what it owns. Deleting one here would not remove anything at
    the source — the next sync recreates the order — but it would take the SCM-side
    deliveries and events with it. So the request has to be refused, not tidied up
    afterwards.
    """

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="bc-del", slug="bc-del")
        cls.user = _member(cls.team, "bc-del@example.com")
        cls.bc_po = PurchaseOrder.objects.create(
            team=cls.team,
            external_id="bc-1",
            po_number="PO-BC-1",
            source_system=PurchaseOrderSource.BUSINESS_CENTRAL,
        )
        cls.bc_line = PurchaseOrderLine.objects.create(
            team=cls.team, purchase_order=cls.bc_po, external_id="bl1", line_no="1", item_no="A"
        )
        cls.bc_delivery = SupplierDelivery.objects.create(
            team=cls.team, purchase_order=cls.bc_po, delivery_reference="D-BC-1"
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["team"] = self.team.pk
        session.save()

    @property
    def _url(self):
        return reverse("procurement:purchase_order_delete", args=[self.bc_po.pk])

    def _assert_survived(self):
        self.assertTrue(PurchaseOrder.objects.filter(pk=self.bc_po.pk).exists())
        self.assertTrue(PurchaseOrderLine.objects.filter(pk=self.bc_line.pk).exists())
        self.assertTrue(SupplierDelivery.objects.filter(pk=self.bc_delivery.pk).exists())

    def test_the_service_refuses_a_business_central_order(self):
        with self.assertRaises(PermissionDenied):
            delete_purchase_order(purchase_order=self.bc_po)
        self._assert_survived()

    def test_a_post_to_the_endpoint_is_refused_and_says_why(self):
        resp = self.client.post(self._url, follow=True)

        self.assertRedirects(resp, reverse("procurement:purchase_order_list"))
        self.assertContains(resp, "managed by Business Central and cannot be deleted")
        self._assert_survived()

    def test_an_htmx_delete_is_refused_with_403_so_the_row_is_not_swapped_away(self):
        resp = self.client.delete(self._url, HTTP_HX_REQUEST="true")

        # htmx does not swap on a 4xx, so the row survives along with the order.
        self.assertEqual(resp.status_code, 403)
        self._assert_survived()

    def test_the_list_offers_no_delete_button_for_a_business_central_order(self):
        resp = self.client.get(reverse("procurement:purchase_order_list"))

        self.assertContains(resp, f"po-row-{self.bc_po.pk}")
        self.assertNotContains(resp, self._url)

    def test_the_workspace_offers_no_delete_action(self):
        resp = self.client.get(reverse("procurement:purchase_order_detail", args=[self.bc_po.pk]))

        self.assertContains(resp, "Managed by Business Central")
        self.assertNotContains(resp, self._url)

    def test_another_teams_business_central_order_is_not_reachable(self):
        other_team = Team.objects.create(name="bc-del-other", slug="bc-del-other")
        other_user = _member(other_team, "other-bc-del@example.com")
        self.client.force_login(other_user)
        session = self.client.session
        session["team"] = other_team.pk
        session.save()

        resp = self.client.post(self._url)

        self.assertEqual(resp.status_code, 404)
        self._assert_survived()
