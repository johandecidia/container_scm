"""Tests for exception detection service."""

import datetime

from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.exception_detection import (
    ExceptionIssue,
    ExceptionReport,
    check_container_exceptions,
    check_shipment_exceptions,
    merge_exception_reports,
)
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


class ExceptionIssuePairingTest(TestCase):
    """Each exception must carry the reason it was raised.

    The flat ``exception_types`` and ``details`` lists de-duplicate differently once
    several containers are merged, so a caller that wants to print a reason beside a
    type cannot pair them up by index. ``issues`` is the pairing, and these tests
    exist because a work queue row that showed the wrong reason for the right code
    would still look entirely plausible.
    """

    def test_a_container_issue_carries_its_own_reason(self):
        team = _team("exc-pair")
        container = _container(team, serial="888888")
        TrackingEvent.objects.create(
            team=team,
            container=container,
            provider=_provider(),
            event_type=TrackingEvent.EventType.CUSTOMS_HOLD,
            location_name="Rotterdam",
            event_datetime=timezone.now(),
        )
        report = check_container_exceptions(team, container)
        self.assertEqual([issue.exception_type for issue in report.issues], ["customs_hold"])
        self.assertEqual(report.issues[0].detail, "Customs hold at Rotterdam")

    def test_a_stale_container_pairs_the_gap_with_the_type_that_names_it(self):
        """Two issues at once is where index alignment silently goes wrong."""
        team = _team("exc-pair-stale")
        container = _container(team, serial="888889")
        TrackingEvent.objects.create(
            team=team,
            container=container,
            provider=_provider(),
            event_type=TrackingEvent.EventType.CUSTOMS_HOLD,
            location_name="Rotterdam",
            event_datetime=timezone.now() - datetime.timedelta(days=9),
        )
        by_type = {issue.exception_type: issue.detail for issue in check_container_exceptions(team, container).issues}
        self.assertEqual(by_type["customs_hold"], "Customs hold at Rotterdam")
        self.assertEqual(by_type["missing_event"], "No tracking update for 9 days")

    def test_merging_keeps_one_issue_per_type_and_every_reason(self):
        """One hold across two boxes is one issue for the shipment — but two reasons."""
        merged = merge_exception_reports(
            [
                ExceptionReport(
                    has_exception=True,
                    exception_types=["customs_hold"],
                    details=["Customs hold at Rotterdam"],
                    issues=[ExceptionIssue("customs_hold", "Customs hold at Rotterdam")],
                ),
                ExceptionReport(
                    has_exception=True,
                    exception_types=["customs_hold"],
                    details=["Customs hold at Gothenburg"],
                    issues=[ExceptionIssue("customs_hold", "Customs hold at Gothenburg")],
                ),
            ]
        )
        self.assertEqual(merged.exception_types, ["customs_hold"])
        self.assertEqual([issue.detail for issue in merged.issues], ["Customs hold at Rotterdam"])
        self.assertEqual(merged.details, ["Customs hold at Rotterdam", "Customs hold at Gothenburg"])

    def test_merging_nothing_reports_no_exception(self):
        self.assertFalse(merge_exception_reports([None, ExceptionReport(has_exception=False)]).has_exception)
