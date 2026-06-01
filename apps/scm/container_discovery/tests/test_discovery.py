"""Tests for the container discovery service and schema."""

from django.test import TestCase

from apps.scm.container_discovery.discovery_service import (
    DummyCarrierDiscoveryProvider,
    run_container_discovery,
)
from apps.scm.container_discovery.models import (
    ContainerDiscoveryEvent,
    ContainerPool,
    ContainerPoolStatus,
)
from apps.scm.container_discovery.services import create_planned_container
from apps.scm.integrations.carriers.schemas import ContainerDiscoveryResult
from apps.teams.models import Team


def _team(slug="cd-disc-team") -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


class ContainerDiscoveryResultSchemaTest(TestCase):
    def test_validates_complete_result(self):
        result = ContainerDiscoveryResult(
            container_number="MCUU1234567",
            carrier_code="maersk",
            carrier_name="Maersk",
            booking_number="BKG-001",
            bl_number="BL-001",
            shipment_reference="REF-001",
            current_status="IN_TRANSIT",
            raw_payload={"key": "value"},
        )
        self.assertEqual(result.container_number, "MCUU1234567")
        self.assertEqual(result.booking_number, "BKG-001")

    def test_optional_fields_default_to_none(self):
        result = ContainerDiscoveryResult(
            container_number="MCUU9999999",
            carrier_code="dummy",
            carrier_name="Dummy",
            raw_payload={},
        )
        self.assertIsNone(result.booking_number)
        self.assertIsNone(result.bl_number)
        self.assertIsNone(result.shipment_reference)


class DummyProviderTest(TestCase):
    def setUp(self):
        self.provider = DummyCarrierDiscoveryProvider()

    def test_detects_mcuu_container(self):
        result = self.provider.discover_container("MCUU1234567")
        self.assertIsNotNone(result)
        self.assertEqual(result.container_number, "MCUU1234567")
        self.assertEqual(result.carrier_code, "dummy")

    def test_detects_mcuu_lowercase(self):
        result = self.provider.discover_container("mcuu1234567")
        self.assertIsNotNone(result)

    def test_returns_none_for_unknown_container(self):
        result = self.provider.discover_container("MSCU9999999")
        self.assertIsNone(result)


class RunContainerDiscoveryTest(TestCase):
    def _team(self):
        return _team()

    def test_detects_mcuu_container_and_updates_status(self):
        team = self._team()
        entry = create_planned_container(team=team, container_number="MCUU1111111")
        self.assertEqual(entry.status, ContainerPoolStatus.PLANNED)

        summary = run_container_discovery(team=team)

        entry.refresh_from_db()
        self.assertEqual(entry.status, ContainerPoolStatus.DETECTED)
        self.assertEqual(summary["detected"], 1)

    def test_creates_detected_event(self):
        team = self._team()
        create_planned_container(team=team, container_number="MCUU2222222")

        run_container_discovery(team=team)

        event = ContainerDiscoveryEvent.objects.filter(
            team=team,
            container_number="MCUU2222222",
            event_type=ContainerDiscoveryEvent.EventType.CONTAINER_DETECTED,
        ).first()
        self.assertIsNotNone(event)
        self.assertIsNotNone(event.detected_at)

    def test_non_mcuu_container_not_detected(self):
        team = self._team()
        create_planned_container(team=team, container_number="MSCU9999999")

        summary = run_container_discovery(team=team)

        self.assertEqual(summary["detected"], 0)
        entry = ContainerPool.objects.get(team=team, container_number="MSCU9999999")
        self.assertEqual(entry.status, ContainerPoolStatus.PLANNED)

    def test_already_detected_container_not_reprocessed(self):
        team = self._team()
        ContainerPool.objects.create(team=team, container_number="MCUU3333333", status=ContainerPoolStatus.DETECTED)

        summary = run_container_discovery(team=team)

        # Should have 0 planned entries processed
        self.assertEqual(summary["planned"], 0)
        self.assertEqual(summary["detected"], 0)

    def test_failed_provider_creates_search_failed_event(self):
        from unittest.mock import MagicMock

        from apps.scm.integrations.carriers.base import CarrierDiscoveryProvider

        team = self._team()
        create_planned_container(team=team, container_number="MSCU8888888")

        failing_provider = MagicMock(spec=CarrierDiscoveryProvider)
        failing_provider.provider_code = "failing"
        failing_provider.discover_container.side_effect = Exception("Network error")

        run_container_discovery(team=team, providers=[failing_provider])

        event = ContainerDiscoveryEvent.objects.filter(
            team=team,
            container_number="MSCU8888888",
            event_type=ContainerDiscoveryEvent.EventType.SEARCH_FAILED,
        ).first()
        self.assertIsNotNone(event)
