"""Which clock a local time at a location is on, and reading one across DST."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.scm.containers.location_time import (
    FROM_ACTIVE,
    FROM_LOCATION,
    FROM_PARENT_LOCATION,
    localize,
    resolve_location_timezone,
)
from apps.scm.containers.models import ContainerLocation
from apps.teams.models import Team

STOCKHOLM = ZoneInfo("Europe/Stockholm")


class ResolveLocationTimezoneTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="TZ", slug="location-tz")
        cls.port = ContainerLocation.objects.create(team=cls.team, name="Göteborg", timezone="Europe/Stockholm")
        cls.terminal = ContainerLocation.objects.create(team=cls.team, name="Oceanterminalen", parent_location=cls.port)
        cls.rotterdam = ContainerLocation.objects.create(team=cls.team, name="Rotterdam", timezone="Europe/Amsterdam")
        cls.bare = ContainerLocation.objects.create(team=cls.team, name="Somewhere")

    def test_the_locations_own_timezone_wins(self):
        with timezone.override("America/New_York"):
            resolved = resolve_location_timezone(self.team, self.rotterdam)

        self.assertEqual((resolved.name, resolved.source), ("Europe/Amsterdam", FROM_LOCATION))

    def test_a_location_without_one_takes_its_nearest_parents(self):
        resolved = resolve_location_timezone(self.team, self.terminal)

        self.assertEqual((resolved.name, resolved.source), ("Europe/Stockholm", FROM_PARENT_LOCATION))
        self.assertEqual(resolved.location, self.port)

    def test_an_unrecognised_zone_name_is_skipped(self):
        self.bare.timezone = "Mars/Olympus"

        with timezone.override("Asia/Shanghai"):
            resolved = resolve_location_timezone(self.team, self.bare)

        self.assertEqual((resolved.name, resolved.source), ("Asia/Shanghai", FROM_ACTIVE))

    def test_with_no_location_timezone_the_active_one_is_used(self):
        with timezone.override("Asia/Shanghai"):
            self.assertEqual(resolve_location_timezone(self.team, self.bare).name, "Asia/Shanghai")
        # No user timezone active: the system default.
        self.assertEqual(resolve_location_timezone(self.team, self.bare).name, "UTC")


class LocalizeTest(SimpleTestCase):
    def test_summer_time_is_cest(self):
        local = localize(datetime(2026, 9, 17, 16, 0, 51), STOCKHOLM)

        self.assertEqual(local.instant.astimezone(UTC), datetime(2026, 9, 17, 14, 0, 51, tzinfo=UTC))
        self.assertFalse(local.is_ambiguous or local.is_nonexistent)

    def test_winter_time_is_cet(self):
        local = localize(datetime(2026, 1, 15, 16, 0, 51), STOCKHOLM)

        self.assertEqual(local.instant.astimezone(UTC), datetime(2026, 1, 15, 15, 0, 51, tzinfo=UTC))

    def test_a_repeated_autumn_time_is_flagged_and_read_as_summer_time(self):
        # 2026-10-25: 03:00 CEST falls back to 02:00 CET, so 02:30 happens twice.
        local = localize(datetime(2026, 10, 25, 2, 30), STOCKHOLM)

        self.assertTrue(local.is_ambiguous)
        self.assertEqual(local.instant.astimezone(UTC), datetime(2026, 10, 25, 0, 30, tzinfo=UTC))

    def test_a_skipped_spring_time_is_flagged(self):
        # 2026-03-29: 02:00 CET jumps to 03:00 CEST, so 02:30 never happens.
        local = localize(datetime(2026, 3, 29, 2, 30), STOCKHOLM)

        self.assertTrue(local.is_nonexistent)
        self.assertEqual(local.instant.astimezone(UTC), datetime(2026, 3, 29, 1, 30, tzinfo=UTC))

    def test_an_aware_time_keeps_its_own_offset(self):
        value = datetime(2026, 9, 17, 16, 0, 51, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertEqual(localize(value, STOCKHOLM).instant, value)
