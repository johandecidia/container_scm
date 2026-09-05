"""Shared setup for the arrival lifecycle tests.

One canonical location hierarchy and one kind of shipment, used by every LOC-3 test
module, so the interpreter, the queues, the attention list and the workspaces are all
tested against the same shape of data.

The hierarchy is the one the domain actually describes, three levels deep, because
the direction of containment is a rule worth testing in both directions:

.. code-block:: text

    Göteborg (port)
        Oceanterminalen (terminal)          ← where shipments here are routed
            Oceanterminalen Bay 4 (depot)
    MCR Depot (depot)                       ← unrelated, elsewhere

Shipments are created **departed** — with an ETD and an actual departure — because
that is what ``status=IN_TRANSIT`` means, and because the arrival lifecycle only
counts movements from the start of the shipment's own cycle. A fixture with no
departure would be testing the fallback bound rather than the ordinary case.
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
from apps.teams.models import Team

from .factories import equipment_type, make_user_and_team


def make_container(team: Team, serial: str) -> Container:
    return Container.objects.create(
        team=team,
        owner_code="MSK",
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit("MSK", "U", serial),
        equipment_type=equipment_type(),
    )


class ArrivalScenarioTestCase(TestCase):
    """A team with a port, a terminal inside it, a bay inside that, and a depot elsewhere."""

    team: Team
    port: ContainerLocation
    terminal: ContainerLocation
    bay: ContainerLocation
    elsewhere: ContainerLocation

    @classmethod
    def setUpTestData(cls):
        # Named from the test class, so each class gets its own tenant and the suite
        # cannot depend on another class's data.
        slug = cls.__name__.lower()[:40]
        cls.user, cls.team = make_user_and_team(f"{slug}@example.com", slug)
        cls.port = create_location(
            cls.team, {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT"}
        )
        cls.terminal = create_location(
            cls.team,
            {"name": "Oceanterminalen", "location_type": LocationType.TERMINAL, "parent_location": cls.port},
        )
        cls.bay = create_location(
            cls.team,
            {"name": "Oceanterminalen Bay 4", "location_type": LocationType.DEPOT, "parent_location": cls.terminal},
        )
        cls.elsewhere = create_location(cls.team, {"name": "MCR Depot", "location_type": LocationType.DEPOT})

    def shipment(
        self,
        number: str,
        *,
        eta_days: int = 3,
        destination: ContainerLocation | None = None,
        canonical: bool = True,
        departed_days: int = 10,
    ) -> Shipment:
        """A departed shipment bound for the terminal, due to arrive in *eta_days*."""
        return Shipment.objects.create(
            team=self.team,
            shipment_number=number,
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            destination_port="Gothenburg",
            destination_location=(destination or self.terminal) if canonical else None,
            etd=timezone.localdate() - timedelta(days=departed_days),
            actual_departure_at=timezone.now() - timedelta(days=departed_days),
            eta=timezone.localdate() + timedelta(days=eta_days),
            original_eta=timezone.localdate() + timedelta(days=eta_days),
        )

    def linked(self, shipment: Shipment, serial: str, sequence: int = 0) -> Container:
        container = make_container(self.team, serial)
        ShipmentContainer.objects.create(shipment=shipment, container=container, sequence=sequence)
        return container

    def arrive(self, container: Container, movement_type: str = MovementType.GATE_IN, **kwargs) -> None:
        """Record arrival evidence at the terminal, through the real movement service."""
        kwargs.setdefault("to_location", self.terminal)
        record_container_movement(
            team=self.team,
            container=container,
            movement_type=movement_type,
            # A receipt is a depot reporting one it handled; a gate move is an
            # operator. Both are direct observations, and the difference is only
            # provenance — see EvidenceStrength.
            source=LocationSource.DEPOT if movement_type == MovementType.RECEIVED else LocationSource.MANUAL,
            **kwargs,
        )
