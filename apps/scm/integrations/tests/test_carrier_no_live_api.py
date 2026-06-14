"""Live API guard tests — verify that no carrier client makes real HTTP calls.

All carrier clients are stubs that raise NotImplementedError. These tests document
and enforce that contract: calling any network method on a carrier client raises
NotImplementedError, making it impossible for tests to accidentally trigger live
API calls through the standard adapter interface.
"""

import contextlib
import unittest
from unittest.mock import patch

from apps.scm.integrations.carriers.registry import get_carrier_client_class, get_carrier_parser_class

# All carriers registered in the system.
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

# Focus carriers from the task specification.
FOCUS_CARRIER_CODES = ["maersk", "msc", "cma_cgm", "cosco", "hapag_lloyd", "one"]


class CarrierClientFetchTrackingRaisesNotImplementedTest(unittest.TestCase):
    """fetch_tracking() on every carrier client must raise NotImplementedError.

    This is the primary guard against accidental live API calls: if a carrier
    client is not yet implemented, it must refuse to make HTTP requests.
    """

    def test_carrier_client_fetch_tracking_raises_not_implemented(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                client_class = get_carrier_client_class(code)
                client = client_class()
                with self.assertRaises(
                    NotImplementedError, msg=f"{code} fetch_tracking should raise NotImplementedError"
                ):
                    client.fetch_tracking(container_number="TEST1234567")

    def test_carrier_client_test_connection_raises_not_implemented(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                client_class = get_carrier_client_class(code)
                client = client_class()
                with self.assertRaises(
                    NotImplementedError, msg=f"{code} test_connection should raise NotImplementedError"
                ):
                    client.test_connection()

    def test_carrier_parser_parse_tracking_events_raises_not_implemented(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                parser_class = get_carrier_parser_class(code)
                parser = parser_class()
                with self.assertRaises(
                    NotImplementedError, msg=f"{code} parse_tracking_events should raise NotImplementedError"
                ):
                    parser.parse_tracking_events({"raw": "data"})


class CarrierClientDoesNotImportHttpLibrariesAtInstantiationTest(unittest.TestCase):
    """Carrier clients must not trigger HTTP calls on instantiation.

    Instantiating a carrier client class should be safe and side-effect-free.
    """

    def test_all_carrier_clients_can_be_instantiated_without_error(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                client_class = get_carrier_client_class(code)
                # Must not raise on __init__
                client = client_class()
                self.assertIsNotNone(client)

    def test_all_carrier_parsers_can_be_instantiated_without_error(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                parser_class = get_carrier_parser_class(code)
                parser = parser_class()
                self.assertIsNotNone(parser)


class CarrierFetchTrackingAllReferenceTypesRaisesNotImplementedTest(unittest.TestCase):
    """fetch_tracking() raises NotImplementedError for all reference types, not just container."""

    def _assert_not_implemented_for_all_refs(self, code: str) -> None:
        client_class = get_carrier_client_class(code)
        client = client_class()

        with self.assertRaises(NotImplementedError):
            client.fetch_tracking(container_number="TEST1234567")
        with self.assertRaises(NotImplementedError):
            client.fetch_tracking(bill_of_lading_number="BL123456")
        with self.assertRaises(NotImplementedError):
            client.fetch_tracking(booking_number="BKG123456")

    def test_maersk_raises_for_all_reference_types(self):
        self._assert_not_implemented_for_all_refs("maersk")

    def test_msc_raises_for_all_reference_types(self):
        self._assert_not_implemented_for_all_refs("msc")

    def test_cma_cgm_raises_for_all_reference_types(self):
        self._assert_not_implemented_for_all_refs("cma_cgm")

    def test_hapag_lloyd_raises_for_all_reference_types(self):
        self._assert_not_implemented_for_all_refs("hapag_lloyd")

    def test_one_raises_for_all_reference_types(self):
        self._assert_not_implemented_for_all_refs("one")

    def test_cosco_raises_for_all_reference_types(self):
        self._assert_not_implemented_for_all_refs("cosco")


class NoSocketCallsMadeByCarrierClientsTest(unittest.TestCase):
    """Carrier clients must not open network sockets during fetch_tracking.

    This test uses socket-level patching as an extra safety net to confirm
    that even if a carrier accidentally bypasses NotImplementedError checking,
    no real network connections are attempted.
    """

    def test_carrier_tests_do_not_call_live_api(self):
        """Socket creation is never triggered by any focus carrier client."""
        import socket

        original_socket = socket.socket

        calls_made = []

        def tracking_socket(*args, **kwargs):
            calls_made.append(args)
            return original_socket(*args, **kwargs)

        with patch("socket.socket", side_effect=tracking_socket):
            for code in FOCUS_CARRIER_CODES:
                client_class = get_carrier_client_class(code)
                client = client_class()
                with contextlib.suppress(NotImplementedError):  # Expected — client is a stub
                    client.fetch_tracking(container_number="SAFE1234567")

        self.assertEqual(
            calls_made,
            [],
            f"Socket was opened unexpectedly by a carrier client: {calls_made}",
        )
