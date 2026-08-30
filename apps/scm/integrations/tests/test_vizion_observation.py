"""The Phase 1B identity experiment, exercised against every outcome it can reach.

`compare_fetches` is the instrument that will settle — from live data — whether a Vizion
milestone `id` is a real identity. These tests do not settle it: they prove the instrument
reads each of the three possible worlds correctly, and that it says INCONCLUSIVE rather
than guessing when the evidence does not distinguish them.

Each scenario is built by editing a copy of the transshipment fixture, so what is being
simulated is visible in the test rather than buried in another fixture file.
"""

import copy
import json
import pathlib

from django.test import SimpleTestCase

from apps.scm.integrations.vizion.observation import (
    IDENTITY_INCONCLUSIVE,
    IDENTITY_STABLE_AND_REUSED,
    IDENTITY_STABLE_NEW_ID_ON_FLIP,
    IDENTITY_UNSTABLE,
    compare_fetches,
)
from apps.scm.integrations.vizion.recording import REDACTED, sanitize, write_fixture

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "vizion"
CONTAINER_NUMBER = "BBCU3273070"


def updates() -> list[dict]:
    return json.loads((FIXTURES / "updates_transshipment.json").read_text())


def first_only() -> list[dict]:
    """Just the earlier envelope, so a second fetch can be simulated against it."""
    return [updates()[0]]


def milestone(payload_updates: list[dict], milestone_id: str) -> dict:
    for update in payload_updates:
        for item in update["payload"]["milestones"]:
            if item["id"] == milestone_id:
                return item
    raise AssertionError(f"No milestone {milestone_id}")


class UnchangedRefetchTest(SimpleTestCase):
    """Two identical fetches: nothing moved, so nothing may be concluded."""

    def setUp(self):
        self.comparison = compare_fetches(first_only(), first_only(), container_number=CONTAINER_NUMBER)

    def test_every_milestone_matches(self):
        self.assertEqual(self.comparison.common_milestones, 8)
        self.assertEqual(self.comparison.added, ())
        self.assertEqual(self.comparison.removed, ())

    def test_ids_are_reported_stable(self):
        self.assertIs(self.comparison.ids_stable, True)

    def test_ordering_is_unchanged(self):
        self.assertFalse(self.comparison.order_changed)

    def test_no_new_update_envelopes(self):
        self.assertEqual(self.comparison.new_update_ids, ())

    def test_the_verdict_is_inconclusive_because_no_forecast_was_realised(self):
        # Stable ids alone do not answer the question. What matters is what happens to the
        # id when a forecast becomes an observation, and nothing did here.
        self.assertEqual(self.comparison.identity_verdict, IDENTITY_INCONCLUSIVE)
        self.assertIn("Re-run", self.comparison.recommendation)


class UnstableIdTest(SimpleTestCase):
    """The Traqo outcome: ids are per-fetch scratch."""

    def setUp(self):
        second = first_only()
        for index, item in enumerate(second[0]["payload"]["milestones"]):
            item["id"] = f"regenerated-{index}"
        self.comparison = compare_fetches(first_only(), second, container_number=CONTAINER_NUMBER)

    def test_instability_is_detected(self):
        self.assertIs(self.comparison.ids_stable, False)
        self.assertEqual(self.comparison.identity_verdict, IDENTITY_UNSTABLE)

    def test_the_milestones_still_match_on_identity_not_on_id(self):
        # The point of matching on DCSA type/code/leg/place: an id change must not look
        # like every milestone having been replaced.
        self.assertEqual(self.comparison.common_milestones, 8)
        self.assertEqual(self.comparison.added, ())

    def test_the_recommendation_is_to_keep_the_field_based_fingerprint(self):
        self.assertIn("Keep the field-based fingerprint", self.comparison.recommendation)


class ForecastRealisedTest(SimpleTestCase):
    """A forecast became an observation. This is the case the strategy turns on."""

    def _flip(self, *, new_id: str | None):
        second = first_only()
        eta = milestone(second, "m-0007")
        eta["planned"] = False
        eta["journey_event"]["event_classifier"] = "ACT"
        eta["timestamp"] = "2026-09-11T06:15:00.000+02:00"
        if new_id is not None:
            eta["id"] = new_id
        return compare_fetches(first_only(), second, container_number=CONTAINER_NUMBER)

    def test_a_reused_id_is_reported_as_such(self):
        comparison = self._flip(new_id=None)

        self.assertEqual(comparison.identity_verdict, IDENTITY_STABLE_AND_REUSED)
        realisation = comparison.forecast_realisations[0]
        self.assertEqual(realisation.first_classifier, "EST")
        self.assertEqual(realisation.second_classifier, "ACT")
        self.assertFalse(realisation.id_changed)
        self.assertTrue(realisation.timestamp_changed)

    def test_a_reused_id_recommendation_names_the_trade_off_rather_than_endorsing_it(self):
        comparison = self._flip(new_id=None)

        # An id-keyed fingerprint would collapse the flip to one row — which also destroys
        # the record of what was forecast. The instrument must not present that as free.
        self.assertIn("trade-off", comparison.recommendation)

    def test_a_replaced_id_is_reported_as_such(self):
        comparison = self._flip(new_id="m-9999")

        self.assertEqual(comparison.identity_verdict, IDENTITY_STABLE_NEW_ID_ON_FLIP)
        self.assertTrue(comparison.forecast_realisations[0].id_changed)
        self.assertIn("Keep the field-based fingerprint", comparison.recommendation)

    def test_the_flip_is_matched_as_one_milestone_not_two(self):
        comparison = self._flip(new_id="m-9999")

        # The identity key excludes the timestamp and the classifier precisely so that a
        # realised forecast is recognised as the same milestone rather than as one removed
        # and one added.
        self.assertEqual(comparison.added, ())
        self.assertEqual(comparison.removed, ())


class JourneyProgressedTest(SimpleTestCase):
    """A genuinely new milestone, and later enrichment of an existing one."""

    def test_a_new_milestone_is_reported_as_added(self):
        second = first_only()
        extra = copy.deepcopy(milestone(second, "m-0008"))
        extra["id"] = "m-0100"
        extra["journey_event"]["event_type"] = "GTOT"
        extra["journey_event"]["journey_type"] = "EQUIPMENT"
        extra["journey_event"]["event_classifier"] = "ACT"
        second[0]["payload"]["milestones"].append(extra)

        comparison = compare_fetches(first_only(), second, container_number=CONTAINER_NUMBER)

        self.assertEqual(len(comparison.added), 1)
        self.assertEqual(len(comparison.removed), 0)

    def test_richer_metadata_arriving_later_is_reported(self):
        first = first_only()
        customs = milestone(first, "m-0006")
        customs["location"].pop("unlocode")

        second = first_only()
        late = milestone(second, "m-0006")
        late["vessel"] = "ONE APUS"
        late["vessel_imo"] = "9806079"
        late["location"]["geolocation"] = {"latitude": 1.2644, "longitude": 103.7717}

        comparison = compare_fetches(first, second, container_number=CONTAINER_NUMBER)

        enriched = [change for change in comparison.changes if change.enriched_fields]
        self.assertEqual(len(enriched), 1)
        self.assertEqual(
            set(enriched[0].enriched_fields),
            {"vessel", "vessel_imo", "location.unlocode", "location.geolocation"},
        )

    def test_a_new_update_envelope_is_reported(self):
        comparison = compare_fetches(first_only(), updates(), container_number=CONTAINER_NUMBER)

        self.assertEqual(comparison.new_update_ids, ("3b6f0a11-2222-4a00-9000-000000000002",))

    def test_milestones_without_ids_yield_no_stability_claim(self):
        first, second = first_only(), first_only()
        for payload in (first, second):
            for item in payload[0]["payload"]["milestones"]:
                item.pop("id")

        comparison = compare_fetches(first, second, container_number=CONTAINER_NUMBER)

        # "No id to compare" is a different finding from "the id changed".
        self.assertIsNone(comparison.ids_stable)
        self.assertEqual(comparison.identity_verdict, IDENTITY_INCONCLUSIVE)

    def test_an_empty_second_fetch_does_not_crash(self):
        comparison = compare_fetches(first_only(), [], container_number=CONTAINER_NUMBER)

        self.assertEqual(comparison.common_milestones, 0)
        self.assertEqual(len(comparison.removed), 8)


class RecordingTest(SimpleTestCase):
    """A recorded response must be safe to commit and still readable by the mapper."""

    def test_account_identifiers_are_removed(self):
        cleaned = sanitize(updates())

        self.assertNotIn("organization_id", cleaned[0])
        self.assertNotIn("callback_url", cleaned[0])

    def test_nested_account_objects_are_removed(self):
        reference = json.loads((FIXTURES / "reference_aci_completed_oney.json").read_text())

        cleaned = sanitize(reference)

        self.assertNotIn("organization", cleaned)
        self.assertNotIn("organization_id", cleaned)

    def test_secret_shaped_keys_are_redacted_rather_than_dropped(self):
        cleaned = sanitize({"api_key": "live-key", "authorization": "Bearer x", "nested": {"token": "t"}})

        self.assertEqual(cleaned["api_key"], REDACTED)
        self.assertEqual(cleaned["authorization"], REDACTED)
        self.assertEqual(cleaned["nested"]["token"], REDACTED)

    def test_the_evidence_is_kept(self):
        cleaned = sanitize(updates())

        # Redacting these would destroy exactly what the identity experiment measures.
        self.assertEqual(cleaned[0]["reference_id"], "e8991c95-5db2-4c0c-8a02-119611f769df")
        self.assertEqual(cleaned[0]["payload"]["milestones"][0]["id"], "m-0001")
        self.assertEqual(cleaned[0]["payload"]["carrier_scac"], "ONEY")

    def test_a_sanitized_payload_is_still_mappable(self):
        from apps.scm.integrations.vizion.mapper import map_vizion_updates

        events = map_vizion_updates(sanitize(updates()), container_number=CONTAINER_NUMBER)

        self.assertEqual(len(events), 10)

    def test_writing_a_fixture_produces_sanitized_json(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = write_fixture(directory, "recorded", updates())

            written = json.loads(path.read_text())
            self.assertEqual(path.name, "recorded.json")
            self.assertNotIn("organization_id", written[0])
            self.assertEqual(written[0]["payload"]["container_id"], CONTAINER_NUMBER)
