"""Tests for analytics selectors."""

import datetime

from django.test import TestCase

from apps.scm.analytics.models import AnalyticsSnapshot
from apps.scm.analytics.selectors import (
    get_latest_snapshot,
    get_snapshot_for_date,
    get_snapshots_for_team,
)
from apps.teams.models import Team


def _snapshot(team: Team, date: datetime.date, **kwargs) -> AnalyticsSnapshot:
    return AnalyticsSnapshot.objects.create(team=team, date=date, **kwargs)


class GetSnapshotsForTeamTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Selector Team", slug="selector-team")
        cls.other_team = Team.objects.create(name="Other Selector", slug="other-selector")

    def test_returns_only_team_snapshots(self):
        _snapshot(self.team, datetime.date(2026, 1, 1))
        _snapshot(self.other_team, datetime.date(2026, 1, 1))
        qs = get_snapshots_for_team(self.team)
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().team, self.team)

    def test_ordered_newest_first(self):
        _snapshot(self.team, datetime.date(2026, 1, 1))
        _snapshot(self.team, datetime.date(2026, 1, 3))
        _snapshot(self.team, datetime.date(2026, 1, 2))
        dates = list(get_snapshots_for_team(self.team).values_list("date", flat=True))
        self.assertEqual(dates, sorted(dates, reverse=True))


class GetLatestSnapshotTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Latest Team", slug="latest-team")

    def test_returns_none_when_no_snapshots(self):
        self.assertIsNone(get_latest_snapshot(self.team))

    def test_returns_most_recent(self):
        _snapshot(self.team, datetime.date(2026, 1, 1))
        newest = _snapshot(self.team, datetime.date(2026, 1, 5))
        self.assertEqual(get_latest_snapshot(self.team), newest)

    def test_does_not_return_other_team_snapshot(self):
        other = Team.objects.create(name="Other Latest", slug="other-latest")
        _snapshot(other, datetime.date(2026, 1, 5))
        self.assertIsNone(get_latest_snapshot(self.team))


class GetSnapshotForDateTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Date Team", slug="date-team")
        cls.target_date = datetime.date(2026, 3, 15)

    def test_returns_correct_snapshot(self):
        snapshot = _snapshot(self.team, self.target_date)
        result = get_snapshot_for_date(self.team, self.target_date)
        self.assertEqual(result, snapshot)

    def test_returns_none_for_wrong_date(self):
        _snapshot(self.team, self.target_date)
        result = get_snapshot_for_date(self.team, datetime.date(2026, 3, 16))
        self.assertIsNone(result)

    def test_returns_none_for_other_team(self):
        other = Team.objects.create(name="Other Date", slug="other-date")
        _snapshot(other, self.target_date)
        result = get_snapshot_for_date(self.team, self.target_date)
        self.assertIsNone(result)
