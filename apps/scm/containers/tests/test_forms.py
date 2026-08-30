"""Tests for ContainerForm."""

from django.test import TestCase

from apps.scm.containers.forms import ContainerForm
from apps.scm.containers.models import EquipmentType
from apps.scm.containers.utils import calculate_check_digit

# CSQU305418 → check digit 8 → CSQU3054188
VALID_OWNER = "CSQ"
VALID_CAT = "U"
VALID_SERIAL = "305418"
VALID_CHECK = calculate_check_digit(VALID_OWNER, VALID_CAT, VALID_SERIAL)  # 8
VALID_ID = f"{VALID_OWNER}{VALID_CAT}{VALID_SERIAL}{VALID_CHECK}"  # CSQU3054188


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _base_data(container_id=None, **kwargs) -> dict:
    if container_id is None:
        container_id = VALID_ID
    et = _et()
    data = {
        "container_id_input": container_id,
        "equipment_type": et.pk,
        "status": "AVAILABLE",
        "condition": "GOOD",
        "color_system": "UNKNOWN",
    }
    data.update(kwargs)
    return data


class ContainerFormValidTest(TestCase):
    def test_valid_form_is_valid(self):
        form = ContainerForm(_base_data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_get_container_data_returns_correct_parts(self):
        form = ContainerForm(_base_data())
        self.assertTrue(form.is_valid())
        data = form.get_container_data()
        self.assertEqual(data["owner_code"], VALID_OWNER)
        self.assertEqual(data["category_id"], VALID_CAT)
        self.assertEqual(data["serial_number"], VALID_SERIAL)
        self.assertEqual(data["check_digit"], VALID_CHECK)

    def test_lowercase_id_normalised(self):
        form = ContainerForm(_base_data(container_id=VALID_ID.lower()))
        self.assertTrue(form.is_valid(), form.errors)
        data = form.get_container_data()
        self.assertEqual(data["owner_code"], VALID_OWNER)

    def test_team_and_user_fields_not_exposed(self):
        form = ContainerForm()
        for field in ("team", "created_by", "updated_by", "owner_code", "category_id", "serial_number", "check_digit"):
            self.assertNotIn(field, form.fields)


class ContainerFormInvalidTest(TestCase):
    def test_invalid_format_gives_error(self):
        form = ContainerForm(_base_data(container_id="NOTANID"))
        self.assertFalse(form.is_valid())
        self.assertIn("container_id_input", form.errors)

    def test_wrong_check_digit_gives_error(self):
        # Build ID with wrong check digit
        wrong_id = f"{VALID_OWNER}{VALID_CAT}{VALID_SERIAL}{(VALID_CHECK + 1) % 10}"
        form = ContainerForm(_base_data(container_id=wrong_id))
        self.assertFalse(form.is_valid())
        self.assertIn("container_id_input", form.errors)

    def test_wrong_category_gives_error(self):
        # X is not a valid category identifier (only U, A, J, Z)
        form = ContainerForm(_base_data(container_id=f"CSQX{VALID_SERIAL}{VALID_CHECK}"))
        self.assertFalse(form.is_valid())
        self.assertIn("container_id_input", form.errors)
