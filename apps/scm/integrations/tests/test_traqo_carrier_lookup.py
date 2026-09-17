"""Tests for Traqo's carrier lookup: parsing, confidence, and what it would justify.

Offline. Every envelope is written here, including the two shapes production actually
returned — an identified carrier sourced from history, and a flat ``carrier: null`` for
a container Traqo has never tracked. No test makes a network call.

The assertions that matter most are the negative ones. A lookup that names nothing must
not read as a carrier; a lookup that omits ``slot_consumed`` must not read as "no slot
consumed"; and a Container SCM carrier code must never be compared against a SCAC as a
raw string, because that reported every correct lookup as a contradiction until it was
fixed.
"""

from django.test import TestCase

from apps.scm.integrations.carriers.exceptions import (
    CarrierInvalidResponseError,
    CarrierUnsupportedReferenceError,
)
from apps.scm.integrations.traqo.carrier_lookup import (
    ACTION_ACCEPT_WITH_CORROBORATION,
    ACTION_AUTO_ACCEPT,
    ACTION_MANUAL_VERIFICATION,
    ACTION_REJECT,
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_UNKNOWN,
    assess_lookup,
    read_carrier_lookup,
    read_confidence,
)
from apps.scm.integrations.traqo.client import TraqoClient


def envelope(carrier=None, *, candidates=None, unavailable=None, cached=False, slot=False, number="BBCU3273070"):
    """Build a lookup envelope in the shape production returns."""
    data = {
        "number": number,
        "type": "container",
        "carrier": carrier,
        "candidates": candidates if candidates is not None else [],
        "sources_unavailable": unavailable if unavailable is not None else [],
        "cached": cached,
        "slot_consumed": slot,
    }
    return {"success": True, "data": data}


def carrier(scac="MAEU", name="Maersk", confidence="high", source="history", reason="Previously tracked"):
    return {"scac": scac, "name": name, "source": source, "confidence": confidence, "reason": reason}


# The two envelopes production actually returned on 2026-08-29, kept verbatim so a
# change in Traqo's shape shows up as a test failure rather than as a silent reparse.
PRODUCTION_IDENTIFIED = {
    "success": True,
    "data": {
        "number": "CPWU2588297",
        "type": "container",
        "carrier": {
            "scac": "MAEU",
            "name": "Maersk",
            "source": "history",
            "confidence": "high",
            "reason": "Previously tracked successfully under this carrier",
        },
        "candidates": [
            {
                "scac": "MAEU",
                "name": "Maersk",
                "source": "history",
                "confidence": "high",
                "reason": "Previously tracked successfully under this carrier",
            }
        ],
        "sources_unavailable": [],
        "cached": False,
        "slot_consumed": False,
    },
}

PRODUCTION_UNIDENTIFIED = {
    "success": True,
    "data": {
        "number": "BBCU3273070",
        "type": "container",
        "carrier": None,
        "candidates": [],
        "sources_unavailable": [],
        "cached": True,
        "slot_consumed": False,
    },
}


class ConfidenceReadingTest(TestCase):
    """Traqo's confidence wording, mapped without inventing certainty."""

    def test_known_words_map_to_bands(self):
        self.assertEqual(read_confidence("high"), CONFIDENCE_HIGH)
        self.assertEqual(read_confidence("HIGH"), CONFIDENCE_HIGH)
        self.assertEqual(read_confidence("medium"), CONFIDENCE_MEDIUM)
        self.assertEqual(read_confidence("low"), CONFIDENCE_LOW)

    def test_numeric_scores_map_to_bands(self):
        self.assertEqual(read_confidence("0.95"), CONFIDENCE_HIGH)
        self.assertEqual(read_confidence("0.7"), CONFIDENCE_MEDIUM)
        self.assertEqual(read_confidence("0.2"), CONFIDENCE_LOW)
        self.assertEqual(read_confidence("92"), CONFIDENCE_HIGH)

    def test_an_unrecognised_word_stays_unknown(self):
        """Rounding an unfamiliar label to the nearest familiar one would invent certainty."""
        self.assertEqual(read_confidence("fairly-sure-ish"), CONFIDENCE_UNKNOWN)
        self.assertEqual(read_confidence(""), CONFIDENCE_UNKNOWN)
        self.assertEqual(read_confidence(None), CONFIDENCE_UNKNOWN)


class CarrierLookupParsingTest(TestCase):
    """Reading a lookup response, including the shapes production returned."""

    def test_the_production_identified_envelope_is_read_in_full(self):
        lookup = read_carrier_lookup(PRODUCTION_IDENTIFIED, reference="CPWU2588297")

        self.assertTrue(lookup.identified)
        self.assertEqual(lookup.scac, "MAEU")
        self.assertEqual(lookup.carrier_name, "Maersk")
        self.assertEqual(lookup.confidence, CONFIDENCE_HIGH)
        self.assertEqual(lookup.stated_confidence, "high")
        self.assertEqual(lookup.source, "history")
        self.assertEqual(lookup.reason, "Previously tracked successfully under this carrier")
        self.assertEqual(lookup.cached, False)
        self.assertEqual(lookup.slot_consumed, False)
        self.assertEqual(lookup.carrier_code, "maersk")
        self.assertIs(lookup.carrier_supported_by_traqo, True)

    def test_the_winner_is_not_counted_as_its_own_rival(self):
        lookup = read_carrier_lookup(PRODUCTION_IDENTIFIED, reference="CPWU2588297")

        self.assertEqual(len(lookup.candidates), 1)
        self.assertEqual(lookup.rival_candidates, ())

    def test_the_production_unidentified_envelope_names_no_carrier(self):
        lookup = read_carrier_lookup(PRODUCTION_UNIDENTIFIED, reference="BBCU3273070")

        self.assertFalse(lookup.identified)
        self.assertEqual(lookup.scac, "")
        self.assertEqual(lookup.confidence, CONFIDENCE_UNKNOWN)
        self.assertEqual(lookup.candidates, ())
        self.assertIsNone(lookup.carrier_supported_by_traqo)
        # The account facts are still read: a null carrier is not an unreadable response.
        self.assertEqual(lookup.cached, True)
        self.assertEqual(lookup.slot_consumed, False)

    def test_multiple_candidates_are_all_kept_with_their_own_confidence(self):
        payload = envelope(
            carrier(scac="MSCU", name="MSC", confidence="medium"),
            candidates=[
                carrier(scac="MSCU", name="MSC", confidence="medium"),
                carrier(scac="MAEU", name="Maersk", confidence="low"),
                carrier(scac="CMDU", name="CMA CGM", confidence="low"),
            ],
        )

        lookup = read_carrier_lookup(payload, reference="BBCU3273070")

        self.assertEqual(lookup.scac, "MSCU")
        self.assertEqual([c.scac for c in lookup.candidates], ["MSCU", "MAEU", "CMDU"])
        self.assertEqual([c.scac for c in lookup.rival_candidates], ["MAEU", "CMDU"])
        self.assertEqual(lookup.candidates[1].confidence, CONFIDENCE_LOW)

    def test_unavailable_sources_are_recorded(self):
        payload = envelope(carrier(), unavailable=["bl_index", {"source": "carrier_api"}])

        lookup = read_carrier_lookup(payload, reference="CPWU2588297")

        self.assertEqual(lookup.unavailable_sources, ("bl_index", "carrier_api"))

    def test_a_cached_response_is_reported_as_cached(self):
        lookup = read_carrier_lookup(envelope(carrier(), cached=True), reference="CPWU2588297")

        self.assertIs(lookup.cached, True)

    def test_slot_consumed_true_is_reported_rather_than_assumed_false(self):
        """The docs say lookups are free; the response is what gets believed."""
        lookup = read_carrier_lookup(envelope(carrier(), slot=True), reference="CPWU2588297")

        self.assertIs(lookup.slot_consumed, True)

    def test_an_absent_slot_flag_is_none_and_not_false(self):
        """ "Traqo did not say" must never be reported as "Traqo said no"."""
        payload = envelope(carrier())
        del payload["data"]["slot_consumed"]

        lookup = read_carrier_lookup(payload, reference="CPWU2588297")

        self.assertIsNone(lookup.slot_consumed)

    def test_a_malformed_response_yields_nothing_identified_rather_than_raising(self):
        for bad in (None, [], "nope", {}, {"data": "not an object"}):
            lookup = read_carrier_lookup(bad, reference="BBCU3273070")
            self.assertFalse(lookup.identified)

    def test_the_raw_response_is_preserved(self):
        lookup = read_carrier_lookup(PRODUCTION_IDENTIFIED, reference="CPWU2588297")

        self.assertEqual(lookup.raw, PRODUCTION_IDENTIFIED)

    def test_a_scac_traqo_cannot_track_is_flagged(self):
        lookup = read_carrier_lookup(envelope(carrier(scac="ZZZZ", name="Nobody")), reference="BBCU3273070")

        self.assertIs(lookup.carrier_supported_by_traqo, False)
        self.assertEqual(lookup.carrier_code, "")


class LookupAssessmentTest(TestCase):
    """What Container SCM would do on the lookup alone — benchmark output only."""

    def test_high_confidence_corroborated_by_our_own_evidence_auto_accepts(self):
        lookup = read_carrier_lookup(PRODUCTION_IDENTIFIED, reference="CPWU2588297")

        assessment = assess_lookup(lookup, known_carrier_codes=("maersk",))

        self.assertEqual(assessment.action, ACTION_AUTO_ACCEPT)
        self.assertEqual(assessment.contradicted_by, ())

    def test_a_carrier_code_is_not_compared_against_a_scac_as_a_raw_string(self):
        """The regression: 'maersk' vs 'MAEU' used to read as a contradiction."""
        lookup = read_carrier_lookup(PRODUCTION_IDENTIFIED, reference="CPWU2588297")

        assessment = assess_lookup(lookup, prefix_suggestion="maersk", known_carrier_codes=("maersk",))

        self.assertEqual(assessment.contradicted_by, ())
        self.assertEqual(len(assessment.corroborated_by), 2)

    def test_high_confidence_with_nothing_corroborating_needs_corroboration(self):
        lookup = read_carrier_lookup(PRODUCTION_IDENTIFIED, reference="CPWU2588297")

        assessment = assess_lookup(lookup)

        self.assertEqual(assessment.action, ACTION_ACCEPT_WITH_CORROBORATION)
        self.assertIn("one source", assessment.rationale)

    def test_high_confidence_with_a_rival_candidate_needs_corroboration(self):
        payload = envelope(
            carrier(scac="MAEU", confidence="high"),
            candidates=[carrier(scac="MAEU", confidence="high"), carrier(scac="MSCU", confidence="high")],
        )

        assessment = assess_lookup(read_carrier_lookup(payload, reference="BBCU3273070"))

        self.assertEqual(assessment.action, ACTION_ACCEPT_WITH_CORROBORATION)

    def test_medium_confidence_needs_corroboration(self):
        payload = envelope(carrier(confidence="medium"))

        assessment = assess_lookup(read_carrier_lookup(payload, reference="CPWU2588297"))

        self.assertEqual(assessment.action, ACTION_ACCEPT_WITH_CORROBORATION)

    def test_low_confidence_requires_manual_verification(self):
        payload = envelope(carrier(confidence="low"))

        assessment = assess_lookup(read_carrier_lookup(payload, reference="CPWU2588297"))

        self.assertEqual(assessment.action, ACTION_MANUAL_VERIFICATION)

    def test_an_unidentified_lookup_is_rejected(self):
        lookup = read_carrier_lookup(PRODUCTION_UNIDENTIFIED, reference="BBCU3273070")

        assessment = assess_lookup(lookup)

        self.assertEqual(assessment.action, ACTION_REJECT)
        self.assertIn("named no carrier", assessment.rationale)

    def test_a_disagreement_with_our_own_evidence_goes_to_a_human(self):
        payload = envelope(carrier(scac="MSCU", name="MSC", confidence="high"))

        assessment = assess_lookup(read_carrier_lookup(payload, reference="X"), known_carrier_codes=("maersk",))

        self.assertEqual(assessment.action, ACTION_MANUAL_VERIFICATION)
        self.assertEqual(len(assessment.contradicted_by), 1)

    def test_an_untrackable_scac_is_rejected_before_a_slot_could_be_spent(self):
        payload = envelope(carrier(scac="ZZZZ", confidence="high"))

        assessment = assess_lookup(read_carrier_lookup(payload, reference="X"))

        self.assertEqual(assessment.action, ACTION_REJECT)
        self.assertIn("spend a slot", assessment.rationale)

    def test_an_absent_prefix_hint_is_not_a_contradiction(self):
        """Most prefixes are simply not in the registry; that is not disagreement."""
        lookup = read_carrier_lookup(PRODUCTION_IDENTIFIED, reference="CPWU2588297")

        assessment = assess_lookup(lookup, prefix_suggestion="")

        self.assertEqual(assessment.contradicted_by, ())


class CarrierLookupClientTest(TestCase):
    """The client's URL shape and its refusal to call on nothing."""

    def test_the_lookup_url_is_distinct_from_the_container_url(self):
        client = TraqoClient(base_url="https://example.test/api/v1", api_key="x")

        self.assertEqual(client.carrier_lookup_url(), "https://example.test/api/v1/carriers/lookup")
        self.assertNotIn("container", client.carrier_lookup_url())

    def test_the_sandbox_lookup_url_carries_the_sandbox_segment(self):
        client = TraqoClient(base_url="https://example.test/api/v1", sandbox=True)

        self.assertEqual(client.carrier_lookup_url(), "https://example.test/api/v1/sandbox/carriers/lookup")

    def test_an_empty_reference_is_refused_before_any_request(self):
        client = TraqoClient(base_url="https://example.test/api/v1", api_key="x")

        with self.assertRaises(CarrierUnsupportedReferenceError):
            client.lookup_carrier("   ")

    def test_a_success_false_body_is_rejected(self):
        class _Stub:
            def get(self, url, params=None):
                return {"success": False, "message": "no"}

        client = TraqoClient(base_url="https://example.test/api/v1", api_key="x")
        client._http = _Stub()

        with self.assertRaises(CarrierInvalidResponseError):
            client.lookup_carrier("BBCU3273070")

    def test_a_non_object_body_is_rejected(self):
        class _Stub:
            def get(self, url, params=None):
                return ["not", "an", "object"]

        client = TraqoClient(base_url="https://example.test/api/v1", api_key="x")
        client._http = _Stub()

        with self.assertRaises(CarrierInvalidResponseError):
            client.lookup_carrier("BBCU3273070")

    def test_the_reference_is_sent_uppercased_as_the_number_param(self):
        seen = {}

        class _Stub:
            def get(self, url, params=None):
                seen.update({"url": url, "params": params})
                return envelope(carrier())

        client = TraqoClient(base_url="https://example.test/api/v1", api_key="x")
        client._http = _Stub()

        client.lookup_carrier("bbcu3273070")

        self.assertEqual(seen["params"], {"number": "BBCU3273070"})
        self.assertEqual(seen["url"], "https://example.test/api/v1/carriers/lookup")

    def test_a_lookup_never_touches_the_container_endpoint(self):
        """The whole point of the separate call: no shipment, no slot."""
        calls = []

        class _Stub:
            def get(self, url, params=None):
                calls.append(url)
                return envelope(carrier())

        client = TraqoClient(base_url="https://example.test/api/v1", api_key="x")
        client._http = _Stub()

        client.lookup_carrier("BBCU3273070")

        self.assertEqual(calls, ["https://example.test/api/v1/carriers/lookup"])
