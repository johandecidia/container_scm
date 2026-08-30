"""Tests for the CMA CGM Track & Trace client.

Every test drives the client with an injected fake session — no live call is made, and
the socket guard in test_carrier_no_live_api.py enforces that separately.

The API key values here are obvious dummies. Nothing in this file, or in the shipped
configuration it asserts against, is a real credential or a real customer reference.
"""

import json
import pathlib

import requests
from django.test import TestCase

from apps.scm.integrations.carriers.base import CarrierCapability
from apps.scm.integrations.carriers.cma_cgm.client import (
    CARRIER_NAME,
    PROVIDER_CODE,
    PUBLIC_TRACK_AND_TRACE_CONFIG,
    CmaCgmClient,
    resolve_config,
)
from apps.scm.integrations.carriers.cma_cgm.parser import CmaCgmParser
from apps.scm.integrations.carriers.dcsa.carrier_parser import DcsaCarrierParser
from apps.scm.integrations.carriers.dcsa.client import DcsaCarrierClient
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
from apps.scm.integrations.carriers.factory import build_carrier_client, build_carrier_parser
from apps.scm.integrations.carriers.registry import get_carrier_definition
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential, IntegrationRequestLog
from apps.teams.models import Team

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "carriers"

API_KEY = "test-cma-api-key"
CONTAINER_NUMBER = "CMAU1234564"
BOOKING_NUMBER = "CMA-BKG-987654"
BL_NUMBER = "CMA-BL-987654"

# A test-only endpoint. The shipped production values are asserted separately by
# CmaCgmPublicEndpointTest.
TEST_CONFIG = {
    **PUBLIC_TRACK_AND_TRACE_CONFIG,
    "base_url": "https://example.invalid/cma",
    "test_connection_reference": CONTAINER_NUMBER,
    "max_retries": 1,
    "retry_backoff_seconds": 0,
}


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _integration(team: Team, config: dict) -> Integration:
    return Integration.objects.create(
        team=team,
        name=CARRIER_NAME,
        provider_code=PROVIDER_CODE,
        provider_family=Integration.ProviderFamily.CARRIER,
        api_style=Integration.ApiStyle.DCSA,
        config=config,
        is_active=True,
    )


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, invalid_json=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self._invalid_json = invalid_json

    def json(self):
        if self._invalid_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Records requests and replays canned responses or errors."""

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.requests: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"url": url, "headers": headers or {}, "params": params or {}, "timeout": timeout})
        if self.error is not None:
            raise self.error
        if not self.responses:
            return FakeResponse(200, [])
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client(team, config=None, *, session=None, credentials=None) -> CmaCgmClient:
    integration = _integration(team, config or TEST_CONFIG)
    if credentials is not None:
        set_integration_credentials(integration, IntegrationCredential.AuthType.API_KEY, credentials)
    return CmaCgmClient(integration, session=session)


def _events_fixture() -> list:
    return json.loads((FIXTURES / "cma_cgm_events_response.json").read_text())


# ---------------------------------------------------------------------------
# Architecture: CMA CGM adds configuration and identity, not a second stack
# ---------------------------------------------------------------------------


class CmaCgmArchitectureTest(TestCase):
    """The carrier-specific layer must stay thin and reuse the shared DCSA pipeline."""

    def test_client_is_a_dcsa_carrier_client(self):
        self.assertTrue(issubclass(CmaCgmClient, DcsaCarrierClient))

    def test_parser_is_a_dcsa_carrier_parser(self):
        self.assertTrue(issubclass(CmaCgmParser, DcsaCarrierParser))

    def test_client_does_not_reimplement_the_dcsa_transport(self):
        """fetch_tracking and test_connection come from the shared client, not from CMA CGM."""
        for method in ("fetch_tracking", "test_connection", "discover_containers"):
            with self.subTest(method=method):
                self.assertNotIn(method, CmaCgmClient.__dict__)

    def test_parser_does_not_reimplement_dcsa_parsing(self):
        self.assertNotIn("parse_tracking_events", CmaCgmParser.__dict__)

    def test_registry_resolves_the_cma_adapter(self):
        definition = get_carrier_definition(PROVIDER_CODE)
        self.assertIs(definition.client_class, CmaCgmClient)
        self.assertIs(definition.parser_class, CmaCgmParser)

    def test_registry_and_client_agree_on_the_account_number(self):
        definition = get_carrier_definition(PROVIDER_CODE)
        self.assertFalse(definition.capabilities.requires_account_number)
        self.assertEqual(
            definition.capabilities.requires_account_number,
            CmaCgmClient.capabilities.requires_account_number,
        )

    def test_capabilities_describe_what_is_implemented_for_tracking(self):
        capabilities = CmaCgmClient.capabilities
        self.assertIsInstance(capabilities, CarrierCapability)
        self.assertTrue(capabilities.supports_pull)
        self.assertTrue(capabilities.supports_dcsa)
        self.assertTrue(capabilities.supports_tracking_by_container)
        self.assertTrue(capabilities.supports_tracking_by_bl)
        self.assertTrue(capabilities.supports_tracking_by_booking)


# ---------------------------------------------------------------------------
# The shipped public Track & Trace configuration
# ---------------------------------------------------------------------------


class CmaCgmPublicEndpointTest(TestCase):
    """The shipped config produces exactly the request the CMA CGM Swagger specifies.

    These assertions pin the documented contract:
    ``GET https://apis.cma-cgm.net/operation/trackandtrace/v1/events?equipmentReference=…``
    with a ``keyId`` header. Changing any of them changes what we send CMA CGM.
    """

    def setUp(self):
        self.team = _team("cma-public-team")

    def _client(self, session):
        return _client(
            self.team,
            dict(PUBLIC_TRACK_AND_TRACE_CONFIG),
            session=session,
            credentials={"api_key": API_KEY},
        )

    def test_base_url_is_the_cma_api_host(self):
        self.assertEqual(PUBLIC_TRACK_AND_TRACE_CONFIG["base_url"], "https://apis.cma-cgm.net")

    def test_tracking_path_is_the_dcsa_events_endpoint(self):
        self.assertEqual(
            PUBLIC_TRACK_AND_TRACE_CONFIG["tracking_path"],
            "/operation/trackandtrace/v1/events",
        )

    def test_auth_style_is_an_api_key_header(self):
        self.assertEqual(PUBLIC_TRACK_AND_TRACE_CONFIG["auth_style"], "api_key_header")

    def test_api_key_header_name_is_exactly_key_id(self):
        """The Swagger's ApiKeyAuth scheme names the header ``keyId`` — case included."""
        self.assertEqual(PUBLIC_TRACK_AND_TRACE_CONFIG["api_key_header_name"], "keyId")

    def test_config_builds_the_public_events_url(self):
        config = resolve_config(PUBLIC_TRACK_AND_TRACE_CONFIG)
        self.assertEqual(
            config.tracking_url,
            "https://apis.cma-cgm.net/operation/trackandtrace/v1/events",
        )

    def test_the_request_goes_to_the_configured_url(self):
        session = FakeSession([FakeResponse(200, [])])
        self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(
            session.requests[0]["url"],
            "https://apis.cma-cgm.net/operation/trackandtrace/v1/events",
        )

    def test_configured_timeout_is_thirty_seconds(self):
        session = FakeSession([FakeResponse(200, [])])
        self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(session.requests[0]["timeout"], 30)

    def test_the_config_carries_no_credential(self):
        """The endpoint settings ship in code, so they must hold no credential value."""
        for forbidden in ("api_key", "client_id", "client_secret", "password", "token", "secret"):
            self.assertNotIn(forbidden, PUBLIC_TRACK_AND_TRACE_CONFIG)
        # extra_headers is sent verbatim on every request and must stay non-secret.
        self.assertEqual(set(PUBLIC_TRACK_AND_TRACE_CONFIG["extra_headers"]), {"Accept"})

    def test_the_config_carries_no_customer_reference(self):
        """A reference known to an account belongs to that account, not to this repo."""
        self.assertNotIn("test_connection_reference", PUBLIC_TRACK_AND_TRACE_CONFIG)

    def test_the_key_comes_from_the_credential_service_not_the_config(self):
        client = _client(self.team, dict(PUBLIC_TRACK_AND_TRACE_CONFIG), session=FakeSession())
        with self.assertRaises(CarrierConfigurationError):
            client.fetch_tracking(container_number=CONTAINER_NUMBER)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class CmaCgmConfigurationTest(TestCase):
    """Live access requires a verified configuration; nothing is guessed."""

    def setUp(self):
        self.team = _team("cma-config-team")

    def test_unconfigured_client_refuses_to_call(self):
        with self.assertRaises(CarrierConfigurationError):
            CmaCgmClient().fetch_tracking(container_number=CONTAINER_NUMBER)

    def test_empty_config_lists_every_missing_key(self):
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config({})
        message = str(ctx.exception)
        for key in ("base_url", "tracking_path", "auth_style", "reference_params"):
            self.assertIn(key, message)

    def test_the_carrier_is_named_in_the_configuration_error(self):
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config({})
        self.assertIn(CARRIER_NAME, str(ctx.exception))

    def test_api_key_style_requires_the_header_name(self):
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config({**TEST_CONFIG, "api_key_header_name": ""})
        self.assertIn("api_key_header_name", str(ctx.exception))

    def test_unknown_reference_kind_is_rejected(self):
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config({**TEST_CONFIG, "reference_params": {"vessel_name": "vessel"}})
        self.assertIn("vessel_name", str(ctx.exception))

    def test_reference_without_a_configured_param_is_refused(self):
        config = {**TEST_CONFIG, "reference_params": {"container_number": "equipmentReference"}}
        client = _client(self.team, config, session=FakeSession(), credentials={"api_key": API_KEY})
        with self.assertRaises(CarrierConfigurationError):
            client.fetch_tracking(booking_number=BOOKING_NUMBER)

    def test_connection_test_requires_a_reference(self):
        """The shipped config has none, so an unconfigured check says so explicitly."""
        client = _client(
            self.team,
            dict(PUBLIC_TRACK_AND_TRACE_CONFIG),
            session=FakeSession(),
            credentials={"api_key": API_KEY},
        )
        with self.assertRaises(CarrierConfigurationError) as ctx:
            client.test_connection()
        self.assertIn("test_connection_reference", str(ctx.exception))

    def test_connection_test_succeeds_once_a_reference_is_configured(self):
        session = FakeSession([FakeResponse(200, [])])
        client = _client(self.team, session=session, credentials={"api_key": API_KEY})
        result = client.test_connection()
        self.assertTrue(result["success"])
        self.assertEqual(session.requests[0]["params"]["equipmentReference"], CONTAINER_NUMBER)

    def test_connection_test_succeeds_on_no_data(self):
        session = FakeSession([FakeResponse(404)])
        client = _client(self.team, session=session, credentials={"api_key": API_KEY})
        self.assertTrue(client.test_connection()["success"])


# ---------------------------------------------------------------------------
# Request construction and authentication
# ---------------------------------------------------------------------------


class CmaCgmRequestTest(TestCase):
    def setUp(self):
        self.team = _team("cma-request-team")

    def _client(self, session, config=None):
        return _client(self.team, config, session=session, credentials={"api_key": API_KEY})

    def test_container_number_is_sent_as_equipment_reference(self):
        session = FakeSession([FakeResponse(200, [])])
        self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(session.requests[0]["params"]["equipmentReference"], CONTAINER_NUMBER)

    def test_booking_number_is_sent_as_carrier_booking_reference(self):
        session = FakeSession([FakeResponse(200, [])])
        self._client(session).fetch_tracking(booking_number=BOOKING_NUMBER)
        self.assertEqual(session.requests[0]["params"]["carrierBookingReference"], BOOKING_NUMBER)

    def test_bill_of_lading_is_sent_as_transport_document_reference(self):
        session = FakeSession([FakeResponse(200, [])])
        self._client(session).fetch_tracking(bill_of_lading_number=BL_NUMBER)
        self.assertEqual(session.requests[0]["params"]["transportDocumentReference"], BL_NUMBER)

    def test_the_configured_page_size_is_sent_as_limit(self):
        session = FakeSession([FakeResponse(200, [])])
        self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(session.requests[0]["params"]["limit"], 100)

    def test_no_cursor_is_sent_on_the_first_page(self):
        session = FakeSession([FakeResponse(200, [])])
        self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertNotIn("cursor", session.requests[0]["params"])

    def test_api_key_is_sent_in_the_key_id_header(self):
        session = FakeSession([FakeResponse(200, [])])
        self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(session.requests[0]["headers"]["keyId"], API_KEY)

    def test_no_other_authentication_header_is_sent(self):
        """Only what the Swagger specifies: no Authorization, no x-api-key spellings."""
        session = FakeSession([FakeResponse(200, [])])
        self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        headers = session.requests[0]["headers"]
        for absent in ("Authorization", "KeyId", "apikey", "x-api-key", "consumer-key"):
            self.assertNotIn(absent, headers)

    def test_accept_json_header_is_sent(self):
        session = FakeSession([FakeResponse(200, [])])
        self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(session.requests[0]["headers"]["Accept"], "application/json")

    def test_missing_api_key_credential_is_a_configuration_error(self):
        client = _client(self.team, session=FakeSession())
        with self.assertRaises(CarrierConfigurationError):
            client.fetch_tracking(container_number=CONTAINER_NUMBER)

    def test_two_references_are_rejected_before_any_call(self):
        """The shared contract is one reference per call; CMA CGM does not bend it.

        Combining equipmentReference with carrierBookingReference would narrow a
        container to one commercial cycle, but that is a change to the shared carrier
        contract, not something to work around here. See CmaCgmClient's docstring.
        """
        session = FakeSession()
        with self.assertRaises(CarrierUnsupportedReferenceError):
            self._client(session).fetch_tracking(
                container_number=CONTAINER_NUMBER,
                booking_number=BOOKING_NUMBER,
            )
        self.assertEqual(session.requests, [])

    def test_no_reference_is_rejected_before_any_call(self):
        session = FakeSession()
        with self.assertRaises(CarrierUnsupportedReferenceError):
            self._client(session).fetch_tracking()
        self.assertEqual(session.requests, [])


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class CmaCgmPaginationTest(TestCase):
    """CMA CGM pages /events with a cursor advertised in the Next-Page header."""

    def setUp(self):
        self.team = _team("cma-pagination-team")

    def _client(self, session, config=None):
        return _client(self.team, config, session=session, credentials={"api_key": API_KEY})

    def _event(self, event_id: str) -> dict:
        return {
            "eventID": event_id,
            "eventType": "EQUIPMENT",
            "eventClassifierCode": "ACT",
            "equipmentEventTypeCode": "LOAD",
            "eventDateTime": "2026-03-11T10:00:00Z",
            "equipmentReference": CONTAINER_NUMBER,
        }

    def test_the_next_page_cursor_is_followed(self):
        session = FakeSession(
            [
                FakeResponse(200, [self._event("E1")], headers={"Next-Page": "cursor123"}),
                FakeResponse(200, [self._event("E2")]),
            ]
        )
        self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)

        self.assertEqual(len(session.requests), 2)
        self.assertNotIn("cursor", session.requests[0]["params"])
        self.assertEqual(session.requests[0]["params"]["equipmentReference"], CONTAINER_NUMBER)
        self.assertEqual(session.requests[1]["params"]["cursor"], "cursor123")
        self.assertEqual(session.requests[1]["params"]["equipmentReference"], CONTAINER_NUMBER)

    def test_events_from_every_page_are_returned_once(self):
        session = FakeSession(
            [
                FakeResponse(200, [self._event("E1")], headers={"Next-Page": "cursor123"}),
                FakeResponse(200, [self._event("E2")], headers={"Next-Page": "cursor456"}),
                FakeResponse(200, [self._event("E3")]),
            ]
        )
        payload = self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual([event["eventID"] for event in payload["events"]], ["E1", "E2", "E3"])

    def test_paged_events_reach_the_parser(self):
        session = FakeSession(
            [
                FakeResponse(200, [self._event("E1")], headers={"Next-Page": "cursor123"}),
                FakeResponse(200, [self._event("E2")]),
            ]
        )
        payload = self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        events = CmaCgmParser().parse_tracking_events(payload)
        self.assertEqual([event.raw_event_id for event in events], ["E1", "E2"])

    def test_a_single_page_response_makes_one_request(self):
        session = FakeSession([FakeResponse(200, [self._event("E1")])])
        self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(len(session.requests), 1)

    def test_the_header_name_is_matched_case_insensitively(self):
        """requests normalises header case; a carrier may not send it as documented."""
        session = FakeSession(
            [
                FakeResponse(200, [self._event("E1")], headers={"next-page": "cursor123"}),
                FakeResponse(200, [self._event("E2")]),
            ]
        )
        self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(len(session.requests), 2)

    def test_an_empty_next_page_header_ends_pagination(self):
        session = FakeSession([FakeResponse(200, [self._event("E1")], headers={"Next-Page": "  "})])
        self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(len(session.requests), 1)

    def test_the_max_page_limit_stops_an_endless_cursor(self):
        """A carrier that always advertises a next page must not loop forever."""
        config = {**TEST_CONFIG, "pagination": {**TEST_CONFIG["pagination"], "max_pages": 3}}
        session = FakeSession(
            [FakeResponse(200, [self._event(f"E{index}")], headers={"Next-Page": f"c{index}"}) for index in range(10)]
        )
        payload = self._client(session, config).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(len(session.requests), 3)
        self.assertEqual(len(payload["events"]), 3)

    def test_the_default_max_page_limit_is_twenty(self):
        session = FakeSession(
            [FakeResponse(200, [self._event(f"E{index}")], headers={"Next-Page": f"c{index}"}) for index in range(30)]
        )
        self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(len(session.requests), 20)

    def test_a_repeated_cursor_stops_pagination(self):
        session = FakeSession(
            [
                FakeResponse(200, [self._event("E1")], headers={"Next-Page": "same"}),
                FakeResponse(200, [self._event("E2")], headers={"Next-Page": "same"}),
            ]
        )
        payload = self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(len(session.requests), 2)
        self.assertEqual([event["eventID"] for event in payload["events"]], ["E1", "E2"])

    def test_an_error_on_a_later_page_is_not_a_partial_success(self):
        """A failed second page must raise, not quietly return page one."""
        session = FakeSession(
            [
                FakeResponse(200, [self._event("E1")], headers={"Next-Page": "cursor123"}),
                FakeResponse(500),
                FakeResponse(500),
            ]
        )
        with self.assertRaises(CarrierServerError):
            self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)

    def test_half_configured_pagination_is_refused(self):
        """A cursor parameter with no header to read is a silently truncated history."""
        config = {**TEST_CONFIG, "pagination": {"cursor_param": "cursor"}}
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config(config)
        self.assertIn("next_page_header", str(ctx.exception))

    def test_events_wrapped_in_an_object_are_merged_across_pages(self):
        session = FakeSession(
            [
                FakeResponse(200, {"events": [self._event("E1")]}, headers={"Next-Page": "cursor123"}),
                FakeResponse(200, {"events": [self._event("E2")]}),
            ]
        )
        payload = self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual([event["eventID"] for event in payload["events"]], ["E1", "E2"])

    def test_a_tracking_data_envelope_is_merged_not_silently_dropped(self):
        """The parser accepts ``trackingData`` too, so merging must not ignore it."""
        session = FakeSession(
            [
                FakeResponse(200, {"trackingData": [self._event("E1")]}, headers={"Next-Page": "cursor123"}),
                FakeResponse(200, {"trackingData": [self._event("E2")]}),
            ]
        )
        payload = self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual([event["eventID"] for event in payload["events"]], ["E1", "E2"])
        self.assertNotIn("trackingData", payload, "One event list, under one key")
        self.assertEqual(len(CmaCgmParser().parse_tracking_events(payload)), 2)

    def test_pagination_is_off_when_it_is_not_configured(self):
        """Maersk and every other DCSA carrier keep making exactly one request."""
        config = {**TEST_CONFIG}
        config.pop("pagination")
        session = FakeSession([FakeResponse(200, [self._event("E1")], headers={"Next-Page": "cursor123"})])
        self._client(session, config).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(len(session.requests), 1)
        self.assertNotIn("limit", session.requests[0]["params"])


# ---------------------------------------------------------------------------
# Error semantics — an empty result is not a failure, and a failure is not empty
# ---------------------------------------------------------------------------


class CmaCgmErrorSemanticsTest(TestCase):
    def setUp(self):
        self.team = _team("cma-errors-team")

    def _client(self, session, config=None):
        return _client(self.team, config, session=session, credentials={"api_key": API_KEY})

    def test_an_empty_array_is_a_successful_call_with_no_events(self):
        session = FakeSession([FakeResponse(200, [])])
        payload = self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(payload, {"events": []})
        self.assertEqual(CmaCgmParser().parse_tracking_events(payload), [])

    def test_an_empty_events_object_is_a_successful_call_with_no_events(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        payload = self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(CmaCgmParser().parse_tracking_events(payload), [])

    def test_404_is_no_data_not_an_error(self):
        session = FakeSession([FakeResponse(404)])
        with self.assertRaises(CarrierNoDataError):
            self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)

    def test_401_is_an_authentication_error(self):
        session = FakeSession([FakeResponse(401), FakeResponse(401)])
        with self.assertRaises(CarrierAuthenticationError):
            self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)

    def test_403_is_an_authentication_error(self):
        session = FakeSession([FakeResponse(403)])
        with self.assertRaises(CarrierAuthenticationError):
            self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)

    def test_an_authentication_failure_is_permanent_not_transient(self):
        session = FakeSession([FakeResponse(403)])
        with self.assertRaises(CarrierAuthenticationError) as ctx:
            self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertFalse(ctx.exception.transient)

    def test_429_retries_then_reports_the_carriers_retry_after(self):
        session = FakeSession(
            [
                FakeResponse(429, headers={"Retry-After": "0"}),
                FakeResponse(429, headers={"Retry-After": "5"}),
            ]
        )
        with self.assertRaises(CarrierRateLimitError) as ctx:
            self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(ctx.exception.retry_after, 5)
        self.assertEqual(len(session.requests), 2)
        self.assertTrue(ctx.exception.transient)

    def test_429_that_clears_on_retry_succeeds(self):
        session = FakeSession([FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, [])])
        payload = self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(payload, {"events": []})

    def test_500_retries_then_raises_a_server_error(self):
        session = FakeSession([FakeResponse(500), FakeResponse(500)])
        with self.assertRaises(CarrierServerError) as ctx:
            self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertTrue(ctx.exception.transient)

    def test_503_retries_then_raises_a_server_error(self):
        session = FakeSession([FakeResponse(503), FakeResponse(503)])
        with self.assertRaises(CarrierServerError):
            self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)

    def test_a_5xx_that_recovers_is_returned(self):
        session = FakeSession([FakeResponse(500), FakeResponse(200, [{"eventID": "E1"}])])
        payload = self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(payload["events"][0]["eventID"], "E1")

    def test_a_timeout_raises_carrier_timeout_after_retries(self):
        session = FakeSession(error=requests.Timeout("timed out"))
        with self.assertRaises(CarrierTimeoutError):
            self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(len(session.requests), 2, "One retry with max_retries=1")

    def test_malformed_json_is_an_invalid_response_not_an_empty_history(self):
        session = FakeSession([FakeResponse(200, invalid_json=True)])
        with self.assertRaises(CarrierInvalidResponseError):
            self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)

    def test_a_scalar_body_is_an_invalid_response_not_an_empty_history(self):
        session = FakeSession([FakeResponse(200, "not an event list")])
        with self.assertRaises(CarrierInvalidResponseError):
            self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)

    def test_an_unexpected_4xx_is_an_invalid_response(self):
        session = FakeSession([FakeResponse(400)])
        with self.assertRaises(CarrierInvalidResponseError):
            self._client(session).fetch_tracking(container_number=CONTAINER_NUMBER)

    def test_a_structurally_invalid_payload_is_rejected_by_the_parser(self):
        with self.assertRaises(CarrierInvalidResponseError):
            CmaCgmParser().parse_tracking_events("a string")


# ---------------------------------------------------------------------------
# Parsing — DCSA events, locations, vessels and CMA CGM's own extension data
# ---------------------------------------------------------------------------


class CmaCgmParserTest(TestCase):
    """The shared DCSA parser normalises a CMA CGM /events response."""

    def setUp(self):
        self.parser = CmaCgmParser()
        self.events = self.parser.parse_tracking_events({"events": _events_fixture()})

    def test_every_event_is_normalised(self):
        self.assertEqual(len(self.events), 4)

    def test_source_provider_is_cma_cgm(self):
        for event in self.events:
            self.assertEqual(event.source_provider, PROVIDER_CODE)

    def test_equipment_load_actual(self):
        event = self.events[0]
        self.assertEqual(event.event_type, "EQUIPMENT")
        self.assertEqual(event.event_code, "LOAD")
        self.assertEqual(event.event_classifier, "ACT")
        self.assertTrue(event.is_actual)
        self.assertFalse(event.is_estimated)

    def test_transport_departure_actual(self):
        event = self.events[1]
        self.assertEqual(event.event_type, "TRANSPORT")
        self.assertEqual(event.event_code, "DEPA")
        self.assertEqual(event.event_classifier, "ACT")
        self.assertTrue(event.is_actual)

    def test_equipment_discharge_actual(self):
        event = self.events[2]
        self.assertEqual(event.event_type, "EQUIPMENT")
        self.assertEqual(event.event_code, "DISC")
        self.assertTrue(event.is_actual)

    def test_transport_arrival_estimated_is_never_treated_as_actual(self):
        event = self.events[3]
        self.assertEqual(event.event_type, "TRANSPORT")
        self.assertEqual(event.event_code, "ARRI")
        self.assertEqual(event.event_classifier, "EST")
        self.assertTrue(event.is_estimated)
        self.assertFalse(event.is_actual)

    def test_event_datetimes_are_parsed_as_aware_datetimes(self):
        for event in self.events:
            self.assertIsNotNone(event.event_datetime)
            self.assertIsNotNone(event.event_datetime.tzinfo)

    def test_the_container_is_read_from_the_flat_equipment_reference(self):
        self.assertEqual(self.events[0].container_number, CONTAINER_NUMBER)

    def test_the_container_is_read_from_the_dcsa_references_array(self):
        """A TRANSPORT event names its container only in ``references``."""
        self.assertEqual(self.events[1].container_number, CONTAINER_NUMBER)

    def test_booking_and_transport_document_references_survive(self):
        load = self.events[0]
        self.assertEqual(load.booking_number, BOOKING_NUMBER)
        self.assertEqual(load.bill_of_lading_number, BL_NUMBER)

    def test_document_references_are_read_from_the_dcsa_arrays(self):
        departure = self.events[1]
        self.assertEqual(departure.booking_number, BOOKING_NUMBER)
        self.assertEqual(departure.bill_of_lading_number, BL_NUMBER)

    def test_location_name_and_unlocode_are_parsed(self):
        load = self.events[0]
        self.assertEqual(load.location_name, "Port of Shanghai")
        self.assertEqual(load.location_unlocode, "CNSHA")

    def test_coordinates_are_parsed_for_the_container_map(self):
        load = self.events[0]
        self.assertEqual(load.latitude, "31.2304")
        self.assertEqual(load.longitude, "121.4737")

    def test_facility_name_is_parsed(self):
        self.assertEqual(self.events[0].facility_name, "Shanghai Yangshan Terminal")

    def test_the_location_inside_the_transport_call_is_parsed(self):
        arrival = self.events[3]
        self.assertEqual(arrival.location_name, "Port of Southampton")
        self.assertEqual(arrival.location_unlocode, "GBSOU")
        self.assertEqual(arrival.latitude, "50.8996")

    def test_vessel_name_and_imo_are_parsed(self):
        load = self.events[0]
        self.assertEqual(load.vessel_name, "CMA CGM ANTOINE DE SAINT EXUPERY")
        self.assertEqual(load.vessel_imo, "9776418")

    def test_vessel_is_parsed_from_the_transport_call(self):
        departure = self.events[1]
        self.assertEqual(departure.vessel_name, "CMA CGM ANTOINE DE SAINT EXUPERY")
        self.assertEqual(departure.vessel_imo, "9776418")

    def test_voyage_number_and_transport_mode_are_parsed(self):
        load = self.events[0]
        self.assertEqual(load.voyage_number, "0FE5ME1MA")
        self.assertEqual(load.transport_mode, "VESSEL")

    def test_the_carrier_event_id_is_kept_for_deduplication(self):
        self.assertEqual(self.events[0].raw_event_id, "CMA-EVT-LOAD-1")

    def test_carrier_specific_data_survives_on_the_raw_payload(self):
        """No CMA CGM column exists, and none should — the raw event keeps everything."""
        carrier_data = self.events[0].raw_payload["carrierSpecificData"]
        self.assertEqual(carrier_data["internalEventCode"], "LOA")
        self.assertEqual(carrier_data["internalEventLabel"], "Loaded on board")
        self.assertEqual(carrier_data["transportationPhase"], "MAIN_CARRIAGE")
        self.assertEqual(carrier_data["shipmentLocationType"], "POL")
        self.assertEqual(carrier_data["numberOfUnits"], 1)

    def test_unmodelled_location_and_vessel_detail_survives_on_the_raw_payload(self):
        load = self.events[0]
        self.assertEqual(load.raw_payload["eventLocation"]["facilityCode"], "SHAPORT")
        self.assertEqual(load.raw_payload["eventLocation"]["facilityTypeCode"], "POTE")
        self.assertEqual(load.raw_payload["transportCall"]["vessel"]["vesselFlag"], "FR")
        self.assertEqual(load.raw_payload["transportCall"]["vessel"]["vesselCallSignNumber"], "FLSU")

    def test_the_raw_payload_is_the_original_event_verbatim(self):
        self.assertEqual(self.events[0].raw_payload, _events_fixture()[0])

    def test_a_bare_array_response_is_parsed(self):
        """CMA CGM's /events answers with an array; the client wraps it for the parser."""
        self.assertEqual(len(self.parser.parse_tracking_events(_events_fixture())), 4)

    def test_an_empty_payload_is_an_empty_list_not_an_error(self):
        self.assertEqual(self.parser.parse_tracking_events({"events": []}), [])
        self.assertEqual(self.parser.parse_tracking_events([]), [])
        self.assertEqual(self.parser.parse_tracking_events(None), [])

    def test_a_malformed_event_does_not_lose_the_others(self):
        payload = {"events": [*_events_fixture(), None]}
        self.assertEqual(len(self.parser.parse_tracking_events(payload)), 4)


# ---------------------------------------------------------------------------
# Request logging must never leak the API key
# ---------------------------------------------------------------------------


class CmaCgmRequestLoggingTest(TestCase):
    def setUp(self):
        self.team = _team("cma-logging-team")

    def _fetch(self, session):
        client = _client(self.team, session=session, credentials={"api_key": API_KEY})
        return client.fetch_tracking(container_number=CONTAINER_NUMBER)

    def test_only_the_path_is_logged_never_host_or_query(self):
        self._fetch(FakeSession([FakeResponse(200, [])]))
        log = IntegrationRequestLog.objects.get(team=self.team)
        self.assertEqual(log.endpoint, "/cma/operation/trackandtrace/v1/events")
        self.assertNotIn("?", log.endpoint)
        self.assertNotIn("example.invalid", log.endpoint)

    def test_the_api_key_never_appears_in_the_request_log(self):
        self._fetch(FakeSession([FakeResponse(200, [])]))
        for log in IntegrationRequestLog.objects.filter(team=self.team):
            serialised = json.dumps(
                {
                    "endpoint": log.endpoint,
                    "error_message": log.error_message,
                    "request_id": log.request_id,
                    "provider_code": log.provider_code,
                }
            )
            self.assertNotIn(API_KEY, serialised)

    def test_no_secret_reaches_the_log_or_the_error_on_a_rejected_key(self):
        session = FakeSession([FakeResponse(401), FakeResponse(401)])
        with self.assertRaises(CarrierAuthenticationError) as ctx:
            self._fetch(session)
        self.assertNotIn(API_KEY, str(ctx.exception))
        for log in IntegrationRequestLog.objects.filter(team=self.team):
            self.assertNotIn(API_KEY, log.endpoint + log.error_message)
            self.assertNotIn("keyId", log.endpoint)

    def test_no_data_is_logged_as_a_successful_call(self):
        with self.assertRaises(CarrierNoDataError):
            self._fetch(FakeSession([FakeResponse(404)]))
        log = IntegrationRequestLog.objects.get(team=self.team)
        self.assertTrue(log.success, "The carrier answered; it just had nothing to say")
        self.assertEqual(log.status_code, 404)

    def test_every_page_is_logged(self):
        self._fetch(
            FakeSession(
                [
                    FakeResponse(200, [], headers={"Next-Page": "cursor123"}),
                    FakeResponse(200, []),
                ]
            )
        )
        self.assertEqual(IntegrationRequestLog.objects.filter(team=self.team).count(), 2)


# ---------------------------------------------------------------------------
# Multi-tenancy
# ---------------------------------------------------------------------------


class CmaCgmTeamIsolationTest(TestCase):
    """One team's CMA CGM integration is never reachable from another team."""

    def setUp(self):
        self.team_a = _team("cma-tenant-a")
        self.team_b = _team("cma-tenant-b")
        self.key_a = "test-cma-api-key-team-a"
        self.key_b = "test-cma-api-key-team-b"

    def _configure(self, team, api_key):
        integration = _integration(team, dict(TEST_CONFIG))
        set_integration_credentials(integration, IntegrationCredential.AuthType.API_KEY, {"api_key": api_key})
        return integration

    def test_the_factory_resolves_the_teams_own_integration(self):
        integration = self._configure(self.team_a, self.key_a)
        client = build_carrier_client(PROVIDER_CODE, team=self.team_a)
        self.assertIsInstance(client, CmaCgmClient)
        self.assertEqual(client.integration.pk, integration.pk)

    def test_the_factory_builds_the_registered_parser(self):
        self.assertIsInstance(build_carrier_parser(PROVIDER_CODE), CmaCgmParser)

    def test_a_team_without_an_integration_cannot_borrow_another_teams(self):
        self._configure(self.team_b, self.key_b)
        with self.assertRaises(CarrierConfigurationError):
            build_carrier_client(PROVIDER_CODE, team=self.team_a, require_integration=True)

    def test_an_unconfigured_team_gets_a_client_that_refuses_to_call(self):
        self._configure(self.team_b, self.key_b)
        client = build_carrier_client(PROVIDER_CODE, team=self.team_a)
        with self.assertRaises(CarrierConfigurationError):
            client.fetch_tracking(container_number=CONTAINER_NUMBER)

    def test_each_team_sends_its_own_key(self):
        self._configure(self.team_a, self.key_a)
        self._configure(self.team_b, self.key_b)

        session_a = FakeSession([FakeResponse(200, [])])
        CmaCgmClient(
            build_carrier_client(PROVIDER_CODE, team=self.team_a).integration,
            session=session_a,
        ).fetch_tracking(container_number=CONTAINER_NUMBER)

        session_b = FakeSession([FakeResponse(200, [])])
        CmaCgmClient(
            build_carrier_client(PROVIDER_CODE, team=self.team_b).integration,
            session=session_b,
        ).fetch_tracking(container_number=CONTAINER_NUMBER)

        self.assertEqual(session_a.requests[0]["headers"]["keyId"], self.key_a)
        self.assertEqual(session_b.requests[0]["headers"]["keyId"], self.key_b)

    def test_request_logs_stay_with_the_calling_team(self):
        self._configure(self.team_a, self.key_a)
        self._configure(self.team_b, self.key_b)
        CmaCgmClient(
            build_carrier_client(PROVIDER_CODE, team=self.team_a).integration,
            session=FakeSession([FakeResponse(200, [])]),
        ).fetch_tracking(container_number=CONTAINER_NUMBER)
        self.assertEqual(IntegrationRequestLog.objects.filter(team=self.team_a).count(), 1)
        self.assertEqual(IntegrationRequestLog.objects.filter(team=self.team_b).count(), 0)

    def test_a_business_system_integration_is_never_used_as_a_carrier(self):
        Integration.objects.create(
            team=self.team_a,
            name="Not a carrier",
            provider_code=PROVIDER_CODE,
            provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
            config=dict(TEST_CONFIG),
            is_active=True,
        )
        with self.assertRaises(CarrierConfigurationError):
            build_carrier_client(PROVIDER_CODE, team=self.team_a, require_integration=True)


# ---------------------------------------------------------------------------
# Discovery reuses the same endpoint
# ---------------------------------------------------------------------------


class CmaCgmDiscoveryTest(TestCase):
    """Container discovery reuses the tracking endpoint's equipment references."""

    def setUp(self):
        self.team = _team("cma-discovery-team")

    def _client(self, session):
        return _client(self.team, session=session, credentials={"api_key": API_KEY})

    def test_distinct_containers_are_discovered_from_a_booking(self):
        session = FakeSession([FakeResponse(200, _events_fixture())])
        results = self._client(session).discover_containers(booking_number=BOOKING_NUMBER)
        self.assertEqual([result.container_number for result in results], [CONTAINER_NUMBER])
        self.assertEqual(results[0].carrier_code, PROVIDER_CODE)
        self.assertEqual(results[0].carrier_name, CARRIER_NAME)

    def test_discovery_uses_the_booking_parameter(self):
        session = FakeSession([FakeResponse(200, [])])
        self._client(session).discover_containers(booking_number=BOOKING_NUMBER)
        self.assertEqual(session.requests[0]["params"]["carrierBookingReference"], BOOKING_NUMBER)

    def test_no_data_returns_an_empty_list(self):
        session = FakeSession([FakeResponse(404)])
        self.assertEqual(self._client(session).discover_containers(booking_number=BOOKING_NUMBER), [])
