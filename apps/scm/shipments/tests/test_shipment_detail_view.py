"""Tests for the enhanced shipment detail view (Shipment Visibility)."""

import datetime

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine
from apps.scm.shipments.services import add_container_to_shipment, create_shipment
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryLine, SupplierDeliveryStatus
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team: Team, serial: str = "500001") -> Container:
    check = calculate_check_digit("MSC", "U", serial)
    return Container.objects.create(
        team=team,
        owner_code="MSC",
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_et(),
    )


def _make_user_and_team(username: str, slug: str):
    team = Team.objects.create(name=slug, slug=slug)
    user = CustomUser.objects.create_user(username=username, password="pass")
    team.members.add(user, through_defaults={"role": ROLE_MEMBER})
    return user, team


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentDetailVisibilityTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = _make_user_and_team("detail-vis@example.com", "detail-vis-team")
        cls.other_user, cls.other_team = _make_user_and_team("detail-other@example.com", "detail-other-team")
        cls.shipment = create_shipment(cls.team, cls.user, {"shipment_number": "VIS-001"})
        cls.other_shipment = create_shipment(cls.other_team, cls.other_user, {"shipment_number": "VIS-OTHER-1"})

    def _login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_detail_page_loads_for_correct_team(self):
        client = self._login(self.user)
        response = client.get(reverse("shipments:detail", args=[self.shipment.pk]))
        self.assertEqual(response.status_code, 200)

    def test_detail_shows_shipment_number(self):
        client = self._login(self.user)
        response = client.get(reverse("shipments:detail", args=[self.shipment.pk]))
        self.assertContains(response, "VIS-001")

    def test_detail_shows_containers(self):
        container = _container(self.team, serial="600001")
        add_container_to_shipment(self.team, self.shipment, container, self.user)
        client = self._login(self.user)
        response = client.get(reverse("shipments:detail", args=[self.shipment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("containers", response.context)

    def test_detail_shows_timeline(self):
        client = self._login(self.user)
        response = client.get(reverse("shipments:detail", args=[self.shipment.pk]))
        self.assertIn("timeline_events", response.context)

    def test_cross_team_access_blocked(self):
        client = self._login(self.other_user)
        # other_user cannot see team A's shipment
        response = client.get(reverse("shipments:detail", args=[self.shipment.pk]))
        self.assertEqual(response.status_code, 404)

    def test_detail_context_contains_purchase_orders(self):
        client = self._login(self.user)
        response = client.get(reverse("shipments:detail", args=[self.shipment.pk]))
        self.assertIn("purchase_orders", response.context)

    def test_detail_context_contains_supplier_deliveries(self):
        client = self._login(self.user)
        response = client.get(reverse("shipments:detail", args=[self.shipment.pk]))
        self.assertIn("supplier_deliveries", response.context)

    def test_purchase_orders_linked_via_container_shown(self):
        shipment = create_shipment(self.team, self.user, {"shipment_number": "VIS-PO-TEST"})
        container = _container(self.team, serial="700001")
        add_container_to_shipment(self.team, shipment, container, self.user)

        po = PurchaseOrder.objects.create(
            team=self.team,
            external_id="PO-VIS-1",
            po_number="PO-VIS-1",
            supplier_no="S1",
            supplier_name="VIS Supplier",
            order_date=datetime.date.today(),
        )
        pol = PurchaseOrderLine.objects.create(
            team=self.team,
            purchase_order=po,
            external_id="LINE-VIS-1",
            line_no="1",
            item_no="ITEM-VIS",
        )
        delivery = SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=po,
            delivery_reference="DEL-VIS-1",
            status=SupplierDeliveryStatus.SHIPPED,
        )
        SupplierDeliveryLine.objects.create(
            team=self.team,
            delivery=delivery,
            purchase_order_line=pol,
            container=container,
        )

        client = self._login(self.user)
        response = client.get(reverse("shipments:detail", args=[shipment.pk]))
        self.assertEqual(response.status_code, 200)
        pos = response.context["purchase_orders"]
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0].po_number, "PO-VIS-1")

    def test_supplier_deliveries_linked_via_container_shown(self):
        shipment = create_shipment(self.team, self.user, {"shipment_number": "VIS-SD-TEST"})
        container = _container(self.team, serial="800001")
        add_container_to_shipment(self.team, shipment, container, self.user)

        po = PurchaseOrder.objects.create(
            team=self.team,
            external_id="PO-VIS-SD",
            po_number="PO-VIS-SD",
            supplier_no="S2",
            supplier_name="SD Supplier",
            order_date=datetime.date.today(),
        )
        pol = PurchaseOrderLine.objects.create(
            team=self.team,
            purchase_order=po,
            external_id="LINE-VIS-SD",
            line_no="1",
            item_no="ITEM-SD",
        )
        delivery = SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=po,
            delivery_reference="DEL-VIS-SD",
            status=SupplierDeliveryStatus.IN_TRANSIT,
        )
        SupplierDeliveryLine.objects.create(
            team=self.team,
            delivery=delivery,
            purchase_order_line=pol,
            container=container,
        )

        client = self._login(self.user)
        response = client.get(reverse("shipments:detail", args=[shipment.pk]))
        self.assertEqual(response.status_code, 200)
        sds = response.context["supplier_deliveries"]
        self.assertEqual(len(sds), 1)
        self.assertEqual(sds[0].delivery_reference, "DEL-VIS-SD")
