"""Tests for the Hapag-Lloyd carrier, the second user of the shared DCSA pipeline.

These focus on what is genuinely Hapag-Lloyd's own — its identity, capabilities and
fixture — plus enough transport coverage to prove the shared client is actually
wired up. The transport itself is tested once, against Maersk.
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
from apps.scm.integrations.carriers.hapag_lloyd.client import HapagLloydClient, resolve_config
from apps.scm.integrations.carriers.hapag_lloyd.parser import HapagLloydParser
from apps.scm.integrations.carriers.registry import get_carrier_definition
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential, IntegrationRequestLog
from apps.teams.models import Team

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "carriers"
API_KEY = "hapag-secret-key"

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


def _client(team, session=None, config=None) -> HapagLloydClient:
    integration = Integration.objects.create(
        team=team,
        name="Hapag-Lloyd",
        provider_code="hapag_lloyd",
        provider_family=Integration.ProviderFamily.CARRIER,
        api_style=Integration.ApiStyle.DCSA,
        config=config or CONFIG,
        is_active=True,
    )
    set_integration_credentials(integration, IntegrationCredential.AuthType.API_KEY, {"api_key": API_KEY})
    return HapagLloydClient(integration, session=session)


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
