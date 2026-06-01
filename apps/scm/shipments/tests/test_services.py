"""Tests for shipment services."""

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentContainer, ShipmentEvent
from apps.scm.shipments.services import (
    add_container_to_shipment,
    cancel_shipment,
    change_shipment_status,
    create_shipment,
    create_shipment_event,
    remove_container_from_shipment,
    update_shipment,
)
from apps.teams.models import Team
from apps.users.models import CustomUser


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team: Team, owner: str = "CSQ", serial: str = "305418") -> Container:
    check = calculate_check_digit(owner, "U", serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_et(),
    )


class CreateShipmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Svc Create Team", slug="svc-create-team")
        cls.user = CustomUser.objects.create_user(username="svc-create@example.com", password="pass")

    def test_creates_shipment(self):
        s = create_shipment(self.team, self.user, {"shipment_number": "SHP-SVC-1"})
        self.assertIsNotNone(s.pk)
        self.assertEqual(s.team, self.team)

    def test_sets_created_by(self):
        s = create_shipment(self.team, self.user, {})
        self.assertEqual(s.created_by, self.user)

    def test_creates_created_event(self):
        s = create_shipment(self.team, self.user, {})
        events = ShipmentEvent.objects.filter(shipment=s, event_type=ShipmentEvent.EventType.CREATED)
        self.assertEqual(events.count(), 1)

    def test_default_status_is_draft(self):
        s = create_shipment(self.team, self.user, {})
        self.assertEqual(s.status, Shipment.Status.DRAFT)


class UpdateShipmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Svc Update Team", slug="svc-update-team")
        cls.user = CustomUser.objects.create_user(username="svc-update@example.com", password="pass")

    def setUp(self):
        self.shipment = create_shipment(self.team, self.user, {"shipment_number": "SHP-UPD-1"})

    def test_updates_fields(self):
        update_shipment(self.shipment, self.user, {"carrier": "Hapag-Lloyd"})
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.carrier, "Hapag-Lloyd")

    def test_ignores_status_in_data(self):
        update_shipment(self.shipment, self.user, {"status": Shipment.Status.CANCELLED})
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.DRAFT)

    def test_eta_change_creates_event(self):
        import datetime

        new_eta = datetime.date(2026, 9, 1)
        update_shipment(self.shipment, self.user, {"eta": new_eta})
        events = ShipmentEvent.objects.filter(shipment=self.shipment, event_type=ShipmentEvent.EventType.ETA_UPDATED)
        self.assertEqual(events.count(), 1)


class ChangeShipmentStatusTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Status Team", slug="status-team")
        cls.user = CustomUser.objects.create_user(username="status@example.com", password="pass")

    def setUp(self):
        self.shipment = create_shipment(self.team, self.user, {})

    def test_changes_status(self):
        change_shipment_status(self.shipment, self.user, Shipment.Status.BOOKED)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.BOOKED)

    def test_creates_status_changed_event(self):
        change_shipment_status(self.shipment, self.user, Shipment.Status.BOOKED)
        events = ShipmentEvent.objects.filter(shipment=self.shipment, event_type=ShipmentEvent.EventType.STATUS_CHANGED)
        self.assertEqual(events.count(), 1)

    def test_delivered_creates_delivered_event(self):
        change_shipment_status(self.shipment, self.user, Shipment.Status.DELIVERED)
        events = ShipmentEvent.objects.filter(shipment=self.shipment, event_type=ShipmentEvent.EventType.DELIVERED)
        self.assertEqual(events.count(), 1)

    def test_same_status_does_not_create_event(self):
        initial_count = ShipmentEvent.objects.filter(shipment=self.shipment).count()
        change_shipment_status(self.shipment, self.user, Shipment.Status.DRAFT)
        self.assertEqual(ShipmentEvent.objects.filter(shipment=self.shipment).count(), initial_count)


class CancelShipmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Cancel Team", slug="cancel-team")
        cls.user = CustomUser.objects.create_user(username="cancel@example.com", password="pass")

    def test_cancel_sets_cancelled_status(self):
        s = create_shipment(self.team, self.user, {})
        cancel_shipment(s, self.user)
        s.refresh_from_db()
        self.assertEqual(s.status, Shipment.Status.CANCELLED)

    def test_cancel_creates_cancelled_event(self):
        s = create_shipment(self.team, self.user, {})
        cancel_shipment(s, self.user)
        events = ShipmentEvent.objects.filter(shipment=s, event_type=ShipmentEvent.EventType.CANCELLED)
        self.assertEqual(events.count(), 1)


class AddContainerToShipmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="AddCont Team", slug="addcont-team")
        cls.other_team = Team.objects.create(name="Other AddCont Team", slug="other-addcont-team")
        cls.user = CustomUser.objects.create_user(username="addcont@example.com", password="pass")
        cls.shipment = create_shipment(cls.team, cls.user, {})
        cls.container = _container(cls.team)

    def test_adds_container(self):
        sc = add_container_to_shipment(self.team, self.shipment, self.container, self.user)
        self.assertIsNotNone(sc.pk)
        self.assertTrue(ShipmentContainer.objects.filter(shipment=self.shipment, container=self.container).exists())

    def test_creates_container_added_event(self):
        add_container_to_shipment(self.team, self.shipment, self.container, self.user)
        events = ShipmentEvent.objects.filter(
            shipment=self.shipment, event_type=ShipmentEvent.EventType.CONTAINER_ADDED
        )
        self.assertEqual(events.count(), 1)

    def test_cross_team_container_raises(self):
        other_container = _container(self.other_team, owner="EVR", serial="999993")
        with self.assertRaises(ValidationError):
            add_container_to_shipment(self.team, self.shipment, other_container, self.user)


class RemoveContainerFromShipmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="RemCont Team", slug="remcont-team")
        cls.user = CustomUser.objects.create_user(username="remcont@example.com", password="pass")

    def setUp(self):
        self.shipment = create_shipment(self.team, self.user, {})
        self.container = _container(self.team)
        self.sc = add_container_to_shipment(self.team, self.shipment, self.container, self.user)

    def test_removes_link(self):
        sc_pk = self.sc.pk
        remove_container_from_shipment(self.team, self.shipment, self.sc, self.user)
        self.assertFalse(ShipmentContainer.objects.filter(pk=sc_pk).exists())

    def test_creates_container_removed_event(self):
        remove_container_from_shipment(self.team, self.shipment, self.sc, self.user)
        events = ShipmentEvent.objects.filter(
            shipment=self.shipment, event_type=ShipmentEvent.EventType.CONTAINER_REMOVED
        )
        self.assertTrue(events.exists())


class CreateShipmentEventTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="EvSvc Team", slug="evsvc-team")
        cls.user = CustomUser.objects.create_user(username="evsvc@example.com", password="pass")
        cls.shipment = Shipment.objects.create(team=cls.team)

    def test_creates_event(self):
        event = create_shipment_event(
            shipment=self.shipment,
            event_type=ShipmentEvent.EventType.NOTE_ADDED,
            description="A note.",
            user=self.user,
        )
        self.assertIsNotNone(event.pk)
        self.assertEqual(event.created_by, self.user)

    def test_metadata_stored(self):
        event = create_shipment_event(
            shipment=self.shipment,
            event_type=ShipmentEvent.EventType.NOTE_ADDED,
            description="With meta.",
            metadata={"key": "value"},
        )
        self.assertEqual(event.metadata["key"], "value")
