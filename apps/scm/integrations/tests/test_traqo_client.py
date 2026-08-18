"""Transport and error-classification tests for the Traqo client.

No test here touches the network: every request goes through an injected session, and
the one representative payload is the captured sandbox response in
``fixtures/traqo/sandbox_container_MRSU6859427.json``.
"""

import json
import pathlib

import requests
from django.test import SimpleTestCase, override_settings

from apps.scm.integrations.carriers.exceptions import (
    CarrierAuthenticationError,
    CarrierConfigurationError,
    CarrierInvalidResponseError,
    CarrierNoDataError,
    CarrierRateLimitError,
    CarrierServerError,
    CarrierTimeoutError,
    CarrierUnsupportedReferenceError,
)
from apps.scm.integrations.carriers.http import HttpConfig
from apps.scm.integrations.traqo.client import PRODUCTION_BASE_URL, TraqoClient
from apps.scm.integrations.traqo.errors import (
    TraqoDeveloperModeDisabledError,
    TraqoPaymentOverdueError,
    TraqoShipmentLimitReachedError,
)
from apps.scm.integrations.traqo.sealines import (
    CARRIER_CODE_TO_SEALINE,
    carrier_code_for_sealine,
    resolve_sealine,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "traqo"
CONTAINER_NUMBER = "MRSU6859427"
API_KEY = "traqo-secret-key-do-not-log"

# No sleeping and no retrying in tests: the shared transport's retry behaviour is
# covered by the carrier suite, and here it would only slow the classification tests.
NO_RETRIES = HttpConfig(max_retries=0, retry_backoff_seconds=0)


def sandbox_payload() -> dict:
    return json.loads((FIXTURES / "sandbox_container_MRSU6859427.json").read_text())


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, invalid_json=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self._invalid_json = invalid_json

    def json(self):
        if self._invalid_json:
            raise ValueError("not JSON")
        return self._payload


class FakeSession:
    """Records the requests made and replays queued responses."""

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"url": url, "headers": headers or {}, "params": params or {}, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.responses.pop(0) if self.responses else FakeResponse(200, sandbox_payload())

    @property
    def last(self) -> dict:
        return self.requests[-1]


def _error_response(status_code: int, message: str, headers=None, **extra) -> FakeResponse:
    """Traqo's error envelope, as observed live."""
    body = {"success": False, "statusCode": status_code, "message": message, **extra}
    return FakeResponse(status_code, body, headers=headers)


class TraqoUrlAndAuthTest(SimpleTestCase):
    """The sandbox and production differ by one path segment and one header."""

    def test_sandbox_container_url(self):
        client = TraqoClient(sandbox=True)
        self.assertEqual(
            client.container_url(CONTAINER_NUMBER),
            f"{PRODUCTION_BASE_URL}/sandbox/container/{CONTAINER_NUMBER}",
        )

    def test_production_container_url(self):
        client = TraqoClient(api_key=API_KEY)
        self.assertEqual(
            client.container_url(CONTAINER_NUMBER),
            f"{PRODUCTION_BASE_URL}/container/{CONTAINER_NUMBER}",
        )

    def test_base_url_override_is_used_without_a_trailing_slash(self):
        client = TraqoClient(base_url="https://example.invalid/api/v1/", sandbox=True)
        self.assertEqual(
            client.container_url(CONTAINER_NUMBER),
            f"https://example.invalid/api/v1/sandbox/container/{CONTAINER_NUMBER}",
        )

    def test_mandatory_sealine_is_sent_as_a_query_parameter(self):
        session = FakeSession()
        client = TraqoClient(sandbox=True, session=session, http_config=NO_RETRIES)

        client.get_container(CONTAINER_NUMBER, "MAEU")

        self.assertEqual(session.last["params"], {"sealine": "MAEU"})

    def test_a_carrier_code_is_translated_into_its_scac(self):
        session = FakeSession()
        client = TraqoClient(sandbox=True, session=session, http_config=NO_RETRIES)

        client.get_container(CONTAINER_NUMBER, "maersk")

        self.assertEqual(session.last["params"], {"sealine": "MAEU"})

    def test_production_sends_a_bearer_token(self):
        session = FakeSession()
        client = TraqoClient(api_key=API_KEY, session=session, http_config=NO_RETRIES)

        client.get_container(CONTAINER_NUMBER, "MAEU")

        self.assertEqual(session.last["headers"]["Authorization"], f"Bearer {API_KEY}")

    def test_the_sandbox_sends_no_credential(self):
        session = FakeSession()
        client = TraqoClient(api_key=API_KEY, sandbox=True, session=session, http_config=NO_RETRIES)

        client.get_container(CONTAINER_NUMBER, "MAEU")

        self.assertNotIn("Authorization", session.last["headers"])

    def test_production_without_a_key_is_a_configuration_error(self):
        client = TraqoClient(api_key="", session=FakeSession(), http_config=NO_RETRIES)

        with self.assertRaises(CarrierConfigurationError):
            client.get_container(CONTAINER_NUMBER, "MAEU")

    def test_a_reference_that_is_not_a_container_number_is_refused(self):
        session = FakeSession()
        client = TraqoClient(sandbox=True, session=session, http_config=NO_RETRIES)

        with self.assertRaises(CarrierUnsupportedReferenceError):
            client.get_container("NOT-A-BOX", "MAEU")

        self.assertEqual(session.requests, [])

    @override_settings(TRAQO_ENABLED=False, TRAQO_API_KEY=API_KEY, TRAQO_BASE_URL=PRODUCTION_BASE_URL)
    def test_from_settings_refuses_live_calls_while_disabled(self):
        with self.assertRaises(CarrierConfigurationError):
            TraqoClient.from_settings(sandbox=False)

    @override_settings(TRAQO_ENABLED=False, TRAQO_API_KEY="", TRAQO_BASE_URL=PRODUCTION_BASE_URL)
    def test_from_settings_allows_the_sandbox_while_disabled(self):
        client = TraqoClient.from_settings(sandbox=True)

        self.assertTrue(client.sandbox)


class TraqoCredentialSafetyTest(SimpleTestCase):
    """The API key must not reach a URL, a log line or an error message."""

    def test_the_key_is_never_in_the_url_or_the_query(self):
        session = FakeSession()
        client = TraqoClient(api_key=API_KEY, session=session, http_config=NO_RETRIES)

        client.get_container(CONTAINER_NUMBER, "MAEU")

        self.assertNotIn(API_KEY, session.last["url"])
        self.assertNotIn(API_KEY, json.dumps(session.last["params"]))

    def test_the_key_is_not_in_an_error_message(self):
        session = FakeSession([_error_response(401, "Invalid or missing API key.")])
        client = TraqoClient(api_key=API_KEY, session=session, http_config=NO_RETRIES)

        with self.assertRaises(CarrierAuthenticationError) as ctx:
            client.get_container(CONTAINER_NUMBER, "MAEU")

        self.assertNotIn(API_KEY, str(ctx.exception))

    def test_the_key_is_not_in_the_logged_request_path(self):
        session = FakeSession()
        client = TraqoClient(api_key=API_KEY, session=session, http_config=NO_RETRIES)

        with self.assertLogs("apps.scm.integrations", level="INFO") as logs:
            client.get_container(CONTAINER_NUMBER, "MAEU")

        self.assertNotIn(API_KEY, "\n".join(logs.output))


class TraqoTransportFailureTest(SimpleTestCase):
    """Network-level failures classify through the shared transport."""

    def test_timeout(self):
        client = TraqoClient(
            sandbox=True,
            session=FakeSession(error=requests.Timeout("slow")),
            http_config=NO_RETRIES,
        )

        with self.assertRaises(CarrierTimeoutError):
            client.get_container(CONTAINER_NUMBER, "MAEU")

    def test_connection_error(self):
        client = TraqoClient(
            sandbox=True,
            session=FakeSession(error=requests.ConnectionError("no route")),
            http_config=NO_RETRIES,
        )

        with self.assertRaises(CarrierTimeoutError):
            client.get_container(CONTAINER_NUMBER, "MAEU")

    def test_malformed_json(self):
        client = TraqoClient(
            sandbox=True,
            session=FakeSession([FakeResponse(200, invalid_json=True)]),
            http_config=NO_RETRIES,
        )

        with self.assertRaises(CarrierInvalidResponseError):
            client.get_container(CONTAINER_NUMBER, "MAEU")

    def test_a_200_that_is_not_an_object(self):
        client = TraqoClient(
            sandbox=True,
            session=FakeSession([FakeResponse(200, payload=[1, 2, 3])]),
            http_config=NO_RETRIES,
        )

        with self.assertRaises(CarrierInvalidResponseError):
            client.get_container(CONTAINER_NUMBER, "MAEU")

    def test_a_200_without_a_data_object(self):
        client = TraqoClient(
            sandbox=True,
            session=FakeSession([FakeResponse(200, payload={"success": True})]),
            http_config=NO_RETRIES,
        )

        with self.assertRaises(CarrierInvalidResponseError):
            client.get_container(CONTAINER_NUMBER, "MAEU")

    def test_a_200_reporting_failure(self):
        client = TraqoClient(
            sandbox=True,
            session=FakeSession([FakeResponse(200, payload={"success": False, "message": "nope"})]),
            http_config=NO_RETRIES,
        )

        with self.assertRaises(CarrierInvalidResponseError):
            client.get_container(CONTAINER_NUMBER, "MAEU")


class TraqoStatusClassificationTest(SimpleTestCase):
    """Each documented Traqo status becomes a distinct, typed outcome."""

    def _fails_with(self, response, expected):
        client = TraqoClient(api_key=API_KEY, session=FakeSession([response]), http_config=NO_RETRIES)
        with self.assertRaises(expected) as ctx:
            client.get_container(CONTAINER_NUMBER, "MAEU")
        return ctx.exception

    def test_400_invalid_parameters(self):
        error = self._fails_with(
            _error_response(400, "sealine is required. Provide a 4-character SCAC code (e.g. MAEU)."),
            CarrierInvalidResponseError,
        )
        self.assertEqual(error.status_code, 400)
        self.assertIn("sealine is required", str(error))
        self.assertFalse(error.transient)

    def test_401_invalid_key(self):
        error = self._fails_with(
            _error_response(401, "Invalid or missing API key."),
            CarrierAuthenticationError,
        )
        self.assertFalse(error.transient)

    def test_402_shipment_limit_reached_is_transient(self):
        error = self._fails_with(
            _error_response(402, "Shipment limit reached.", reason="shipment_limit_reached"),
            TraqoShipmentLimitReachedError,
        )
        # A quota, not a dead account: transient, so tracking is not written off.
        self.assertTrue(error.transient)
        self.assertIsInstance(error, CarrierRateLimitError)

    def test_402_shipment_limit_reached_from_the_message_alone(self):
        error = self._fails_with(
            _error_response(402, "Your plan's shipment_limit_reached — close a shipment to add another."),
            TraqoShipmentLimitReachedError,
        )
        self.assertTrue(error.transient)

    def test_402_shipment_limit_honours_retry_after(self):
        error = self._fails_with(
            _error_response(
                402,
                "Shipment limit reached.",
                headers={"Retry-After": "600"},
                reason="shipment_limit_reached",
            ),
            TraqoShipmentLimitReachedError,
        )
        self.assertEqual(error.retry_after, 600)

    def test_402_payment_overdue_needs_a_human(self):
        error = self._fails_with(
            _error_response(402, "Payment overdue.", reason="payment_overdue"),
            TraqoPaymentOverdueError,
        )
        self.assertFalse(error.transient)
        self.assertIsInstance(error, CarrierConfigurationError)

    def test_402_without_a_reason_is_treated_as_the_case_a_retry_cannot_fix(self):
        error = self._fails_with(
            _error_response(402, "Payment required."),
            TraqoPaymentOverdueError,
        )
        self.assertIn("did not state", str(error))

    def test_403_developer_mode_disabled(self):
        error = self._fails_with(
            _error_response(403, "Developer mode is disabled for this account."),
            TraqoDeveloperModeDisabledError,
        )
        self.assertFalse(error.transient)
        self.assertIsInstance(error, CarrierConfigurationError)

    def test_404_is_no_data_rather_than_a_failure(self):
        self._fails_with(_error_response(404, "Shipment not found."), CarrierNoDataError)

    def test_429_carries_retry_after(self):
        error = self._fails_with(
            _error_response(429, "Too many requests.", headers={"Retry-After": "120"}),
            CarrierRateLimitError,
        )
        self.assertEqual(error.retry_after, 120)
        self.assertTrue(error.transient)

    def test_502_upstream_carrier_failure_is_transient(self):
        error = self._fails_with(
            _error_response(502, "Upstream carrier unavailable."),
            CarrierServerError,
        )
        self.assertTrue(error.transient)
        self.assertEqual(error.status_code, 502)

    def test_a_non_json_error_body_still_classifies_by_status(self):
        client = TraqoClient(
            api_key=API_KEY,
            session=FakeSession([FakeResponse(403, invalid_json=True)]),
            http_config=NO_RETRIES,
        )

        with self.assertRaises(TraqoDeveloperModeDisabledError):
            client.get_container(CONTAINER_NUMBER, "MAEU")


class TraqoSealineTest(SimpleTestCase):
    """Sealines come from Traqo's published carrier list, never from a guess."""

    def test_registry_carrier_codes_map_to_the_scacs_traqo_publishes(self):
        self.assertEqual(resolve_sealine("maersk"), "MAEU")
        self.assertEqual(resolve_sealine("cma_cgm"), "CMDU")
        self.assertEqual(resolve_sealine("Hapag-Lloyd"), "HLCU")
        self.assertEqual(resolve_sealine("msc"), "MSCU")
        self.assertEqual(resolve_sealine("cosco"), "COSU")
        self.assertEqual(resolve_sealine("one"), "ONEY")
        self.assertEqual(resolve_sealine("hmm"), "HDMU")
        self.assertEqual(resolve_sealine("zim"), "ZIMU")
        self.assertEqual(resolve_sealine("yang_ming"), "YMLU")

    def test_a_scac_passes_through(self):
        self.assertEqual(resolve_sealine("maeu"), "MAEU")
        self.assertEqual(resolve_sealine(" OOLU "), "OOLU")

    def test_a_carrier_traqo_does_not_publish_is_refused_rather_than_guessed(self):
        # Evergreen is absent from Traqo's carrier list; inventing EGLV here would
        # produce calls that fail for a reason nobody could trace.
        self.assertNotIn("evergreen", CARRIER_CODE_TO_SEALINE)
        with self.assertRaises(CarrierConfigurationError):
            resolve_sealine("evergreen")

    def test_an_empty_or_unrecognised_value_is_refused(self):
        for value in ("", "   ", "not a carrier", "MAE", "MAEU1"):
            with self.assertRaises(CarrierConfigurationError):
                resolve_sealine(value)

    def test_the_inverse_lookup_names_the_carrier_container_scm_knows(self):
        self.assertEqual(carrier_code_for_sealine("MAEU"), "maersk")
        self.assertEqual(carrier_code_for_sealine("CMDU"), "cma_cgm")
        # A SCAC Traqo supports and Container SCM has no adapter for.
        self.assertIsNone(carrier_code_for_sealine("OOLU"))
