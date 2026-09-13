"""A shipment's canonical origin and destination, and what must not overwrite them.

The point of these two columns is that they are *decisions*, not observations. The
port text is what a carrier or a booking said; the location is which of MCR's own
places that is. So the tests here are mostly about both records surviving:

* setting a location does not change the reported text,
* a tracking refresh changes neither,
* and a location belonging to another team cannot be attached at all.

The overwrite test is the important one. Nothing in the tracking pipeline writes
these columns today, and this test is what will notice if that ever changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.scm.containers.choices import LocationType
from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.services import create_location
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.shipments.selectors import filter_shipments
from apps.scm.shipments.services import update_shipment_eta
from apps.scm.tracking.ingestion import persist_normalised_event
from apps.scm.tracking.models import TrackingProvider
from apps.teams.models import Team
from apps.users.models import CustomUser


def _equipment() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team, serial: str) -> Container:
    return Container.objects.create(
        team=team,
        owner_code="MSK",
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit("MSK", "U", serial),
        equipment_type=_equipment(),
    )


class CanonicalRoutingTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Routing", slug="ship-routing")
        cls.other_team = Team.objects.create(name="Theirs", slug="ship-routing-theirs")

    def setUp(self):
        self.port = create_location(
            self.team, {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT"}
        )
        self.terminal = create_location(
            self.team,
            {"name": "Oceanterminalen", "location_type": LocationType.DEPOT, "parent_location": self.port},
        )
        self.shanghai = create_location(self.team, {"name": "Shanghai", "location_type": LocationType.PORT})

    def test_a_shipment_carries_canonical_origin_and_destination(self):
        shipment = Shipment.objects.create(
            team=self.team,
            shipment_number="SHP-1",
            origin_location=self.shanghai,
            destination_location=self.terminal,
        )
        shipment.refresh_from_db()
        self.assertEqual(shipment.origin_location, self.shanghai)
        self.assertEqual(shipment.destination_location, self.terminal)

    def test_the_canonical_fks_are_optional(self):
        """Every existing shipment has neither, and stays valid."""
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-LEGACY", destination_port="Gothenburg")
        shipment.full_clean()
        self.assertIsNone(shipment.destination_location_id)

    def test_the_reported_text_is_kept_alongside_the_canonical_place(self):
        """Both records: what the carrier called it, and which place we decided it is."""
        shipment = Shipment.objects.create(
            team=self.team,
            shipment_number="SHP-BOTH",
            origin_port="Shanghai",
            destination_port="Gothenburg",
            origin_location=self.shanghai,
            destination_location=self.terminal,
        )
        shipment.refresh_from_db()
        self.assertEqual(shipment.destination_port, "Gothenburg")
        self.assertEqual(shipment.destination_location, self.terminal)

    def test_choosing_a_location_does_not_change_the_reported_text(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-KEEP", destination_port="GOTEBORG, SE")
        shipment.destination_location = self.terminal
        shipment.save(update_fields=["destination_location"])
        shipment.refresh_from_db()
        self.assertEqual(shipment.destination_port, "GOTEBORG, SE")

    def test_a_location_from_another_team_is_rejected(self):
        theirs = create_location(self.other_team, {"name": "Their Depot"})
        shipment = Shipment(team=self.team, shipment_number="SHP-X", destination_location=theirs)
        with self.assertRaises(ValidationError):
            shipment.full_clean()

    def test_deleting_a_location_does_not_delete_the_shipment(self):
        """The booking outlives the location record; the text destination remains."""
        shipment = Shipment.objects.create(
            team=self.team,
            shipment_number="SHP-SURVIVE",
            destination_port="Gothenburg",
            destination_location=self.terminal,
        )
        self.terminal.delete()
        shipment.refresh_from_db()
        self.assertIsNone(shipment.destination_location_id)
        self.assertEqual(shipment.destination_port, "Gothenburg")

    def test_a_shipment_is_findable_by_its_canonical_destination_name(self):
        Shipment.objects.create(
            team=self.team, shipment_number="SHP-FIND", destination_port="", destination_location=self.terminal
        )
        found = [s.shipment_number for s in filter_shipments(self.team, search="Oceanterminalen")]
        self.assertEqual(found, ["SHP-FIND"])


class ProviderRefreshDoesNotOverwriteTest(TestCase):
    """A carrier refresh must never replace a destination somebody chose.

    Not an assertion about intent — an assertion about behaviour, run against the
    real ingestion and ETA paths. If a future change starts writing
    ``destination_location`` from a provider payload, this fails.
    """

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Refresh", slug="ship-refresh")
        cls.user = CustomUser.objects.create_user(username="refresh@example.com", password="pw")

    def setUp(self):
        self.port = create_location(
            self.team, {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT"}
        )
        self.terminal = create_location(
            self.team,
            {"name": "Oceanterminalen", "location_type": LocationType.DEPOT, "parent_location": self.port},
        )
        self.shipment = Shipment.objects.create(
            team=self.team,
            shipment_number="SHP-REFRESH",
            status=Shipment.Status.IN_TRANSIT,
            destination_port="Gothenburg",
            # The operator's own decision: not the port, the terminal inside it.
            destination_location=self.terminal,
            eta=timezone.localdate() + timedelta(days=5),
        )
        self.container = _container(self.team, "990001")
        ShipmentContainer.objects.create(shipment=self.shipment, container=self.container)

    def test_ingesting_a_carrier_event_about_the_port_leaves_the_terminal_chosen(self):
        persist_normalised_event(
            team=self.team,
            provider=TrackingProvider.objects.get_or_create(code="maersk", defaults={"name": "Maersk"})[0],
            normalised=NormalisedTrackingEvent(
                event_type="EQUIPMENT",
                event_classifier="ACT",
                event_code="DISC",
                event_datetime=datetime(2024, 3, 10, 8, 0, tzinfo=UTC),
                location_name="GOTEBORG",
                location_unlocode="SEGOT",
                container_number=self.container.container_id,
                raw_event_id="EVT-DISC",
            ),
            shipment=self.shipment,
            container=self.container,
        )
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.destination_location, self.terminal)
        self.assertEqual(self.shipment.destination_port, "Gothenburg")

    def test_an_eta_update_leaves_the_routing_alone(self):
        update_shipment_eta(
            shipment=self.shipment,
            eta_date=timezone.localdate() + timedelta(days=9),
            source="maersk",
            location_name="GOTEBORG",
            location_unlocode="SEGOT",
        )
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.destination_location, self.terminal)
        self.assertEqual(self.shipment.destination_port, "Gothenburg")

    def test_the_event_still_resolves_to_the_port_it_actually_named(self):
        """The event's own location and the shipment's destination are different
        facts, and the first does not become the second."""
        event, _created = persist_normalised_event(
            team=self.team,
            provider=TrackingProvider.objects.get_or_create(code="maersk", defaults={"name": "Maersk"})[0],
            normalised=NormalisedTrackingEvent(
                event_type="EQUIPMENT",
                event_classifier="ACT",
                event_datetime=datetime(2024, 3, 10, 8, 0, tzinfo=UTC),
                location_unlocode="SEGOT",
                container_number=self.container.container_id,
                raw_event_id="EVT-PORT",
            ),
            shipment=self.shipment,
        )
        self.assertEqual(event.location, self.port)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.destination_location, self.terminal)
