"""Tests for the shipment ETA engine (update_shipment_eta)."""

import datetime

from django.test import TestCase

from apps.scm.shipments.models import ShipmentEvent
from apps.scm.shipments.services import create_shipment, update_shipment_eta
from apps.teams.models import Team
from apps.users.models import CustomUser


class UpdateShipmentEtaTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="ETA Team", slug="eta-team")
        cls.user = CustomUser.objects.create_user(username="eta@example.com", password="pass")
        cls.shipment = create_shipment(cls.team, cls.user, {"shipment_number": "ETA-001"})

    def test_updates_current_eta(self):
        eta = datetime.date(2026, 8, 1)
        update_shipment_eta(self.shipment, eta, source="manual")
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.eta, eta)

    def test_saves_eta_source(self):
        update_shipment_eta(self.shipment, datetime.date(2026, 8, 5), source="carrier")
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.eta_source, "carrier")

    def test_sets_eta_last_updated(self):
        update_shipment_eta(self.shipment, datetime.date(2026, 8, 10), source="manual")
        self.shipment.refresh_from_db()
        self.assertIsNotNone(self.shipment.eta_last_updated)

    def test_saves_eta_confidence(self):
        update_shipment_eta(self.shipment, datetime.date(2026, 8, 15), source="tracking", confidence="high")
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.eta_confidence, "high")

    def test_sets_original_eta_on_first_update(self):
        shipment = create_shipment(self.team, self.user, {"shipment_number": "ETA-ORIG"})
        eta = datetime.date(2026, 9, 1)
        update_shipment_eta(shipment, eta, source="manual")
        shipment.refresh_from_db()
        self.assertEqual(shipment.original_eta, eta)

    def test_original_eta_not_overwritten_on_second_update(self):
        shipment = create_shipment(self.team, self.user, {"shipment_number": "ETA-ORIG2"})
        first_eta = datetime.date(2026, 9, 1)
        update_shipment_eta(shipment, first_eta, source="manual")
        update_shipment_eta(shipment, datetime.date(2026, 9, 15), source="carrier")
        shipment.refresh_from_db()
        self.assertEqual(shipment.original_eta, first_eta)

    def test_creates_eta_updated_event_when_eta_changes(self):
        shipment = create_shipment(self.team, self.user, {"shipment_number": "ETA-EVT"})
        update_shipment_eta(shipment, datetime.date(2026, 8, 20), source="manual")
        events = ShipmentEvent.objects.filter(shipment=shipment, event_type=ShipmentEvent.EventType.ETA_UPDATED)
        self.assertEqual(events.count(), 1)

    def test_no_event_when_eta_unchanged(self):
        eta = datetime.date(2026, 8, 25)
        shipment = create_shipment(self.team, self.user, {"shipment_number": "ETA-NOEVT"})
        update_shipment_eta(shipment, eta, source="manual")
        # Call again with same date
        update_shipment_eta(shipment, eta, source="carrier")
        events = ShipmentEvent.objects.filter(shipment=shipment, event_type=ShipmentEvent.EventType.ETA_UPDATED)
        self.assertEqual(events.count(), 1)
