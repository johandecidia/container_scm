"""Adapter contract tests — verify that every carrier client and parser
satisfies the interface contract defined by BaseCarrierClient / BaseCarrierParser.

Each carrier must:
- Define a non-empty provider_code class attribute.
- Inherit from the correct base class.
- Have provider_code matching the carrier registry entry.
"""

import unittest

from apps.scm.integrations.carriers.base import BaseCarrierClient, BaseCarrierParser
from apps.scm.integrations.carriers.registry import get_carrier_definition, list_carriers

ALL_CARRIER_CODES = [
    "maersk",
    "msc",
    "cma_cgm",
    "cosco",
    "hapag_lloyd",
    "one",
    "evergreen",
    "hmm",
    "yang_ming",
    "zim",
]


class CarrierClientInheritanceTest(unittest.TestCase):
    """Every carrier client class must inherit from BaseCarrierClient."""

    def test_all_carrier_clients_inherit_from_base(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                defn = get_carrier_definition(code)
                self.assertTrue(
                    issubclass(defn.client_class, BaseCarrierClient),
                    f"{defn.client_class.__name__} must inherit from BaseCarrierClient",
                )

    def test_all_carrier_parsers_inherit_from_base(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                defn = get_carrier_definition(code)
                self.assertTrue(
                    issubclass(defn.parser_class, BaseCarrierParser),
                    f"{defn.parser_class.__name__} must inherit from BaseCarrierParser",
                )


class CarrierClientProviderCodeTest(unittest.TestCase):
    """Every carrier client and parser must declare a non-empty provider_code."""

    def test_all_client_classes_have_provider_code(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                defn = get_carrier_definition(code)
                client = defn.client_class()
                self.assertTrue(
                    client.provider_code,
                    f"{defn.client_class.__name__}.provider_code must be a non-empty string",
                )

    def test_all_parser_classes_have_provider_code(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                defn = get_carrier_definition(code)
                parser = defn.parser_class()
                self.assertTrue(
                    parser.provider_code,
                    f"{defn.parser_class.__name__}.provider_code must be a non-empty string",
                )

    def test_client_provider_code_matches_registry(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                defn = get_carrier_definition(code)
                client = defn.client_class()
                self.assertEqual(
                    client.provider_code,
                    code,
                    f"{defn.client_class.__name__}.provider_code ({client.provider_code!r}) "
                    f"must match registry key ({code!r})",
                )

    def test_parser_provider_code_matches_registry(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                defn = get_carrier_definition(code)
                parser = defn.parser_class()
                self.assertEqual(
                    parser.provider_code,
                    code,
                    f"{defn.parser_class.__name__}.provider_code ({parser.provider_code!r}) "
                    f"must match registry key ({code!r})",
                )


class CarrierClientMethodSignatureTest(unittest.TestCase):
    """fetch_tracking must accept all four keyword-only reference arguments."""

    def test_fetch_tracking_accepts_container_number(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                client = get_carrier_definition(code).client_class()
                # Must accept the kwarg (NotImplementedError is fine — it means the
                # client is a stub, not that the interface is broken).
                try:
                    client.fetch_tracking(container_number="TEST1234567")
                except NotImplementedError:
                    pass
                except TypeError as exc:
                    self.fail(f"{code}: fetch_tracking does not accept container_number — {exc}")

    def test_fetch_tracking_accepts_bill_of_lading_number(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                client = get_carrier_definition(code).client_class()
                try:
                    client.fetch_tracking(bill_of_lading_number="BL-TEST")
                except NotImplementedError:
                    pass
                except TypeError as exc:
                    self.fail(f"{code}: fetch_tracking does not accept bill_of_lading_number — {exc}")

    def test_fetch_tracking_accepts_booking_number(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                client = get_carrier_definition(code).client_class()
                try:
                    client.fetch_tracking(booking_number="BKG-TEST")
                except NotImplementedError:
                    pass
                except TypeError as exc:
                    self.fail(f"{code}: fetch_tracking does not accept booking_number — {exc}")


class CarrierCapabilityConsistencyTest(unittest.TestCase):
    """Carrier capability flags must be consistent with registry metadata."""

    def test_dcsa_carriers_have_supports_dcsa_true(self):
        dcsa_carriers = ["maersk", "cma_cgm", "hapag_lloyd"]
        for code in dcsa_carriers:
            with self.subTest(carrier=code):
                defn = get_carrier_definition(code)
                self.assertTrue(
                    defn.capabilities.supports_dcsa,
                    f"{code} is a DCSA carrier but supports_dcsa is False in registry",
                )

    def test_non_dcsa_carriers_have_supports_dcsa_false(self):
        non_dcsa_carriers = ["msc", "cosco", "one", "evergreen", "hmm", "yang_ming", "zim"]
        for code in non_dcsa_carriers:
            with self.subTest(carrier=code):
                defn = get_carrier_definition(code)
                self.assertFalse(
                    defn.capabilities.supports_dcsa,
                    f"{code} is listed as non-DCSA but supports_dcsa is True in registry",
                )

    def test_all_carrier_definitions_have_capabilities(self):
        for defn in list_carriers():
            with self.subTest(carrier=defn.provider_code):
                from apps.scm.integrations.carriers.base import CarrierCapability

                self.assertIsInstance(
                    defn.capabilities,
                    CarrierCapability,
                    f"{defn.provider_code} capabilities must be a CarrierCapability instance",
                )

    def test_webhook_carriers_also_support_pull(self):
        """A carrier that supports webhooks must also support pull — webhooks complement, not replace, polling."""
        for defn in list_carriers():
            if defn.capabilities.supports_webhooks:
                with self.subTest(carrier=defn.provider_code):
                    self.assertTrue(
                        defn.capabilities.supports_pull,
                        f"{defn.provider_code} supports_webhooks but not supports_pull",
                    )
