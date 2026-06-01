"""Tests for the business system registry and placeholder clients."""

import unittest

from apps.scm.integrations.business_systems.registry import (
    UnknownBusinessSystemError,
    get_business_system_definition,
    list_business_systems,
)


class BusinessSystemRegistryTest(unittest.TestCase):
    def test_get_business_central_definition(self):
        defn = get_business_system_definition("business_central")
        self.assertIsNotNone(defn)

    def test_get_john_evans_definition(self):
        defn = get_business_system_definition("john_evans")
        self.assertIsNotNone(defn)

    def test_list_business_systems_has_length_two(self):
        systems = list_business_systems()
        self.assertEqual(len(systems), 2)

    def test_business_central_has_non_empty_name(self):
        defn = get_business_system_definition("business_central")
        self.assertTrue(len(defn.name) > 0)

    def test_john_evans_has_non_empty_name(self):
        defn = get_business_system_definition("john_evans")
        self.assertTrue(len(defn.name) > 0)

    def test_unknown_system_raises_unknown_business_system_error(self):
        with self.assertRaises(UnknownBusinessSystemError):
            get_business_system_definition("unknown_system")

    def test_list_business_systems_sorted_by_system_code(self):
        systems = list_business_systems()
        codes = [s.system_code for s in systems]
        self.assertEqual(codes, sorted(codes))


class BusinessCentralClientTest(unittest.TestCase):
    def test_system_code_is_business_central(self):
        from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient

        self.assertEqual(BusinessCentralClient.system_code, "business_central")


class JohnEvansClientTest(unittest.TestCase):
    def test_system_code_is_john_evans(self):
        from apps.scm.integrations.business_systems.john_evans.client import JohnEvansClient

        self.assertEqual(JohnEvansClient.system_code, "john_evans")
