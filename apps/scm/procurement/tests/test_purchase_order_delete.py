"""Tests for permanently deleting a purchase order from the list view."""

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
