"""Tests for shipment selectors — team isolation is critical."""

from django.test import TestCase

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentEvent
from apps.scm.shipments.selectors import (
    filter_shipments,
    get_shipment_containers,
    get_shipment_events,
    get_team_shipment,
    get_team_shipments,
)
from apps.teams.models import Team


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team: Team, owner: str = "MSC", serial: str = "111111") -> Container:
    check = calculate_check_digit(owner, "U", serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_et(),
    )


def _shipment(team: Team, number: str = "SHP-SEL-1", **kwargs) -> Shipment:
    return Shipment.objects.create(team=team, shipment_number=number, **kwargs)


class GetTeamShipmentsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Sel Team", slug="sel-team")
        cls.other_team = Team.objects.create(name="Other Sel Team", slug="other-sel-team")
        cls.own = _shipment(cls.team, "SHP-OWN")
        cls.other = _shipment(cls.other_team, "SHP-OTHER")

    def test_returns_own_team_shipments(self):
        qs = get_team_shipments(self.team)
        self.assertIn(self.own, qs)

    def test_does_not_return_other_team_shipments(self):
        qs = get_team_shipments(self.team)
        self.assertNotIn(self.other, qs)


class GetTeamShipmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Get Team", slug="get-team")
        cls.other_team = Team.objects.create(name="Other Get Team", slug="other-get-team")
        cls.own = _shipment(cls.team, "SHP-GET-OWN")
        cls.other = _shipment(cls.other_team, "SHP-GET-OTHER")

    def test_returns_own_shipment(self):
        s = get_team_shipment(self.team, self.own.pk)
        self.assertEqual(s, self.own)

    def test_raises_for_other_team_shipment(self):
        from apps.scm.shipments.models import Shipment

        with self.assertRaises(Shipment.DoesNotExist):
            get_team_shipment(self.team, self.other.pk)


class FilterShipmentsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Filter Team", slug="filter-team")
        cls.draft = _shipment(cls.team, "SHP-DRAFT", status=Shipment.Status.DRAFT)
        cls.booked = _shipment(cls.team, "SHP-BOOKED", status=Shipment.Status.BOOKED, carrier="Maersk")

    def test_filter_by_status(self):
        qs = filter_shipments(self.team, status=Shipment.Status.DRAFT)
        self.assertIn(self.draft, qs)
        self.assertNotIn(self.booked, qs)

    def test_search_by_carrier(self):
        qs = filter_shipments(self.team, search="Maersk")
        self.assertIn(self.booked, qs)
        self.assertNotIn(self.draft, qs)

    def test_search_by_shipment_number(self):
        qs = filter_shipments(self.team, search="DRAFT")
        self.assertIn(self.draft, qs)

    def test_sort_oldest(self):
        qs = list(filter_shipments(self.team, sort="oldest"))
        self.assertEqual(qs[0], self.draft)


class GetShipmentContainersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="SC Sel Team", slug="sc-sel-team")
        cls.other_team = Team.objects.create(name="Other SC Sel Team", slug="other-sc-sel-team")
        cls.shipment = _shipment(cls.team, "SHP-CONT-SEL")
        cls.container = _container(cls.team)
        from apps.scm.shipments.models import ShipmentContainer

        cls.sc = ShipmentContainer.objects.create(shipment=cls.shipment, container=cls.container)

    def test_returns_linked_containers(self):
        qs = get_shipment_containers(self.team, self.shipment)
        self.assertIn(self.sc, qs)

    def test_other_team_shipment_returns_empty(self):
        other_shipment = _shipment(self.other_team, "SHP-OTHER-CONT")
        qs = get_shipment_containers(self.team, other_shipment)
        self.assertEqual(list(qs), [])


class GetShipmentEventsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Ev Sel Team", slug="ev-sel-team")
        cls.other_team = Team.objects.create(name="Other Ev Sel Team", slug="other-ev-sel-team")
        cls.shipment = _shipment(cls.team, "SHP-EV-SEL")
        cls.event = ShipmentEvent.objects.create(
            shipment=cls.shipment,
            event_type=ShipmentEvent.EventType.CREATED,
            description="Created.",
        )

    def test_returns_events_for_shipment(self):
        qs = get_shipment_events(self.team, self.shipment)
        self.assertIn(self.event, qs)

    def test_other_team_shipment_returns_empty(self):
        other_shipment = _shipment(self.other_team, "SHP-OTHER-EV")
        qs = get_shipment_events(self.team, other_shipment)
        self.assertEqual(list(qs), [])
