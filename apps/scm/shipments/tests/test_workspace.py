"""Tests for shipment workspace selector — team isolation and data assembly."""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentContainer, ShipmentEvent
from apps.scm.shipments.selectors import ShipmentWorkspace, get_shipment_workspace
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


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


def _shipment(team: Team, number: str = "SWS-001", **kwargs) -> Shipment:
    return Shipment.objects.create(team=team, shipment_number=number, **kwargs)


class ShipmentWorkspaceSelectorTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="SWS Team", slug="sws-team")
        cls.other_team = Team.objects.create(name="Other SWS Team", slug="other-sws-team")
        cls.shipment = _shipment(cls.team)
        cls.container = _container(cls.team)
        cls.sc = ShipmentContainer.objects.create(shipment=cls.shipment, container=cls.container)
        cls.event = ShipmentEvent.objects.create(
            shipment=cls.shipment,
            event_type=ShipmentEvent.EventType.CREATED,
            description="Shipment created.",
        )

    def test_returns_shipment_workspace_dataclass(self):
        ws = get_shipment_workspace(self.team, self.shipment)
        self.assertIsInstance(ws, ShipmentWorkspace)

    def test_workspace_includes_shipment(self):
        ws = get_shipment_workspace(self.team, self.shipment)
        self.assertEqual(ws.shipment, self.shipment)

    def test_workspace_includes_containers(self):
        ws = get_shipment_workspace(self.team, self.shipment)
        self.assertIn(self.sc, ws.containers)

    def test_workspace_includes_events(self):
        ws = get_shipment_workspace(self.team, self.shipment)
        self.assertIn(self.event, ws.events)

    def test_workspace_no_tracking_event_when_none(self):
        ws = get_shipment_workspace(self.team, self.shipment)
        self.assertIsNone(ws.latest_tracking_event)

    def test_workspace_other_team_shipment_returns_empty_containers(self):
        other_shipment = _shipment(self.other_team, "SWS-OTHER")
        ws = get_shipment_workspace(self.team, other_shipment)
        self.assertEqual(ws.containers, [])

    def test_workspace_other_team_shipment_returns_empty_events(self):
        other_shipment = _shipment(self.other_team, "SWS-OTHER-EV")
        ws = get_shipment_workspace(self.team, other_shipment)
        self.assertEqual(ws.events, [])


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentDetailViewWorkspaceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="SWS View Team", slug="sws-view-team")
        cls.user = CustomUser.objects.create_user(username="sws-view@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.other_team = Team.objects.create(name="Other SWS View Team", slug="other-sws-view-team")
        cls.shipment = _shipment(cls.team, "SWS-VIEW-001")
        cls.other_shipment = _shipment(cls.other_team, "SWS-VIEW-OTHER")

    def test_detail_view_renders_for_own_shipment(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("shipments:detail", kwargs={"pk": self.shipment.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertIn("workspace", response.context)

    def test_detail_view_404_for_other_team_shipment(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("shipments:detail", kwargs={"pk": self.other_shipment.pk}))
        self.assertIn(response.status_code, [404, 403])
