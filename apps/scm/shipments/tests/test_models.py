"""Tests for Shipment, ShipmentContainer, and ShipmentEvent models."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentContainer, ShipmentEvent
from apps.teams.models import Team


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team: Team, owner: str = "MSC", serial: str = "123456") -> Container:
    check = calculate_check_digit(owner, "U", serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_et(),
    )


def _shipment(team: Team, **kwargs) -> Shipment:
    return Shipment.objects.create(team=team, **kwargs)


class ShipmentModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Model Team", slug="model-team")

    def test_create_shipment(self):
        s = _shipment(self.team, shipment_number="SHP001")
        self.assertIsNotNone(s.pk)
        self.assertEqual(s.team, self.team)

    def test_default_status_is_draft(self):
        s = _shipment(self.team)
        self.assertEqual(s.status, Shipment.Status.DRAFT)

    def test_str_uses_shipment_number(self):
        s = _shipment(self.team, shipment_number="SHP-42")
        self.assertEqual(str(s), "SHP-42")

    def test_str_falls_back_to_reference(self):
        s = _shipment(self.team, reference="REF-99")
        self.assertIn("REF-99", str(s))

    def test_str_falls_back_to_pk(self):
        s = _shipment(self.team)
        self.assertIn(str(s.pk), str(s))

    def test_unique_shipment_number_per_team(self):
        _shipment(self.team, shipment_number="UNIQ-1")
        with self.assertRaises((IntegrityError, ValidationError)):
            _shipment(self.team, shipment_number="UNIQ-1")

    def test_same_shipment_number_in_different_teams(self):
        other_team = Team.objects.create(name="Other Team", slug="other-team-m")
        _shipment(self.team, shipment_number="SHARED")
        # Should not raise
        s2 = _shipment(other_team, shipment_number="SHARED")
        self.assertIsNotNone(s2.pk)

    def test_all_status_choices_valid(self):
        for status_value, _ in Shipment.Status.choices:
            s = Shipment(team=self.team, status=status_value)
            self.assertEqual(s.status, status_value)

    def test_tracking_fields_present(self):
        s = _shipment(self.team)
        self.assertEqual(s.tracking_status, "")
        self.assertIsNone(s.last_tracking_sync_at)
        self.assertEqual(s.carrier, "")
        self.assertEqual(s.carrier_booking_reference, "")
        self.assertEqual(s.bill_of_lading_number, "")


class ShipmentContainerModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="SC Model Team", slug="sc-model-team")
        cls.shipment = _shipment(cls.team, shipment_number="SHP-SC-1")
        cls.container = _container(cls.team)

    def test_link_container_to_shipment(self):
        sc = ShipmentContainer.objects.create(shipment=self.shipment, container=self.container)
        self.assertIsNotNone(sc.pk)
        self.assertEqual(sc.shipment, self.shipment)
        self.assertEqual(sc.container, self.container)

    def test_duplicate_container_in_same_shipment_raises(self):
        ShipmentContainer.objects.create(shipment=self.shipment, container=self.container)
        with self.assertRaises(IntegrityError):
            ShipmentContainer.objects.create(shipment=self.shipment, container=self.container)

    def test_container_can_appear_in_multiple_shipments(self):
        other_shipment = _shipment(self.team, shipment_number="SHP-SC-2")
        ShipmentContainer.objects.create(shipment=self.shipment, container=self.container)
        sc2 = ShipmentContainer.objects.create(shipment=other_shipment, container=self.container)
        self.assertIsNotNone(sc2.pk)

    def test_cross_team_clean_raises_validation_error(self):
        other_team = Team.objects.create(name="Other SC Team", slug="other-sc-team")
        other_container = _container(other_team, owner="EVR", serial="999991")
        sc = ShipmentContainer(shipment=self.shipment, container=other_container)
        with self.assertRaises(ValidationError):
            sc.clean()


class ShipmentEventModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Event Team", slug="event-team")
        cls.shipment = _shipment(cls.team, shipment_number="SHP-EV-1")

    def test_create_event(self):
        event = ShipmentEvent.objects.create(
            shipment=self.shipment,
            event_type=ShipmentEvent.EventType.CREATED,
            description="Shipment created.",
        )
        self.assertIsNotNone(event.pk)
        self.assertEqual(event.shipment, self.shipment)
        self.assertEqual(event.event_type, ShipmentEvent.EventType.CREATED)

    def test_metadata_defaults_to_empty_dict(self):
        event = ShipmentEvent.objects.create(
            shipment=self.shipment,
            event_type=ShipmentEvent.EventType.NOTE_ADDED,
            description="A note.",
        )
        self.assertEqual(event.metadata, {})

    def test_all_event_types_present(self):
        expected = {
            "CREATED",
            "STATUS_CHANGED",
            "CONTAINER_ADDED",
            "CONTAINER_REMOVED",
            "ETA_UPDATED",
            "DELIVERED",
            "CANCELLED",
            "TRACKING_UPDATED",
            "NOTE_ADDED",
        }
        actual = {v for v, _ in ShipmentEvent.EventType.choices}
        self.assertEqual(expected, actual)

    def test_str_representation(self):
        event = ShipmentEvent.objects.create(
            shipment=self.shipment,
            event_type=ShipmentEvent.EventType.CREATED,
            description="Created.",
        )
        self.assertIn("Created", str(event))
