"""Tests for the carrier registry — provider codes, lookups, and error handling."""

import unittest

from apps.scm.integrations.carriers.registry import (
    UnknownCarrierError,
    carrier_supports,
    get_carrier_client_class,
    get_carrier_definition,
    get_carrier_parser_class,
    list_carriers,
)

EXPECTED_PROVIDER_CODES = [
    "msc",
    "maersk",
    "cma_cgm",
    "cosco",
    "hapag_lloyd",
    "one",
    "evergreen",
    "hmm",
    "yang_ming",
    "zim",
]


class CarrierRegistryProviderCodesTest(unittest.TestCase):
    def test_all_expected_provider_codes_exist(self):
        for code in EXPECTED_PROVIDER_CODES:
            with self.subTest(provider_code=code):
                defn = get_carrier_definition(code)
                self.assertEqual(defn.provider_code, code)

    def test_get_carrier_definition_returns_carrier_definition(self):
        from apps.scm.integrations.carriers.registry import CarrierDefinition

        for code in EXPECTED_PROVIDER_CODES:
            with self.subTest(provider_code=code):
                defn = get_carrier_definition(code)
                self.assertIsInstance(defn, CarrierDefinition)

    def test_unknown_provider_code_raises_unknown_carrier_error(self):
        with self.assertRaises(UnknownCarrierError):
            get_carrier_definition("totally_unknown_carrier_xyz")

    def test_list_carriers_returns_ten_items(self):
        carriers = list_carriers()
        self.assertEqual(len(carriers), 10)

    def test_get_carrier_client_class_returns_a_class(self):
        for code in EXPECTED_PROVIDER_CODES:
            with self.subTest(provider_code=code):
                cls = get_carrier_client_class(code)
                self.assertTrue(isinstance(cls, type), f"Expected a class for {code}, got {cls!r}")

    def test_get_carrier_parser_class_returns_a_class(self):
        for code in EXPECTED_PROVIDER_CODES:
            with self.subTest(provider_code=code):
                cls = get_carrier_parser_class(code)
                self.assertTrue(isinstance(cls, type), f"Expected a class for {code}, got {cls!r}")

    def test_carrier_supports_invalid_capability_raises_attribute_error(self):
        with self.assertRaises(AttributeError):
            carrier_supports("maersk", "this_capability_does_not_exist")

    def test_list_carriers_sorted_by_provider_code(self):
        carriers = list_carriers()
        codes = [c.provider_code for c in carriers]
        self.assertEqual(codes, sorted(codes))
