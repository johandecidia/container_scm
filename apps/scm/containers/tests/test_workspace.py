"""Tests for container workspace selector — team isolation and data assembly."""

from django.test import TestCase

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.selectors import ContainerWorkspace, get_container_workspace
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentContainer
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


def _shipment(team: Team, number: str = "WS-SHP-001") -> Shipment:
    return Shipment.objects.create(team=team, shipment_number=number)


class ContainerWorkspaceSelectorTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="WS Team", slug="ws-team")
        cls.other_team = Team.objects.create(name="Other WS Team", slug="other-ws-team")
        cls.container = _container(cls.team)
        cls.shipment = _shipment(cls.team)
        cls.sc = ShipmentContainer.objects.create(shipment=cls.shipment, container=cls.container)

    def test_returns_container_workspace_dataclass(self):
        ws = get_container_workspace(self.team, self.container)
        self.assertIsInstance(ws, ContainerWorkspace)

    def test_workspace_includes_container(self):
        ws = get_container_workspace(self.team, self.container)
        self.assertEqual(ws.container, self.container)

    def test_workspace_includes_shipment_containers(self):
        ws = get_container_workspace(self.team, self.container)
        self.assertIn(self.sc, ws.shipment_containers)

    def test_workspace_excludes_other_team_shipments(self):
        other_container = _container(self.other_team, owner="CMA", serial="222222")
        other_shipment = _shipment(self.other_team, "WS-OTHER-SHP")
        # Associate other_container with other_team's shipment
        other_sc = ShipmentContainer.objects.create(shipment=other_shipment, container=other_container)

        # Query workspace for our container — other_sc should not appear
        ws = get_container_workspace(self.team, self.container)
        self.assertNotIn(other_sc, ws.shipment_containers)

    def test_workspace_no_tracking_event_when_none(self):
        ws = get_container_workspace(self.team, self.container)
        self.assertIsNone(ws.latest_tracking_event)

    def test_workspace_empty_subscriptions_when_none(self):
        ws = get_container_workspace(self.team, self.container)
        self.assertEqual(ws.tracking_subscriptions, [])
