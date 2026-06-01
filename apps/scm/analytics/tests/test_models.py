"""Tests for AnalyticsSnapshot model."""

import datetime

from django.db import IntegrityError
from django.test import TestCase

from apps.scm.analytics.models import AnalyticsSnapshot
from apps.teams.models import BaseTeamModel, Team


class AnalyticsSnapshotInheritanceTest(TestCase):
    def test_extends_base_team_model(self):
        self.assertTrue(issubclass(AnalyticsSnapshot, BaseTeamModel))

    def test_has_timestamps(self):
        field_names = [f.name for f in AnalyticsSnapshot._meta.fields]
        self.assertIn("created_at", field_names)
        self.assertIn("updated_at", field_names)


class AnalyticsSnapshotModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Test Team", slug="test-team")
        cls.today = datetime.date(2026, 1, 15)

    def test_create_snapshot(self):
        snapshot = AnalyticsSnapshot.objects.create(
            team=self.team,
            date=self.today,
            total_shipments=10,
            active_shipments=5,
            completed_shipments=3,
            containers_in_transit=4,
            containers_delivered=6,
        )
        self.assertEqual(snapshot.total_shipments, 10)
        self.assertIsNone(snapshot.avg_transit_days)

    def test_default_numeric_fields_are_zero(self):
        snapshot = AnalyticsSnapshot.objects.create(team=self.team, date=self.today)
        self.assertEqual(snapshot.total_shipments, 0)
        self.assertEqual(snapshot.active_shipments, 0)
        self.assertEqual(snapshot.completed_shipments, 0)
        self.assertEqual(snapshot.containers_in_transit, 0)
        self.assertEqual(snapshot.containers_delivered, 0)

    def test_same_team_duplicate_date_raises(self):
        AnalyticsSnapshot.objects.create(team=self.team, date=self.today)
        with self.assertRaises(IntegrityError):
            AnalyticsSnapshot.objects.create(team=self.team, date=self.today)

    def test_different_teams_same_date_allowed(self):
        team2 = Team.objects.create(name="Other Team", slug="other-team")
        AnalyticsSnapshot.objects.create(team=self.team, date=self.today)
        # Should not raise
        AnalyticsSnapshot.objects.create(team=team2, date=self.today)
        self.assertEqual(AnalyticsSnapshot.objects.count(), 2)

    def test_str_contains_date_and_team(self):
        snapshot = AnalyticsSnapshot.objects.create(team=self.team, date=self.today)
        self.assertIn(str(self.today), str(snapshot))
