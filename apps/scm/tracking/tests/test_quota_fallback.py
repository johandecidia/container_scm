"""What the resolution chain does when our provider account is out of capacity.

One question, and it is about money rather than about tracking: if Traqo cannot take on
another shipment, may the chain fall through to Vizion — which bills per reference?

The answer is no, and the reason is that the alternative is invisible. Before this, a
402 from the Traqo probe was an ERROR like any other and the chain continued exactly as
it continues past a timeout, so a spent Traqo allowance quietly started buying Vizion
identifications instead. Nobody chose that, and nothing on any page would have said it
was happening.

What is *not* changed matters as much: a Traqo timeout, a 5xx or a rejected key still
falls through to Vizion. None of those means we are out of budget.
"""

from django.test import TestCase, override_settings

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.integrations.carriers import carrier_resolution
from apps.scm.integrations.carriers.carrier_resolution import resolve_carrier_for_container
from apps.scm.integrations.carriers.exceptions import CarrierServerError, CarrierTimeoutError
from apps.scm.integrations.models import Integration
from apps.scm.integrations.traqo import carrier_probe
from apps.scm.integrations.traqo import discovery as traqo_discovery
from apps.scm.integrations.traqo.errors import TraqoPaymentOverdueError, TraqoShipmentLimitReachedError
from apps.scm.integrations.vizion.discovery import NOT_FOUND, VizionCarrierIdentification
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "quota-fallback"}}
TRAQO_LIVE = {"CACHES": _LOCMEM, "TRAQO_ENABLED": True, "TRAQO_API_KEY": "fallback-key"}


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team, owner="BBC", serial="327307") -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit(owner, "U", serial),
        equipment_type=_equipment_type(),
    )


def _probe_error(exc) -> carrier_probe.TraqoCarrierProbeResult:
    """A probe that stopped on ``exc``, shaped as the real probe would report it."""
    return carrier_probe.TraqoCarrierProbeResult(
        container_number="BBCU3273070",
        outcome=carrier_probe.ERROR,
        error_kind=type(exc).__name__,
        error_message=str(exc),
    )


@override_settings(**TRAQO_LIVE)
class QuotaStopsBeforeVizionTest(TestCase):
    """An exhausted provider budget is not answered by spending a different one."""

    def setUp(self):
        self.team = Team.objects.create(name="fallback", slug="quota-fallback")
        self.container = _container(self.team)
        self.vizion_calls: list[str] = []

    def _vizion(self, container_number):
        self.vizion_calls.append(container_number)
        raise AssertionError("Vizion ACI was reached after the provider account was blocked.")

    def _lookup(self, container_number, **_kwargs):
        return traqo_discovery.TraqoCarrierDiscovery(
            container_number=container_number,
            status=traqo_discovery.NOT_FOUND,
            reason="No shipment found for this number.",
        )

    def _resolve(self, probe_result, *, vizion=None):
        return resolve_carrier_for_container(
            team=self.team,
            container=self.container,
            traqo_lookup=self._lookup,
            traqo_probe=lambda **_kwargs: probe_result,
            vizion_identify=vizion or self._vizion,
            use_trusted_knowledge=False,
        )

    def test_a_spent_allowance_stops_the_chain_before_vizion(self):
        resolution = self._resolve(_probe_error(TraqoShipmentLimitReachedError("no slots")))

        self.assertFalse(resolution.resolved)
        self.assertEqual(self.vizion_calls, [])
        step = resolution.step_for(carrier_resolution.STEP_VIZION_ACI)
        self.assertEqual(step.outcome, carrier_resolution.SKIPPED)
        self.assertIn("cannot take on another shipment", step.detail)

    def test_an_unpaid_account_stops_the_chain_before_vizion(self):
        resolution = self._resolve(_probe_error(TraqoPaymentOverdueError("overdue")))

        self.assertFalse(resolution.resolved)
        self.assertEqual(self.vizion_calls, [])
        self.assertEqual(
            resolution.step_for(carrier_resolution.STEP_VIZION_ACI).outcome,
            carrier_resolution.SKIPPED,
        )

    def test_the_direct_sweep_still_runs_because_it_costs_no_third_party_money(self):
        """Quota exhaustion is about Traqo, not about the team's own carrier accounts."""
        resolution = self._resolve(_probe_error(TraqoShipmentLimitReachedError("no slots")))

        # Reached and reported, rather than skipped as Vizion was. Which verdict it
        # returns depends on what this team has connected; that it ran is the point.
        self.assertNotEqual(
            resolution.step_for(carrier_resolution.STEP_DIRECT_API).outcome,
            carrier_resolution.SKIPPED,
        )

    def test_a_direct_hit_after_a_spent_allowance_still_resolves(self):
        """The fallback the policy preserves: somebody we already pay for can answer."""
        from apps.scm.tracking.tests.test_manual_refresh import PAYLOAD, _fake_client, _patch_carriers

        Integration.objects.create(
            team=self.team,
            name="maersk",
            provider_code="maersk",
            provider_family=Integration.ProviderFamily.CARRIER,
            is_active=True,
        )
        from apps.scm.tracking.tests.test_manual_refresh import _normalised_event

        clients = {"maersk": _fake_client("maersk", PAYLOAD)}
        events = {"maersk": [_normalised_event(self.container.container_id)]}

        with _patch_carriers(clients, events):
            resolution = self._resolve(_probe_error(TraqoShipmentLimitReachedError("no slots")))

        self.assertTrue(resolution.resolved)
        self.assertEqual(resolution.carrier_code, "maersk")
        self.assertEqual(self.vizion_calls, [])

    def test_a_timeout_is_not_a_budget_problem_and_still_reaches_vizion(self):
        """Deliberately unchanged: an outage says nothing about our allowance."""
        reached: list[str] = []

        def _vizion(container_number):
            reached.append(container_number)
            return VizionCarrierIdentification(
                container_number=container_number,
                status=NOT_FOUND,
            )

        self._resolve(_probe_error(CarrierTimeoutError("timed out")), vizion=_vizion)

        self.assertEqual(reached, [self.container.container_id])

    def test_a_server_error_is_not_a_budget_problem_either(self):
        reached: list[str] = []

        def _vizion(container_number):
            reached.append(container_number)
            return VizionCarrierIdentification(
                container_number=container_number,
                status=NOT_FOUND,
            )

        self._resolve(_probe_error(CarrierServerError("502")), vizion=_vizion)

        self.assertEqual(reached, [self.container.container_id])

    def test_the_probe_reports_the_distinction_itself(self):
        """So no caller has to match error class names to learn it."""
        self.assertTrue(_probe_error(TraqoShipmentLimitReachedError("x")).account_blocked)
        self.assertTrue(_probe_error(TraqoPaymentOverdueError("x")).account_blocked)
        self.assertFalse(_probe_error(CarrierTimeoutError("x")).account_blocked)
        self.assertFalse(_probe_error(CarrierServerError("x")).account_blocked)
