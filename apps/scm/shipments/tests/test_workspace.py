"""Tests for the shipment detail read context — data assembly and team isolation.

These used to cover ``get_shipment_workspace``, which assembled the same
containers, subscriptions and latest tracking event that
``get_shipment_detail_context`` already assembled. The detail page built both and
paid for both. The workspace is gone; the assertions it earned are here.
"""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentContainer, ShipmentEvent
from apps.scm.shipments.selectors import get_shipment_detail_context, get_shipment_events
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


class ShipmentDetailContextTest(TestCase):
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

    def _context(self, shipment=None) -> dict:
        return get_shipment_detail_context(self.team, (shipment or self.shipment).pk)

    def test_context_includes_the_shipment(self):
        self.assertEqual(self._context()["shipment"], self.shipment)

    def test_context_includes_containers(self):
        self.assertIn(self.sc, self._context()["containers"])

    def test_context_includes_the_shipments_own_events_on_the_timeline(self):
        titles = {item.title for item in self._context()["timeline_events"]}
        self.assertIn(self.event.get_event_type_display(), titles)

    def test_no_tracking_event_when_the_carrier_has_reported_nothing(self):
        self.assertIsNone(self._context()["latest_tracking_event"])

    def test_another_teams_shipment_is_not_reachable_by_id(self):
        other = _shipment(self.other_team, "SWS-OTHER")
        with self.assertRaises(Shipment.DoesNotExist):
            get_shipment_detail_context(self.team, other.pk)

    def test_another_teams_shipment_yields_no_events(self):
        other = _shipment(self.other_team, "SWS-OTHER-EV")
        self.assertEqual(list(get_shipment_events(self.team, other)), [])


@override_settings(STORAGES=_TEST_STORAGES)
class ShipmentDetailViewTest(TestCase):
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
        self.assertIn("containers", response.context)
        self.assertIn("visibility", response.context)

    def test_detail_view_404_for_other_team_shipment(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("shipments:detail", kwargs={"pk": self.other_shipment.pk}))
        self.assertIn(response.status_code, [404, 403])
