"""Live API guard tests — no carrier client may make a real HTTP call from the suite.

Two groups, derived from the registry so this stays correct as carriers are built:

Stub carriers
    Have no implementation and must raise CarrierNotImplementedError (which is also a
    NotImplementedError). That keeps a stub from ever being mistaken for a carrier
    that answered with no data.

Implemented carriers
    Have a real transport, so the guard is different: without a configured
    Integration they must raise CarrierConfigurationError *before* touching the
    network. There is no hardcoded endpoint to fall back on.

The socket check below applies to both, and is the backstop for either contract
being violated.
"""

import contextlib
import unittest
from unittest.mock import patch

from apps.scm.integrations.carriers.exceptions import CarrierConfigurationError, CarrierError
from apps.scm.integrations.carriers.registry import get_carrier_client_class, get_carrier_parser_class

from .carrier_status import ALL_CARRIER_CODES, IMPLEMENTED_CARRIER_CODES, STUB_CARRIER_CODES


class StubCarriersRaiseNotImplementedTest(unittest.TestCase):
    """A carrier without an implementation must refuse, not return nothing."""

    def test_stub_client_fetch_tracking_raises_not_implemented(self):
        for code in STUB_CARRIER_CODES:
            with self.subTest(carrier=code):
                client = get_carrier_client_class(code)()
                with self.assertRaises(NotImplementedError):
                    client.fetch_tracking(container_number="TEST1234567")

    def test_stub_client_test_connection_raises_not_implemented(self):
        for code in STUB_CARRIER_CODES:
            with self.subTest(carrier=code), self.assertRaises(NotImplementedError):
                get_carrier_client_class(code)().test_connection()

    def test_stub_parser_raises_not_implemented(self):
        for code in STUB_CARRIER_CODES:
            with self.subTest(carrier=code), self.assertRaises(NotImplementedError):
                get_carrier_parser_class(code)().parse_tracking_events({"raw": "data"})

    def test_stub_client_raises_for_every_reference_type(self):
        for code in STUB_CARRIER_CODES:
            with self.subTest(carrier=code):
                client = get_carrier_client_class(code)()
                for kwargs in (
                    {"container_number": "TEST1234567"},
                    {"bill_of_lading_number": "BL123456"},
                    {"booking_number": "BKG123456"},
                ):
                    with self.assertRaises(NotImplementedError):
                        client.fetch_tracking(**kwargs)


class ImplementedCarriersRequireConfigurationTest(unittest.TestCase):
    """An implemented carrier must not reach the network without configuration."""

    def test_unconfigured_client_raises_configuration_error(self):
        for code in IMPLEMENTED_CARRIER_CODES:
            with self.subTest(carrier=code):
                client = get_carrier_client_class(code)()
                with self.assertRaises(CarrierConfigurationError):
                    client.fetch_tracking(container_number="TEST1234567")

    def test_unconfigured_test_connection_raises_configuration_error(self):
        for code in IMPLEMENTED_CARRIER_CODES:
            with self.subTest(carrier=code), self.assertRaises(CarrierConfigurationError):
                get_carrier_client_class(code)().test_connection()

    def test_implemented_parser_handles_an_empty_payload(self):
        """An implemented parser answers with an empty list rather than refusing."""
        for code in IMPLEMENTED_CARRIER_CODES:
            with self.subTest(carrier=code):
                self.assertEqual(get_carrier_parser_class(code)().parse_tracking_events({"events": []}), [])


class CarrierClientInstantiationIsSideEffectFreeTest(unittest.TestCase):
    """Instantiating a carrier client or parser must be safe."""

    def test_all_carrier_clients_can_be_instantiated_without_error(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                self.assertIsNotNone(get_carrier_client_class(code)())

    def test_all_carrier_parsers_can_be_instantiated_without_error(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                self.assertIsNotNone(get_carrier_parser_class(code)())


class NoSocketCallsMadeByCarrierClientsTest(unittest.TestCase):
    """No carrier client opens a socket during fetch_tracking.

    This is the backstop: even if a client stopped raising the expected error, it
    still must not reach the network from the test suite.
    """

    def test_no_carrier_opens_a_socket(self):
        import socket

        original_socket = socket.socket
        calls_made = []

        def tracking_socket(*args, **kwargs):
            calls_made.append(args)
            return original_socket(*args, **kwargs)

        with patch("socket.socket", side_effect=tracking_socket):
            for code in ALL_CARRIER_CODES:
                client = get_carrier_client_class(code)()
                # Stubs raise NotImplementedError, implemented clients raise a
                # configuration error — neither may touch the network.
                with contextlib.suppress(CarrierError, NotImplementedError):
                    client.fetch_tracking(container_number="SAFE1234567")

        self.assertEqual(
            calls_made,
            [],
            f"Socket was opened unexpectedly by a carrier client: {calls_made}",
        )
