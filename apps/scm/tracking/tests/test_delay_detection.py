"""Tests for delay detection service."""

import datetime

from django.test import TestCase
from django.utils import timezone

from apps.scm.shipments.models import Shipment
from apps.scm.tracking.delay_detection import check_shipment_delay, get_eta_drift_days
from apps.scm.tracking.models import ETAHistory, TrackingEvent, TrackingProvider
from apps.teams.models import Team


def _team(slug):
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _provider():
    return TrackingProvider.objects.get_or_create(
        code="DELAY_TEST_PROVIDER",
        defaults={"name": "Delay Test Provider", "provider_type": TrackingProvider.ProviderType.MANUAL},
    )[0]


def _shipment(team, original_eta=None, current_eta=None, actual_arrival_at=None):
    return Shipment.objects.create(
        team=team,
        original_eta=original_eta,
        eta=current_eta,
        actual_arrival_at=actual_arrival_at,
    )


class CheckShipmentDelayTest(TestCase):
    def test_no_delay_when_no_eta(self):
        team = _team("delay-no-eta")
        shipment = _shipment(team)
        report = check_shipment_delay(team, shipment)
        self.assertFalse(report.is_delayed)

    def test_delayed_when_eta_moved_forward(self):
        team = _team("delay-eta-forward")
        original = datetime.date(2026, 7, 1)
        current = datetime.date(2026, 7, 10)
        shipment = _shipment(team, original_eta=original, current_eta=current)
        report = check_shipment_delay(team, shipment)
        self.assertTrue(report.is_delayed)
        self.assertEqual(report.eta_drift_days, 9)

    def test_not_delayed_when_eta_improved(self):
        team = _team("delay-eta-earlier")
        original = datetime.date(2026, 7, 10)
        current = datetime.date(2026, 7, 5)
        shipment = _shipment(team, original_eta=original, current_eta=current)
        report = check_shipment_delay(team, shipment)
        self.assertFalse(report.is_delayed)

    def test_delayed_via_carrier_delay_event(self):
        team = _team("delay-carrier-event")
        shipment = _shipment(team)
        provider = _provider()
        TrackingEvent.objects.create(
            team=team,
            shipment=shipment,
            provider=provider,
            event_type=TrackingEvent.EventType.DELAY,
        )
        report = check_shipment_delay(team, shipment)
        self.assertTrue(report.is_delayed)
        self.assertIn("delay", report.reason.lower())

    def test_delayed_when_overdue_no_arrival(self):
        team = _team("delay-overdue")
        past_eta = datetime.date(2026, 1, 1)
        shipment = _shipment(team, original_eta=past_eta, current_eta=past_eta)
        report = check_shipment_delay(team, shipment)
        self.assertTrue(report.is_delayed)

    def test_not_delayed_when_arrived(self):
        team = _team("delay-arrived")
        past_eta = datetime.date(2026, 1, 1)
        shipment = _shipment(team, original_eta=past_eta, current_eta=past_eta, actual_arrival_at=timezone.now())
        report = check_shipment_delay(team, shipment)
        self.assertFalse(report.is_delayed)


class GetEtaDriftDaysTest(TestCase):
    def test_drift_calculated_correctly(self):
        team = _team("drift-calc")
        shipment = _shipment(team)
        ETAHistory.objects.create(
            team=team,
            shipment=shipment,
            previous_eta=datetime.date(2026, 7, 1),
            new_eta=datetime.date(2026, 7, 5),
            changed_at=timezone.now(),
        )
        ETAHistory.objects.create(
            team=team,
            shipment=shipment,
            previous_eta=datetime.date(2026, 7, 5),
            new_eta=datetime.date(2026, 7, 8),
            changed_at=timezone.now(),
        )
        drift = get_eta_drift_days(team, shipment)
        # From first previous_eta (Jul 1) to last new_eta (Jul 8) = 7 days
        self.assertEqual(drift, 7)

    def test_no_history_returns_zero(self):
        team = _team("drift-empty")
        shipment = _shipment(team)
        self.assertEqual(get_eta_drift_days(team, shipment), 0)
