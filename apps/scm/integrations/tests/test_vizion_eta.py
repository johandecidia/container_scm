"""Vizion's ETA is a milestone, not a field — and the POC must say which milestone.

Traqo's ``data.eta`` had to be recorded as ``provider_defined`` because its value turned
out to be the last event in the list rather than any arrival. Vizion states which
milestone its ETA is: a TRANSPORT/ARRI at the POD. These tests hold it to that, and check
that the target degrades honestly when the label is missing rather than being assumed.
"""

import copy
import json
import pathlib
from datetime import UTC, datetime

from django.test import SimpleTestCase

from apps.scm.integrations.vizion.eta import read_vizion_eta_observation
from apps.scm.integrations.vizion.mapper import read_latest_payload
from apps.scm.tracking.eta_observations import ETA_TARGET_PROVIDER_DEFINED, ETA_TARGET_VESSEL_ARRIVAL_POD

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "vizion"
OBSERVED_AT = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def updates() -> list[dict]:
    return json.loads((FIXTURES / "updates_transshipment.json").read_text())


def first_payload() -> dict:
    return updates()[0]["payload"]


class VizionEtaTest(SimpleTestCase):
    def test_the_pod_arrival_forecast_is_the_eta(self):
        observation = read_vizion_eta_observation(first_payload(), observed_at=OBSERVED_AT)

        self.assertIsNotNone(observation)
        self.assertEqual(observation.provider_code, "vizion")
        self.assertEqual(observation.eta_at, datetime(2026, 9, 10, 5, 0, tzinfo=UTC))
        self.assertEqual(observation.eta_date.isoformat(), "2026-09-10")

    def test_the_target_is_specific_because_vizion_labels_it(self):
        observation = read_vizion_eta_observation(first_payload(), observed_at=OBSERVED_AT)

        # A *specific* target, unlike Traqo's. The benchmark refuses to subtract two ETAs
        # unless both name the same one, so this distinction has downstream teeth.
        self.assertEqual(observation.target, ETA_TARGET_VESSEL_ARRIVAL_POD)
        self.assertEqual(observation.target_name, "Rotterdam, Netherlands")
        self.assertEqual(observation.target_unlocode, "NLRTM")

    def test_the_vessel_and_voyage_behind_the_forecast_travel_with_it(self):
        observation = read_vizion_eta_observation(first_payload(), observed_at=OBSERVED_AT)

        self.assertEqual(observation.context["eta_vessel"], "ONE APUS")
        self.assertEqual(observation.context["eta_vessel_imo"], "9806079")
        self.assertEqual(observation.context["eta_voyage"], "112W")
        self.assertEqual(observation.context["eta_source"], "carrier")

    def test_an_actual_arrival_is_not_an_eta(self):
        payload = first_payload()
        for milestone in payload["milestones"]:
            if milestone["id"] == "m-0007":
                milestone["planned"] = False
                milestone["journey_event"]["event_classifier"] = "ACT"

        # The POD arrival has flipped to the ATA. There is no forecast left, and the only
        # other arrival in the payload is the actual transshipment call.
        self.assertIsNone(read_vizion_eta_observation(payload, observed_at=OBSERVED_AT))

    def test_no_forecast_arrival_yields_no_observation(self):
        payload = first_payload()
        payload["milestones"] = [m for m in payload["milestones"] if m["id"] not in ("m-0007",)]

        self.assertIsNone(read_vizion_eta_observation(payload, observed_at=OBSERVED_AT))

    def test_an_unlabelled_forecast_arrival_degrades_to_provider_defined(self):
        payload = first_payload()
        for milestone in payload["milestones"]:
            if milestone["id"] == "m-0007":
                milestone.pop("shipment_location")

        observation = read_vizion_eta_observation(payload, observed_at=OBSERVED_AT)

        # Still Vizion's view of when the box next arrives, but calling an unlabelled leg
        # the POD would invent a claim Vizion did not make.
        self.assertEqual(observation.target, ETA_TARGET_PROVIDER_DEFINED)
        self.assertEqual(observation.eta_at, datetime(2026, 9, 10, 5, 0, tzinfo=UTC))

    def test_the_pod_wins_over_an_earlier_forecast_leg(self):
        payload = first_payload()
        transship = copy.deepcopy(payload["milestones"][3])
        transship["id"] = "m-9001"
        transship["timestamp"] = "2026-09-20T00:00:00.000+08:00"
        transship["planned"] = True
        transship["journey_event"]["event_classifier"] = "EST"
        payload["milestones"].append(transship)

        observation = read_vizion_eta_observation(payload, observed_at=OBSERVED_AT)

        # The later forecast is at a transshipment port, so it is not the journey's ETA
        # even though it is further in the future.
        self.assertEqual(observation.target, ETA_TARGET_VESSEL_ARRIVAL_POD)
        self.assertEqual(observation.eta_at, datetime(2026, 9, 10, 5, 0, tzinfo=UTC))

    def test_the_newest_update_supplies_the_current_eta(self):
        observation = read_vizion_eta_observation(read_latest_payload(updates()), observed_at=OBSERVED_AT)

        self.assertEqual(observation.eta_at, datetime(2026, 9, 12, 7, 30, tzinfo=UTC))

    def test_an_empty_payload_yields_nothing(self):
        self.assertIsNone(read_vizion_eta_observation({}, observed_at=OBSERVED_AT))
        self.assertIsNone(read_vizion_eta_observation("nonsense", observed_at=OBSERVED_AT))
