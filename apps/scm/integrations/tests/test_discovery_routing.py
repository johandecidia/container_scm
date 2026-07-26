"""Tests for how shipment discovery chooses a carrier and a reference.

The point of these tests is that discovery asks one carrier — the shipment's own —
using a reference that carrier actually supports, instead of fanning out across
every registered carrier.
"""

from unittest import mock

from django.test import TestCase

from apps.scm.containers.models import EquipmentType
from apps.scm.integrations.carriers.base import BaseCarrierClient, CarrierCapability
from apps.scm.integrations.carriers.discovery_service import (
    discover_containers_for_shipment,
    get_shipment_carrier_code,
)
from apps.scm.integrations.carriers.exceptions import CarrierNoDataError, CarrierNotImplementedError
from apps.scm.integrations.carriers.registry import (
    list_carriers,
    resolve_carrier_code,
    suggest_carrier_for_owner_code,
)
from apps.scm.integrations.carriers.schemas import ContainerDiscoveryResult
from apps.scm.shipments.models import Shipment
from apps.teams.models import Team


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _equipment_type():
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _shipment(team: Team, **kwargs) -> Shipment:
    defaults = {"shipment_number": f"SHP-{team.slug}"}
    defaults.update(kwargs)
    return Shipment.objects.create(team=team, **defaults)


class RecordingClient(BaseCarrierClient):
    """Carrier client that records how it was queried."""

    provider_code = "maersk"

    def __init__(self, capabilities=None, results=None, error=None):
        super().__init__(None)
        if capabilities is not None:
            self.capabilities = capabilities
        self.results = results or []
        self.error = error
        self.calls: list[dict] = []

    def discover_containers(self, **kwargs):
        self.calls.append({key: value for key, value in kwargs.items() if value})
        if self.error is not None:
            raise self.error
        return list(self.results)


class CarrierCodeResolutionTest(TestCase):
    """Free-text carrier values resolve to registered codes, or to nothing."""

    def test_provider_code_resolves(self):
        self.assertEqual(resolve_carrier_code("maersk"), "maersk")

    def test_registered_name_resolves(self):
        self.assertEqual(resolve_carrier_code("Hapag-Lloyd"), "hapag_lloyd")

    def test_name_with_qualifier_resolves(self):
        self.assertEqual(resolve_carrier_code("CMA CGM"), "cma_cgm")

    def test_case_and_separators_are_ignored(self):
        self.assertEqual(resolve_carrier_code("HAPAG LLOYD"), "hapag_lloyd")

    def test_unknown_carrier_resolves_to_none(self):
        """An unrecognised carrier must not silently become a different one."""
        self.assertIsNone(resolve_carrier_code("Some Regional Feeder Line"))

    def test_blank_resolves_to_none(self):
        self.assertIsNone(resolve_carrier_code(""))


class OwnerPrefixSuggestionTest(TestCase):
    """Owner prefixes suggest a carrier without ever asserting one."""

    def test_known_prefix_suggests_owner(self):
        self.assertEqual(suggest_carrier_for_owner_code("MRKU"), "maersk")
        self.assertEqual(suggest_carrier_for_owner_code("HLXU"), "hapag_lloyd")

    def test_prefix_from_a_full_container_number_works(self):
        self.assertEqual(suggest_carrier_for_owner_code("MSCU1234567"), "msc")

    def test_unknown_prefix_suggests_nothing(self):
        self.assertIsNone(suggest_carrier_for_owner_code("ZZZU"))

    def test_short_value_suggests_nothing(self):
        self.assertIsNone(suggest_carrier_for_owner_code("MR"))

    def test_prefixes_are_unique_across_carriers(self):
        seen: dict[str, str] = {}
        for definition in list_carriers():
            for prefix in definition.owner_prefixes:
                self.assertNotIn(prefix, seen, f"{prefix} claimed by both {seen.get(prefix)} and {definition.name}")
                seen[prefix] = definition.provider_code


class ShipmentCarrierResolutionTest(TestCase):
    def setUp(self):
        self.team = _team("disc-route-team")

    def test_shipment_carrier_is_resolved(self):
        shipment = _shipment(self.team, carrier="Maersk")
        self.assertEqual(get_shipment_carrier_code(shipment), "maersk")

    def test_shipment_without_carrier_resolves_to_none(self):
        self.assertIsNone(get_shipment_carrier_code(_shipment(self.team)))


class DiscoveryRoutingTest(TestCase):
    """Discovery queries one carrier, with one supported reference."""

    def setUp(self):
        self.team = _team("disc-routing-team")
        _equipment_type()

    def test_booking_reference_is_preferred(self):
        shipment = _shipment(
            self.team,
            carrier="Maersk",
            carrier_booking_reference="BKG-1",
            bill_of_lading_number="BL-1",
            reference="REF-1",
        )
        client = RecordingClient(
            capabilities=CarrierCapability(
                supports_pull=True, supports_tracking_by_booking=True, supports_tracking_by_bl=True
            )
        )
        discover_containers_for_shipment(shipment, providers=[client])
        self.assertEqual(client.calls, [{"booking_number": "BKG-1"}])

    def test_falls_back_to_bill_of_lading(self):
        shipment = _shipment(self.team, carrier="Maersk", bill_of_lading_number="BL-2", reference="REF-2")
        client = RecordingClient(capabilities=CarrierCapability(supports_pull=True, supports_tracking_by_bl=True))
        discover_containers_for_shipment(shipment, providers=[client])
        self.assertEqual(client.calls, [{"bill_of_lading_number": "BL-2"}])

    def test_unsupported_reference_is_not_sent(self):
        """A carrier that cannot search by shipment reference is not asked to."""
        shipment = _shipment(self.team, carrier="Maersk", reference="REF-3")
        client = RecordingClient(capabilities=CarrierCapability(supports_pull=True, supports_tracking_by_booking=True))
        summary = discover_containers_for_shipment(shipment, providers=[client])
        self.assertEqual(client.calls, [])
        self.assertTrue(summary["skipped"])

    def test_shipment_reference_is_used_when_supported(self):
        shipment = _shipment(self.team, carrier="Maersk", reference="REF-4")
        client = RecordingClient(
            capabilities=CarrierCapability(supports_pull=True, supports_tracking_by_shipment_reference=True)
        )
        discover_containers_for_shipment(shipment, providers=[client])
        self.assertEqual(client.calls, [{"shipment_reference": "REF-4"}])

    def test_unknown_carrier_is_skipped_not_broadcast(self):
        """An unrecognised carrier must not cause a sweep of every registered carrier."""
        shipment = _shipment(self.team, carrier="Some Feeder Line", carrier_booking_reference="BKG-5")
        with mock.patch("apps.scm.integrations.carriers.factory.build_carrier_client") as build_client:
            summary = discover_containers_for_shipment(shipment)
        build_client.assert_not_called()
        self.assertTrue(summary["skipped"])

    def test_missing_carrier_is_skipped(self):
        shipment = _shipment(self.team, carrier_booking_reference="BKG-6")
        summary = discover_containers_for_shipment(shipment)
        self.assertTrue(summary["skipped"])
        self.assertEqual(summary["discovered_count"], 0)

    def test_configured_carrier_is_built_for_the_shipment_team(self):
        shipment = _shipment(self.team, carrier="maersk", carrier_booking_reference="BKG-7")
        client = RecordingClient(capabilities=CarrierCapability(supports_pull=True, supports_tracking_by_booking=True))
        with mock.patch(
            "apps.scm.integrations.carriers.factory.build_carrier_client", return_value=client
        ) as build_client:
            discover_containers_for_shipment(shipment)
        build_client.assert_called_once_with("maersk", team=self.team)

    def test_stub_adapter_is_skipped(self):
        shipment = _shipment(self.team, carrier="Maersk", carrier_booking_reference="BKG-8")
        client = RecordingClient(
            capabilities=CarrierCapability(supports_pull=True, supports_tracking_by_booking=True),
            error=CarrierNotImplementedError("stub"),
        )
        summary = discover_containers_for_shipment(shipment, providers=[client])
        self.assertTrue(summary["skipped"])
        self.assertEqual(summary["errors"], [])

    def test_no_data_is_not_an_error(self):
        shipment = _shipment(self.team, carrier="Maersk", carrier_booking_reference="BKG-9")
        client = RecordingClient(
            capabilities=CarrierCapability(supports_pull=True, supports_tracking_by_booking=True),
            error=CarrierNoDataError("404"),
        )
        summary = discover_containers_for_shipment(shipment, providers=[client])
        self.assertFalse(summary["skipped"])
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["discovered_count"], 0)

    def test_open_shipments_task_selects_shipments_without_containers(self):
        """Regression: the task filtered on a `containers` relation that does not exist.

        Shipment links containers through ShipmentContainer (related name
        shipment_containers), so the old queryset raised FieldError at runtime.
        """
        from apps.scm.containers.models import Container
        from apps.scm.integrations.tasks import discover_containers_for_open_shipments_task
        from apps.scm.shipments.models import ShipmentContainer

        without_containers = _shipment(
            self.team, shipment_number="SHP-OPEN-1", carrier="Maersk", carrier_booking_reference="BKG-OPEN-1"
        )
        with_containers = _shipment(
            self.team, shipment_number="SHP-OPEN-2", carrier="Maersk", carrier_booking_reference="BKG-OPEN-2"
        )
        container = Container.objects.create(
            team=self.team,
            owner_code="MRK",
            category_id="U",
            serial_number="123456",
            check_digit=3,
            equipment_type=_equipment_type(),
        )
        ShipmentContainer.objects.create(shipment=with_containers, container=container)
        delivered = _shipment(
            self.team,
            shipment_number="SHP-OPEN-3",
            carrier="Maersk",
            carrier_booking_reference="BKG-OPEN-3",
            status=Shipment.Status.DELIVERED,
        )

        seen: list[int] = []

        def record(shipment):
            seen.append(shipment.pk)
            return {"skipped": True}

        with mock.patch(
            "apps.scm.integrations.carriers.discovery_service.discover_containers_for_shipment",
            side_effect=record,
        ):
            totals = discover_containers_for_open_shipments_task.run(self.team.pk)

        self.assertEqual(seen, [without_containers.pk])
        self.assertNotIn(with_containers.pk, seen)
        self.assertNotIn(delivered.pk, seen)
        self.assertEqual(totals["shipments_skipped"], 1)

    def test_open_shipments_task_is_team_scoped(self):
        from apps.scm.integrations.tasks import discover_containers_for_open_shipments_task

        other_team = _team("disc-routing-other-team")
        _shipment(other_team, shipment_number="SHP-OTHER", carrier="Maersk", carrier_booking_reference="BKG-OTHER")

        seen: list[int] = []
        with mock.patch(
            "apps.scm.integrations.carriers.discovery_service.discover_containers_for_shipment",
            side_effect=lambda shipment: seen.append(shipment.pk) or {"skipped": True},
        ):
            discover_containers_for_open_shipments_task.run(self.team.pk)
        self.assertEqual(seen, [])

    def test_discovered_container_is_created_and_linked(self):
        shipment = _shipment(self.team, carrier="Maersk", carrier_booking_reference="BKG-10")
        client = RecordingClient(
            capabilities=CarrierCapability(supports_pull=True, supports_tracking_by_booking=True),
            results=[
                ContainerDiscoveryResult(
                    container_number="MRKU1234563",
                    carrier_code="maersk",
                    carrier_name="Maersk",
                )
            ],
        )
        summary = discover_containers_for_shipment(shipment, providers=[client])
        self.assertEqual(summary["discovered_count"], 1)
        self.assertEqual(summary["containers_created"], 1)
        self.assertEqual(summary["containers_linked"], 1)
        self.assertEqual(summary["subscriptions_created"], 1)
        self.assertEqual(summary["reference_used"], "booking_number")
