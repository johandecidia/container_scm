"""Tests for ISO 6346 container ID helpers."""

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from apps.scm.containers.utils import (
    calculate_check_digit,
    parse_container_id,
    validate_container_id,
)

# CSQU305418 → check digit 8 (verified by algorithm)
# Wikipedia's CSQU3054187 is an error; correct is CSQU3054188.
VALID_OWNER = "CSQ"
VALID_CAT = "U"
VALID_SERIAL = "305418"
VALID_CHECK = 8
VALID_ID = "CSQU3054188"


class CalculateCheckDigitTest(SimpleTestCase):
    def test_known_combination(self):
        self.assertEqual(calculate_check_digit(VALID_OWNER, VALID_CAT, VALID_SERIAL), VALID_CHECK)

    def test_returns_int(self):
        result = calculate_check_digit("ABC", "U", "000000")
        self.assertIsInstance(result, int)

    def test_result_in_valid_range(self):
        result = calculate_check_digit("MSC", "U", "123456")
        self.assertIn(result, range(10))

    def test_raises_on_wrong_prefix_length(self):
        with self.assertRaises(ValueError):
            calculate_check_digit("MSC", "U", "12345")  # only 5 digits

    def test_different_inputs_give_consistent_results(self):
        r1 = calculate_check_digit("MSC", "U", "999999")
        r2 = calculate_check_digit("MSC", "U", "999999")
        self.assertEqual(r1, r2)


class ValidateContainerIdTest(SimpleTestCase):
    def test_valid_id_passes(self):
        # No exception raised
        validate_container_id(VALID_OWNER, VALID_CAT, VALID_SERIAL, VALID_CHECK)

    def test_wrong_check_digit_raises(self):
        with self.assertRaises(ValidationError):
            validate_container_id(VALID_OWNER, VALID_CAT, VALID_SERIAL, 7)

    def test_string_check_digit_coerced(self):
        validate_container_id(VALID_OWNER, VALID_CAT, VALID_SERIAL, str(VALID_CHECK))


class ParseContainerIdTest(SimpleTestCase):
    def test_parses_valid_id(self):
        result = parse_container_id(VALID_ID)
        self.assertEqual(result["owner_code"], VALID_OWNER)
        self.assertEqual(result["category_id"], VALID_CAT)
        self.assertEqual(result["serial_number"], VALID_SERIAL)
        self.assertEqual(result["check_digit"], VALID_CHECK)

    def test_lowercase_normalised(self):
        result = parse_container_id(VALID_ID.lower())
        self.assertEqual(result["owner_code"], VALID_OWNER)

    def test_strips_whitespace(self):
        result = parse_container_id(f"  {VALID_ID}  ")
        self.assertEqual(result["owner_code"], VALID_OWNER)

    def test_invalid_format_raises(self):
        with self.assertRaises(ValidationError):
            parse_container_id("NOTANID")

    def test_wrong_category_raises(self):
        # A is not a valid category identifier (only U, J, Z)
        with self.assertRaises(ValidationError):
            parse_container_id("CSQA3054188")

    def test_too_short_raises(self):
        with self.assertRaises(ValidationError):
            parse_container_id("CSQU30541")
