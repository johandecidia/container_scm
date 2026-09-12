"""Tests for Traqo candidate probing: which carriers get asked, and what counts as an answer.

Three things are pinned down here.

*Order.* The list of carriers to try is deterministic and evidence-first, because the cap
means the order decides what actually gets asked. A probe that tried the default list
before the carrier somebody named would spend its five calls on guesses.

*Cost.* Every assertion about a hit is paired with one about how many requests it took.
Traqo's container endpoint may consume a shipment slot, so "found on the first candidate"
has to mean exactly one call, and a cap has to be a cap.

*What proves a carrier.* Sending ``sealine=ONEY`` is the question. The tests below are
mostly about refusing to treat it as the answer: no events, a different container, or a
SCAC nothing can be routed to are all "not this carrier", however healthy the HTTP.

No test here reaches the network — the fetch is injected.
"""

from django.test import TestCase

from apps.scm.integrations.carriers.carrier_discovery import SOURCE_OWNER_PREFIX, SOURCE_PREFERRED
from apps.scm.integrations.carriers.exceptions import (
    CarrierAuthenticationError,
    CarrierNoDataError,
    CarrierServerError,
    CarrierTimeoutError,
)
from apps.scm.integrations.traqo import carrier_probe
from apps.scm.integrations.traqo.carrier_probe import (
    DEFAULT_TRAQO_DISCOVERY_ORDER,
    MAX_TRAQO_PROBE_ATTEMPTS,
    SOURCE_DISCOVERY_ORDER,
    TraqoProbeCandidate,
    build_traqo_probe_candidates,
    probe_candidate_carriers,
)
from apps.scm.integrations.traqo.errors import TraqoShipmentLimitReachedError
from apps.scm.integrations.traqo.service import TraqoContainerResponse

# The acceptance container. BBCU belongs to no carrier in the registry, so nothing can
# shortcut to an answer from the number's shape.
CONTAINER = "BBCU3273070"


def _event(container_number=CONTAINER):
    from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent

    return NormalisedTrackingEvent(
        event_type="EQUIPMENT",
        event_code="GTIN",
        container_number=container_number,
        source_provider="traqo",
    )


def _response(*, sealine, events=1, reported_sealine=None, reference=CONTAINER, container_number=CONTAINER):
    """A Traqo answer of the shape the real client returns."""
    data = {"events_table": [{"idx": 1}] * events}
    if reference is not None:
        data["reference_number"] = reference
    if reported_sealine is not False:
        data["sealine"] = reported_sealine if reported_sealine is not None else sealine
    return TraqoContainerResponse(
        container_number=container_number,
        sealine=sealine,
        payload={"success": True, "data": data},
        events=tuple(_event() for _ in range(events)),
    )


class FakeFetch:
    """A stand-in for one Traqo container call, recording every sealine it was asked with.

    ``behaviour`` maps a sealine to a response or an exception; anything not named
    answers CarrierNoDataError, which is what Traqo does for a container it has no
    shipment for.
    """

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or {}
        self.calls: list[str] = []

    def __call__(self, *, container_number, sealine, sandbox=False, client=None):
        self.calls.append(sealine)
        outcome = self.behaviour.get(sealine)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise CarrierNoDataError("Traqo has no shipment for this container.", provider_code="traqo")
        return outcome

    def probe(self, *, candidates=None, codes=(), **kwargs):
        return probe_candidate_carriers(
            container_number=CONTAINER,
            candidates=candidates,
            candidate_carrier_codes=codes,
            fetch=self,
            **kwargs,
        )


def _candidates(*codes):
    return build_traqo_probe_candidates(preferred_carrier_codes=codes, discovery_order=())


class CandidateOrderTest(TestCase):
    """Which carriers are worth asking Traqo about, and in what order."""

    def codes(self, **kwargs):
        return [candidate.carrier_code for candidate in build_traqo_probe_candidates(**kwargs)]

    def test_nothing_known_falls_back_to_the_configured_discovery_order(self):
        self.assertEqual(self.codes(), list(DEFAULT_TRAQO_DISCOVERY_ORDER))

    def test_a_preferred_carrier_comes_before_the_default_order(self):
        codes = self.codes(preferred_carrier_codes=["zim"])

        self.assertEqual(codes[0], "zim")
        self.assertEqual(codes[1], DEFAULT_TRAQO_DISCOVERY_ORDER[0])

    def test_preferred_carriers_keep_the_order_they_were_given(self):
        self.assertEqual(self.codes(preferred_carrier_codes=["zim", "msc"])[:2], ["zim", "msc"])

    def test_a_carrier_named_twice_is_asked_once(self):
        """ONE heads the default order, so preferring it must not queue it twice."""
        codes = self.codes(preferred_carrier_codes=["one"])

        self.assertEqual(codes.count("one"), 1)
        self.assertEqual(codes[0], "one")

    def test_a_carrier_traqo_publishes_no_sealine_for_is_not_a_candidate(self):
        """Evergreen is registered here and absent from Traqo's sealine list."""
        codes = self.codes(preferred_carrier_codes=["evergreen"])

        self.assertNotIn("evergreen", codes)
        self.assertEqual(codes, list(DEFAULT_TRAQO_DISCOVERY_ORDER))

    def test_an_unknown_carrier_name_is_not_a_candidate(self):
        self.assertEqual(self.codes(preferred_carrier_codes=["Regional Feeder"]), list(DEFAULT_TRAQO_DISCOVERY_ORDER))

    def test_a_carrier_name_is_resolved_to_its_registry_code(self):
        self.assertEqual(self.codes(preferred_carrier_codes=["Hapag-Lloyd"])[0], "hapag_lloyd")

    def test_excluded_carriers_are_left_out_entirely(self):
        codes = self.codes(exclude_carrier_codes=frozenset({"one", "maersk"}))

        self.assertNotIn("one", codes)
        self.assertNotIn("maersk", codes)
        self.assertEqual(codes[0], "cma_cgm")

    def test_an_exclusion_beats_a_preference(self):
        """A caller that has just asked a carrier does not want it asked again."""
        codes = self.codes(preferred_carrier_codes=["zim"], exclude_carrier_codes=frozenset({"zim"}))

        self.assertNotIn("zim", codes)

    def test_the_owner_prefix_orders_the_list_and_nothing_more(self):
        """MSCU belongs to MSC. A hint moves it up; only Traqo's data can make it the answer."""
        candidates = build_traqo_probe_candidates(container_number="MSCU1234567")

        self.assertEqual(candidates[0].carrier_code, "msc")
        self.assertEqual(candidates[0].source, SOURCE_OWNER_PREFIX)
        self.assertEqual([candidate.carrier_code for candidate in candidates].count("msc"), 1)

    def test_a_named_carrier_outranks_the_owner_prefix(self):
        candidates = build_traqo_probe_candidates(container_number="MSCU1234567", preferred_carrier_codes=["zim"])

        self.assertEqual(candidates[0].carrier_code, "zim")
        self.assertEqual(candidates[0].source, SOURCE_PREFERRED)
        self.assertEqual(candidates[1].carrier_code, "msc")

    def test_the_acceptance_containers_prefix_suggests_nobody(self):
        candidates = build_traqo_probe_candidates(container_number=CONTAINER)

        self.assertEqual([candidate.source for candidate in candidates], [SOURCE_DISCOVERY_ORDER] * len(candidates))

    def test_every_candidate_carries_the_sealine_it_will_be_asked_with(self):
        by_code = {candidate.carrier_code: candidate.sealine for candidate in build_traqo_probe_candidates()}

        self.assertEqual(by_code["one"], "ONEY")
        self.assertEqual(by_code["hapag_lloyd"], "HLCU")

    def test_the_order_is_the_same_every_time(self):
        first = self.codes(container_number=CONTAINER, preferred_carrier_codes=["zim"])
        second = self.codes(container_number=CONTAINER, preferred_carrier_codes=["zim"])

        self.assertEqual(first, second)

    def test_the_builder_is_not_truncated_to_the_attempt_cap(self):
        """Capping calls and building the list are separate decisions."""
        self.assertGreater(len(build_traqo_probe_candidates()), MAX_TRAQO_PROBE_ATTEMPTS)


class ProbeOutcomeTest(TestCase):
    """What Traqo's answers do to the probe, one candidate at a time."""

    def test_the_first_candidate_with_data_ends_the_probe(self):
        fetch = FakeFetch({"ONEY": _response(sealine="ONEY")})

        result = fetch.probe(candidates=_candidates("one", "maersk", "msc"))

        self.assertTrue(result.found)
        self.assertEqual(result.carrier_code, "one")
        self.assertEqual(result.sealine, "ONEY")
        self.assertEqual(fetch.calls, ["ONEY"], "candidates were asked after one had answered")

    def test_a_hit_carries_the_payload_and_the_mapped_events_out(self):
        """So the caller stores what was fetched instead of asking Traqo again."""
        fetch = FakeFetch({"ONEY": _response(sealine="ONEY", events=3)})

        result = fetch.probe(candidates=_candidates("one"))

        self.assertEqual(len(result.events), 3)
        self.assertEqual(result.raw_payload["data"]["sealine"], "ONEY")

    def test_a_hit_names_the_carrier_the_registry_knows(self):
        fetch = FakeFetch({"ONEY": _response(sealine="ONEY")})

        self.assertIn("Ocean Network Express", fetch.probe(candidates=_candidates("one")).carrier_name)

    def test_a_carrier_without_the_box_does_not_stop_the_next_one(self):
        fetch = FakeFetch({"CMDU": _response(sealine="CMDU")})

        result = fetch.probe(candidates=_candidates("one", "maersk", "cma_cgm"))

        self.assertEqual(result.carrier_code, "cma_cgm")
        self.assertEqual(fetch.calls, ["ONEY", "MAEU", "CMDU"])

    def test_a_technical_failure_does_not_stop_the_next_candidate(self):
        """ONEY times out, MAEU has nothing, CMDU answers — the recorded real-world shape."""
        fetch = FakeFetch(
            {
                "ONEY": CarrierTimeoutError("timed out", provider_code="traqo"),
                "CMDU": _response(sealine="CMDU"),
            }
        )

        result = fetch.probe(candidates=_candidates("one", "maersk", "cma_cgm"))

        self.assertTrue(result.found)
        self.assertEqual(result.carrier_code, "cma_cgm")
        self.assertEqual([attempt.outcome for attempt in result.attempts], ["error", "not_found", "found"])

    def test_a_server_error_is_told_apart_from_no_data(self):
        fetch = FakeFetch({"ONEY": CarrierServerError("502", provider_code="traqo")})

        result = fetch.probe(candidates=_candidates("one"))

        self.assertEqual(result.outcome, carrier_probe.ERROR)
        self.assertEqual(result.attempts[0].error_kind, "CarrierServerError")

    def test_every_candidate_answering_no_is_a_plain_not_found(self):
        result = FakeFetch().probe(candidates=_candidates("one", "maersk"))

        self.assertFalse(result.found)
        self.assertEqual(result.outcome, carrier_probe.NOT_FOUND)
        self.assertEqual(result.carrier_code, "")

    def test_every_candidate_failing_technically_is_not_a_missing_carrier(self):
        fetch = FakeFetch(
            {
                "ONEY": CarrierTimeoutError("timed out", provider_code="traqo"),
                "MAEU": CarrierTimeoutError("timed out", provider_code="traqo"),
            }
        )

        result = fetch.probe(candidates=_candidates("one", "maersk"))

        self.assertEqual(result.outcome, carrier_probe.ERROR)

    def test_no_candidates_is_neither_an_answer_nor_a_fault(self):
        fetch = FakeFetch()

        result = fetch.probe(candidates=[])

        self.assertEqual(result.outcome, carrier_probe.SKIPPED)
        self.assertEqual(fetch.calls, [])

    def test_an_unexpected_error_is_classified_rather_than_raised(self):
        fetch = FakeFetch({"ONEY": RuntimeError("reader bug")})

        result = fetch.probe(candidates=_candidates("one"))

        self.assertEqual(result.attempts[0].error_kind, "unexpected")
        self.assertEqual(result.outcome, carrier_probe.ERROR)


class ProbeCostTest(TestCase):
    """What a probe is allowed to spend."""

    def test_the_attempt_cap_is_respected(self):
        fetch = FakeFetch()

        result = fetch.probe(candidates=build_traqo_probe_candidates(), max_attempts=3)

        self.assertEqual(len(fetch.calls), 3)
        self.assertEqual(result.calls_made, 3)

    def test_the_default_cap_applies_to_the_whole_default_order(self):
        fetch = FakeFetch()

        fetch.probe(candidates=build_traqo_probe_candidates())

        self.assertEqual(len(fetch.calls), MAX_TRAQO_PROBE_ATTEMPTS)

    def test_a_zero_cap_asks_nobody(self):
        fetch = FakeFetch({"ONEY": _response(sealine="ONEY")})

        result = fetch.probe(candidates=_candidates("one"), max_attempts=0)

        self.assertEqual(fetch.calls, [])
        self.assertEqual(result.outcome, carrier_probe.SKIPPED)

    def test_a_skipped_carrier_costs_no_attempt(self):
        """Evergreen has no sealine, so it never uses up one of the five calls."""
        fetch = FakeFetch()

        fetch.probe(codes=["evergreen", "one", "maersk"], max_attempts=2)

        self.assertEqual(fetch.calls, ["ONEY", "MAEU"])

    def test_a_rejected_key_stops_the_remaining_candidates(self):
        """401 is not about this carrier, and four more calls would be told the same thing."""
        fetch = FakeFetch({"ONEY": CarrierAuthenticationError("invalid API key", provider_code="traqo")})

        result = fetch.probe(candidates=build_traqo_probe_candidates())

        self.assertEqual(fetch.calls, ["ONEY"])
        self.assertEqual(result.outcome, carrier_probe.ERROR)
        self.assertEqual(result.error_kind, "CarrierAuthenticationError")

    def test_a_full_shipment_quota_stops_the_remaining_candidates(self):
        fetch = FakeFetch({"ONEY": TraqoShipmentLimitReachedError("no slots", provider_code="traqo")})

        result = fetch.probe(candidates=build_traqo_probe_candidates())

        self.assertEqual(fetch.calls, ["ONEY"])
        self.assertEqual(result.error_kind, "TraqoShipmentLimitReachedError")

    def test_traqo_not_being_configured_costs_no_call_at_all(self):
        """Read from settings, not discovered by failing five times over."""
        result = probe_candidate_carriers(container_number=CONTAINER, candidate_carrier_codes=["one"])

        self.assertEqual(result.outcome, carrier_probe.NOT_CONFIGURED)
        self.assertEqual(result.attempts, ())

    def test_no_container_number_asks_nobody(self):
        result = probe_candidate_carriers(container_number="", candidate_carrier_codes=["one"])

        self.assertEqual(result.outcome, carrier_probe.SKIPPED)


class ProbeVerificationTest(TestCase):
    """A sealine on the wire is the question. These are the rules for calling it an answer."""

    def test_a_shipment_with_no_events_does_not_prove_a_carrier(self):
        """The false-positive shape: Traqo answers 200 with nothing in it."""
        fetch = FakeFetch({"ONEY": _response(sealine="ONEY", events=0)})

        result = fetch.probe(candidates=_candidates("one"))

        self.assertFalse(result.found)
        self.assertEqual(result.attempts[0].outcome, carrier_probe.NOT_FOUND)
        self.assertEqual(result.attempts[0].error_kind, "no_events")

    def test_a_payload_for_another_container_is_never_mapped_onto_this_one(self):
        fetch = FakeFetch({"ONEY": _response(sealine="ONEY", reference="MSKU1234567")})

        result = fetch.probe(candidates=_candidates("one"))

        self.assertFalse(result.found)
        self.assertEqual(result.attempts[0].outcome, carrier_probe.ERROR)
        self.assertEqual(result.attempts[0].error_kind, "reference_mismatch")

    def test_a_payload_that_echoes_no_reference_is_still_this_containers(self):
        """Traqo answered about the container in the URL; not echoing it is not a mismatch."""
        fetch = FakeFetch({"ONEY": _response(sealine="ONEY", reference=None)})

        self.assertTrue(fetch.probe(candidates=_candidates("one")).found)

    def test_the_sealine_traqo_reports_wins_over_the_one_we_asked_with(self):
        """The request is the question; the response is what Traqo actually has."""
        fetch = FakeFetch({"ONEY": _response(sealine="ONEY", reported_sealine="CMDU")})

        result = fetch.probe(candidates=_candidates("one"))

        self.assertEqual(result.carrier_code, "cma_cgm")
        self.assertEqual(result.sealine, "CMDU")

    def test_the_requested_sealine_stands_in_when_traqo_states_none(self):
        fetch = FakeFetch({"ONEY": _response(sealine="ONEY", reported_sealine=False)})

        result = fetch.probe(candidates=_candidates("one"))

        self.assertEqual(result.carrier_code, "one")
        self.assertEqual(result.sealine, "ONEY")

    def test_a_sealine_no_registered_carrier_claims_is_not_a_resolution(self):
        """Traqo covers OOLU and this system has no adapter for it: nothing to route to."""
        fetch = FakeFetch({"ONEY": _response(sealine="ONEY", reported_sealine="OOLU")})

        result = fetch.probe(candidates=_candidates("one"))

        self.assertFalse(result.found)
        self.assertEqual(result.attempts[0].error_kind, "unregistered_sealine")

    def test_a_hit_reports_which_candidates_were_asked_and_what_they_said(self):
        fetch = FakeFetch({"CMDU": _response(sealine="CMDU")})

        result = fetch.probe(candidates=_candidates("one", "maersk", "cma_cgm"))

        self.assertEqual(
            [(attempt.sealine, attempt.outcome) for attempt in result.attempts],
            [("ONEY", "not_found"), ("MAEU", "not_found"), ("CMDU", "found")],
        )

    def test_the_probe_writes_nothing(self):
        from apps.scm.tracking.models import TrackingEvent, TrackingSubscription

        FakeFetch({"ONEY": _response(sealine="ONEY")}).probe(candidates=_candidates("one"))

        self.assertEqual(TrackingSubscription.objects.count(), 0)
        self.assertEqual(TrackingEvent.objects.count(), 0)


class ProbeCandidateCodesTest(TestCase):
    """The plain-codes entry point, for a caller that has not built candidates."""

    def test_codes_are_translated_to_sealines_in_the_order_given(self):
        fetch = FakeFetch({"MAEU": _response(sealine="MAEU")})

        result = fetch.probe(codes=["one", "maersk"])

        self.assertEqual(fetch.calls, ["ONEY", "MAEU"])
        self.assertEqual(result.carrier_code, "maersk")

    def test_a_carrier_traqo_cannot_be_asked_about_is_dropped(self):
        fetch = FakeFetch()

        fetch.probe(codes=["evergreen"])

        self.assertEqual(fetch.calls, [])

    def test_a_candidate_knows_the_carriers_display_name(self):
        self.assertIn("Maersk", TraqoProbeCandidate(carrier_code="maersk", sealine="MAEU").carrier_name)
