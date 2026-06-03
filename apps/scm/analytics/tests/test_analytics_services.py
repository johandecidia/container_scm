"""Tests for extended analytics service functions: transit, carrier, container, supplier."""

import datetime

from django.test import TestCase
from django.utils import timezone

from apps.scm.analytics.services import (
    get_carrier_analytics,
    get_container_analytics,
    get_supplier_analytics,
    get_transit_time_analytics,
)
from apps.scm.containers.choices import ContainerStatus
from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderStatus
from apps.scm.shipments.models import Shipment
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryStatus
from apps.teams.models import Team


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team: Team, owner: str = "CSQ", serial: str = "305418", **kwargs) -> Container:
    check = calculate_check_digit(owner, "U", serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_et(),
        **kwargs,
    )


def _shipment(team: Team, **kwargs) -> Shipment:
    return Shipment.objects.create(team=team, **kwargs)


def _po(team: Team, supplier_no: str, supplier_name: str, **kwargs) -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=f"{supplier_no}-{PurchaseOrder.objects.count()}",
        po_number=f"PO-{PurchaseOrder.objects.count() + 1}",
        supplier_no=supplier_no,
        supplier_name=supplier_name,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Transit time analytics tests
# ---------------------------------------------------------------------------


class TransitTimeAnalyticsEmptyTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Transit Team", slug="transit-team")

    def test_returns_zero_count_when_no_data(self):
        result = get_transit_time_analytics(self.team)
        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["avg_days"])
        self.assertIsNone(result["min_days"])
        self.assertIsNone(result["max_days"])
        self.assertEqual(result["delayed_count"], 0)
        self.assertEqual(result["on_time_count"], 0)

    def test_ignores_shipments_without_dates(self):
        _shipment(self.team, status=Shipment.Status.DELIVERED)  # no dates
        result = get_transit_time_analytics(self.team)
        self.assertEqual(result["count"], 0)


class TransitTimeAnalyticsDataTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="TT Data", slug="tt-data-team")
        cls.other_team = Team.objects.create(name="Other TT", slug="other-tt-team")
        now = timezone.now()
        eta = (now - timezone.timedelta(days=15)).date()
        _shipment(
            cls.team,
            status=Shipment.Status.DELIVERED,
            actual_departure_at=now - timezone.timedelta(days=20),
            actual_arrival_at=now - timezone.timedelta(days=10),
            eta=eta,
        )
        _shipment(
            cls.team,
            status=Shipment.Status.DELIVERED,
            actual_departure_at=now - timezone.timedelta(days=15),
            actual_arrival_at=now - timezone.timedelta(days=5),
            eta=(now - timezone.timedelta(days=4)).date(),  # arrived on time
        )
        # Other team — should not affect
        _shipment(
            cls.other_team,
            status=Shipment.Status.DELIVERED,
            actual_departure_at=now - timezone.timedelta(days=30),
            actual_arrival_at=now,
        )

    def test_count(self):
        result = get_transit_time_analytics(self.team)
        self.assertEqual(result["count"], 2)

    def test_avg_days(self):
        result = get_transit_time_analytics(self.team)
        self.assertAlmostEqual(result["avg_days"], 10.0, places=1)

    def test_min_max_days(self):
        result = get_transit_time_analytics(self.team)
        self.assertAlmostEqual(result["min_days"], 10.0, places=1)
        self.assertAlmostEqual(result["max_days"], 10.0, places=1)

    def test_team_isolation(self):
        result_team = get_transit_time_analytics(self.team)
        result_other = get_transit_time_analytics(self.other_team)
        self.assertEqual(result_team["count"], 2)
        self.assertEqual(result_other["count"], 1)

    def test_delayed_vs_on_time_classification(self):
        result = get_transit_time_analytics(self.team)
        # First shipment arrived after its eta → delayed
        # Second shipment arrived before its eta → on time
        self.assertEqual(result["delayed_count"], 1)
        self.assertEqual(result["on_time_count"], 1)

    def test_date_filter_from(self):
        now = timezone.now().date()
        future_date = now + datetime.timedelta(days=1)
        result = get_transit_time_analytics(self.team, date_from=future_date)
        self.assertEqual(result["count"], 0)

    def test_date_filter_to(self):
        now = timezone.now().date()
        past_date = now - datetime.timedelta(days=100)
        result = get_transit_time_analytics(self.team, date_to=past_date)
        self.assertEqual(result["count"], 0)


# ---------------------------------------------------------------------------
# Carrier analytics tests
# ---------------------------------------------------------------------------


class CarrierAnalyticsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Carrier Team", slug="carrier-team")
        cls.other_team = Team.objects.create(name="Other Carrier", slug="other-carrier-team")
        now = timezone.now()
        # Maersk: 2 shipments, 1 delayed
        _shipment(
            cls.team, carrier="Maersk", status=Shipment.Status.IN_TRANSIT, eta=now.date() - datetime.timedelta(days=2)
        )
        _shipment(
            cls.team, carrier="Maersk", status=Shipment.Status.BOOKED, eta=now.date() + datetime.timedelta(days=5)
        )
        # CMA CGM: 1 exception
        _shipment(cls.team, carrier="CMA CGM", status=Shipment.Status.EXCEPTION)
        # Other team
        _shipment(cls.other_team, carrier="Maersk", status=Shipment.Status.IN_TRANSIT)

    def test_returns_list_of_carriers(self):
        result = get_carrier_analytics(self.team)
        carriers = [r["carrier"] for r in result]
        self.assertIn("Maersk", carriers)
        self.assertIn("CMA CGM", carriers)

    def test_team_isolation(self):
        result = get_carrier_analytics(self.team)
        maersk = next(r for r in result if r["carrier"] == "Maersk")
        self.assertEqual(maersk["shipment_count"], 2)

    def test_delayed_count(self):
        result = get_carrier_analytics(self.team)
        maersk = next(r for r in result if r["carrier"] == "Maersk")
        self.assertEqual(maersk["delayed_count"], 1)

    def test_exception_count(self):
        result = get_carrier_analytics(self.team)
        cma = next(r for r in result if r["carrier"] == "CMA CGM")
        self.assertEqual(cma["exception_count"], 1)

    def test_sorted_by_shipment_count_desc(self):
        result = get_carrier_analytics(self.team)
        counts = [r["shipment_count"] for r in result]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_empty_team(self):
        empty_team = Team.objects.create(name="Empty", slug="empty-carrier-team")
        result = get_carrier_analytics(empty_team)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Container analytics tests
# ---------------------------------------------------------------------------


class ContainerAnalyticsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Ctn Analytics", slug="ctn-analytics-team")
        cls.other_team = Team.objects.create(name="Other Ctn", slug="other-ctn-team")
        _container(cls.team, owner="AAA", serial="000001", status=ContainerStatus.AVAILABLE)
        _container(cls.team, owner="AAA", serial="000002", status=ContainerStatus.IN_TRANSIT)
        _container(cls.team, owner="AAA", serial="000003", status=ContainerStatus.BOOKED)
        _container(cls.other_team, owner="BBB", serial="111001", status=ContainerStatus.AVAILABLE)

    def test_total_count(self):
        result = get_container_analytics(self.team)
        self.assertEqual(result["total"], 3)

    def test_status_counts(self):
        result = get_container_analytics(self.team)
        self.assertEqual(result["available"], 1)
        self.assertEqual(result["in_transit"], 1)
        self.assertEqual(result["booked"], 1)

    def test_team_isolation(self):
        result = get_container_analytics(self.team)
        self.assertEqual(result["total"], 3)
        result_other = get_container_analytics(self.other_team)
        self.assertEqual(result_other["total"], 1)

    def test_empty_team(self):
        empty_team = Team.objects.create(name="Empty Ctn", slug="empty-ctn-team")
        result = get_container_analytics(empty_team)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["available"], 0)


# ---------------------------------------------------------------------------
# Supplier analytics tests
# ---------------------------------------------------------------------------


class SupplierAnalyticsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Supplier Team", slug="supplier-team")
        cls.other_team = Team.objects.create(name="Other Supplier", slug="other-supplier-team")
        cls.po1 = _po(cls.team, "S001", "Supplier A", status=PurchaseOrderStatus.OPEN)
        cls.po2 = _po(cls.team, "S001", "Supplier A", status=PurchaseOrderStatus.RELEASED)
        cls.po3 = _po(cls.team, "S002", "Supplier B", status=PurchaseOrderStatus.OPEN)
        _po(cls.other_team, "S001", "Supplier A", status=PurchaseOrderStatus.OPEN)

    def test_returns_per_supplier_data(self):
        result = get_supplier_analytics(self.team)
        nos = [r["supplier_no"] for r in result]
        self.assertIn("S001", nos)
        self.assertIn("S002", nos)

    def test_po_count_per_supplier(self):
        result = get_supplier_analytics(self.team)
        s001 = next(r for r in result if r["supplier_no"] == "S001")
        self.assertEqual(s001["po_count"], 2)

    def test_team_isolation(self):
        result = get_supplier_analytics(self.team)
        s001 = next(r for r in result if r["supplier_no"] == "S001")
        self.assertEqual(s001["po_count"], 2)  # not 3

    def test_sorted_by_po_count(self):
        result = get_supplier_analytics(self.team)
        counts = [r["po_count"] for r in result]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_delivery_counts(self):
        delivery = SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=self.po1,
            delivery_reference="DR-001",
            status=SupplierDeliveryStatus.RECEIVED,
        )
        result = get_supplier_analytics(self.team)
        s001 = next(r for r in result if r["supplier_no"] == "S001")
        self.assertEqual(s001["completed_delivery_count"], 1)
        delivery.delete()

    def test_empty_team(self):
        empty_team = Team.objects.create(name="Empty Supplier", slug="empty-supplier-team")
        result = get_supplier_analytics(empty_team)
        self.assertEqual(result, [])
