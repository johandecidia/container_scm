"""Tests for shipment-based carrier discovery service and auto-link."""

from django.test import TestCase

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.carriers.auto_link import create_or_link_discovered_container
from apps.scm.integrations.carriers.discovery_service import discover_containers_for_shipment
from apps.scm.integrations.carriers.schemas import ContainerDiscoveryResult
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.models import TrackingSubscription
from apps.teams.models import Team

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _team(slug: str) -> Team:
    return Team.objects.create(name=slug, slug=slug)


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _shipment(team: Team, **kwargs) -> Shipment:
    defaults = {"shipment_number": "SHP-DISC-001"}
    defaults.update(kwargs)
    return Shipment.objects.create(team=team, **defaults)


def _result(container_number: str = "MSCU1234566", **kwargs) -> ContainerDiscoveryResult:
    defaults = {
        "container_number": container_number,
        "carrier_code": "maersk",
        "carrier_name": "Maersk",
        "booking_number": "BKG-001",
    }
    defaults.update(kwargs)
    return ContainerDiscoveryResult(**defaults)


class FakeDiscoveryProvider:
    """Fake carrier client that returns a fixed list of ContainerDiscoveryResult."""

    def __init__(self, results: list[ContainerDiscoveryResult]):
        self._results = results

    def discover_containers(
        self,
        *,
        booking_number=None,
        bill_of_lading_number=None,
        shipment_reference=None,
    ) -> list[ContainerDiscoveryResult]:
        return list(self._results)


class FailingDiscoveryProvider:
    """Fake carrier client that always raises an exception."""

    def discover_containers(self, **kwargs):
        raise RuntimeError("Simulated API failure")


# ---------------------------------------------------------------------------
# discovery_service.discover_containers_for_shipment
# ---------------------------------------------------------------------------


class DiscoverContainersForShipmentSkipTest(TestCase):
    """Shipments without any reference should be skipped."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("disc-skip-team")

    def test_discovery_requires_reference(self):
        shipment = _shipment(self.team, carrier_booking_reference="", bill_of_lading_number="", reference="")
        summary = discover_containers_for_shipment(shipment)
        self.assertTrue(summary["skipped"])
        self.assertEqual(summary["discovered_count"], 0)


class DiscoverContainersCreatesContainerTest(TestCase):
    """Provider result should cause Container creation."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("disc-create-team")
        _et()  # ensure equipment type exists

    def test_discovery_creates_container(self):
        shipment = _shipment(self.team, carrier_booking_reference="BKG-CREATE-001")
        result = _result("MSCU1234566")
        provider = FakeDiscoveryProvider([result])

        summary = discover_containers_for_shipment(shipment, providers=[provider])

        self.assertEqual(summary["containers_created"], 1)
        self.assertTrue(
            Container.objects.filter(team=self.team, owner_code="MSC", serial_number="123456", category_id="U").exists()
        )


class DiscoverContainersLinksShipmentTest(TestCase):
    """Provider result should create a ShipmentContainer link."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("disc-link-team")
        _et()

    def test_discovery_links_container_to_shipment(self):
        shipment = _shipment(self.team, carrier_booking_reference="BKG-LINK-001")
        result = _result("MSCU1234566")
        provider = FakeDiscoveryProvider([result])

        discover_containers_for_shipment(shipment, providers=[provider])

        container = Container.objects.get(team=self.team, owner_code="MSC", serial_number="123456", category_id="U")
        self.assertTrue(ShipmentContainer.objects.filter(shipment=shipment, container=container).exists())


class DiscoverContainersNoDuplicateContainerTest(TestCase):
    """Running discovery twice should not create duplicate containers."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("disc-nodup-cont-team")
        _et()

    def test_discovery_does_not_duplicate_container(self):
        shipment = _shipment(self.team, carrier_booking_reference="BKG-NODUP-001")
        result = _result("MSCU1234566")
        provider = FakeDiscoveryProvider([result])

        discover_containers_for_shipment(shipment, providers=[provider])
        discover_containers_for_shipment(shipment, providers=[provider])

        self.assertEqual(
            Container.objects.filter(team=self.team, owner_code="MSC", serial_number="123456", category_id="U").count(),
            1,
        )


class DiscoverContainersNoDuplicateShipmentContainerTest(TestCase):
    """Running discovery twice should not create duplicate ShipmentContainer records."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("disc-nodup-sc-team")
        _et()

    def test_discovery_does_not_duplicate_shipment_container(self):
        shipment = _shipment(self.team, carrier_booking_reference="BKG-NODUP-SC-001")
        result = _result("MSCU1234566")
        provider = FakeDiscoveryProvider([result])

        discover_containers_for_shipment(shipment, providers=[provider])
        discover_containers_for_shipment(shipment, providers=[provider])

        container = Container.objects.get(team=self.team, owner_code="MSC", serial_number="123456", category_id="U")
        self.assertEqual(ShipmentContainer.objects.filter(shipment=shipment, container=container).count(), 1)


class DiscoverContainersCreatesSubscriptionTest(TestCase):
    """Discovery should create a TrackingSubscription for the discovered container."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("disc-sub-team")
        _et()

    def test_discovery_creates_tracking_subscription(self):
        shipment = _shipment(self.team, carrier_booking_reference="BKG-SUB-001")
        result = _result("MSCU1234566")
        provider = FakeDiscoveryProvider([result])

        discover_containers_for_shipment(shipment, providers=[provider])

        container = Container.objects.get(team=self.team, owner_code="MSC", serial_number="123456", category_id="U")
        self.assertTrue(
            TrackingSubscription.objects.filter(
                team=self.team,
                container=container,
                reference_type=TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
            ).exists()
        )


class DiscoverContainersMultipleContainersTest(TestCase):
    """Provider returning multiple containers should create/link them all."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("disc-multi-team")
        _et()

    def test_discovery_handles_multiple_containers(self):
        shipment = _shipment(self.team, carrier_booking_reference="BKG-MULTI-001")
        results = [
            _result("MSCU1234566"),
            _result("HLXU3456784"),
        ]
        provider = FakeDiscoveryProvider(results)

        summary = discover_containers_for_shipment(shipment, providers=[provider])

        self.assertEqual(summary["discovered_count"], 2)
        self.assertEqual(summary["containers_created"], 2)
        self.assertEqual(summary["containers_linked"], 2)


class DiscoverContainersProviderErrorTest(TestCase):
    """Provider exceptions should be captured in errors without crashing the batch."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("disc-err-team")

    def test_discovery_provider_error_is_reported(self):
        shipment = _shipment(self.team, carrier_booking_reference="BKG-ERR-001")
        failing = FailingDiscoveryProvider()

        summary = discover_containers_for_shipment(shipment, providers=[failing])

        self.assertFalse(summary["skipped"])
        self.assertEqual(len(summary["errors"]), 1)
        self.assertIn("Simulated API failure", summary["errors"][0])
        self.assertEqual(summary["discovered_count"], 0)


# ---------------------------------------------------------------------------
# auto_link.create_or_link_discovered_container
# ---------------------------------------------------------------------------


class AutoLinkInvalidContainerIdTest(TestCase):
    """Invalid container ID should be skipped gracefully."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("al-invalid-team")
        _et()

    def test_invalid_container_id_skipped(self):
        shipment = _shipment(self.team, carrier_booking_reference="BKG-INVALID-001")
        result = ContainerDiscoveryResult(
            container_number="NOT-VALID",
            carrier_code="test",
            carrier_name="Test Carrier",
        )
        summary = create_or_link_discovered_container(team=self.team, shipment=shipment, result=result)
        self.assertFalse(summary["container_created"])
