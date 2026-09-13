"""Tests for the Hapag-Lloyd carrier, a user of the shared DCSA pipeline.

These focus on what is genuinely Hapag-Lloyd's own — its identity, capabilities,
gateway authentication and response shape — plus enough transport coverage to prove
the shared client is actually wired up. The transport itself is tested once, against
Maersk.

The end-to-end path (persistence, location resolution, discovery, coexistence with
Traqo) is in ``test_hapag_lloyd_pipeline.py``. No test here makes a live call.
"""

import json
import pathlib

import requests
from django.test import TestCase

from apps.scm.integrations.carriers.dcsa.client import DcsaCarrierClient
from apps.scm.integrations.carriers.exceptions import (
    CarrierAuthenticationError,
    CarrierConfigurationError,
    CarrierNoDataError,
    CarrierRateLimitError,
    CarrierTimeoutError,
)
from apps.scm.integrations.carriers.hapag_lloyd.client import (
    TRACK_AND_TRACE_CONFIG,
    HapagLloydClient,
    resolve_config,
)
from apps.scm.integrations.carriers.hapag_lloyd.parser import HapagLloydParser
from apps.scm.integrations.carriers.registry import get_carrier_definition
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential, IntegrationRequestLog
from apps.teams.models import Team

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "carriers"
API_KEY = "hapag-secret-key"
CLIENT_ID = "hapag-client-id"
CLIENT_SECRET = "hapag-client-secret"

# Placeholder endpoint values; the real ones come from the Hapag-Lloyd API portal.
CONFIG = {
    "base_url": "https://example.invalid/hapag",
    "tracking_path": "/events",
    "auth_style": "api_key_header",
    "api_key_header_name": "X-Api-Key",
    "reference_params": {
        "container_number": "equipmentReference",
        "bill_of_lading_number": "transportDocumentReference",
    },
    "test_connection_reference": "HLXU1234567",
    "max_retries": 1,
    "retry_backoff_seconds": 0,
}

# The live shape: the gateway's two credential headers, against a placeholder host.
GATEWAY_CONFIG = {
    **TRACK_AND_TRACE_CONFIG,
    "base_url": "https://example.invalid/hapag",
    "tracking_path": "/events",
    "test_connection_reference": "HLXU8891233",
    "max_retries": 1,
    "retry_backoff_seconds": 0,
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"url": url, "headers": headers or {}, "params": params or {}})
        if self.error is not None:
            raise self.error
        return self.responses.pop(0) if self.responses else FakeResponse(200, {"events": []})


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _client(team, session=None, config=None, credentials=None) -> HapagLloydClient:
    integration = Integration.objects.create(
        team=team,
        name="Hapag-Lloyd",
        provider_code="hapag_lloyd",
        provider_family=Integration.ProviderFamily.CARRIER,
        api_style=Integration.ApiStyle.DCSA,
        config=config or CONFIG,
        is_active=True,
    )
    set_integration_credentials(
        integration,
        IntegrationCredential.AuthType.API_KEY,
        credentials if credentials is not None else {"api_key": API_KEY},
    )
    return HapagLloydClient(integration, session=session)


def _gateway_client(team, session=None, config=None, credentials=None) -> HapagLloydClient:
    """A client configured the way the setup command configures a live one."""
    return _client(
        team,
        session=session,
        config=config or GATEWAY_CONFIG,
        credentials=credentials
        if credentials is not None
        else {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )


class HapagLloydUsesSharedPipelineTest(TestCase):
    """The second carrier must add identity, not another transport."""

    def test_client_is_a_dcsa_carrier_client(self):
        self.assertTrue(issubclass(HapagLloydClient, DcsaCarrierClient))

    def test_client_defines_no_transport_of_its_own(self):
        """Adding a DCSA carrier must not mean copying fetch_tracking again."""
        for method in ("fetch_tracking", "test_connection", "discover_containers"):
            with self.subTest(method=method):
                self.assertNotIn(method, HapagLloydClient.__dict__)

    def test_parser_delegates_to_the_shared_dcsa_parser(self):
        from apps.scm.integrations.carriers.dcsa.carrier_parser import DcsaCarrierParser

        self.assertTrue(issubclass(HapagLloydParser, DcsaCarrierParser))
        self.assertNotIn("parse_tracking_events", HapagLloydParser.__dict__)

    def test_registry_entry_matches_the_client(self):
        definition = get_carrier_definition("hapag_lloyd")
        self.assertIs(definition.client_class, HapagLloydClient)
        self.assertIs(definition.parser_class, HapagLloydParser)
        self.assertTrue(definition.capabilities.supports_dcsa)


class HapagLloydConfigurationTest(TestCase):
    def setUp(self):
        self.team = _team("hapag-config-team")

    def test_unconfigured_client_refuses_to_call(self):
        with self.assertRaises(CarrierConfigurationError):
            HapagLloydClient().fetch_tracking(container_number="HLXU1234567")

    def test_missing_configuration_names_the_carrier_and_the_keys(self):
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config({})
        message = str(ctx.exception)
        self.assertIn("Hapag-Lloyd", message)
        self.assertIn("base_url", message)

    def test_unsupported_reference_kind_is_rejected(self):
        with self.assertRaises(CarrierConfigurationError):
            resolve_config({**CONFIG, "reference_params": {"vessel_imo": "imo"}})

    def test_reference_without_a_configured_param_is_refused(self):
        client = _client(self.team, FakeSession())
        with self.assertRaises(CarrierConfigurationError):
            client.fetch_tracking(booking_number="BKG-1")


class HapagLloydGatewayAuthTest(TestCase):
    """Hapag-Lloyd's gateway wants two credential headers, not one and not a token."""

    def setUp(self):
        self.team = _team("hapag-auth-team")

    def test_shipped_config_selects_the_two_header_style(self):
        self.assertEqual(TRACK_AND_TRACE_CONFIG["auth_style"], "client_id_secret_headers")
        self.assertEqual(TRACK_AND_TRACE_CONFIG["client_id_header_name"], "X-IBM-Client-Id")
        self.assertEqual(TRACK_AND_TRACE_CONFIG["client_secret_header_name"], "X-IBM-Client-Secret")

    def test_both_credentials_are_sent_as_headers(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        _gateway_client(self.team, session).fetch_tracking(container_number="HLXU8891233")
        headers = session.requests[0]["headers"]
        self.assertEqual(headers["X-IBM-Client-Id"], CLIENT_ID)
        self.assertEqual(headers["X-IBM-Client-Secret"], CLIENT_SECRET)

    def test_no_token_request_is_made(self):
        """The gateway style must not spend a round trip acquiring a token."""
        session = FakeSession([FakeResponse(200, {"events": []})])
        _gateway_client(self.team, session).fetch_tracking(container_number="HLXU8891233")
        self.assertEqual(len(session.requests), 1)
        self.assertTrue(session.requests[0]["url"].endswith("/events"))

    def test_header_names_can_be_overridden(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        config = {
            **GATEWAY_CONFIG,
            "client_id_header_name": "X-Gw-Id",
            "client_secret_header_name": "X-Gw-Secret",
        }
        _gateway_client(self.team, session, config=config).fetch_tracking(container_number="HLXU8891233")
        headers = session.requests[0]["headers"]
        self.assertEqual(headers["X-Gw-Id"], CLIENT_ID)
        self.assertEqual(headers["X-Gw-Secret"], CLIENT_SECRET)
        self.assertNotIn("X-IBM-Client-Id", headers)

    def test_missing_credentials_are_a_configuration_error(self):
        """Not an authentication error: nothing was ever sent to be rejected."""
        client = _gateway_client(self.team, FakeSession(), credentials={})
        with self.assertRaises(CarrierConfigurationError) as ctx:
            client.fetch_tracking(container_number="HLXU8891233")
        self.assertIn("client_id", str(ctx.exception))

    def test_secret_without_an_id_is_also_refused(self):
        client = _gateway_client(self.team, FakeSession(), credentials={"client_secret": CLIENT_SECRET})
        with self.assertRaises(CarrierConfigurationError):
            client.fetch_tracking(container_number="HLXU8891233")

    def test_rejected_credentials_are_an_authentication_error(self):
        session = FakeSession([FakeResponse(401), FakeResponse(401)])
        with self.assertRaises(CarrierAuthenticationError):
            _gateway_client(self.team, session).fetch_tracking(container_number="HLXU8891233")

    def test_credentials_never_reach_the_request_log(self):
        _gateway_client(self.team, FakeSession([FakeResponse(200, {"events": []})])).fetch_tracking(
            container_number="HLXU8891233"
        )
        log = IntegrationRequestLog.objects.get(team=self.team)
        logged = log.endpoint + log.error_message
        self.assertNotIn(CLIENT_ID, logged)
        self.assertNotIn(CLIENT_SECRET, logged)

    def test_a_token_url_switches_to_the_oauth_grant(self):
        """A product that does use a grant needs configuration, not new code."""
        from apps.scm.integrations.carriers.oauth import ClientCredentialsAuth

        config = {
            **GATEWAY_CONFIG,
            "auth_style": "oauth2_client_credentials",
            "token_url": "https://example.invalid/oauth/token",
        }
        client = _gateway_client(self.team, FakeSession(), config=config)
        self.assertIsInstance(client._build_auth(), ClientCredentialsAuth)

    def test_oauth_style_without_a_token_url_is_refused(self):
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config({**GATEWAY_CONFIG, "auth_style": "oauth2_client_credentials"})
        self.assertIn("token_url", str(ctx.exception))

    def test_unknown_auth_style_is_refused(self):
        with self.assertRaises(CarrierConfigurationError) as ctx:
            resolve_config({**GATEWAY_CONFIG, "auth_style": "mutual_tls"})
        self.assertIn("client_id_secret_headers", str(ctx.exception))


class HapagLloydTransportTest(TestCase):
    def setUp(self):
        self.team = _team("hapag-transport-team")

    def test_reference_is_sent_as_the_configured_parameter(self):
        session = FakeSession([FakeResponse(200, {"events": []})])
        _client(self.team, session).fetch_tracking(container_number="HLXU1234567")
        self.assertEqual(session.requests[0]["params"], {"equipmentReference": "HLXU1234567"})
        self.assertEqual(session.requests[0]["headers"]["X-Api-Key"], API_KEY)

    def test_404_is_no_data(self):
        with self.assertRaises(CarrierNoDataError):
            _client(self.team, FakeSession([FakeResponse(404)])).fetch_tracking(container_number="HLXU1234567")

    def test_403_is_an_authentication_error(self):
        with self.assertRaises(CarrierAuthenticationError):
            _client(self.team, FakeSession([FakeResponse(403)])).fetch_tracking(container_number="HLXU1234567")

    def test_timeout_is_classified(self):
        with self.assertRaises(CarrierTimeoutError):
            _client(self.team, FakeSession(error=requests.Timeout("t"))).fetch_tracking(container_number="HLXU1234567")

    def test_rate_limit_carries_retry_after(self):
        session = FakeSession([FakeResponse(429, headers={"Retry-After": "900"})])
        with self.assertRaises(CarrierRateLimitError) as ctx:
            _client(self.team, session).fetch_tracking(container_number="HLXU1234567")
        self.assertEqual(ctx.exception.retry_after, 900)

    def test_request_logging_keeps_the_key_out(self):
        _client(self.team, FakeSession([FakeResponse(200, {"events": []})])).fetch_tracking(
            container_number="HLXU1234567"
        )
        log = IntegrationRequestLog.objects.get(team=self.team)
        self.assertEqual(log.endpoint, "/hapag/events")
        self.assertNotIn(API_KEY, log.endpoint + log.error_message)


class HapagLloydParsingTest(TestCase):
    """Normalisation of the Hapag-Lloyd fixture through the shared DCSA parser."""

    def setUp(self):
        self.payload = json.loads((FIXTURES / "hapag_lloyd_tracking_response.json").read_text())
        self.events = HapagLloydParser().parse_tracking_events(self.payload)

    def test_all_fixture_events_are_parsed(self):
        self.assertEqual(len(self.events), 3)

    def test_source_provider_is_hapag_lloyd(self):
        for event in self.events:
            self.assertEqual(event.source_provider, "hapag_lloyd")

    def test_load_and_departure_are_actual(self):
        self.assertTrue(self.events[0].is_actual)
        self.assertTrue(self.events[1].is_actual)

    def test_arrival_is_estimated_not_actual(self):
        """The fixture's arrival is a forecast and must never read as an arrival."""
        arrival = self.events[2]
        self.assertEqual(arrival.event_code, "ARRI")
        self.assertTrue(arrival.is_estimated)
        self.assertFalse(arrival.is_actual)

    def test_container_reference_is_extracted(self):
        self.assertEqual(self.events[0].container_number, "HLXU1234567")

    def test_empty_payload_is_an_empty_list(self):
        self.assertEqual(HapagLloydParser().parse_tracking_events({"events": []}), [])

    def test_discovery_finds_the_fixture_container(self):
        team = _team("hapag-discovery-team")
        session = FakeSession([FakeResponse(200, self.payload)])
        results = _client(team, session).discover_containers(bill_of_lading_number="HLCU-BL-1")
        self.assertEqual([result.container_number for result in results], ["HLXU1234567"])
        self.assertEqual(results[0].carrier_code, "hapag_lloyd")
        self.assertEqual(results[0].carrier_name, "Hapag-Lloyd")


class HapagLloydDcsaShapeTest(TestCase):
    """Normalisation of the shape Hapag-Lloyd actually sends.

    Hapag-Lloyd puts the place in ``eventLocation`` or inside ``transportCall``, and
    names the container in ``references`` on events that have no ``equipmentReference``
    of their own. The flat spelling in the older fixture exercises neither, so this
    reads a response built to the live shape instead.
    """

    @classmethod
    def setUpTestData(cls):
        cls.payload = json.loads((FIXTURES / "hapag_lloyd_dcsa_events.json").read_text())

    def setUp(self):
        self.events = HapagLloydParser().parse_tracking_events(self.payload)
        self.by_code = {event.event_code: event for event in self.events}

    def test_every_event_is_parsed(self):
        self.assertEqual(len(self.events), 5)
        self.assertEqual(
            [event.event_code for event in self.events],
            ["RECE", "GTIN", "DEPA", "STUF", "ARRI"],
        )

    def test_source_provider_is_hapag_lloyd(self):
        for event in self.events:
            self.assertEqual(event.source_provider, "hapag_lloyd")

    def test_every_event_is_tied_to_the_container(self):
        """Including the transport and shipment events, which name it only in references."""
        for event in self.events:
            with self.subTest(code=event.event_code):
                self.assertEqual(event.container_number, "HLXU8891233")

    def test_nested_event_location_is_read(self):
        gate_in = self.by_code["GTIN"]
        self.assertEqual(gate_in.location_name, "Container Terminal Altenwerder")
        self.assertEqual(gate_in.location_unlocode, "DEHAM")
        self.assertEqual(gate_in.latitude, "53.500000")
        self.assertEqual(gate_in.longitude, "9.933333")

    def test_location_inside_the_transport_call_is_read(self):
        departure = self.by_code["DEPA"]
        self.assertEqual(departure.location_name, "Hamburg")
        self.assertEqual(departure.location_unlocode, "DEHAM")
        self.assertEqual(departure.latitude, "53.550000")

    def test_unlocode_survives_a_location_without_coordinates(self):
        arrival = self.by_code["ARRI"]
        self.assertEqual(arrival.location_unlocode, "USNYC")
        self.assertEqual(arrival.location_name, "New York")
        self.assertEqual(arrival.latitude, "")

    def test_vessel_and_voyage_come_from_the_transport_call(self):
        departure = self.by_code["DEPA"]
        self.assertEqual(departure.vessel_name, "HAMBURG EXPRESS")
        self.assertEqual(departure.vessel_imo, "9450648")
        self.assertEqual(departure.voyage_number, "0034W")
        self.assertEqual(departure.transport_mode, "VESSEL")

    def test_import_voyage_is_used_for_the_inbound_leg(self):
        self.assertEqual(self.by_code["ARRI"].voyage_number, "0034E")

    def test_offset_timestamps_are_parsed_with_their_offset(self):
        gate_in = self.by_code["GTIN"]
        self.assertIsNotNone(gate_in.event_datetime)
        self.assertEqual(gate_in.event_datetime.utcoffset().total_seconds(), 7200)
        self.assertEqual(gate_in.event_datetime_timezone, "+02:00")
        self.assertEqual(self.by_code["ARRI"].event_datetime.utcoffset().total_seconds(), -14400)

    def test_actual_and_estimated_are_kept_apart(self):
        self.assertTrue(self.by_code["DEPA"].is_actual)
        self.assertFalse(self.by_code["DEPA"].is_estimated)
        arrival = self.by_code["ARRI"]
        self.assertTrue(arrival.is_estimated)
        self.assertFalse(arrival.is_actual)

    def test_document_references_are_extracted(self):
        gate_in = self.by_code["GTIN"]
        self.assertEqual(gate_in.booking_number, "HLCU-BKG-8891234")
        self.assertEqual(gate_in.bill_of_lading_number, "HLCUDE1889123478")

    def test_shipment_milestone_needs_no_transport_call(self):
        booking = self.by_code["RECE"]
        self.assertEqual(booking.event_type, "SHIPMENT")
        self.assertEqual(booking.location_name, "")
        self.assertEqual(booking.description, "Booking request received")

    def test_carrier_event_ids_are_kept_for_deduplication(self):
        self.assertEqual(self.by_code["GTIN"].raw_event_id, "5f1e8a10-0000-4000-8000-00000000a002")
        self.assertEqual(len({event.raw_event_id for event in self.events}), 5)

    def test_an_unmapped_event_code_is_still_a_usable_event(self):
        """STUF has no internal counterpart; discarding it would lose real evidence."""
        stuffing = self.by_code["STUF"]
        self.assertEqual(stuffing.event_type, "EQUIPMENT")
        self.assertEqual(stuffing.event_code, "STUF")
        self.assertTrue(stuffing.is_actual)
        self.assertEqual(stuffing.description, "Container stuffing completed at inland depot")
        self.assertEqual(stuffing.location_name, "Bremer Binnenterminal Nord")
        self.assertEqual(stuffing.latitude, "53.108000")

    def test_the_raw_event_is_kept_verbatim(self):
        self.assertEqual(self.by_code["GTIN"].raw_payload["ISOEquipmentCode"], "45G1")
