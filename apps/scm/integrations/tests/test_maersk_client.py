"""Tests for the Maersk Track & Trace client.

Every test drives the client with an injected fake session — no live call is made,
and the socket guard in test_carrier_no_live_api.py enforces that separately.
"""

import json
import pathlib
from unittest import mock

import requests
from django.test import TestCase

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
from apps.scm.integrations.carriers.factory import build_carrier_client
from apps.scm.integrations.carriers.maersk.client import (
    PUBLIC_TRACK_AND_TRACE_CONFIG,
    MaerskClient,
    resolve_config,
)
from apps.scm.integrations.carriers.maersk.parser import MaerskParser
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential, IntegrationRequestLog
from apps.teams.models import Team

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "carriers"

API_KEY = "super-secret-consumer-key"
CLIENT_SECRET = "super-secret-client-secret"

# Placeholder endpoint values. These stand in for the real ones, which come from the
# Maersk API portal — see carriers/maersk/README.md.
BASE_URL = "https://example.invalid/maersk"
TRACKING_PATH = "/track-and-trace/events"

API_KEY_CONFIG = {
    "base_url": BASE_URL,
    "tracking_path": TRACKING_PATH,
    "auth_style": "api_key_header",
    "api_key_header_name": "Consumer-Key",
    "reference_params": {
        "container_number": "equipmentReference",
        "bill_of_lading_number": "transportDocumentReference",
        "booking_number": "carrierBookingReference",
    },
    "test_connection_reference": "MRKU1234563",
    "max_retries": 1,
    "retry_backoff_seconds": 0,
}

OAUTH_CONFIG = {
    **API_KEY_CONFIG,
    "auth_style": "oauth2_client_credentials",
    "api_key_header_name": "",
    "token_url": "https://example.invalid/oauth/token",
    "scope": "track-and-trace",
}


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _integration(team: Team, config: dict) -> Integration:
    return Integration.objects.create(
        team=team,
        name="Maersk",
        provider_code="maersk",
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
            return FakeResponse(200, {"events": []})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client(team, config, *, session=None, credentials=None) -> MaerskClient:
    integration = _integration(team, config)
    if credentials is not None:
        set_integration_credentials(integration, IntegrationCredential.AuthType.API_KEY, credentials)
    return MaerskClient(integration, session=session)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class MaerskConfigurationTest(TestCase):
    """Live access requires a verified configuration; nothing is guessed."""

    def setUp(self):
        self.team = _team("maersk-config-team")

    def test_unconfigured_client_refuses_to_call(self):
        with self.assertRaises(CarrierConfigurationError):
            MaerskClient().fetch_tracking(container_number="MRKU1234563")

    def test_empty_config_lists_every_missing_key(self):
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config({})
        message = str(ctx.exception)
        for key in ("base_url", "tracking_path", "auth_style", "reference_params"):
            self.assertIn(key, message)

    def test_missing_base_url_is_reported(self):
        config = {**API_KEY_CONFIG, "base_url": ""}
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config(config)
        self.assertIn("base_url", str(ctx.exception))

    def test_unknown_auth_style_is_rejected(self):
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config({**API_KEY_CONFIG, "auth_style": "magic"})
        self.assertIn("auth_style", str(ctx.exception))

    def test_oauth_requires_token_url(self):
        config = {**OAUTH_CONFIG}
        config.pop("token_url")
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config(config)
        self.assertIn("token_url", str(ctx.exception))

    def test_api_key_style_requires_header_name(self):
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config({**API_KEY_CONFIG, "api_key_header_name": ""})
        self.assertIn("api_key_header_name", str(ctx.exception))

    def test_unknown_reference_kind_is_rejected(self):
        config = {**API_KEY_CONFIG, "reference_params": {"vessel_name": "vessel"}}
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config(config)
        self.assertIn("vessel_name", str(ctx.exception))

    def test_valid_config_builds_the_tracking_url(self):
        config = resolve_config(API_KEY_CONFIG)
        self.assertEqual(config.tracking_url, f"{BASE_URL}/track-and-trace/events")

    def test_reference_without_a_configured_param_is_refused(self):
        config = {**API_KEY_CONFIG, "reference_params": {"container_number": "equipmentReference"}}
        client = _client(self.team, config, session=FakeSession(), credentials={"api_key": API_KEY})
        with self.assertRaises(CarrierConfigurationError):
            client.fetch_tracking(booking_number="BKG-1")

    def test_connection_test_requires_a_reference(self):
        config = {**API_KEY_CONFIG, "test_connection_reference": ""}
        client = _client(self.team, config, session=FakeSession(), credentials={"api_key": API_KEY})
        with self.assertRaises(CarrierConfigurationError) as ctx:
            client.test_connection()
        self.assertIn("test_connection_reference", str(ctx.exception))


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class MaerskAuthenticationTest(TestCase):
    def setUp(self):
        self.team = _team("maersk-auth-team")

    def test_api_key_is_sent_in_the_configured_header(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        client = _client(self.team, API_KEY_CONFIG, session=session, credentials={"api_key": API_KEY})
        client.fetch_tracking(container_number="MRKU1234563")
        self.assertEqual(session.requests[0]["headers"]["Consumer-Key"], API_KEY)

    def test_oauth_token_is_requested_and_sent_as_bearer(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        client = _client(
            self.team,
            OAUTH_CONFIG,
            session=session,
            credentials={"client_id": "id", "client_secret": CLIENT_SECRET},
        )
        with mock.patch("apps.scm.integrations.carriers.oauth.requests.post") as post:
            post.return_value = FakeResponse(200, {"access_token": "token-abc", "expires_in": 3600})
            client.fetch_tracking(container_number="MRKU1234563")

        post.assert_called_once()
        self.assertEqual(session.requests[0]["headers"]["Authorization"], "Bearer token-abc")

    def test_oauth_token_is_reused_between_calls(self):
        session = FakeSession([FakeResponse(200, {"events": []}), FakeResponse(200, {"events": []})])
        client = _client(
            self.team,
            OAUTH_CONFIG,
            session=session,
            credentials={"client_id": "id", "client_secret": CLIENT_SECRET},
        )
        with mock.patch("apps.scm.integrations.carriers.oauth.requests.post") as post:
            post.return_value = FakeResponse(200, {"access_token": "token-abc", "expires_in": 3600})
            client.fetch_tracking(container_number="MRKU1234563")
            client.fetch_tracking(container_number="MRKU1234563")
        self.assertEqual(post.call_count, 1)

    def test_missing_api_key_credential_is_a_configuration_error(self):
        client = _client(self.team, API_KEY_CONFIG, session=FakeSession())
        with self.assertRaises(CarrierConfigurationError):
            client.fetch_tracking(container_number="MRKU1234563")

    def test_failed_token_request_is_an_authentication_error(self):
        client = _client(
            self.team,
            OAUTH_CONFIG,
            session=FakeSession(),
            credentials={"client_id": "id", "client_secret": CLIENT_SECRET},
        )
        with (
            mock.patch("apps.scm.integrations.carriers.oauth.requests.post") as post,
            self.assertRaises(CarrierAuthenticationError),
        ):
            post.return_value = FakeResponse(401, {"error": "invalid_client"})
            client.fetch_tracking(container_number="MRKU1234563")

    def test_401_triggers_one_refresh_then_fails(self):
        session = FakeSession([FakeResponse(401), FakeResponse(401)])
        client = _client(
            self.team,
            OAUTH_CONFIG,
            session=session,
            credentials={"client_id": "id", "client_secret": CLIENT_SECRET},
        )
        with (
            mock.patch("apps.scm.integrations.carriers.oauth.requests.post") as post,
            self.assertRaises(CarrierAuthenticationError),
        ):
            post.return_value = FakeResponse(200, {"access_token": "t", "expires_in": 3600})
            client.fetch_tracking(container_number="MRKU1234563")
        self.assertEqual(len(session.requests), 2, "One refresh and retry, then give up")

    def test_403_is_an_authentication_error(self):
        session = FakeSession([FakeResponse(403)])
        client = _client(self.team, API_KEY_CONFIG, session=session, credentials={"api_key": API_KEY})
        with self.assertRaises(CarrierAuthenticationError):
            client.fetch_tracking(container_number="MRKU1234563")


# ---------------------------------------------------------------------------
# Transport behaviour
# ---------------------------------------------------------------------------


class MaerskTransportTest(TestCase):
    def setUp(self):
        self.team = _team("maersk-transport-team")

    def _client(self, session, config=None):
        return _client(self.team, config or API_KEY_CONFIG, session=session, credentials={"api_key": API_KEY})

    def test_reference_is_sent_as_the_configured_query_parameter(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        self._client(session).fetch_tracking(container_number="MRKU1234563")
        self.assertEqual(session.requests[0]["params"], {"equipmentReference": "MRKU1234563"})

    def test_bill_of_lading_uses_its_own_parameter(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        self._client(session).fetch_tracking(bill_of_lading_number="MAEU-BL-1")
        self.assertEqual(session.requests[0]["params"], {"transportDocumentReference": "MAEU-BL-1"})

    def test_two_references_are_rejected_before_any_call(self):
        session = FakeSession()
        with self.assertRaises(CarrierUnsupportedReferenceError):
            self._client(session).fetch_tracking(container_number="MRKU1234563", booking_number="BKG-1")
        self.assertEqual(session.requests, [])

    def test_configured_timeout_is_applied(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        self._client(session, {**API_KEY_CONFIG, "request_timeout_seconds": 7}).fetch_tracking(
            container_number="MRKU1234563"
        )
        self.assertEqual(session.requests[0]["timeout"], 7)

    def test_timeout_raises_carrier_timeout_after_retries(self):
        session = FakeSession(error=requests.Timeout("timed out"))
        with self.assertRaises(CarrierTimeoutError):
            self._client(session).fetch_tracking(container_number="MRKU1234563")
        self.assertEqual(len(session.requests), 2, "One retry with max_retries=1")

    def test_connection_error_raises_carrier_timeout(self):
        session = FakeSession(error=requests.ConnectionError("reset"))
        with self.assertRaises(CarrierTimeoutError):
            self._client(session).fetch_tracking(container_number="MRKU1234563")

    def test_404_is_no_data_not_an_error(self):
        session = FakeSession([FakeResponse(404)])
        with self.assertRaises(CarrierNoDataError):
            self._client(session).fetch_tracking(container_number="MRKU1234563")

    def test_no_data_status_is_configurable(self):
        session = FakeSession([FakeResponse(204)])
        client = self._client(session, {**API_KEY_CONFIG, "no_data_statuses": [204]})
        with self.assertRaises(CarrierNoDataError):
            client.fetch_tracking(container_number="MRKU1234563")

    def test_429_retries_then_raises_with_retry_after(self):
        session = FakeSession(
            [
                FakeResponse(429, headers={"Retry-After": "0"}),
                FakeResponse(429, headers={"Retry-After": "5"}),
            ]
        )
        with self.assertRaises(CarrierRateLimitError) as ctx:
            self._client(session).fetch_tracking(container_number="MRKU1234563")
        self.assertEqual(ctx.exception.retry_after, 5)
        self.assertEqual(len(session.requests), 2)

    def test_long_retry_after_fails_fast_instead_of_blocking(self):
        """Waiting minutes inside the request would hold a worker and its sync lock."""
        session = FakeSession([FakeResponse(429, headers={"Retry-After": "600"})])
        with self.assertRaises(CarrierRateLimitError) as ctx:
            self._client(session).fetch_tracking(container_number="MRKU1234563")
        self.assertEqual(ctx.exception.retry_after, 600)
        self.assertEqual(len(session.requests), 1, "No inline wait for a long Retry-After")

    def test_short_retry_after_is_waited_out(self):
        session = FakeSession([FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, {"events": []})])
        payload = self._client(session).fetch_tracking(container_number="MRKU1234563")
        self.assertEqual(payload, {"events": []})

    def test_429_that_clears_on_retry_succeeds(self):
        session = FakeSession([FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, {"events": []})])
        payload = self._client(session).fetch_tracking(container_number="MRKU1234563")
        self.assertEqual(payload, {"events": []})

    def test_5xx_retries_then_raises_server_error(self):
        session = FakeSession([FakeResponse(503), FakeResponse(503)])
        with self.assertRaises(CarrierServerError) as ctx:
            self._client(session).fetch_tracking(container_number="MRKU1234563")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_5xx_that_recovers_is_returned(self):
        session = FakeSession([FakeResponse(500), FakeResponse(200, {"events": [{"eventID": "E1"}]})])
        payload = self._client(session).fetch_tracking(container_number="MRKU1234563")
        self.assertEqual(payload["events"][0]["eventID"], "E1")

    def test_invalid_json_is_an_invalid_response(self):
        session = FakeSession([FakeResponse(200, invalid_json=True)])
        with self.assertRaises(CarrierInvalidResponseError):
            self._client(session).fetch_tracking(container_number="MRKU1234563")

    def test_unexpected_4xx_is_an_invalid_response(self):
        session = FakeSession([FakeResponse(400)])
        with self.assertRaises(CarrierInvalidResponseError):
            self._client(session).fetch_tracking(container_number="MRKU1234563")

    def test_list_response_is_wrapped_for_the_parser(self):
        session = FakeSession([FakeResponse(200, [{"eventID": "E1"}])])
        payload = self._client(session).fetch_tracking(container_number="MRKU1234563")
        self.assertEqual(payload, {"events": [{"eventID": "E1"}]})

    def test_test_connection_succeeds_on_no_data(self):
        session = FakeSession([FakeResponse(404)])
        result = self._client(session).test_connection()
        self.assertTrue(result["success"])

    def test_test_connection_succeeds_on_data(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        self.assertTrue(self._client(session).test_connection()["success"])


# ---------------------------------------------------------------------------
# Request logging must never leak secrets
# ---------------------------------------------------------------------------


class MaerskRequestLoggingTest(TestCase):
    def setUp(self):
        self.team = _team("maersk-logging-team")

    def _fetch(self, session, **kwargs):
        client = _client(self.team, API_KEY_CONFIG, session=session, credentials={"api_key": API_KEY})
        return client.fetch_tracking(container_number="MRKU1234563", **kwargs)

    def test_successful_request_is_logged(self):
        self._fetch(FakeSession([FakeResponse(200, {"events": []})]))
        log = IntegrationRequestLog.objects.get(team=self.team)
        self.assertTrue(log.success)
        self.assertEqual(log.status_code, 200)
        self.assertEqual(log.method, "GET")

    def test_only_the_path_is_logged_never_host_or_query(self):
        self._fetch(FakeSession([FakeResponse(200, {"events": []})]))
        log = IntegrationRequestLog.objects.get(team=self.team)
        self.assertEqual(log.endpoint, f"/maersk{TRACKING_PATH}")
        self.assertNotIn("?", log.endpoint)
        self.assertNotIn("example.invalid", log.endpoint)

    def test_api_key_never_appears_in_the_request_log(self):
        """A credential in a header or query parameter must not reach the log table."""
        self._fetch(FakeSession([FakeResponse(200, {"events": []})]))
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

    def test_oauth_secret_never_appears_in_the_request_log(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        client = _client(
            self.team,
            OAUTH_CONFIG,
            session=session,
            credentials={"client_id": "id", "client_secret": CLIENT_SECRET},
        )
        with mock.patch("apps.scm.integrations.carriers.oauth.requests.post") as post:
            post.return_value = FakeResponse(200, {"access_token": "token-abc", "expires_in": 60})
            client.fetch_tracking(container_number="MRKU1234563")

        for log in IntegrationRequestLog.objects.filter(team=self.team):
            self.assertNotIn(CLIENT_SECRET, log.error_message + log.endpoint)
            self.assertNotIn("token-abc", log.error_message + log.endpoint)

    def test_failed_request_is_logged_with_status_and_message(self):
        with self.assertRaises(CarrierServerError):
            self._fetch(FakeSession([FakeResponse(503), FakeResponse(503)]))
        log = IntegrationRequestLog.objects.filter(team=self.team).first()
        self.assertFalse(log.success)
        self.assertEqual(log.status_code, 503)
        self.assertIn("server error", log.error_message.lower())

    def test_no_data_is_logged_as_a_successful_call(self):
        with self.assertRaises(CarrierNoDataError):
            self._fetch(FakeSession([FakeResponse(404)]))
        log = IntegrationRequestLog.objects.get(team=self.team)
        self.assertTrue(log.success, "The carrier answered; it just had nothing to say")
        self.assertEqual(log.status_code, 404)

    def test_logging_is_team_scoped(self):
        other = _team("maersk-logging-other-team")
        self._fetch(FakeSession([FakeResponse(200, {"events": []})]))
        self.assertEqual(IntegrationRequestLog.objects.filter(team=other).count(), 0)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class MaerskParserTest(TestCase):
    """The Maersk parser normalises real DCSA fixture data."""

    def setUp(self):
        self.payload = json.loads((FIXTURES / "maersk_tracking_response.json").read_text())
        self.parser = MaerskParser()

    def test_fixture_produces_normalised_events(self):
        events = self.parser.parse_tracking_events(self.payload)
        self.assertEqual(len(events), 2)

    def test_actual_and_estimated_are_distinguished(self):
        actual, estimated = self.parser.parse_tracking_events(self.payload)
        self.assertTrue(actual.is_actual)
        self.assertTrue(estimated.is_estimated)
        self.assertFalse(estimated.is_actual)

    def test_vessel_voyage_and_location_survive(self):
        event = self.parser.parse_tracking_events(self.payload)[0]
        self.assertEqual(event.vessel_name, "MAERSK EINDHOVEN")
        self.assertEqual(event.vessel_imo, "9778791")
        self.assertEqual(event.voyage_number, "213E")
        self.assertEqual(event.location_unlocode, "GBFXT")

    def test_source_provider_is_maersk(self):
        for event in self.parser.parse_tracking_events(self.payload):
            self.assertEqual(event.source_provider, "maersk")

    def test_empty_payload_is_an_empty_list_not_an_error(self):
        self.assertEqual(self.parser.parse_tracking_events({"events": []}), [])
        self.assertEqual(self.parser.parse_tracking_events({}), [])
        self.assertEqual(self.parser.parse_tracking_events(None), [])

    def test_enveloped_payload_is_unwrapped(self):
        events = self.parser.parse_tracking_events({"data": self.payload})
        self.assertEqual(len(events), 2)

    def test_non_json_payload_is_rejected_not_silently_empty(self):
        with self.assertRaises(CarrierInvalidResponseError):
            self.parser.parse_tracking_events("a string")

    def test_a_malformed_event_does_not_lose_the_others(self):
        payload = {"events": [{"eventID": "GOOD", "eventType": "EQUIPMENT"}, None]}
        self.assertEqual(len(self.parser.parse_tracking_events(payload)), 1)


class MaerskPublicEndpointTest(TestCase):
    """The shipped public Track & Trace config produces exactly the verified request.

    These assertions pin the contract that was confirmed against the live endpoint:
    ``GET https://api.maersk.com/track-and-trace/public-events?equipmentReference=…``
    with ``consumer-key`` and ``API-Version: 1``. A change to any of them here is a
    change to what we send Maersk, and should have to be justified.
    """

    def setUp(self):
        self.team = _team("maersk-public-team")

    def _client(self, session):
        return _client(
            self.team,
            dict(PUBLIC_TRACK_AND_TRACE_CONFIG),
            session=session,
            credentials={"api_key": API_KEY},
        )

    def test_config_is_valid_and_builds_the_public_events_url(self):
        config = resolve_config(PUBLIC_TRACK_AND_TRACE_CONFIG)
        self.assertEqual(config.tracking_url, "https://api.maersk.com/track-and-trace/public-events")

    def test_container_number_is_sent_as_equipment_reference(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        self._client(session).fetch_tracking(container_number="TRDU9258963")
        self.assertEqual(session.requests[0]["params"], {"equipmentReference": "TRDU9258963"})

    def test_consumer_key_header_carries_the_api_key(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        self._client(session).fetch_tracking(container_number="TRDU9258963")
        self.assertEqual(session.requests[0]["headers"]["consumer-key"], API_KEY)

    def test_api_version_header_is_sent(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        self._client(session).fetch_tracking(container_number="TRDU9258963")
        self.assertEqual(session.requests[0]["headers"]["API-Version"], "1")

    def test_accept_json_header_is_sent(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        self._client(session).fetch_tracking(container_number="TRDU9258963")
        self.assertEqual(session.requests[0]["headers"]["Accept"], "application/json")

    def test_configured_timeout_is_thirty_seconds(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        self._client(session).fetch_tracking(container_number="TRDU9258963")
        self.assertEqual(session.requests[0]["timeout"], 30)

    def test_404_is_no_data_for_the_public_endpoint(self):
        session = FakeSession([FakeResponse(404)])
        with self.assertRaises(CarrierNoDataError):
            self._client(session).fetch_tracking(container_number="TRDU9258963")

    def test_401_is_an_authentication_error_and_is_not_retried_forever(self):
        session = FakeSession([FakeResponse(401), FakeResponse(401)])
        with self.assertRaises(CarrierAuthenticationError):
            self._client(session).fetch_tracking(container_number="TRDU9258963")
        self.assertLessEqual(len(session.requests), 2)

    def test_403_is_an_authentication_error(self):
        session = FakeSession([FakeResponse(403)])
        with self.assertRaises(CarrierAuthenticationError):
            self._client(session).fetch_tracking(container_number="TRDU9258963")

    def test_429_reports_the_carriers_retry_after(self):
        session = FakeSession([FakeResponse(429, headers={"Retry-After": "600"})])
        with self.assertRaises(CarrierRateLimitError) as ctx:
            self._client(session).fetch_tracking(container_number="TRDU9258963")
        self.assertEqual(ctx.exception.retry_after, 600)

    def test_no_secret_reaches_the_request_log(self):
        session = FakeSession([FakeResponse(401), FakeResponse(401)])
        with self.assertRaises(CarrierAuthenticationError):
            self._client(session).fetch_tracking(container_number="TRDU9258963")
        for log in IntegrationRequestLog.objects.filter(team=self.team):
            self.assertNotIn(API_KEY, log.endpoint + log.error_message)
            self.assertNotIn("consumer-key", log.endpoint)

    def test_the_config_carries_no_credential(self):
        """The endpoint settings ship in code, so they must hold no credential value."""
        for forbidden in ("api_key", "client_id", "client_secret", "password", "token", "secret"):
            self.assertNotIn(forbidden, PUBLIC_TRACK_AND_TRACE_CONFIG)
        # extra_headers is sent verbatim on every request and must stay non-secret.
        self.assertEqual(
            set(PUBLIC_TRACK_AND_TRACE_CONFIG["extra_headers"]),
            {"API-Version", "Accept"},
        )

    def test_the_key_comes_from_the_credential_service_not_the_config(self):
        client = _client(self.team, dict(PUBLIC_TRACK_AND_TRACE_CONFIG), session=FakeSession())
        with self.assertRaises(CarrierConfigurationError):
            client.fetch_tracking(container_number="TRDU9258963")

    def test_bill_of_lading_is_refused_rather_than_guessed(self):
        """Only container tracking is configured for the public endpoint."""
        session = FakeSession()
        with self.assertRaises(CarrierConfigurationError):
            self._client(session).fetch_tracking(bill_of_lading_number="MAEU-BL-1")
        self.assertEqual(session.requests, [])


class MaerskTeamIsolationTest(TestCase):
    """One team's Maersk integration is never reachable from another team."""

    def setUp(self):
        self.team_a = _team("maersk-tenant-a")
        self.team_b = _team("maersk-tenant-b")
        self.key_a = "team-a-consumer-key"
        self.key_b = "team-b-consumer-key"

    def _configure(self, team, api_key):
        integration = _integration(team, dict(PUBLIC_TRACK_AND_TRACE_CONFIG))
        set_integration_credentials(integration, IntegrationCredential.AuthType.API_KEY, {"api_key": api_key})
        return integration

    def test_a_team_without_an_integration_cannot_borrow_another_teams(self):
        self._configure(self.team_b, self.key_b)
        with self.assertRaises(CarrierConfigurationError):
            build_carrier_client("maersk", team=self.team_a, require_integration=True)

    def test_an_unconfigured_team_gets_a_client_that_refuses_to_call(self):
        self._configure(self.team_b, self.key_b)
        client = build_carrier_client("maersk", team=self.team_a)
        with self.assertRaises(CarrierConfigurationError):
            client.fetch_tracking(container_number="TRDU9258963")

    def test_each_team_sends_its_own_key(self):
        self._configure(self.team_a, self.key_a)
        self._configure(self.team_b, self.key_b)

        session_a = FakeSession([FakeResponse(200, {"events": []})])
        client_a = MaerskClient(
            build_carrier_client("maersk", team=self.team_a).integration,
            session=session_a,
        )
        client_a.fetch_tracking(container_number="TRDU9258963")

        session_b = FakeSession([FakeResponse(200, {"events": []})])
        client_b = MaerskClient(
            build_carrier_client("maersk", team=self.team_b).integration,
            session=session_b,
        )
        client_b.fetch_tracking(container_number="TRDU9258963")

        self.assertEqual(session_a.requests[0]["headers"]["consumer-key"], self.key_a)
        self.assertEqual(session_b.requests[0]["headers"]["consumer-key"], self.key_b)

    def test_request_logs_stay_with_the_calling_team(self):
        self._configure(self.team_a, self.key_a)
        self._configure(self.team_b, self.key_b)
        client = MaerskClient(
            build_carrier_client("maersk", team=self.team_a).integration,
            session=FakeSession([FakeResponse(200, {"events": []})]),
        )
        client.fetch_tracking(container_number="TRDU9258963")
        self.assertEqual(IntegrationRequestLog.objects.filter(team=self.team_a).count(), 1)
        self.assertEqual(IntegrationRequestLog.objects.filter(team=self.team_b).count(), 0)

    def test_a_business_system_integration_is_never_used_as_a_carrier(self):
        Integration.objects.create(
            team=self.team_a,
            name="Not a carrier",
            provider_code="maersk",
            provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
            config=dict(PUBLIC_TRACK_AND_TRACE_CONFIG),
            is_active=True,
        )
        with self.assertRaises(CarrierConfigurationError):
            build_carrier_client("maersk", team=self.team_a, require_integration=True)


class MaerskCapabilityTest(TestCase):
    """The public endpoint needs no account number, and must not claim otherwise."""

    def test_account_number_is_not_required(self):
        self.assertFalse(MaerskClient().capabilities.requires_account_number)

    def test_container_tracking_is_supported(self):
        self.assertTrue(MaerskClient().capabilities.supports_tracking_by_container)

    def test_an_api_key_alone_is_enough_to_call(self):
        team = _team("maersk-no-account-number")
        session = FakeSession([FakeResponse(200, {"events": []})])
        client = _client(
            team,
            dict(PUBLIC_TRACK_AND_TRACE_CONFIG),
            session=session,
            credentials={"api_key": API_KEY},
        )
        client.fetch_tracking(container_number="TRDU9258963")
        self.assertEqual(len(session.requests), 1)


class MaerskDiscoveryTest(TestCase):
    """Container discovery reuses the tracking endpoint's equipment references."""

    def setUp(self):
        self.team = _team("maersk-discovery-team")

    def _client(self, session):
        return _client(self.team, API_KEY_CONFIG, session=session, credentials={"api_key": API_KEY})

    def test_distinct_containers_are_discovered(self):
        payload = json.loads((FIXTURES / "maersk_tracking_response.json").read_text())
        session = FakeSession([FakeResponse(200, payload)])
        results = self._client(session).discover_containers(booking_number="MAEU123456789")
        self.assertEqual([result.container_number for result in results], ["MRKU1234567"])
        self.assertEqual(results[0].carrier_code, "maersk")

    def test_no_data_returns_an_empty_list(self):
        session = FakeSession([FakeResponse(404)])
        self.assertEqual(self._client(session).discover_containers(booking_number="BKG-1"), [])

    def test_discovery_uses_the_booking_parameter(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        self._client(session).discover_containers(booking_number="BKG-1")
        self.assertEqual(session.requests[0]["params"], {"carrierBookingReference": "BKG-1"})
