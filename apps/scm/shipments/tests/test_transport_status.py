"""Tests for deriving shipment transport state and ETA from carrier events.

The rule under test throughout: only actual events move a shipment forward. A
forecast must never make a shipment look arrived or delivered.
"""

from datetime import UTC, datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.shipments.models import Shipment, ShipmentContainer, ShipmentEvent
from apps.scm.shipments.services import calculate_eta_delta_minutes, update_shipment_eta
from apps.scm.shipments.transport_status import apply_tracking_to_shipment, get_transport_snapshot
from apps.scm.tracking.models import ETAHistory, TrackingEvent, TrackingProvider
from apps.teams.models import Team

DEPARTED_AT = datetime(2024, 3, 10, 8, 0, tzinfo=UTC)
ARRIVED_AT = datetime(2024, 3, 25, 14, 0, tzinfo=UTC)
ESTIMATED_ARRIVAL = datetime(2024, 3, 24, 6, 0, tzinfo=UTC)


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


class TransportStatusTestBase(TestCase):
    def setUp(self):
        self.team = _team(self.team_slug)
        self.provider = TrackingProvider.objects.get_or_create(code="maersk", defaults={"name": "Maersk"})[0]
        self.shipment = Shipment.objects.create(
            team=self.team, shipment_number=f"SHP-{self.team_slug}", carrier="Maersk"
        )

    def _event(self, event_type, when, *, time_type=TrackingEvent.EventTimeType.ACTUAL, **kwargs):
        defaults = {
            "team": self.team,
            "provider": self.provider,
            "shipment": self.shipment,
            "event_type": event_type,
            "event_time_type": time_type,
            "event_datetime": when,
            "event_fingerprint": f"{event_type}-{time_type}-{when.isoformat()}",
        }
        defaults.update(kwargs)
        return TrackingEvent.objects.create(**defaults)


class SnapshotTest(TransportStatusTestBase):
    team_slug = "transport-snapshot"

    def test_no_events_gives_an_empty_snapshot(self):
        snapshot = get_transport_snapshot(self.shipment)
        self.assertIsNone(snapshot.actual_departure_at)
        self.assertIsNone(snapshot.actual_arrival_at)
        self.assertFalse(snapshot.has_departed)

    def test_actual_departure_is_picked_up(self):
        self._event(TrackingEvent.EventType.VESSEL_DEPARTED, DEPARTED_AT)
        snapshot = get_transport_snapshot(self.shipment)
        self.assertEqual(snapshot.actual_departure_at, DEPARTED_AT)
        self.assertTrue(snapshot.has_departed)

    def test_actual_arrival_is_picked_up(self):
        self._event(TrackingEvent.EventType.VESSEL_ARRIVED, ARRIVED_AT)
        self.assertEqual(get_transport_snapshot(self.shipment).actual_arrival_at, ARRIVED_AT)

    def test_estimated_arrival_is_not_an_arrival(self):
        """The central rule: a forecast is not an event that happened."""
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ESTIMATED_ARRIVAL,
            time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        snapshot = get_transport_snapshot(self.shipment)
        self.assertIsNone(snapshot.actual_arrival_at)
        self.assertFalse(snapshot.has_arrived)
        self.assertIsNotNone(snapshot.latest_estimated_arrival)

    def test_planned_event_is_not_an_arrival_either(self):
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ESTIMATED_ARRIVAL,
            time_type=TrackingEvent.EventTimeType.PLANNED,
        )
        self.assertIsNone(get_transport_snapshot(self.shipment).actual_arrival_at)

    def test_unclassified_event_is_not_treated_as_actual(self):
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ARRIVED_AT,
            time_type=TrackingEvent.EventTimeType.UNKNOWN,
        )
        self.assertIsNone(get_transport_snapshot(self.shipment).actual_arrival_at)

    def test_earliest_matching_event_wins(self):
        """Carriers backfill; the first actual departure is the departure."""
        self._event(TrackingEvent.EventType.LOADED_ON_VESSEL, DEPARTED_AT)
        self._event(TrackingEvent.EventType.VESSEL_DEPARTED, DEPARTED_AT + timedelta(hours=6))
        self.assertEqual(get_transport_snapshot(self.shipment).actual_departure_at, DEPARTED_AT)

    def test_events_without_a_time_are_ignored(self):
        self._event(TrackingEvent.EventType.VESSEL_DEPARTED, DEPARTED_AT).delete()
        TrackingEvent.objects.create(
            team=self.team,
            provider=self.provider,
            shipment=self.shipment,
            event_type=TrackingEvent.EventType.VESSEL_DEPARTED,
            event_time_type=TrackingEvent.EventTimeType.ACTUAL,
            event_datetime=None,
            event_fingerprint="no-time",
        )
        self.assertIsNone(get_transport_snapshot(self.shipment).actual_departure_at)

    def test_another_teams_events_are_not_used(self):
        other_team = _team("transport-snapshot-other")
        TrackingEvent.objects.create(
            team=other_team,
            provider=self.provider,
            shipment=self.shipment,
            event_type=TrackingEvent.EventType.VESSEL_ARRIVED,
            event_time_type=TrackingEvent.EventTimeType.ACTUAL,
            event_datetime=ARRIVED_AT,
            event_fingerprint="other-team-arrival",
        )
        self.assertIsNone(get_transport_snapshot(self.shipment).actual_arrival_at)


class ApplyTrackingToShipmentTest(TransportStatusTestBase):
    team_slug = "transport-apply"

    def _with_container(self):
        equipment_type = EquipmentType.objects.get_or_create(
            iso_code="22G1",
            defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
        )[0]
        container = Container.objects.create(
            team=self.team,
            owner_code="MRK",
            category_id="U",
            serial_number="123456",
            check_digit=3,
            equipment_type=equipment_type,
        )
        ShipmentContainer.objects.create(shipment=self.shipment, container=container)
        return container

    def test_departure_moves_the_shipment_in_transit(self):
        self._with_container()
        self._event(TrackingEvent.EventType.VESSEL_DEPARTED, DEPARTED_AT)
        apply_tracking_to_shipment(self.shipment)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.actual_departure_at, DEPARTED_AT)
        self.assertEqual(self.shipment.status, Shipment.Status.IN_TRANSIT)

    def test_arrival_moves_the_shipment_arrived(self):
        self._with_container()
        self._event(TrackingEvent.EventType.VESSEL_DEPARTED, DEPARTED_AT)
        self._event(TrackingEvent.EventType.VESSEL_ARRIVED, ARRIVED_AT)
        apply_tracking_to_shipment(self.shipment)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.actual_arrival_at, ARRIVED_AT)
        self.assertEqual(self.shipment.status, Shipment.Status.ARRIVED)

    def test_estimated_arrival_does_not_mark_the_shipment_arrived(self):
        self._with_container()
        self._event(TrackingEvent.EventType.VESSEL_DEPARTED, DEPARTED_AT)
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ESTIMATED_ARRIVAL,
            time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        apply_tracking_to_shipment(self.shipment)
        self.shipment.refresh_from_db()
        self.assertIsNone(self.shipment.actual_arrival_at)
        self.assertEqual(self.shipment.status, Shipment.Status.IN_TRANSIT)

    def test_tracking_status_reflects_the_latest_actual_event(self):
        self._event(TrackingEvent.EventType.VESSEL_DEPARTED, DEPARTED_AT)
        apply_tracking_to_shipment(self.shipment)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.tracking_status, "Vessel Departed")
        self.assertIsNotNone(self.shipment.last_tracking_sync_at)

    def test_one_internal_event_per_transition_not_per_carrier_event(self):
        """Carrier events live in TrackingEvent; the timeline must not say it twice."""
        self._with_container()
        for offset in range(4):
            self._event(TrackingEvent.EventType.GATE_IN, DEPARTED_AT + timedelta(hours=offset))
        self._event(TrackingEvent.EventType.VESSEL_DEPARTED, DEPARTED_AT + timedelta(days=1))
        apply_tracking_to_shipment(self.shipment)

        tracking_updates = ShipmentEvent.objects.filter(
            shipment=self.shipment, event_type=ShipmentEvent.EventType.TRACKING_UPDATED
        )
        self.assertEqual(tracking_updates.count(), 1)
        self.assertEqual(tracking_updates.first().metadata["new_status"], Shipment.Status.IN_TRANSIT)

    def test_no_transition_records_no_internal_event(self):
        self._with_container()
        self._event(TrackingEvent.EventType.VESSEL_DEPARTED, DEPARTED_AT)
        apply_tracking_to_shipment(self.shipment)
        apply_tracking_to_shipment(self.shipment)
        self.assertEqual(
            ShipmentEvent.objects.filter(
                shipment=self.shipment, event_type=ShipmentEvent.EventType.TRACKING_UPDATED
            ).count(),
            1,
        )

    def test_derivation_is_idempotent(self):
        self._with_container()
        self._event(TrackingEvent.EventType.VESSEL_ARRIVED, ARRIVED_AT)
        apply_tracking_to_shipment(self.shipment)
        first_status = Shipment.objects.get(pk=self.shipment.pk).status
        apply_tracking_to_shipment(self.shipment)
        self.assertEqual(Shipment.objects.get(pk=self.shipment.pk).status, first_status)

    def test_a_shipment_without_events_is_untouched(self):
        apply_tracking_to_shipment(self.shipment)
        self.shipment.refresh_from_db()
        self.assertIsNone(self.shipment.actual_departure_at)
        self.assertEqual(self.shipment.status, Shipment.Status.DRAFT)


class EtaFromTrackingTest(TransportStatusTestBase):
    team_slug = "transport-eta"

    def test_estimated_arrival_sets_the_shipment_eta(self):
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ESTIMATED_ARRIVAL,
            time_type=TrackingEvent.EventTimeType.ESTIMATED,
            location_unlocode="NLRTM",
        )
        apply_tracking_to_shipment(self.shipment)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.eta, timezone.localtime(ESTIMATED_ARRIVAL).date())
        self.assertEqual(self.shipment.eta_source, "maersk")

    def test_first_eta_becomes_the_original_eta(self):
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ESTIMATED_ARRIVAL,
            time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        apply_tracking_to_shipment(self.shipment)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.original_eta, self.shipment.eta)

    def test_eta_change_is_recorded_in_history_with_location(self):
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ESTIMATED_ARRIVAL,
            time_type=TrackingEvent.EventTimeType.ESTIMATED,
            location_name="Port of Rotterdam",
            location_unlocode="NLRTM",
        )
        apply_tracking_to_shipment(self.shipment)
        history = ETAHistory.objects.get(shipment=self.shipment)
        self.assertEqual(history.new_eta_at, ESTIMATED_ARRIVAL)
        self.assertEqual(history.location_unlocode, "NLRTM")
        self.assertEqual(history.source, "maersk")
        self.assertIsNotNone(history.received_at)
        self.assertIsNotNone(history.tracking_event_id)

    def test_a_later_forecast_records_the_delay_in_minutes(self):
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ESTIMATED_ARRIVAL,
            time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        apply_tracking_to_shipment(self.shipment)

        delayed = ESTIMATED_ARRIVAL + timedelta(hours=30)
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            delayed,
            time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        apply_tracking_to_shipment(self.shipment)

        latest = ETAHistory.objects.filter(shipment=self.shipment).order_by("-changed_at").first()
        self.assertEqual(latest.delta_minutes, 30 * 60)
        self.assertTrue(latest.is_delay)

    def test_history_is_append_only(self):
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ESTIMATED_ARRIVAL,
            time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        apply_tracking_to_shipment(self.shipment)
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ESTIMATED_ARRIVAL + timedelta(days=2),
            time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        apply_tracking_to_shipment(self.shipment)
        self.assertEqual(ETAHistory.objects.filter(shipment=self.shipment).count(), 2)

    def test_unchanged_forecast_writes_no_new_history(self):
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ESTIMATED_ARRIVAL,
            time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        apply_tracking_to_shipment(self.shipment)
        apply_tracking_to_shipment(self.shipment)
        self.assertEqual(ETAHistory.objects.filter(shipment=self.shipment).count(), 1)

    def test_a_stale_forecast_does_not_overwrite_an_actual_arrival(self):
        """Once it has arrived, an old estimate must not resurrect a future ETA."""
        self._event(TrackingEvent.EventType.VESSEL_ARRIVED, ARRIVED_AT)
        self._event(
            TrackingEvent.EventType.VESSEL_ARRIVED,
            ARRIVED_AT + timedelta(days=5),
            time_type=TrackingEvent.EventTimeType.ESTIMATED,
        )
        apply_tracking_to_shipment(self.shipment)
        self.shipment.refresh_from_db()
        self.assertIsNone(self.shipment.eta)


class EtaDeltaCalculationTest(TestCase):
    def test_precise_values_are_used_when_available(self):
        delta = calculate_eta_delta_minutes(
            previous_eta=ESTIMATED_ARRIVAL.date(),
            new_eta=ESTIMATED_ARRIVAL.date(),
            previous_eta_at=ESTIMATED_ARRIVAL,
            new_eta_at=ESTIMATED_ARRIVAL + timedelta(hours=6),
        )
        self.assertEqual(delta, 360, "A same-day six-hour slip must not round to zero")

    def test_dates_are_used_as_a_fallback(self):
        delta = calculate_eta_delta_minutes(
            previous_eta=ESTIMATED_ARRIVAL.date(),
            new_eta=(ESTIMATED_ARRIVAL + timedelta(days=2)).date(),
        )
        self.assertEqual(delta, 2 * 24 * 60)

    def test_earlier_eta_is_negative(self):
        delta = calculate_eta_delta_minutes(
            previous_eta=None,
            new_eta=None,
            previous_eta_at=ESTIMATED_ARRIVAL,
            new_eta_at=ESTIMATED_ARRIVAL - timedelta(hours=3),
        )
        self.assertEqual(delta, -180)

    def test_first_known_eta_has_no_delta(self):
        """No previous forecast is not the same as a delay of zero."""
        self.assertIsNone(calculate_eta_delta_minutes(previous_eta=None, new_eta=ESTIMATED_ARRIVAL.date()))


class ManualEtaUpdateTest(TestCase):
    """The manual path writes the same history as the carrier path."""

    def setUp(self):
        self.team = _team("transport-manual-eta")
        self.shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-MANUAL")

    def test_manual_update_records_history_and_a_timeline_event(self):
        new_eta = ESTIMATED_ARRIVAL.date()
        update_shipment_eta(self.shipment, new_eta, source="manual")
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.eta, new_eta)
        self.assertEqual(ETAHistory.objects.filter(shipment=self.shipment).count(), 1)
        self.assertTrue(
            ShipmentEvent.objects.filter(
                shipment=self.shipment, event_type=ShipmentEvent.EventType.ETA_UPDATED
            ).exists()
        )

    def test_setting_the_same_eta_again_writes_no_history(self):
        new_eta = ESTIMATED_ARRIVAL.date()
        update_shipment_eta(self.shipment, new_eta, source="manual")
        update_shipment_eta(self.shipment, new_eta, source="manual")
        self.assertEqual(ETAHistory.objects.filter(shipment=self.shipment).count(), 1)
