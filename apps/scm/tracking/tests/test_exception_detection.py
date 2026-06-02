"""Tests for exception detection service."""

import datetime

from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.exception_detection import check_container_exceptions, check_shipment_exceptions
from apps.scm.tracking.models import TrackingEvent, TrackingProvider
from apps.teams.models import Team


def _team(slug):
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _provider():
    return TrackingProvider.objects.get_or_create(
        code="EXCEPTION_TEST_PROVIDER",
        defaults={"name": "Exception Test Provider", "provider_type": TrackingProvider.ProviderType.MANUAL},
    )[0]


def _container(team, serial="222222"):
    eq = EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "description": "20GP"},
    )[0]
    check = calculate_check_digit("MSC", "U", serial)
    return Container.objects.create(
        team=team,
        owner_code="MSC",
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=eq,
    )


class CheckContainerExceptionsTest(TestCase):
    def test_no_exceptions_for_normal_events(self):
        team = _team("exc-normal")
        container = _container(team)
        provider = _provider()
        TrackingEvent.objects.create(
            team=team,
            container=container,
            provider=provider,
            event_type=TrackingEvent.EventType.GATE_IN,
            event_datetime=timezone.now(),
        )
        report = check_container_exceptions(team, container)
        self.assertFalse(report.has_exception)

    def test_detects_customs_hold(self):
        team = _team("exc-customs")
        container = _container(team, serial="333333")
        provider = _provider()
        TrackingEvent.objects.create(
            team=team,
            container=container,
            provider=provider,
            event_type=TrackingEvent.EventType.CUSTOMS_HOLD,
            event_datetime=timezone.now(),
        )
        report = check_container_exceptions(team, container)
        self.assertTrue(report.has_exception)
        self.assertIn("customs_hold", report.exception_types)

    def test_detects_rollover_from_description(self):
        team = _team("exc-rollover")
        container = _container(team, serial="444444")
        provider = _provider()
        TrackingEvent.objects.create(
            team=team,
            container=container,
            provider=provider,
            event_type=TrackingEvent.EventType.UNKNOWN,
            description="Container rolled to next voyage",
            event_datetime=timezone.now(),
        )
        report = check_container_exceptions(team, container)
        self.assertTrue(report.has_exception)
        self.assertIn("rolled", report.exception_types)

    def test_detects_stale_tracking(self):
        team = _team("exc-stale")
        container = _container(team, serial="555555")
        provider = _provider()
        old_time = timezone.now() - datetime.timedelta(days=10)
        TrackingEvent.objects.create(
            team=team,
            container=container,
            provider=provider,
            event_type=TrackingEvent.EventType.GATE_IN,
            event_datetime=old_time,
        )
        report = check_container_exceptions(team, container)
        self.assertTrue(report.has_exception)
        self.assertIn("missing_event", report.exception_types)

    def test_no_events_returns_no_exception(self):
        team = _team("exc-empty")
        container = _container(team, serial="666666")
        report = check_container_exceptions(team, container)
        self.assertFalse(report.has_exception)


class CheckShipmentExceptionsTest(TestCase):
    def test_aggregates_from_containers(self):
        team = _team("exc-shipment")
        container = _container(team, serial="777777")
        shipment = Shipment.objects.create(team=team)
        ShipmentContainer.objects.create(shipment=shipment, container=container)
        provider = _provider()
        TrackingEvent.objects.create(
            team=team,
            container=container,
            provider=provider,
            event_type=TrackingEvent.EventType.CUSTOMS_HOLD,
            event_datetime=timezone.now(),
        )
        report = check_shipment_exceptions(team, shipment)
        self.assertTrue(report.has_exception)
        self.assertIn("customs_hold", report.exception_types)

    def test_no_containers_returns_no_exception(self):
        team = _team("exc-shipment-empty")
        shipment = Shipment.objects.create(team=team)
        report = check_shipment_exceptions(team, shipment)
        self.assertFalse(report.has_exception)
