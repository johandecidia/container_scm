"""Tests for shipment status calculation."""

import datetime

from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.procurement.models import PurchaseOrder
from apps.scm.shipments.models import Shipment, ShipmentEvent
from apps.scm.shipments.services import (
    add_container_to_shipment,
    calculate_shipment_status,
    create_shipment,
    create_shipment_event,
    recalculate_and_save_shipment_status,
)
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryLine, SupplierDeliveryStatus
from apps.teams.models import Team
from apps.users.models import CustomUser


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team: Team, serial: str = "305418") -> Container:
    check = calculate_check_digit("CSQ", "U", serial)
    return Container.objects.create(
        team=team,
        owner_code="CSQ",
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_et(),
    )


class StatusCalculationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Status Calc Team", slug="status-calc-team")
        cls.user = CustomUser.objects.create_user(username="status@example.com", password="pass")

    def _shipment(self, number: str) -> Shipment:
        return create_shipment(self.team, self.user, {"shipment_number": number})

    def test_no_containers_gives_draft(self):
        s = self._shipment("SC-DRAFT")
        self.assertEqual(calculate_shipment_status(s), Shipment.Status.DRAFT)

    def test_container_linked_gives_booked(self):
        s = self._shipment("SC-BOOKED")
        container = _container(self.team, serial="111111")
        add_container_to_shipment(self.team, s, container, self.user)
        s.refresh_from_db()
        self.assertEqual(calculate_shipment_status(s), Shipment.Status.BOOKED)

    def test_actual_departure_gives_in_transit(self):
        s = self._shipment("SC-TRANSIT")
        s.actual_departure_at = timezone.now()
        s.save()
        self.assertEqual(calculate_shipment_status(s), Shipment.Status.IN_TRANSIT)

    def test_actual_arrival_gives_arrived(self):
        s = self._shipment("SC-ARRIVED")
        s.actual_arrival_at = timezone.now()
        s.save()
        self.assertEqual(calculate_shipment_status(s), Shipment.Status.ARRIVED)

    def test_exception_event_gives_exception(self):
        s = self._shipment("SC-EXCEPTION")
        create_shipment_event(s, ShipmentEvent.EventType.EXCEPTION, "Something went wrong")
        self.assertEqual(calculate_shipment_status(s), Shipment.Status.EXCEPTION)

    def test_cancelled_is_sticky(self):
        s = self._shipment("SC-CANCELLED")
        s.status = Shipment.Status.CANCELLED
        s.save()
        # Even without departure/arrival, remains cancelled
        self.assertEqual(calculate_shipment_status(s), Shipment.Status.CANCELLED)

    def test_arrived_with_all_deliveries_received_gives_delivered(self):
        s = self._shipment("SC-DELIVERED")
        container = _container(self.team, serial="222222")
        add_container_to_shipment(self.team, s, container, self.user)
        s.actual_arrival_at = timezone.now()
        s.save()

        po = PurchaseOrder.objects.create(
            team=self.team,
            external_id="PO-DEL-1",
            po_number="PO-DEL-1",
            supplier_no="SUP1",
            supplier_name="Supplier",
            order_date=datetime.date.today(),
        )
        delivery = SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=po,
            delivery_reference="DEL-001",
            status=SupplierDeliveryStatus.RECEIVED,
        )
        from apps.scm.procurement.models import PurchaseOrderLine

        pol = PurchaseOrderLine.objects.create(
            team=self.team,
            purchase_order=po,
            external_id="LINE-1",
            line_no="1",
            item_no="ITEM-1",
        )
        SupplierDeliveryLine.objects.create(
            team=self.team,
            delivery=delivery,
            purchase_order_line=pol,
            container=container,
        )

        self.assertEqual(calculate_shipment_status(s), Shipment.Status.DELIVERED)

    def test_arrived_with_partial_deliveries_gives_partially_received(self):
        s = self._shipment("SC-PARTIAL")
        container = _container(self.team, serial="333333")
        add_container_to_shipment(self.team, s, container, self.user)
        s.actual_arrival_at = timezone.now()
        s.save()

        po = PurchaseOrder.objects.create(
            team=self.team,
            external_id="PO-PART-1",
            po_number="PO-PART-1",
            supplier_no="SUP2",
            supplier_name="Supplier",
            order_date=datetime.date.today(),
        )
        from apps.scm.procurement.models import PurchaseOrderLine

        pol = PurchaseOrderLine.objects.create(
            team=self.team,
            purchase_order=po,
            external_id="LINE-2",
            line_no="1",
            item_no="ITEM-2",
        )
        # One received delivery
        received_delivery = SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=po,
            delivery_reference="DEL-RECV",
            status=SupplierDeliveryStatus.RECEIVED,
        )
        SupplierDeliveryLine.objects.create(
            team=self.team,
            delivery=received_delivery,
            purchase_order_line=pol,
            container=container,
        )
        # One in-transit delivery
        transit_delivery = SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=po,
            delivery_reference="DEL-TRANSIT",
            status=SupplierDeliveryStatus.IN_TRANSIT,
        )
        SupplierDeliveryLine.objects.create(
            team=self.team,
            delivery=transit_delivery,
            purchase_order_line=pol,
            container=container,
        )

        self.assertEqual(calculate_shipment_status(s), Shipment.Status.PARTIALLY_RECEIVED)

    def test_recalculate_and_save_updates_status(self):
        s = self._shipment("SC-SAVE")
        container = _container(self.team, serial="444444")
        add_container_to_shipment(self.team, s, container, self.user)
        s.refresh_from_db()
        self.assertEqual(s.status, Shipment.Status.DRAFT)  # not saved by add_container

        recalculate_and_save_shipment_status(s)
        s.refresh_from_db()
        self.assertEqual(s.status, Shipment.Status.BOOKED)
