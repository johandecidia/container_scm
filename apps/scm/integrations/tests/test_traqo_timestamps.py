"""Traqo's naive timestamps are local times, and this is how they are converted.

The production benchmark of CPWU2588297 against Maersk Direct showed every matched
event off by exactly the location's UTC offset — +8 h for Yantian, +2 h for Gothenburg
— because Phase 1 read Traqo's offsetless ``"YYYY-MM-DD HH:MM:SS"`` as UTC. The
timestamps here are the real ones from that run, with the Maersk instant each must
land on, so the correction is pinned to observed evidence rather than to an assumption.

Traqo publishes an IANA timezone per row of ``locations_table`` and each event points
at one through ``location_id``. That chain is the only authority used: no country, no
coordinates, no other provider, no server timezone.
"""

from datetime import UTC, datetime

from django.test import SimpleTestCase

from apps.scm.integrations.traqo.mapper import (
    TIMESTAMP_AUDIT_KEY,
    TZ_FROM_LOCATION,
    TZ_LOCATION_NOT_FOUND,
    TZ_NO_LOCATION,
    TZ_NOT_PUBLISHED,
    TZ_UNKNOWN_ZONE,
    TZ_UNPARSEABLE,
    map_traqo_container_payload,
)

CONTAINER_NUMBER = "CPWU2588297"

# The three places the production response listed, verbatim — including BORAAS, whose
# timezone Traqo left null.
LOCATIONS = [
    {"location_id": 1, "location": "Yantian", "country": "China", "timezone": "Asia/Shanghai"},
    {"location_id": 2, "location": "Goteborg", "country": "Sweden", "timezone": "Europe/Stockholm"},
    {"location_id": 3, "location": "BORAAS", "country": None, "timezone": None},
]


def payload(*events, locations=None) -> dict:
    """A container response shaped like the production one, with the given events."""
    return {
        "success": True,
        "data": {
            "reference_number": CONTAINER_NUMBER,
            "sealine": "MAEU",
            "events_table": list(events),
            "locations_table": LOCATIONS if locations is None else locations,
        },
    }


def event(location_id, timestamp, **extra) -> dict:
    base = {
        "event_id": 1,
        "idx": 1,
        "location_id": location_id,
        "timestamp": timestamp,
        "event_type": "EQUIPMENT",
        "event_code": "GTIN",
        "is_actual": 1,
        "transport_type": "TRUCK",
    }
    return {**base, **extra}


def mapped(*events, locations=None):
    return map_traqo_container_payload(payload(*events, locations=locations), container_number=CONTAINER_NUMBER)


class TraqoLocalTimeConversionTest(SimpleTestCase):
    """A local timestamp plus the timezone Traqo published for the place."""

    def test_a_chinese_event_is_read_in_shanghai_time(self):
        [result] = mapped(event(1, "2026-05-12 01:09:00"))

        # Yantian is UTC+8 year round; Maersk reported this gate-out at 17:09 the day before.
        self.assertEqual(result.event_datetime, datetime(2026, 5, 11, 17, 9, tzinfo=UTC))
        self.assertEqual(result.event_datetime_timezone, "Asia/Shanghai")

    def test_a_swedish_summer_event_is_read_at_utc_plus_two(self):
        [result] = mapped(event(2, "2026-07-01 15:09:00"))

        self.assertEqual(result.event_datetime, datetime(2026, 7, 1, 13, 9, tzinfo=UTC))
        self.assertEqual(result.event_datetime_timezone, "Europe/Stockholm")

    def test_the_same_wall_clock_time_converts_differently_across_a_dst_boundary(self):
        """Stockholm is +1 in winter and +2 in summer, so a fixed offset would be wrong."""
        winter, summer = mapped(
            event(2, "2026-01-15 08:00:00"),
            event(2, "2026-07-15 08:00:00", event_id=2, idx=2),
        )

        self.assertEqual(winter.event_datetime, datetime(2026, 1, 15, 7, 0, tzinfo=UTC))
        self.assertEqual(summer.event_datetime, datetime(2026, 7, 15, 6, 0, tzinfo=UTC))

    def test_the_zone_actually_used_is_recorded_on_the_event(self):
        [result] = mapped(event(1, "2026-05-12 01:09:00"))

        audit = result.raw_payload[TIMESTAMP_AUDIT_KEY]
        self.assertEqual(audit["provider_timestamp"], "2026-05-12 01:09:00")
        self.assertEqual(audit["timezone"], "Asia/Shanghai")
        self.assertEqual(audit["timezone_status"], TZ_FROM_LOCATION)
        self.assertTrue(audit["converted"])
        self.assertEqual(audit["event_datetime_utc"], "2026-05-11T17:09:00+00:00")

    def test_the_provider_timestamp_is_still_readable_unchanged(self):
        [result] = mapped(event(1, "2026-05-12 01:09:00"))

        # The event verbatim survives beside the conversion, so the correction is auditable.
        self.assertEqual(result.raw_payload["timestamp"], "2026-05-12 01:09:00")
        self.assertEqual(result.raw_payload["location_id"], 1)


class TraqoBenchmarkInstantsTest(SimpleTestCase):
    """Every convertible event of the benchmark container, against Maersk's instant."""

    # (location_id, Traqo local timestamp, what Maersk reported in UTC)
    CASES = (
        (1, "2026-05-12 01:09:00", datetime(2026, 5, 11, 17, 9, tzinfo=UTC)),
        (1, "2026-05-12 15:33:00", datetime(2026, 5, 12, 7, 33, tzinfo=UTC)),
        (1, "2026-05-16 14:54:00", datetime(2026, 5, 16, 6, 54, tzinfo=UTC)),
        (1, "2026-05-17 12:50:00", datetime(2026, 5, 17, 4, 50, tzinfo=UTC)),
        (2, "2026-07-01 15:09:00", datetime(2026, 7, 1, 13, 9, tzinfo=UTC)),
        (2, "2026-07-01 20:00:00", datetime(2026, 7, 1, 18, 0, tzinfo=UTC)),
        (2, "2026-07-10 12:35:00", datetime(2026, 7, 10, 10, 35, tzinfo=UTC)),
        (2, "2026-07-13 13:15:00", datetime(2026, 7, 13, 11, 15, tzinfo=UTC)),
    )

    def test_each_matched_event_lands_on_the_instant_maersk_reported(self):
        for location_id, timestamp, expected in self.CASES:
            with self.subTest(timestamp=timestamp):
                [result] = mapped(event(location_id, timestamp))
                self.assertEqual(result.event_datetime, expected)


class TraqoUnknownTimezoneTest(SimpleTestCase):
    """What happens when Traqo does not say — which BORAAS actually did not."""

    def test_a_location_with_a_null_timezone_is_not_shifted(self):
        [result] = mapped(event(3, "2026-07-13 09:30:05"))

        # Kept exactly as Traqo sent it. Boraas is almost certainly Europe/Stockholm,
        # and inferring that from the country or from Maersk's SEBOS event is precisely
        # what must not happen: it would be a guess wearing an authoritative offset.
        self.assertEqual(result.event_datetime, datetime(2026, 7, 13, 9, 30, 5, tzinfo=UTC))

    def test_no_timezone_is_claimed_for_it(self):
        [result] = mapped(event(3, "2026-07-13 09:30:05"))

        # Blank, while every converted event names its zone — so these rows are findable.
        self.assertEqual(result.event_datetime_timezone, "")

    def test_the_reason_the_zone_is_unknown_is_recorded(self):
        [result] = mapped(event(3, "2026-07-13 09:30:05"))

        audit = result.raw_payload[TIMESTAMP_AUDIT_KEY]
        self.assertEqual(audit["timezone_status"], TZ_NOT_PUBLISHED)
        self.assertFalse(audit["converted"])
        self.assertEqual(audit["provider_timestamp"], "2026-07-13 09:30:05")

    def test_the_event_is_still_mapped_in_full(self):
        [result] = mapped(event(3, "2026-07-13 09:30:05", location="BORAAS"))

        # An unknown zone costs the zone, never the event.
        self.assertEqual(result.location_name, "BORAAS")
        self.assertEqual(result.event_code, "GTIN")
        self.assertIsNotNone(result.event_datetime)

    def test_an_event_naming_no_location_is_left_as_sent(self):
        [result] = mapped({"idx": 1, "timestamp": "2026-03-01 00:00:00", "event_code": "GTIN"})

        self.assertEqual(result.event_datetime, datetime(2026, 3, 1, tzinfo=UTC))
        self.assertEqual(result.event_datetime_timezone, "")
        self.assertEqual(result.raw_payload[TIMESTAMP_AUDIT_KEY]["timezone_status"], TZ_NO_LOCATION)

    def test_a_location_id_the_response_does_not_list_is_left_as_sent(self):
        [result] = mapped(event(99, "2026-03-01 00:00:00"))

        self.assertEqual(result.event_datetime, datetime(2026, 3, 1, tzinfo=UTC))
        self.assertEqual(result.raw_payload[TIMESTAMP_AUDIT_KEY]["timezone_status"], TZ_LOCATION_NOT_FOUND)

    def test_a_timezone_that_is_not_an_iana_name_is_not_invented_into_one(self):
        locations = [{"location_id": 1, "location": "Yantian", "timezone": "China Standard Time"}]

        [result] = mapped(event(1, "2026-05-12 01:09:00"), locations=locations)

        self.assertEqual(result.event_datetime, datetime(2026, 5, 12, 1, 9, tzinfo=UTC))
        self.assertEqual(result.event_datetime_timezone, "")
        self.assertEqual(result.raw_payload[TIMESTAMP_AUDIT_KEY]["timezone_status"], TZ_UNKNOWN_ZONE)

    def test_an_unreadable_timestamp_keeps_the_event_and_says_why(self):
        [result] = mapped(event(1, "next tuesday"))

        self.assertIsNone(result.event_datetime)
        audit = result.raw_payload[TIMESTAMP_AUDIT_KEY]
        self.assertEqual(audit["timezone_status"], TZ_UNPARSEABLE)
        self.assertEqual(audit["provider_timestamp"], "next tuesday")
        self.assertEqual(audit["event_datetime_utc"], "")

    def test_a_stated_offset_beats_the_location_table(self):
        # Traqo does not send offsets today; if it starts, what it states is authoritative.
        [result] = mapped(event(1, "2026-05-12T01:09:00+05:30"))

        self.assertEqual(result.event_datetime, datetime(2026, 5, 11, 19, 39, tzinfo=UTC))

    def test_traqos_own_sync_clock_is_never_used_as_an_event_timezone(self):
        # last_synced_at / last_updated_at / closed_at arrive at UTC+05:30 — Traqo's
        # infrastructure, not the box's location. They must not reach an event.
        response = payload(event(3, "2026-07-13 09:30:05"))
        response["data"]["last_synced_at"] = "2026-08-19 19:55:24.717817"
        response["data"]["last_updated_at"] = "2026-08-19 19:55:37.211575"
        response["data"]["closed_at"] = "2026-08-19 19:55:37.029514"

        [result] = map_traqo_container_payload(response, container_number=CONTAINER_NUMBER)

        self.assertEqual(result.event_datetime, datetime(2026, 7, 13, 9, 30, 5, tzinfo=UTC))
        self.assertEqual(result.event_datetime_timezone, "")
