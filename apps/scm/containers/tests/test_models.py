"""Tests for EquipmentType and Container models."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from apps.scm.containers.choices import ContainerCondition, ContainerStatus
from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.teams.models import BaseTeamModel, Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

# Verified valid container ID parts
OWNER = "CSQ"
CAT = "U"
SERIAL = "305418"
CHECK = calculate_check_digit(OWNER, CAT, SERIAL)  # 8


def _et(iso_code="20GP", category="GP", length_ft=20, description="20' GP") -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code=iso_code,
        defaults={"category": category, "length_ft": length_ft, "high_cube": False, "description": description},
    )[0]


def _container(team, owner=OWNER, serial=SERIAL, **kwargs) -> Container:
    check = calculate_check_digit(owner, "U", serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_et(),
        **kwargs,
    )


class EquipmentTypeModelTest(TestCase):
    def test_create_equipment_type(self):
        et = _et()
        self.assertEqual(et.iso_code, "20GP")
        self.assertTrue(et.is_active)

    def test_image_url_none_without_image(self):
        et = _et()
        self.assertIsNone(et.image_url)

    def test_ordering_by_length_then_category(self):
        _et("20GP", "GP", 20, "20' GP")
        _et("40GP", "GP", 40, "40' GP")
        iso_codes = list(EquipmentType.objects.values_list("iso_code", flat=True))
        self.assertLess(iso_codes.index("20GP"), iso_codes.index("40GP"))

    def test_str_contains_iso_code(self):
        et = _et()
        self.assertIn("20GP", str(et))


class ContainerModelInheritanceTest(TestCase):
    def test_container_extends_base_team_model(self):
        self.assertTrue(issubclass(Container, BaseTeamModel))

    def test_has_timestamps(self):
        field_names = [f.name for f in Container._meta.fields]
        self.assertIn("created_at", field_names)
        self.assertIn("updated_at", field_names)


class ContainerModelTest(TestCase):
    team: Team
    et: EquipmentType

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Model Team", slug="model-team")
        cls.user = CustomUser.objects.create_user(username="model@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.et = _et()

    def _create(self, owner=OWNER, serial=SERIAL, **kwargs) -> Container:
        check = calculate_check_digit(owner, "U", serial)
        return Container.objects.create(
            team=self.team,
            owner_code=owner,
            category_id="U",
            serial_number=serial,
            check_digit=check,
            equipment_type=self.et,
            **kwargs,
        )

    def test_create_container_with_valid_id(self):
        c = self._create()
        self.assertEqual(c.team, self.team)
        self.assertEqual(c.owner_code, OWNER)

    def test_owner_code_normalised_to_uppercase(self):
        c = self._create(owner=OWNER.lower())
        self.assertEqual(c.owner_code, OWNER)

    def test_category_id_normalised_to_uppercase(self):
        check = calculate_check_digit(OWNER, CAT, SERIAL)
        c = Container(
            team=self.team,
            owner_code=OWNER,
            category_id=CAT.lower(),
            serial_number=SERIAL,
            check_digit=check,
            equipment_type=self.et,
        )
        c.save()
        self.assertEqual(c.category_id, CAT)

    def test_wrong_check_digit_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            Container.objects.create(
                team=self.team,
                owner_code=OWNER,
                category_id=CAT,
                serial_number=SERIAL,
                check_digit=0,  # definitely wrong
                equipment_type=self.et,
            )

    def test_duplicate_within_same_team_raises(self):
        self._create()
        with self.assertRaises((IntegrityError, ValidationError)):
            self._create()

    def test_same_container_id_in_different_teams_allowed(self):
        other_team = Team.objects.create(name="Other", slug="other")
        c1 = self._create()
        c2 = Container.objects.create(
            team=other_team,
            owner_code=OWNER,
            category_id=CAT,
            serial_number=SERIAL,
            check_digit=CHECK,
            equipment_type=self.et,
        )
        self.assertEqual(c1.owner_code, c2.owner_code)

    def test_container_id_property(self):
        c = self._create()
        expected = f"{c.owner_code}{c.category_id}{c.serial_number}{c.check_digit}"
        self.assertEqual(c.container_id, expected)

    def test_str_is_container_id(self):
        c = self._create()
        self.assertEqual(str(c), c.container_id)

    def test_default_status_available(self):
        c = self._create()
        self.assertEqual(c.status, ContainerStatus.AVAILABLE)

    def test_default_condition_good(self):
        c = self._create()
        self.assertEqual(c.condition, ContainerCondition.GOOD)

    def test_ordering_newest_first(self):
        self.assertEqual(Container._meta.ordering, ["-created_at"])
