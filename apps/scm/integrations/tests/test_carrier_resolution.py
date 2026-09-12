"""Tests for the carrier-resolution chain: who is moving the box, and at what cost.

Two things are being pinned down here, and the second is as important as the first.

*Correctness.* A carrier is resolved from the strongest evidence available, and the
provenance recorded says which evidence that was — a shipment's carrier field and a paid
Vizion identification must never come back looking the same.

*Cost.* Each step short-circuits the ones below it, so the tests assert on **what was not
called** at least as often as on what came back. Vizion creates a billable reference on
every call; a regression that let it run after Traqo had already answered would be
invisible in an assertion about the returned carrier and expensive in production.

Nothing here touches a live API. All three aggregator calls — Traqo's free lookup, Traqo
candidate probing and Vizion's ACI — are injected, and the direct carriers go through the
same fake clients the discovery-sweep tests drive.
"""

from django.test import TestCase, override_settings

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.carriers import carrier_resolution
from apps.scm.integrations.carriers.carrier_resolution import (
    STEP_DIRECT_API,
    STEP_TRAQO_LOOKUP,
    STEP_TRAQO_PROBE,
    STEP_TRUSTED,
    STEP_VIZION_ACI,
    get_trusted_carrier_for_container,
    resolve_carrier_for_container,
)
from apps.scm.integrations.models import Integration
from apps.scm.integrations.traqo import carrier_probe as traqo_probe_module
from apps.scm.integrations.traqo import discovery as traqo_discovery
from apps.scm.integrations.vizion import discovery as vizion_discovery
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
from apps.scm.tracking.models import CarrierSource

# Reused rather than re-invented: these are the carrier test doubles the discovery sweep
# tests already drive the factory with.
from apps.scm.tracking.tests.test_manual_refresh import _fake_client, _normalised_event, _patch_carriers
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "resolution"}}

# The acceptance container. Its owner prefix, BBCU, belongs to no carrier in the
# registry, so nothing in the chain can shortcut to an answer from the number's shape.
ACCEPTANCE_NUMBER = "BBCU3273070"


def _equipment_type():
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _acceptance_container(team):
    return Container.objects.create(
        team=team,
        owner_code="BBC",
        category_id="U",
        serial_number="327307",
        check_digit=0,
        equipment_type=_equipment_type(),
    )


def _carrier_integration(team, provider_code):
    return Integration.objects.create(
        team=team,
        name=provider_code,
        provider_code=provider_code,
        provider_family=Integration.ProviderFamily.CARRIER,
        is_active=True,
    )


class Spy:
    """A stand-in for one aggregator call that records whether it was made at all.

    ``calls`` is the assertion that matters for the cost tests: an aggregator step that
    should have been short-circuited leaves it empty.
    """

    def __init__(self, result):
        self.result = result
        self.calls: list[str] = []

    def __call__(self, container_number):
        self.calls.append(container_number)
        return self.result


class ProbeSpy:
    """A stand-in for the Traqo candidate probe, which resolution calls with keywords.

    Records the arguments as well as the fact of the call: the probe's cost depends on
    what it is told to try, so "was it called" and "what with" are both assertions the
    cost tests make.
    """

    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    @property
    def container_numbers(self) -> list[str]:
        return [call["container_number"] for call in self.calls]


def _probe_event():
    """One mapped Traqo event, which is what makes a probe evidence rather than an echo."""
    from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent

    return NormalisedTrackingEvent(
        event_type="EQUIPMENT",
        event_code="GTIN",
        container_number=ACCEPTANCE_NUMBER,
        source_provider="traqo",
    )


def _probe_found(carrier_code="one", sealine="ONEY", events=1):
    return traqo_probe_module.TraqoCarrierProbeResult(
        container_number=ACCEPTANCE_NUMBER,
        outcome=traqo_probe_module.FOUND,
        carrier_code=carrier_code,
        carrier_name="Ocean Network Express (ONE)",
        sealine=sealine,
        events=tuple(_probe_event() for _ in range(events)),
        raw_payload={"success": True, "data": {"sealine": sealine, "reference_number": ACCEPTANCE_NUMBER}},
        attempts=(
            traqo_probe_module.TraqoProbeAttempt(
                carrier_code=carrier_code, sealine=sealine, outcome=traqo_probe_module.FOUND
            ),
        ),
    )


def _probe_not_found(asked=("ONEY", "MAEU")):
    return traqo_probe_module.TraqoCarrierProbeResult(
        container_number=ACCEPTANCE_NUMBER,
        outcome=traqo_probe_module.NOT_FOUND,
        attempts=tuple(
            traqo_probe_module.TraqoProbeAttempt(carrier_code="", sealine=sealine, outcome=traqo_probe_module.NOT_FOUND)
            for sealine in asked
        ),
    )


def _probe_error(kind="CarrierAuthenticationError"):
    return traqo_probe_module.TraqoCarrierProbeResult(
        container_number=ACCEPTANCE_NUMBER,
        outcome=traqo_probe_module.ERROR,
        error_kind=kind,
        error_message="401",
        attempts=(
            traqo_probe_module.TraqoProbeAttempt(
                carrier_code="one", sealine="ONEY", outcome=traqo_probe_module.ERROR, error_kind=kind
            ),
        ),
    )


def _probe_not_configured():
    return traqo_probe_module.TraqoCarrierProbeResult(
        container_number=ACCEPTANCE_NUMBER,
        outcome=traqo_probe_module.NOT_CONFIGURED,
        error_kind="not_configured",
    )


def _traqo_found(carrier_code="one", scac="ONEY"):
    return traqo_discovery.TraqoCarrierDiscovery(
        container_number=ACCEPTANCE_NUMBER,
        status=traqo_discovery.FOUND,
        carrier_code=carrier_code,
        carrier_name="ONE (Ocean Network Express)",
        scac=scac,
        confidence="HIGH",
    )


def _traqo_not_found():
    return traqo_discovery.TraqoCarrierDiscovery(
        container_number=ACCEPTANCE_NUMBER,
        status=traqo_discovery.NOT_FOUND,
        reason="No shipment matched.",
    )


def _traqo_error(kind="CarrierTimeoutError"):
    return traqo_discovery.TraqoCarrierDiscovery(
        container_number=ACCEPTANCE_NUMBER,
        status=traqo_discovery.ERROR,
        error_kind=kind,
        error_message="timed out",
    )


def _traqo_not_configured():
    return traqo_discovery.TraqoCarrierDiscovery(
        container_number=ACCEPTANCE_NUMBER,
        status=traqo_discovery.NOT_CONFIGURED,
        error_kind="not_configured",
    )


def _vizion_found(carrier_code="one", scac="ONEY", reference_id="ref-1"):
    return vizion_discovery.VizionCarrierIdentification(
        container_number=ACCEPTANCE_NUMBER,
        status=vizion_discovery.FOUND,
        carrier_code=carrier_code,
        carrier_name="ONE (Ocean Network Express)",
        scac=scac,
        reference_id=reference_id,
        aci_state="IDENTIFIED",
    )


def _vizion_not_found(reference_id="ref-2"):
    return vizion_discovery.VizionCarrierIdentification(
        container_number=ACCEPTANCE_NUMBER,
        status=vizion_discovery.NOT_FOUND,
        reference_id=reference_id,
        aci_state="NOT_FOUND",
    )


def _vizion_unavailable():
    return vizion_discovery.VizionCarrierIdentification(
        container_number=ACCEPTANCE_NUMBER,
        status=vizion_discovery.ERROR,
        error_kind="CarrierAuthenticationError",
        error_message="401",
    )


@override_settings(CACHES=_LOCMEM)
class TrustedCarrierKnowledgeTest(TestCase):
    """What counts as already knowing the carrier, and in what order."""

    def setUp(self):
        self.team = Team.objects.create(name="trusted", slug="trusted")
        self.container = _acceptance_container(self.team)

    def test_nothing_recorded_is_reported_as_nothing(self):
        self.assertEqual(get_trusted_carrier_for_container(self.team, self.container), ("", "", ""))

    def test_a_verified_subscription_is_the_strongest_evidence(self):
        get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code="traqo",
            provider_name="Traqo Ocean",
            carrier_code="one",
            carrier_name="ONE (Ocean Network Express)",
            carrier_source=CarrierSource.VIZION_ACI,
        )
        code, name, source = get_trusted_carrier_for_container(self.team, self.container)

        self.assertEqual(code, "one")
        self.assertEqual(source, CarrierSource.EXISTING_VERIFIED_SOURCE)
        self.assertIn("ONE", name)

    def test_a_verified_subscription_without_a_carrier_is_not_evidence(self):
        """A Traqo watch whose carrier was never established knows nothing to reuse."""
        get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code="traqo",
            provider_name="Traqo Ocean",
        )
        self.assertEqual(get_trusted_carrier_for_container(self.team, self.container), ("", "", ""))

    def test_a_planned_container_carrier_outranks_the_shipments(self):
        from apps.scm.containers.models import PlannedContainer

        PlannedContainer.objects.create(team=self.team, container_number=self.container.container_id, carrier="cosco")
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-1", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        code, _name, source = get_trusted_carrier_for_container(self.team, self.container)

        self.assertEqual(code, "cosco")
        self.assertEqual(source, CarrierSource.PLANNED_CONTAINER)

    def test_the_shipment_carrier_is_used_when_it_is_all_there_is(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-2", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        code, _name, source = get_trusted_carrier_for_container(self.team, self.container)

        self.assertEqual(code, "maersk")
        self.assertEqual(source, CarrierSource.SHIPMENT)

    def test_an_unrecognised_shipment_carrier_is_not_substituted(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-3", carrier="Regional Feeder")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        self.assertEqual(get_trusted_carrier_for_container(self.team, self.container), ("", "", ""))


@override_settings(CACHES=_LOCMEM)
class ResolutionOrderTest(TestCase):
    """The chain's order, and what each step spends when an earlier one has answered."""

    def setUp(self):
        self.team = Team.objects.create(name="chain", slug="chain")
        self.container = _acceptance_container(self.team)

    def resolve(self, *, traqo, vizion, probe=None, direct_behaviour=None, direct_events=None, **kwargs):
        """Run the chain with every provider call spied on and the direct carriers faked.

        ``probe`` defaults to "Traqo has nothing under any candidate", which is what the
        chain below the probe is being tested against. :class:`TraqoProbeStepTest` drives
        the probe itself.
        """
        traqo_spy = Spy(traqo)
        probe_spy = ProbeSpy(probe if probe is not None else _probe_not_found())
        vizion_spy = Spy(vizion)
        behaviour = direct_behaviour or {}
        clients = {code: _fake_client(code, value) for code, value in behaviour.items()}
        with _patch_carriers(clients, direct_events or {}):
            resolution = resolve_carrier_for_container(
                team=self.team,
                container=self.container,
                clients=clients,
                traqo_lookup=traqo_spy,
                traqo_probe=probe_spy,
                vizion_identify=vizion_spy,
                **kwargs,
            )
        self.probe_spy = probe_spy
        return resolution, traqo_spy, vizion_spy, clients

    # -- 1. trusted knowledge -------------------------------------------------

    def test_a_trusted_carrier_reaches_no_provider_at_all(self):
        """The whole point of putting free knowledge first: nobody is called."""
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-T", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        resolution, traqo, vizion, clients = self.resolve(
            traqo=_traqo_found(), vizion=_vizion_found(), direct_behaviour={"maersk": {"events": []}}
        )

        self.assertEqual(resolution.carrier_code, "maersk")
        self.assertEqual(resolution.source, CarrierSource.SHIPMENT)
        self.assertEqual(traqo.calls, [])
        self.assertEqual(self.probe_spy.calls, [])
        self.assertEqual(vizion.calls, [])
        self.assertEqual(clients["maersk"].calls, [])

    def test_a_shipment_carrier_is_not_reported_as_verified(self):
        """Somebody chose it. Nothing has confirmed it can tell us where the box is."""
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-V", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        resolution, *_ = self.resolve(traqo=_traqo_found(), vizion=_vizion_found())

        self.assertFalse(resolution.verified)

    # -- 2. Traqo's free lookup ----------------------------------------------

    def test_traqo_found_short_circuits_direct_discovery_and_vizion(self):
        resolution, traqo, vizion, clients = self.resolve(
            traqo=_traqo_found(),
            vizion=_vizion_found(),
            direct_behaviour={"maersk": {"events": [{"id": 1}]}},
            direct_events={"maersk": [_normalised_event(ACCEPTANCE_NUMBER)]},
        )

        self.assertEqual(resolution.carrier_code, "one")
        self.assertEqual(resolution.source, CarrierSource.TRAQO_LOOKUP)
        self.assertEqual(traqo.calls, [ACCEPTANCE_NUMBER])
        self.assertEqual(self.probe_spy.calls, [], "the free lookup had answered; probing spends shipment calls")
        self.assertEqual(vizion.calls, [])
        self.assertEqual(clients["maersk"].calls, [], "direct discovery ran after Traqo had already answered")
        self.assertIsNone(resolution.step_for(STEP_TRAQO_PROBE))
        self.assertIsNone(resolution.step_for(STEP_DIRECT_API))

    def test_a_traqo_lookup_is_not_treated_as_proof(self):
        resolution, *_ = self.resolve(traqo=_traqo_found(), vizion=_vizion_found())

        self.assertFalse(resolution.verified, "a lookup has not seen this container's events")

    def test_a_scac_no_registered_carrier_claims_is_not_a_resolution(self):
        """OOLU is a carrier Traqo covers and this system has no adapter for."""
        lookup = traqo_discovery.TraqoCarrierDiscovery(
            container_number=ACCEPTANCE_NUMBER,
            status=traqo_discovery.FOUND,
            carrier_code="",
            scac="OOLU",
        )
        resolution, _traqo, vizion, _clients = self.resolve(traqo=lookup, vizion=_vizion_found())

        # Traqo answered, so the step is NOT_FOUND rather than an error — and the chain
        # carries on to the steps that might name something actionable.
        self.assertEqual(resolution.step_for(STEP_TRAQO_LOOKUP).outcome, carrier_resolution.NOT_FOUND)
        self.assertEqual(vizion.calls, [ACCEPTANCE_NUMBER])

    # -- 3. Traqo's container endpoint, asked about likely carriers ----------

    def test_a_lookup_that_names_nobody_lets_the_probe_run(self):
        resolution, traqo, _vizion, _clients = self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_found(),
            probe=_probe_found(),
        )

        self.assertEqual(traqo.calls, [ACCEPTANCE_NUMBER])
        self.assertEqual(self.probe_spy.container_numbers, [ACCEPTANCE_NUMBER])
        self.assertEqual(resolution.carrier_code, "one")
        self.assertEqual(resolution.source, CarrierSource.TRAQO_PROBE)

    def test_a_probe_hit_is_proof_in_a_way_a_lookup_is_not(self):
        """Traqo returned this box's own events. That is evidence, not a name."""
        resolution, *_ = self.resolve(traqo=_traqo_not_found(), vizion=_vizion_found(), probe=_probe_found())

        self.assertTrue(resolution.verified)

    def test_a_probe_hit_carries_its_payload_out_for_the_caller_to_store(self):
        """So activation stores what Traqo already sent instead of asking Traqo again."""
        resolution, *_ = self.resolve(traqo=_traqo_not_found(), vizion=_vizion_found(), probe=_probe_found(events=3))

        self.assertTrue(resolution.has_tracking_payload)
        self.assertEqual(len(resolution.events), 3)
        self.assertEqual(resolution.raw_payload["data"]["reference_number"], ACCEPTANCE_NUMBER)

    def test_a_probe_hit_says_who_returned_the_payload_and_under_what_handle(self):
        """Provider-neutral reuse: activation must not have to guess which step answered."""
        resolution, *_ = self.resolve(traqo=_traqo_not_found(), vizion=_vizion_found(), probe=_probe_found())

        self.assertEqual(resolution.tracking_provider_code, "traqo")
        self.assertEqual(resolution.provider_reference, "ONEY")
        self.assertIsNone(resolution.discovery, "a probe hit is not a direct sweep's payload")

    def test_a_probe_that_found_nothing_is_told_apart_from_one_that_failed(self):
        _carrier_integration(self.team, "cosco")
        not_found, *_ = self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_not_found(),
            probe=_probe_not_found(),
            direct_behaviour={"cosco": {"events": []}},
        )
        errored, *_ = self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_not_found(),
            probe=_probe_error(),
            direct_behaviour={"cosco": {"events": []}},
        )

        self.assertEqual(not_found.step_for(STEP_TRAQO_PROBE).outcome, carrier_resolution.NOT_FOUND)
        self.assertEqual(errored.step_for(STEP_TRAQO_PROBE).outcome, carrier_resolution.ERROR)

    def test_a_probe_failure_does_not_stop_the_chain(self):
        """A rejected Traqo key says nothing about the box, and must not end the search."""
        _carrier_integration(self.team, "cosco")
        resolution, _traqo, vizion, clients = self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_found(),
            probe=_probe_error(),
            direct_behaviour={"cosco": {"events": []}},
        )

        self.assertEqual(clients["cosco"].calls, [ACCEPTANCE_NUMBER], "the chain stopped at a Traqo probe failure")
        self.assertEqual(vizion.calls, [ACCEPTANCE_NUMBER])
        self.assertEqual(resolution.carrier_code, "one")

    def test_traqo_not_being_configured_for_probing_is_its_own_outcome(self):
        resolution, *_ = self.resolve(
            traqo=_traqo_not_configured(), vizion=_vizion_not_found(), probe=_probe_not_configured()
        )

        self.assertEqual(resolution.step_for(STEP_TRAQO_PROBE).outcome, carrier_resolution.NOT_CONFIGURED)

    def test_the_probe_is_told_which_carriers_to_try_first(self):
        """The cap makes the order matter: a preference has to reach the probe to be used."""
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-P", carrier="Maersk")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_found(),
            probe=_probe_found(),
            preferred_carrier_codes=["zim"],
            exclude_carrier_codes=frozenset({"cosco"}),
            use_trusted_knowledge=False,
        )

        call = self.probe_spy.calls[0]
        self.assertEqual(call["preferred_carrier_codes"], ("zim",))
        self.assertEqual(call["exclude_carrier_codes"], frozenset({"cosco"}))

    def test_probing_can_be_turned_off_leaving_the_old_chain(self):
        """The chain as it behaved before this step existed, for a caller on a tighter budget."""
        _carrier_integration(self.team, "cosco")
        resolution, traqo, vizion, clients = self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_found(),
            probe=_probe_found(),
            direct_behaviour={"cosco": {"events": []}},
            use_traqo_probe=False,
        )

        self.assertEqual(self.probe_spy.calls, [])
        self.assertEqual(resolution.step_for(STEP_TRAQO_PROBE).outcome, carrier_resolution.SKIPPED)
        self.assertEqual(traqo.calls, [ACCEPTANCE_NUMBER])
        self.assertEqual(clients["cosco"].calls, [ACCEPTANCE_NUMBER])
        self.assertEqual(vizion.calls, [ACCEPTANCE_NUMBER])
        self.assertEqual(resolution.source, CarrierSource.VIZION_ACI)

    def test_a_probe_hit_spends_nothing_below_it(self):
        """The cost assertion this step exists for: no direct sweep, and no paid reference."""
        _carrier_integration(self.team, "cosco")
        resolution, _traqo, vizion, clients = self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_found(),
            probe=_probe_found(),
            direct_behaviour={"cosco": {"events": [{"id": 1}]}},
            direct_events={"cosco": [_normalised_event(ACCEPTANCE_NUMBER)]},
        )

        self.assertEqual(clients["cosco"].calls, [], "direct discovery ran after the probe had answered")
        self.assertEqual(vizion.calls, [], "Vizion was paid after the probe had answered")
        self.assertIsNone(resolution.step_for(STEP_DIRECT_API))
        self.assertIsNone(resolution.step_for(STEP_VIZION_ACI))
        self.assertEqual(resolution.vizion_reference_id, "")

    def test_a_probe_that_answered_nothing_is_still_reported(self):
        """What was asked is worth keeping even when none of it answered."""
        resolution, *_ = self.resolve(
            traqo=_traqo_not_found(), vizion=_vizion_not_found(), probe=_probe_not_found(asked=("ONEY", "MAEU"))
        )

        self.assertIsNotNone(resolution.traqo_probe)
        self.assertEqual([attempt.sealine for attempt in resolution.traqo_probe.attempts], ["ONEY", "MAEU"])

    # -- 4. the direct carrier APIs ------------------------------------------

    def test_traqo_not_found_lets_direct_discovery_run(self):
        _carrier_integration(self.team, "cosco")
        resolution, traqo, vizion, clients = self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_found(),
            direct_behaviour={"cosco": {"events": [{"id": 1}]}},
            direct_events={"cosco": [_normalised_event(ACCEPTANCE_NUMBER)]},
        )

        self.assertEqual(traqo.calls, [ACCEPTANCE_NUMBER])
        self.assertEqual(clients["cosco"].calls, [ACCEPTANCE_NUMBER])
        self.assertEqual(resolution.carrier_code, "cosco")
        self.assertEqual(resolution.source, CarrierSource.DIRECT_API)
        self.assertEqual(vizion.calls, [], "Vizion ran after a direct carrier had answered with events")

    def test_direct_discovery_is_the_only_step_that_proves_a_carrier(self):
        _carrier_integration(self.team, "cosco")
        resolution, *_ = self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_found(),
            direct_behaviour={"cosco": {"events": [{"id": 1}]}},
            direct_events={"cosco": [_normalised_event(ACCEPTANCE_NUMBER)]},
        )

        self.assertTrue(resolution.verified)

    def test_a_direct_hit_carries_its_payload_out_for_the_caller_to_store(self):
        """So activation stores what was fetched instead of asking the same carrier twice."""
        _carrier_integration(self.team, "cosco")
        resolution, *_ = self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_found(),
            direct_behaviour={"cosco": {"events": [{"id": 1}]}},
            direct_events={"cosco": [_normalised_event(ACCEPTANCE_NUMBER)]},
        )

        self.assertTrue(resolution.has_direct_payload)
        self.assertEqual(len(resolution.events), 1)
        self.assertIsNotNone(resolution.discovery)

    # -- 5. Vizion ACI, last because it costs -------------------------------

    def test_vizion_runs_only_once_both_free_steps_have_failed(self):
        _carrier_integration(self.team, "cosco")
        resolution, traqo, vizion, clients = self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_found(),
            direct_behaviour={"cosco": {"events": []}},
        )

        self.assertEqual(traqo.calls, [ACCEPTANCE_NUMBER])
        self.assertEqual(clients["cosco"].calls, [ACCEPTANCE_NUMBER])
        self.assertEqual(vizion.calls, [ACCEPTANCE_NUMBER])
        self.assertEqual(resolution.carrier_code, "one")
        self.assertEqual(resolution.source, CarrierSource.VIZION_ACI)

    def test_a_vizion_answer_is_recorded_with_its_reference(self):
        """The reference was paid for. Losing it means paying for another one."""
        resolution, *_ = self.resolve(traqo=_traqo_not_found(), vizion=_vizion_found(reference_id="vz-77"))

        self.assertEqual(resolution.vizion_reference_id, "vz-77")

    def test_vizion_is_not_reported_as_proof_either(self):
        """A carrier system answered Vizion, not us. It is an answer, not our evidence."""
        resolution, *_ = self.resolve(traqo=_traqo_not_found(), vizion=_vizion_found())

        self.assertFalse(resolution.verified)

    def test_nothing_anywhere_leaves_the_container_unresolved(self):
        _carrier_integration(self.team, "cosco")
        resolution, *_ = self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_not_found(),
            direct_behaviour={"cosco": {"events": []}},
        )

        self.assertFalse(resolution.resolved)
        self.assertEqual(resolution.carrier_code, "")

    # -- failure semantics ---------------------------------------------------

    def test_a_traqo_technical_failure_is_not_a_missing_carrier(self):
        """A timeout says nothing about the box, and must not stop the chain."""
        _carrier_integration(self.team, "cosco")
        resolution, _traqo, vizion, clients = self.resolve(
            traqo=_traqo_error(),
            vizion=_vizion_found(),
            direct_behaviour={"cosco": {"events": []}},
        )

        self.assertEqual(resolution.step_for(STEP_TRAQO_LOOKUP).outcome, carrier_resolution.ERROR)
        self.assertEqual(clients["cosco"].calls, [ACCEPTANCE_NUMBER], "the chain stopped at a Traqo timeout")
        self.assertEqual(vizion.calls, [ACCEPTANCE_NUMBER])
        self.assertEqual(resolution.carrier_code, "one")

    def test_traqo_not_configured_is_told_apart_from_traqo_saying_no(self):
        resolution, *_ = self.resolve(traqo=_traqo_not_configured(), vizion=_vizion_not_found())

        self.assertEqual(resolution.step_for(STEP_TRAQO_LOOKUP).outcome, carrier_resolution.NOT_CONFIGURED)

    def test_a_vizion_401_is_not_reported_as_an_unknown_container(self):
        _carrier_integration(self.team, "cosco")
        resolution, *_ = self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_unavailable(),
            direct_behaviour={"cosco": {"events": []}},
        )

        self.assertEqual(resolution.step_for(STEP_VIZION_ACI).outcome, carrier_resolution.ERROR)
        self.assertFalse(resolution.resolved, "an auth failure must never produce a carrier")

    def test_a_vizion_failure_records_no_carrier_of_any_kind(self):
        resolution, *_ = self.resolve(traqo=_traqo_not_found(), vizion=_vizion_unavailable())

        self.assertEqual(resolution.carrier_code, "")
        self.assertEqual(resolution.carrier_name, "")
        self.assertEqual(resolution.source, "")

    def test_a_vizion_still_looking_is_not_an_answer(self):
        pending = vizion_discovery.VizionCarrierIdentification(
            container_number=ACCEPTANCE_NUMBER,
            status=vizion_discovery.PENDING,
            reference_id="vz-pending",
            aci_state="PENDING",
        )
        resolution, *_ = self.resolve(traqo=_traqo_not_found(), vizion=pending)

        self.assertFalse(resolution.resolved)
        self.assertEqual(resolution.step_for(STEP_VIZION_ACI).outcome, carrier_resolution.NOT_FOUND)
        # The reference is alive at Vizion, which retries for seven days.
        self.assertEqual(resolution.vizion_reference_id, "vz-pending")

    # -- step bookkeeping ----------------------------------------------------

    def test_a_disabled_step_is_recorded_as_skipped_rather_than_omitted(self):
        """A caller reading the chain has to see the difference between "no" and "not asked"."""
        resolution, traqo, vizion, _clients = self.resolve(
            traqo=_traqo_found(),
            vizion=_vizion_found(),
            use_traqo_lookup=False,
            use_vizion_aci=False,
        )

        self.assertEqual(resolution.step_for(STEP_TRAQO_LOOKUP).outcome, carrier_resolution.SKIPPED)
        self.assertEqual(resolution.step_for(STEP_VIZION_ACI).outcome, carrier_resolution.SKIPPED)
        self.assertEqual(traqo.calls, [])
        self.assertEqual(vizion.calls, [])

    def test_the_chain_summary_names_every_step_in_order(self):
        _carrier_integration(self.team, "cosco")
        resolution, *_ = self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_found(),
            direct_behaviour={"cosco": {"events": []}},
        )

        summary = resolution.summary
        self.assertLess(summary.index(STEP_TRUSTED), summary.index(STEP_TRAQO_LOOKUP))
        self.assertLess(summary.index(STEP_TRAQO_LOOKUP), summary.index(STEP_TRAQO_PROBE))
        self.assertLess(summary.index(STEP_TRAQO_PROBE), summary.index(STEP_DIRECT_API))
        self.assertLess(summary.index(STEP_DIRECT_API), summary.index(STEP_VIZION_ACI))

    def test_resolution_writes_nothing(self):
        from apps.scm.tracking.models import TrackingEvent, TrackingSubscription

        _carrier_integration(self.team, "cosco")
        self.resolve(
            traqo=_traqo_not_found(),
            vizion=_vizion_found(),
            direct_behaviour={"cosco": {"events": [{"id": 1}]}},
            direct_events={"cosco": [_normalised_event(ACCEPTANCE_NUMBER)]},
        )

        self.assertEqual(TrackingSubscription.objects.count(), 0)
        self.assertEqual(TrackingEvent.objects.count(), 0)

    def test_resolution_does_not_touch_the_shipments_carrier(self):
        shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-K", carrier="")
        ShipmentContainer.objects.create(shipment=shipment, container=self.container)

        self.resolve(traqo=_traqo_found(), vizion=_vizion_found())

        shipment.refresh_from_db()
        self.assertEqual(shipment.carrier, "")
