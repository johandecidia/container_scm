"""Tests for the shared carrier adapter contract: typed errors, reference
validation, and configuration/credential injection via the factory.

No test performs network access: every adapter is either a stub (which raises
CarrierNotImplementedError) or is driven with an injected fake.
"""

from django.test import TestCase

from apps.scm.integrations.carriers.base import (
    BaseCarrierClient,
    CarrierCapability,
    ReferenceKind,
    resolve_tracking_reference,
)
from apps.scm.integrations.carriers.exceptions import (
    CarrierAuthenticationError,
    CarrierConfigurationError,
    CarrierError,
    CarrierInvalidResponseError,
    CarrierNoDataError,
    CarrierNotImplementedError,
    CarrierRateLimitError,
    CarrierServerError,
    CarrierTimeoutError,
    CarrierUnsupportedReferenceError,
)
from apps.scm.integrations.carriers.factory import (
    build_carrier_client,
    build_carrier_parser,
    get_carrier_integration,
)
from apps.scm.integrations.carriers.registry import UnknownCarrierError
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.teams.models import Team

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


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _carrier_integration(team: Team, provider_code: str = "maersk", **kwargs) -> Integration:
    defaults = {
        "name": provider_code,
        "provider_family": Integration.ProviderFamily.CARRIER,
        "config": {"request_timeout_seconds": 15},
        "is_active": True,
    }
    defaults.update(kwargs)
    return Integration.objects.create(team=team, provider_code=provider_code, **defaults)


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class CarrierExceptionHierarchyTest(TestCase):
    """Every carrier error is a CarrierError and classifies itself as transient or not."""

    def test_all_carrier_errors_share_a_base(self):
        for exc_class in (
            CarrierConfigurationError,
            CarrierAuthenticationError,
            CarrierRateLimitError,
            CarrierTimeoutError,
            CarrierServerError,
            CarrierInvalidResponseError,
            CarrierNoDataError,
            CarrierUnsupportedReferenceError,
            CarrierNotImplementedError,
        ):
            with self.subTest(error=exc_class.__name__):
                self.assertTrue(issubclass(exc_class, CarrierError))

    def test_transient_errors_are_marked_transient(self):
        for exc_class in (CarrierRateLimitError, CarrierTimeoutError, CarrierServerError):
            with self.subTest(error=exc_class.__name__):
                self.assertTrue(exc_class.transient)

    def test_permanent_errors_are_not_marked_transient(self):
        for exc_class in (
            CarrierConfigurationError,
            CarrierAuthenticationError,
            CarrierInvalidResponseError,
            CarrierUnsupportedReferenceError,
            CarrierNotImplementedError,
        ):
            with self.subTest(error=exc_class.__name__):
                self.assertFalse(exc_class.transient)

    def test_no_data_is_not_transient_and_not_an_auth_error(self):
        """No data is a valid outcome — it must not be confused with a failure type."""
        self.assertFalse(CarrierNoDataError.transient)
        self.assertFalse(issubclass(CarrierNoDataError, CarrierAuthenticationError))

    def test_rate_limit_carries_retry_after(self):
        exc = CarrierRateLimitError("429", provider_code="maersk", retry_after=42)
        self.assertEqual(exc.retry_after, 42)
        self.assertEqual(exc.provider_code, "maersk")

    def test_server_error_carries_status_code(self):
        self.assertEqual(CarrierServerError("boom", status_code=503).status_code, 503)

    def test_not_implemented_error_is_also_a_python_not_implemented_error(self):
        """Stub detection must keep working with the standard Python contract."""
        self.assertTrue(issubclass(CarrierNotImplementedError, NotImplementedError))


# ---------------------------------------------------------------------------
# Reference validation
# ---------------------------------------------------------------------------


_FULL_CAPABILITY = CarrierCapability(
    supports_tracking_by_container=True,
    supports_tracking_by_bl=True,
    supports_tracking_by_booking=True,
)


class ResolveTrackingReferenceTest(TestCase):
    """Exactly one supported reference must be supplied per call."""

    def test_single_container_reference_is_resolved(self):
        ref = resolve_tracking_reference(capabilities=_FULL_CAPABILITY, container_number="MRKU1234567")
        self.assertEqual(ref.kind, ReferenceKind.CONTAINER_NUMBER)
        self.assertEqual(ref.value, "MRKU1234567")

    def test_reference_value_is_stripped(self):
        ref = resolve_tracking_reference(capabilities=_FULL_CAPABILITY, booking_number="  BKG-1 ")
        self.assertEqual(ref.value, "BKG-1")

    def test_no_reference_is_rejected(self):
        with self.assertRaises(CarrierUnsupportedReferenceError):
            resolve_tracking_reference(capabilities=_FULL_CAPABILITY)

    def test_blank_reference_counts_as_absent(self):
        with self.assertRaises(CarrierUnsupportedReferenceError):
            resolve_tracking_reference(capabilities=_FULL_CAPABILITY, container_number="   ")

    def test_two_references_are_rejected(self):
        with self.assertRaises(CarrierUnsupportedReferenceError) as ctx:
            resolve_tracking_reference(
                capabilities=_FULL_CAPABILITY,
                container_number="MRKU1234567",
                booking_number="BKG-1",
            )
        self.assertIn("got 2", str(ctx.exception))

    def test_unsupported_reference_kind_is_rejected(self):
        capability = CarrierCapability(supports_tracking_by_container=True)
        with self.assertRaises(CarrierUnsupportedReferenceError):
            resolve_tracking_reference(
                capabilities=capability,
                provider_code="msc",
                shipment_reference="SHP-1",
            )

    def test_client_resolve_reference_uses_own_capabilities(self):
        class OnlyBookingClient(BaseCarrierClient):
            provider_code = "only-booking"
            capabilities = CarrierCapability(supports_tracking_by_booking=True)

        client = OnlyBookingClient()
        self.assertEqual(client.resolve_reference(booking_number="BKG-9").kind, ReferenceKind.BOOKING_NUMBER)
        with self.assertRaises(CarrierUnsupportedReferenceError):
            client.resolve_reference(container_number="MRKU1234567")


# ---------------------------------------------------------------------------
# Base client contract
# ---------------------------------------------------------------------------


class BaseCarrierClientContractTest(TestCase):
    """The base client is safe to instantiate and refuses to work unconfigured."""

    def test_capabilities_is_a_capability_instance_on_the_base_class(self):
        """Regression: capabilities was a dataclasses.Field object, not a CarrierCapability."""
        self.assertIsInstance(BaseCarrierClient.capabilities, CarrierCapability)
        self.assertIsInstance(BaseCarrierClient().capabilities, CarrierCapability)

    def test_unconfigured_client_reports_not_configured(self):
        client = BaseCarrierClient()
        self.assertFalse(client.is_configured)
        self.assertEqual(client.config, {})
        self.assertEqual(client.credentials, {})

    def test_require_integration_raises_configuration_error(self):
        with self.assertRaises(CarrierConfigurationError):
            BaseCarrierClient().require_integration()

    def test_all_contract_methods_raise_not_implemented_on_base(self):
        client = BaseCarrierClient()
        with self.assertRaises(CarrierNotImplementedError):
            client.test_connection()
        with self.assertRaises(CarrierNotImplementedError):
            client.fetch_tracking(container_number="MRKU1234567")
        with self.assertRaises(CarrierNotImplementedError):
            client.discover_containers(booking_number="BKG-1")

    def test_fetch_tracking_accepts_every_contract_reference(self):
        """All four contract reference kinds must be accepted as keyword arguments."""
        client = BaseCarrierClient()
        for kwargs in (
            {"container_number": "MRKU1234567"},
            {"bill_of_lading_number": "BL-1"},
            {"booking_number": "BKG-1"},
            {"shipment_reference": "SHP-1"},
        ):
            with self.subTest(reference=next(iter(kwargs))), self.assertRaises(CarrierNotImplementedError):
                client.fetch_tracking(**kwargs)


# ---------------------------------------------------------------------------
# Configuration / credential injection
# ---------------------------------------------------------------------------


class CarrierCredentialInjectionTest(TestCase):
    """Credentials and config arrive through the constructor, scoped to one team."""

    def setUp(self):
        self.team = _team("carrier-cred-team")
        self.integration = _carrier_integration(self.team, "maersk", config={"customer_code": "ACME"})
        set_integration_credentials(
            self.integration,
            IntegrationCredential.AuthType.API_KEY,
            {"consumer_key": "secret-key-value"},
        )

    def test_client_exposes_integration_config(self):
        client = build_carrier_client("maersk", integration=self.integration)
        self.assertEqual(client.config["customer_code"], "ACME")

    def test_client_resolves_credentials_from_the_credential_service(self):
        client = build_carrier_client("maersk", integration=self.integration)
        self.assertEqual(client.credentials["consumer_key"], "secret-key-value")

    def test_explicitly_injected_credentials_win(self):
        client = build_carrier_client("maersk", integration=self.integration)
        client._credentials = {"consumer_key": "injected"}
        self.assertEqual(client.credentials["consumer_key"], "injected")

    def test_client_for_other_team_does_not_see_credentials(self):
        """A team without its own integration gets an unconfigured client, not another team's."""
        other = _team("carrier-cred-other-team")
        client = build_carrier_client("maersk", team=other)
        self.assertFalse(client.is_configured)
        self.assertEqual(client.credentials, {})


class CarrierFactoryTest(TestCase):
    """The factory resolves the team's own active carrier integration."""

    def setUp(self):
        self.team = _team("carrier-factory-team")

    def test_resolves_active_carrier_integration_for_team(self):
        integration = _carrier_integration(self.team, "maersk")
        self.assertEqual(get_carrier_integration(self.team, "maersk"), integration)

    def test_ignores_inactive_integration(self):
        _carrier_integration(self.team, "maersk", is_active=False)
        self.assertIsNone(get_carrier_integration(self.team, "maersk"))

    def test_ignores_business_system_integration_with_same_provider_code(self):
        _carrier_integration(
            self.team,
            "maersk",
            provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        )
        self.assertIsNone(get_carrier_integration(self.team, "maersk"))

    def test_build_client_injects_resolved_integration(self):
        integration = _carrier_integration(self.team, "maersk")
        client = build_carrier_client("maersk", team=self.team)
        self.assertEqual(client.integration, integration)
        self.assertTrue(client.is_configured)

    def test_build_client_without_integration_is_unconfigured_but_safe(self):
        client = build_carrier_client("maersk", team=self.team)
        self.assertFalse(client.is_configured)

    def test_require_integration_flag_raises_when_not_configured(self):
        with self.assertRaises(CarrierConfigurationError):
            build_carrier_client("maersk", team=self.team, require_integration=True)

    def test_unknown_carrier_raises_unknown_carrier_error(self):
        with self.assertRaises(UnknownCarrierError):
            build_carrier_client("not-a-carrier", team=self.team)

    def test_build_parser_returns_registered_parser(self):
        parser = build_carrier_parser("maersk")
        self.assertEqual(parser.provider_code, "maersk")

    def test_every_registered_carrier_can_be_built(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                client = build_carrier_client(code, team=self.team)
                self.assertEqual(client.provider_code, code)
                self.assertIsInstance(client.capabilities, CarrierCapability)


class StubCarriersRaiseTypedNotImplementedTest(TestCase):
    """Unimplemented carriers raise the typed stub error, never an empty result."""

    def test_stub_clients_raise_carrier_not_implemented(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code):
                client = build_carrier_client(code)
                with self.assertRaises(CarrierNotImplementedError):
                    client.fetch_tracking(container_number="MRKU1234567")

    def test_stub_parsers_raise_carrier_not_implemented(self):
        for code in ALL_CARRIER_CODES:
            with self.subTest(carrier=code), self.assertRaises(CarrierNotImplementedError):
                build_carrier_parser(code).parse_tracking_events({"events": []})
