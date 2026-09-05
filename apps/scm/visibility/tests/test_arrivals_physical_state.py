"""Expected Arrivals stops expecting what has physically arrived.

LOC-1 made the queue answer "what is routed to Oceanterminalen". LOC-2 makes it
operationally useful by taking things off it once somebody has accepted the box into
that destination.

The distinction being tested is between *arrival evidence* and *current location*.
They give different answers and only one of them is right here:

* A box gated in on Tuesday and trucked onward on Wednesday has arrived. Its current
  location is no longer the destination, and a queue that compared current location
  would put it back on the list of things to expect.
* A box a carrier merely reports somewhere near the destination has not arrived,
  however suggestive the event.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.choices import LocationSource, LocationType, MovementType
from apps.scm.containers.models import Container, ContainerLocation
from apps.scm.containers.movements import record_container_movement
from apps.scm.containers.services import create_location
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.visibility.work_queues import ArrivalQueueFilters, get_arrival_queue
from apps.teams.models import Team

from .factories import equipment_type, make_user_and_team


def _container(team, serial: str) -> Container:
    return Container.objects.create(
        team=team,
        owner_code="MSK",
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit("MSK", "U", serial),
        equipment_type=equipment_type(),
    )


class ArrivalRemovesFromExpectedTest(TestCase):
    """One terminal inside one port, and shipments routed to the terminal."""

    team: Team
    port: ContainerLocation
    terminal: ContainerLocation
    elsewhere: ContainerLocation

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("arrphys@example.com", "arr-phys")
        cls.port = create_location(
            cls.team, {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT"}
        )
        cls.terminal = create_location(
            cls.team,
            {"name": "Oceanterminalen", "location_type": LocationType.DEPOT, "parent_location": cls.port},
        )
        cls.elsewhere = create_location(cls.team, {"name": "MCR Depot", "location_type": LocationType.DEPOT})

    def _shipment(self, number: str, *, containers: int = 1) -> Shipment:
        shipment = Shipment.objects.create(
            team=self.team,
            shipment_number=number,
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            destination_port="Gothenburg",
            destination_location=self.terminal,
            eta=timezone.localdate() + timedelta(days=3),
        )
        for index in range(containers):
            ShipmentContainer.objects.create(
                shipment=shipment,
                container=_container(self.team, f"{number[-4:]}{index:02d}"),
                sequence=index,
            )
        return shipment

    def _queue_numbers(self) -> set[str]:
        queue = get_arrival_queue(self.team, ArrivalQueueFilters(window="30"))
        return {obj.shipment.shipment_number for obj in queue.objects if obj.shipment}

    def test_a_routed_shipment_is_expected_before_it_arrives(self):
        self._shipment("SHP-0001")
        self.assertIn("SHP-0001", self._queue_numbers())

    def test_a_gate_in_at_the_destination_removes_it(self):
        shipment = self._shipment("SHP-0002")
        container = shipment.shipment_containers.first().container

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
        )

        self.assertNotIn("SHP-0002", self._queue_numbers())

    def test_a_receipt_at_the_destination_removes_it(self):
        shipment = self._shipment("SHP-0003")
        container = shipment.shipment_containers.first().container

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.RECEIVED,
            to_location=self.terminal,
            source=LocationSource.DEPOT,
        )

        self.assertNotIn("SHP-0003", self._queue_numbers())

    def test_arriving_and_leaving_again_still_counts_as_arrived(self):
        """The evidence is the arrival, not where the box is standing today."""
        shipment = self._shipment("SHP-0004")
        container = shipment.shipment_containers.first().container

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(days=2),
        )
        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_OUT,
            occurred_at=timezone.now() - timedelta(days=1),
        )

        container.refresh_from_db()
        self.assertIsNone(container.current_location)
        self.assertNotIn("SHP-0004", self._queue_numbers())

    def test_arriving_somewhere_else_does_not_remove_it(self):
        shipment = self._shipment("SHP-0005")
        container = shipment.shipment_containers.first().container

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.elsewhere,
        )

        self.assertIn("SHP-0005", self._queue_numbers())

    def test_a_partly_arrived_shipment_is_still_expected(self):
        """Nineteen boxes in and one outstanding is still an arrival to work."""
        shipment = self._shipment("SHP-0006", containers=2)
        first = shipment.shipment_containers.order_by("sequence").first().container

        record_container_movement(
            team=self.team,
            container=first,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
        )

        self.assertIn("SHP-0006", self._queue_numbers())

    def test_a_shipment_with_no_canonical_destination_is_untouched(self):
        """There is nothing to have arrived at, so nothing can remove it."""
        Shipment.objects.create(
            team=self.team,
            shipment_number="SHP-0007",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            destination_port="Gothenburg",
            eta=timezone.localdate() + timedelta(days=3),
        )
        self.assertIn("SHP-0007", self._queue_numbers())


class LocationExpectedTabTest(TestCase):
    """The location workspace's Expected tab composes the same queue, so it inherits this."""

    team: Team
    terminal: ContainerLocation
    shipment: Shipment
    container: Container

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("arrexp@example.com", "arr-exp")
        cls.terminal = create_location(cls.team, {"name": "Oceanterminalen", "location_type": LocationType.DEPOT})
        cls.shipment = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-EXP-1",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            destination_location=cls.terminal,
            eta=timezone.localdate() + timedelta(days=2),
        )
        cls.container = _container(cls.team, "770001")
        ShipmentContainer.objects.create(shipment=cls.shipment, container=cls.container, sequence=0)

    def _expected_count(self) -> int:
        from apps.scm.containers.location_workspace import get_expected_arrivals

        return get_expected_arrivals(team=self.team, location=self.terminal).count

    def test_expected_before_arrival(self):
        self.assertEqual(self._expected_count(), 1)

    def test_not_expected_after_gate_in(self):
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
        )
        self.assertEqual(self._expected_count(), 0)
