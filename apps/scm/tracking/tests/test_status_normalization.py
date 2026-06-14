"""Tests for tracking status normalization and event-to-entity linking.

Covers:
- normalize_event_type(): external status strings → internal TrackingEventType
- upsert_tracking_event() with shipment and container FK relationships
- ETAHistory record creation via update_shipment_eta()
"""

import datetime

from django.test import TestCase
from django.utils import timezone

from apps.scm.tracking.models import ETAHistory, TrackingEvent, TrackingProvider
from apps.scm.tracking.statuses import TrackingEventType, normalize_event_type
from apps.teams.models import Team


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _provider(code: str = "NORM_PROV") -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(
        code=code,
        defaults={"name": f"Provider {code}", "provider_type": TrackingProvider.ProviderType.MANUAL},
    )[0]


class NormalizeEventTypeTest(TestCase):
    """Tests for normalize_event_type: external → internal event type mapping."""

    def test_carrier_raw_status_is_normalized(self):
        """Known external status strings map to the correct internal EventType."""
        self.assertEqual(normalize_event_type("loaded on board"), TrackingEventType.LOADED_ON_VESSEL)
        self.assertEqual(normalize_event_type("discharged"), TrackingEventType.DISCHARGED)
        self.assertEqual(normalize_event_type("delivered"), TrackingEventType.DELIVERED)

    def test_normalize_is_case_insensitive(self):
        """Normalization ignores case."""
        self.assertEqual(normalize_event_type("LOADED ON VESSEL"), TrackingEventType.LOADED_ON_VESSEL)
        self.assertEqual(normalize_event_type("Gate In"), TrackingEventType.GATE_IN)
        self.assertEqual(normalize_event_type("DISCHARGED"), TrackingEventType.DISCHARGED)

    def test_normalize_strips_whitespace(self):
        """Leading and trailing whitespace is stripped before lookup."""
        self.assertEqual(normalize_event_type("  gate out  "), TrackingEventType.GATE_OUT)

    def test_unknown_status_returns_unknown(self):
        """An unmapped status string returns UNKNOWN."""
        self.assertEqual(normalize_event_type("some_random_status_xyz"), TrackingEventType.UNKNOWN)
        self.assertEqual(normalize_event_type("not_a_carrier_event"), TrackingEventType.UNKNOWN)

    def test_gate_in_variants_map_correctly(self):
        self.assertEqual(normalize_event_type("gate in"), TrackingEventType.GATE_IN)
        self.assertEqual(normalize_event_type("gate-in"), TrackingEventType.GATE_IN)

    def test_gate_out_variants_map_correctly(self):
        self.assertEqual(normalize_event_type("gate out"), TrackingEventType.GATE_OUT)
        self.assertEqual(normalize_event_type("gate-out"), TrackingEventType.GATE_OUT)

    def test_vessel_departure_variants_map_correctly(self):
        self.assertEqual(normalize_event_type("vessel departed"), TrackingEventType.VESSEL_DEPARTED)
        self.assertEqual(normalize_event_type("departure"), TrackingEventType.VESSEL_DEPARTED)

    def test_vessel_arrival_variants_map_correctly(self):
        self.assertEqual(normalize_event_type("vessel arrived"), TrackingEventType.VESSEL_ARRIVED)
        self.assertEqual(normalize_event_type("arrival"), TrackingEventType.VESSEL_ARRIVED)

    def test_eta_updated_maps_correctly(self):
        self.assertEqual(normalize_event_type("eta updated"), TrackingEventType.ETA_UPDATED)
        self.assertEqual(normalize_event_type("eta update"), TrackingEventType.ETA_UPDATED)

    def test_delay_maps_correctly(self):
        self.assertEqual(normalize_event_type("delay"), TrackingEventType.DELAY)
        self.assertEqual(normalize_event_type("delayed"), TrackingEventType.DELAY)

    def test_customs_hold_maps_correctly(self):
        self.assertEqual(normalize_event_type("customs hold"), TrackingEventType.CUSTOMS_HOLD)
        self.assertEqual(normalize_event_type("customs"), TrackingEventType.CUSTOMS_HOLD)

    def test_booking_created_maps_correctly(self):
        self.assertEqual(normalize_event_type("booking created"), TrackingEventType.BOOKING_CREATED)
        self.assertEqual(normalize_event_type("booking confirmed"), TrackingEventType.BOOKING_CREATED)

    def test_transshipment_variants_map_correctly(self):
        self.assertEqual(normalize_event_type("transshipment arrived"), TrackingEventType.TRANSSHIPMENT_ARRIVED)
        self.assertEqual(normalize_event_type("transshipment departed"), TrackingEventType.TRANSSHIPMENT_DEPARTED)

    def test_empty_released_maps_correctly(self):
        self.assertEqual(normalize_event_type("empty released"), TrackingEventType.EMPTY_RELEASED)
        self.assertEqual(normalize_event_type("empty container released"), TrackingEventType.EMPTY_RELEASED)

    def test_loaded_on_vessel_maps_correctly(self):
        self.assertEqual(normalize_event_type("loaded on vessel"), TrackingEventType.LOADED_ON_VESSEL)
        self.assertEqual(normalize_event_type("loaded on board"), TrackingEventType.LOADED_ON_VESSEL)
        self.assertEqual(normalize_event_type("load"), TrackingEventType.LOADED_ON_VESSEL)

    def test_discharged_variants_map_correctly(self):
        self.assertEqual(normalize_event_type("discharged"), TrackingEventType.DISCHARGED)
        self.assertEqual(normalize_event_type("discharge"), TrackingEventType.DISCHARGED)

    def test_return_value_is_valid_event_type(self):
        """All mapped results must be valid TrackingEvent.EventType choices."""
        valid_values = {c[0] for c in TrackingEvent.EventType.choices}
        for raw_status in ["gate in", "loaded on board", "vessel departed", "customs hold", "unknown_xyz"]:
            result = normalize_event_type(raw_status)
            self.assertIn(result, valid_values, f"normalize_event_type({raw_status!r}) returned invalid type")


class TrackingEventLinksTest(TestCase):
    """Test that tracking events correctly link to container and shipment."""

    @classmethod
    def setUpTestData(cls):
        from apps.scm.containers.models import Container, EquipmentType
        from apps.scm.containers.utils import calculate_check_digit
        from apps.scm.shipments.models import Shipment

        cls.team = _team("event-link-team")
        cls.provider = _provider("EVENT_LINK_PROV")

        eq = EquipmentType.objects.get_or_create(
            iso_code="20GP",
            defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
        )[0]
        check = calculate_check_digit("TRK", "U", "100001")
        cls.container = Container.objects.create(
            team=cls.team,
            owner_code="TRK",
            category_id="U",
            serial_number="100001",
            check_digit=check,
            equipment_type=eq,
        )
        cls.shipment = Shipment.objects.create(team=cls.team, shipment_number="LINK-001")

    def test_tracking_event_links_to_container_and_shipment(self):
        """An event created via upsert_tracking_event stores both container and shipment FK."""
        from apps.scm.tracking.services import upsert_tracking_event

        event, created = upsert_tracking_event(
            team=self.team,
            provider=self.provider,
            event_type=TrackingEvent.EventType.GATE_IN,
            event_datetime=timezone.now(),
            shipment=self.shipment,
            container=self.container,
            source_event_id="LINK-EVT-001",
        )

        self.assertTrue(created)
        self.assertEqual(event.shipment_id, self.shipment.pk)
        self.assertEqual(event.container_id, self.container.pk)
        self.assertEqual(event.team, self.team)

    def test_tracking_event_saved_to_history(self):
        """A new tracking event is persisted and can be retrieved from the database."""
        from apps.scm.tracking.services import upsert_tracking_event

        dt = timezone.now()
        event, created = upsert_tracking_event(
            team=self.team,
            provider=self.provider,
            event_type=TrackingEvent.EventType.LOADED_ON_VESSEL,
            event_datetime=dt,
            source_event_id="HISTORY-EVT-001",
        )

        self.assertTrue(created)
        stored = TrackingEvent.objects.get(pk=event.pk)
        self.assertEqual(stored.event_type, TrackingEvent.EventType.LOADED_ON_VESSEL)
        self.assertEqual(stored.team, self.team)

    def test_duplicate_tracking_event_is_ignored(self):
        """Submitting the same source_event_id twice creates only one event record."""
        from apps.scm.tracking.services import upsert_tracking_event

        dt = timezone.now()
        kwargs = dict(
            team=self.team,
            provider=self.provider,
            event_type=TrackingEvent.EventType.DISCHARGED,
            event_datetime=dt,
            source_event_id="DUP-EVT-001",
        )
        _, created1 = upsert_tracking_event(**kwargs)
        _, created2 = upsert_tracking_event(**kwargs)

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(TrackingEvent.objects.filter(source_event_id="DUP-EVT-001", team=self.team).count(), 1)


class ETAHistoryCreationTest(TestCase):
    """Test that ETAHistory records are created when the shipment ETA changes."""

    @classmethod
    def setUpTestData(cls):
        from apps.scm.shipments.services import create_shipment
        from apps.users.models import CustomUser

        cls.team = _team("eta-history-team")
        cls.user = CustomUser.objects.create_user(username="etahist@example.com", password="pass")
        cls.shipment = create_shipment(cls.team, cls.user, {"shipment_number": "ETAH-001"})

    def test_eta_change_creates_eta_history_record(self):
        """Changing the ETA via update_shipment_eta writes an ETAHistory entry."""
        from apps.scm.shipments.services import update_shipment_eta

        new_eta = datetime.date(2026, 9, 15)
        update_shipment_eta(self.shipment, new_eta, source="carrier")

        record = ETAHistory.objects.filter(shipment=self.shipment).first()
        self.assertIsNotNone(record)
        self.assertEqual(record.new_eta, new_eta)
        self.assertEqual(record.team, self.team)

    def test_eta_history_stores_previous_eta(self):
        """ETAHistory preserves the previous ETA value alongside the new one."""
        from apps.scm.shipments.services import create_shipment, update_shipment_eta
        from apps.users.models import CustomUser

        user = CustomUser.objects.create_user(username="etahist2@example.com", password="pass")
        shipment = create_shipment(self.team, user, {"shipment_number": "ETAH-002"})

        first_eta = datetime.date(2026, 9, 1)
        second_eta = datetime.date(2026, 9, 20)
        update_shipment_eta(shipment, first_eta, source="manual")
        update_shipment_eta(shipment, second_eta, source="carrier")

        records = list(ETAHistory.objects.filter(shipment=shipment).order_by("changed_at"))
        self.assertEqual(len(records), 2)
        self.assertIsNone(records[0].previous_eta)
        self.assertEqual(records[0].new_eta, first_eta)
        self.assertEqual(records[1].previous_eta, first_eta)
        self.assertEqual(records[1].new_eta, second_eta)

    def test_no_eta_history_when_eta_unchanged(self):
        """Setting the same ETA twice does not create duplicate ETAHistory records."""
        from apps.scm.shipments.services import create_shipment, update_shipment_eta
        from apps.users.models import CustomUser

        user = CustomUser.objects.create_user(username="etahist3@example.com", password="pass")
        shipment = create_shipment(self.team, user, {"shipment_number": "ETAH-003"})

        eta = datetime.date(2026, 10, 1)
        update_shipment_eta(shipment, eta, source="manual")
        update_shipment_eta(shipment, eta, source="carrier")  # Same date, no change

        count = ETAHistory.objects.filter(shipment=shipment).count()
        self.assertEqual(count, 1)
