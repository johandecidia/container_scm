"""Tests for choosing which provider to fetch a known carrier's tracking through.

Routing is a pure decision, so these tests assert only on the returned route — nothing is
written and nothing is called. What they pin down is the priority (direct first, Traqo
second, nothing third) and the two things that priority is easy to get subtly wrong:

*A direct adapter that cannot be used is not a direct route.* Registered but not
connected, or connected but unable to answer by container number, both have to fall
through to the aggregator rather than routing to a call that cannot succeed.

*An aggregator that cannot cover the carrier is not a route either.* Traqo requires a
sealine, and a carrier it publishes none for has to come back as a clean
NOT_CONFIGURED rather than a Traqo call that will be refused.
"""

from django.test import TestCase, override_settings

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.models import Integration
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.tracking import provider_routing
from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
from apps.scm.tracking.models import CarrierSource
from apps.scm.tracking.provider_routing import (
    AGGREGATOR,
    CARRIER_UNKNOWN,
    DIRECT,
    DIRECT_PROVIDER_AVAILABLE,
    DIRECT_PROVIDER_NOT_CONNECTED,
    DIRECT_PROVIDER_UNAVAILABLE,
    NO_PROVIDER_AVAILABLE,
    NONE,
    get_route_for_subscription,
    resolve_tracking_route,
)
from apps.teams.models import Team

TRAQO_LIVE = {"TRAQO_ENABLED": True, "TRAQO_API_KEY": "routing-key"}
TRAQO_OFF = {"TRAQO_ENABLED": False, "TRAQO_API_KEY": ""}


def _equipment_type():
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


class ProviderRoutingTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="routing", slug="routing")

    def connect(self, provider_code):
        return Integration.objects.create(
            team=self.team,
            name=provider_code,
            provider_code=provider_code,
            provider_family=Integration.ProviderFamily.CARRIER,
            is_active=True,
        )

    def route(self, carrier_code):
        return resolve_tracking_route(team=self.team, carrier_code=carrier_code)

    # -- direct first ---------------------------------------------------------

    @override_settings(**TRAQO_LIVE)
    def test_a_connected_carrier_is_routed_to_its_own_api(self):
        """Direct wins even where Traqo would also work: it is the primary record."""
        self.connect("maersk")

        route = self.route("maersk")

        self.assertEqual(route.provider_code, "maersk")
        self.assertEqual(route.route_type, DIRECT)
        self.assertEqual(route.reason, DIRECT_PROVIDER_AVAILABLE)
        self.assertTrue(route.available)
        self.assertTrue(route.is_direct)

    @override_settings(**TRAQO_LIVE)
    def test_a_direct_route_needs_no_provider_reference(self):
        """A carrier answers about its own container number; nothing else is required."""
        self.connect("maersk")

        self.assertEqual(self.route("maersk").provider_reference, "")

    @override_settings(**TRAQO_LIVE)
    def test_traqo_is_still_reported_as_the_alternative(self):
        self.connect("maersk")

        self.assertEqual(self.route("maersk").alternatives, ("maersk", TRAQO_PROVIDER_CODE))

    # -- Traqo second ---------------------------------------------------------

    @override_settings(**TRAQO_LIVE)
    def test_an_unconnected_carrier_falls_through_to_traqo(self):
        """The acceptance case's routing decision: ONE is not connected, Traqo is."""
        route = self.route("one")

        self.assertEqual(route.provider_code, TRAQO_PROVIDER_CODE)
        self.assertEqual(route.route_type, AGGREGATOR)
        self.assertEqual(route.reason, DIRECT_PROVIDER_NOT_CONNECTED)
        self.assertTrue(route.is_aggregator)

    @override_settings(**TRAQO_LIVE)
    def test_a_traqo_route_carries_the_sealine_to_ask_with(self):
        """Without it Traqo cannot be asked, and would be asked about the wrong carrier."""
        self.assertEqual(self.route("one").provider_reference, "ONEY")

    @override_settings(**TRAQO_LIVE)
    def test_the_carrier_is_named_on_an_aggregator_route(self):
        """Routing to Traqo must not lose who the carrier is — that is the whole point."""
        route = self.route("one")

        self.assertEqual(route.carrier_code, "one")
        self.assertIn("ONE", route.carrier_name)

    @override_settings(**TRAQO_LIVE)
    def test_a_carrier_with_no_pull_support_falls_through_even_when_connected(self):
        """Evergreen is registered and cannot be pulled from; connecting it changes nothing."""
        self.connect("evergreen")

        route = self.route("evergreen")

        # Traqo publishes no sealine for Evergreen either, so this is the clean
        # nothing-available answer rather than a Traqo call that would be refused.
        self.assertEqual(route.route_type, NONE)
        self.assertEqual(route.reason, NO_PROVIDER_AVAILABLE)

    @override_settings(**TRAQO_LIVE)
    def test_a_pullable_carrier_that_is_not_connected_says_so_specifically(self):
        """Not connected is somebody's setting; no adapter is ours to build."""
        self.assertEqual(self.route("zim").reason, DIRECT_PROVIDER_UNAVAILABLE)

    # -- nothing third --------------------------------------------------------

    @override_settings(**TRAQO_OFF)
    def test_no_direct_and_no_traqo_is_a_clean_not_configured(self):
        route = self.route("one")

        self.assertFalse(route.available)
        self.assertEqual(route.route_type, NONE)
        self.assertEqual(route.reason, NO_PROVIDER_AVAILABLE)
        self.assertEqual(route.carrier_code, "one", "the carrier is still known and still reported")

    @override_settings(**TRAQO_OFF)
    def test_traqo_without_a_key_is_not_a_route(self):
        with override_settings(TRAQO_ENABLED=True, TRAQO_API_KEY=""):
            self.assertFalse(self.route("one").available)

    @override_settings(**TRAQO_LIVE)
    def test_an_unknown_carrier_has_nothing_to_route(self):
        route = self.route("Regional Feeder Line")

        self.assertFalse(route.available)
        self.assertEqual(route.reason, CARRIER_UNKNOWN)

    @override_settings(**TRAQO_LIVE)
    def test_an_empty_carrier_is_not_an_error(self):
        route = self.route("")

        self.assertFalse(route.available)
        self.assertEqual(route.reason, CARRIER_UNKNOWN)

    # -- Vizion is deliberately not a tracking route -------------------------

    @override_settings(**TRAQO_OFF, VIZION_ENABLED=True, VIZION_API_KEY="vz")
    def test_vizion_is_never_routed_to_for_tracking(self):
        """It can track. Routing to it would turn every gap into a paid reference."""
        route = self.route("one")

        self.assertNotEqual(route.provider_code, "vizion")
        self.assertFalse(route.available)

    # -- free-text carrier input ---------------------------------------------

    @override_settings(**TRAQO_LIVE)
    def test_a_carrier_name_is_resolved_before_routing(self):
        self.connect("cma_cgm")

        self.assertEqual(self.route("CMA CGM").provider_code, "cma_cgm")


class RouteForExistingSubscriptionTest(TestCase):
    """Reading a watch's own history back as the route it represents."""

    def setUp(self):
        self.team = Team.objects.create(name="routing-read", slug="routing-read")
        self.container = Container.objects.create(
            team=self.team,
            owner_code="BBC",
            category_id="U",
            serial_number="327307",
            check_digit=0,
            equipment_type=_equipment_type(),
        )

    def test_an_aggregator_watch_reads_as_an_aggregator_route(self):
        subscription = get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=TRAQO_PROVIDER_CODE,
            provider_name="Traqo Ocean",
            carrier_code="one",
            carrier_name="ONE (Ocean Network Express)",
            carrier_source=CarrierSource.VIZION_ACI,
            provider_reference="ONEY",
        )

        route = get_route_for_subscription(subscription)

        self.assertEqual(route.carrier_code, "one")
        self.assertEqual(route.provider_code, TRAQO_PROVIDER_CODE)
        self.assertEqual(route.route_type, AGGREGATOR)
        self.assertEqual(route.provider_reference, "ONEY")

    def test_a_direct_watch_reads_as_a_direct_route(self):
        subscription = get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code="maersk",
            provider_name="Maersk",
            carrier_code="maersk",
            carrier_name="Maersk",
            carrier_source=CarrierSource.DIRECT_API,
        )

        route = get_route_for_subscription(subscription)

        self.assertEqual(route.route_type, DIRECT)
        self.assertEqual(route.reason, provider_routing.DIRECT_PROVIDER_AVAILABLE)

    def test_a_watch_with_no_carrier_says_the_carrier_is_unknown(self):
        subscription = get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=TRAQO_PROVIDER_CODE,
            provider_name="Traqo Ocean",
        )

        route = get_route_for_subscription(subscription)

        self.assertEqual(route.reason, CARRIER_UNKNOWN)
        self.assertEqual(route.carrier_code, "")
        self.assertEqual(route.provider_code, TRAQO_PROVIDER_CODE)
