"""Tests for SCM alerts service."""

import datetime

from django.test import TestCase
from django.utils import timezone

from apps.scm.analytics.alerts import get_scm_alerts
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderStatus
from apps.scm.shipments.models import Shipment
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryStatus
from apps.teams.models import Team


def _shipment(team: Team, **kwargs) -> Shipment:
    return Shipment.objects.create(team=team, **kwargs)


class SCMAlertDelayedShipmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Alert Team", slug="alert-team")
        cls.other_team = Team.objects.create(name="Other Alert", slug="other-alert-team")

    def test_delayed_shipment_generates_warning(self):
        yesterday = timezone.now().date() - datetime.timedelta(days=1)
        _shipment(
            self.team,
            status=Shipment.Status.IN_TRANSIT,
            eta=yesterday,
            shipment_number="SHP-001",
        )
        alerts = get_scm_alerts(self.team)
        types = [a.type for a in alerts]
        self.assertIn("delayed_shipment", types)
        delayed = next(a for a in alerts if a.type == "delayed_shipment")
        self.assertEqual(delayed.severity, "warning")

    def test_on_time_shipment_no_alert(self):
        tomorrow = timezone.now().date() + datetime.timedelta(days=1)
        _shipment(
            self.team,
            status=Shipment.Status.IN_TRANSIT,
            eta=tomorrow,
        )
        alerts = get_scm_alerts(self.team)
        types = [a.type for a in alerts]
        self.assertNotIn("delayed_shipment", types)

    def test_team_isolation(self):
        yesterday = timezone.now().date() - datetime.timedelta(days=1)
        _shipment(
            self.other_team,
            status=Shipment.Status.IN_TRANSIT,
            eta=yesterday,
        )
        alerts = get_scm_alerts(self.team)
        types = [a.type for a in alerts]
        self.assertNotIn("delayed_shipment", types)


class SCMAlertExceptionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Exception Team", slug="exception-team")

    def test_exception_shipment_generates_error(self):
        _shipment(self.team, status=Shipment.Status.EXCEPTION, shipment_number="EXCEP-001")
        alerts = get_scm_alerts(self.team)
        types = [a.type for a in alerts]
        self.assertIn("shipment_exception", types)
        exc = next(a for a in alerts if a.type == "shipment_exception")
        self.assertEqual(exc.severity, "error")


class SCMAlertOverdueDeliveryTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Overdue Team", slug="overdue-team")
        cls.po = PurchaseOrder.objects.create(
            team=cls.team,
            external_id="EXT-001",
            po_number="PO-001",
            supplier_no="S001",
            supplier_name="Test Supplier",
            status=PurchaseOrderStatus.OPEN,
        )

    def test_overdue_delivery_generates_warning(self):
        yesterday = timezone.now().date() - datetime.timedelta(days=1)
        SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=self.po,
            delivery_reference="DR-001",
            status=SupplierDeliveryStatus.SHIPPED,
            planned_arrival_date=yesterday,
        )
        alerts = get_scm_alerts(self.team)
        types = [a.type for a in alerts]
        self.assertIn("overdue_delivery", types)
        alert = next(a for a in alerts if a.type == "overdue_delivery")
        self.assertEqual(alert.severity, "warning")

    def test_received_delivery_no_alert(self):
        yesterday = timezone.now().date() - datetime.timedelta(days=1)
        SupplierDelivery.objects.create(
            team=self.team,
            purchase_order=self.po,
            delivery_reference="DR-002",
            status=SupplierDeliveryStatus.RECEIVED,
            planned_arrival_date=yesterday,
        )
        alerts = get_scm_alerts(self.team)
        types = [a.type for a in alerts]
        self.assertNotIn("overdue_delivery", types)


class SCMAlertEmptyTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Clean Team", slug="clean-team")

    def test_no_alerts_for_clean_team(self):
        alerts = get_scm_alerts(self.team)
        self.assertEqual(alerts, [])
