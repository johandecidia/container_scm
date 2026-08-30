"""Reading Traqo's shipment-level ETA out of a response.

The production benchmark settled two things this depends on, and both are asserted here
rather than assumed:

* every event Traqo returned was ``is_actual: 1``, so the ETA is a field and not an
  event — there is nothing to fingerprint and nothing to classify;
* ``data.eta`` equalled the timestamp of the *last* event, the empty container's return
  to the Gothenburg depot, and not the vessel's arrival at the POD eleven days earlier.
  So what the ETA is an ETA *for* is Traqo's business, and is recorded as such.

No network: every payload here is built inline.
"""

import datetime

from django.test import TestCase

from apps.scm.integrations.traqo import PROVIDER_CODE
from apps.scm.integrations.traqo.eta import read_traqo_eta_observation
from apps.scm.integrations.traqo.mapper import (
    TZ_FROM_LOCATION,
    TZ_NO_LOCATION,
    TZ_NOT_PUBLISHED,
    TZ_OFFSET_SUPPLIED,
)
from apps.scm.tracking.eta_observations import ETA_TARGET_PROVIDER_DEFINED

UTC = datetime.UTC
OBSERVED_AT = datetime.datetime(2026, 8, 19, 19, 55, tzinfo=UTC)

GOTEBORG = {"location_id": 2, "location": "Goteborg", "locode": "SEGOT", "timezone": "Europe/Stockholm"}
BORAAS = {"location_id": 3, "location": "BORAAS", "locode": None, "timezone": None}


def payload(**data) -> dict:
    """A Traqo envelope carrying the shipment-level fields, in Traqo's own shape."""
    body = {
        "reference_number": "CPWU2588297",
        "sealine": "MAEU",
        "destination": "Goteborg",
        "locations_table": [GOTEBORG, BORAAS],
        # Traqo's own infrastructure clock, which arrives at UTC+05:30.
        "last_updated_at": "2026-08-19 19:55:37.211575",
        "last_synced_at": "2026-08-19 19:55:24.717817",
    }
    body.update(data)
    return {"success": True, "data": body}


class TraqoEtaReadingTest(TestCase):
    def test_the_eta_is_converted_through_the_destinations_published_zone(self):
        observation = read_traqo_eta_observation(payload(eta="2026-09-14 15:00:00"), observed_at=OBSERVED_AT)

        self.assertIsNotNone(observation)
        self.assertEqual(observation.provider_code, PROVIDER_CODE)
        self.assertEqual(observation.eta_at, datetime.datetime(2026, 9, 14, 13, 0, tzinfo=UTC))
        self.assertEqual(observation.eta_date, datetime.date(2026, 9, 14))
        self.assertEqual(observation.context["timezone"], "Europe/Stockholm")
        self.assertEqual(observation.context["timezone_status"], TZ_FROM_LOCATION)

    def test_the_destination_is_matched_by_name_not_by_row_order(self):
        # BORAAS is the last row; naming Goteborg must still resolve Goteborg's zone.
        observation = read_traqo_eta_observation(payload(eta="2026-01-14 15:00:00"), observed_at=OBSERVED_AT)

        self.assertEqual(observation.target_name, "Goteborg")
        self.assertEqual(observation.target_unlocode, "SEGOT")
        # January, so Stockholm is at +01 and the conversion has to know it.
        self.assertEqual(observation.eta_at, datetime.datetime(2026, 1, 14, 14, 0, tzinfo=UTC))

    def test_a_destination_with_no_published_zone_keeps_the_timestamp_as_sent(self):
        observation = read_traqo_eta_observation(
            payload(eta="2026-07-13 13:15:00", destination="BORAAS"),
            observed_at=OBSERVED_AT,
        )

        # The benchmark's own destination. Guessing Sweden from the country would put an
        # invented offset on the forecast, so the value stands and says why.
        self.assertEqual(observation.eta_at, datetime.datetime(2026, 7, 13, 13, 15, tzinfo=UTC))
        self.assertEqual(observation.eta_date, datetime.date(2026, 7, 13))
        self.assertEqual(observation.context["timezone"], "")
        self.assertEqual(observation.context["timezone_status"], TZ_NOT_PUBLISHED)
        self.assertEqual(observation.context["provider_timestamp"], "2026-07-13 13:15:00")

    def test_a_destination_absent_from_locations_table_is_not_guessed_at(self):
        observation = read_traqo_eta_observation(
            payload(eta="2026-09-14 15:00:00", destination="Nowhere In Particular"),
            observed_at=OBSERVED_AT,
        )

        self.assertEqual(observation.eta_at, datetime.datetime(2026, 9, 14, 15, 0, tzinfo=UTC))
        self.assertEqual(observation.context["timezone_status"], TZ_NO_LOCATION)

    def test_a_sandbox_payload_without_a_locations_table_still_yields_an_observation(self):
        body = payload(eta="2026-05-17 00:00:00", destination="Caucedo, Dominican Republic")
        del body["data"]["locations_table"]

        observation = read_traqo_eta_observation(body, observed_at=OBSERVED_AT)

        self.assertEqual(observation.eta_at, datetime.datetime(2026, 5, 17, 0, 0, tzinfo=UTC))
        self.assertEqual(observation.context["timezone_status"], TZ_NO_LOCATION)

    def test_an_eta_that_states_an_offset_is_honoured(self):
        observation = read_traqo_eta_observation(
            payload(eta="2026-09-14T15:00:00+05:30"),
            observed_at=OBSERVED_AT,
        )

        self.assertEqual(observation.eta_at, datetime.datetime(2026, 9, 14, 9, 30, tzinfo=UTC))
        self.assertEqual(observation.context["timezone_status"], TZ_OFFSET_SUPPLIED)

    def test_what_the_eta_is_for_is_recorded_as_provider_defined(self):
        observation = read_traqo_eta_observation(payload(eta="2026-09-14 15:00:00"), observed_at=OBSERVED_AT)

        self.assertEqual(observation.target, ETA_TARGET_PROVIDER_DEFINED)

    def test_the_providers_own_caveats_are_carried_with_the_forecast(self):
        observation = read_traqo_eta_observation(
            payload(
                eta="2026-09-14 15:00:00",
                eta_reliable=False,
                eta_warning="Vessel schedule unstable",
                status="IN_TRANSIT",
                is_delayed=1,
            ),
            observed_at=OBSERVED_AT,
        )

        self.assertIs(observation.reliable, False)
        self.assertEqual(observation.context["eta_warning"], "Vessel schedule unstable")
        self.assertEqual(observation.context["status"], "IN_TRANSIT")
        self.assertEqual(observation.context["is_delayed"], 1)

    def test_a_missing_reliability_flag_is_not_read_as_either_answer(self):
        observation = read_traqo_eta_observation(payload(eta="2026-09-14 15:00:00"), observed_at=OBSERVED_AT)

        self.assertIsNone(observation.reliable)

    def test_traqos_sync_clock_is_kept_verbatim_and_used_for_nothing(self):
        observation = read_traqo_eta_observation(payload(eta="2026-09-14 15:00:00"), observed_at=OBSERVED_AT)

        self.assertEqual(observation.provider_updated_at, "2026-08-19 19:55:37.211575")
        self.assertEqual(observation.observed_at, OBSERVED_AT)
        # +05:30 is Traqo's infrastructure, not the destination's zone.
        self.assertEqual(observation.context["timezone"], "Europe/Stockholm")

    def test_a_response_with_no_eta_yields_nothing(self):
        self.assertIsNone(read_traqo_eta_observation(payload(), observed_at=OBSERVED_AT))
        self.assertIsNone(read_traqo_eta_observation(payload(eta=None), observed_at=OBSERVED_AT))
        self.assertIsNone(read_traqo_eta_observation(payload(eta=""), observed_at=OBSERVED_AT))

    def test_a_malformed_eta_yields_nothing_rather_than_a_wrong_answer(self):
        for value in ("next tuesday", "2026-13-45 99:99:99", 42, {"eta": "soon"}):
            with self.subTest(value=value):
                self.assertIsNone(read_traqo_eta_observation(payload(eta=value), observed_at=OBSERVED_AT))

    def test_a_response_that_is_not_shaped_like_one_yields_nothing(self):
        self.assertIsNone(read_traqo_eta_observation({}, observed_at=OBSERVED_AT))
        self.assertIsNone(read_traqo_eta_observation({"data": None}, observed_at=OBSERVED_AT))
