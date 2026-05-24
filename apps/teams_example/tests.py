from django.test import TestCase, override_settings

from apps.teams.context import EmptyTeamContextException, current_team
from apps.teams.models import Team
from apps.teams_example.models import Player


class TestTeamFiltering(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team1 = Team.objects.create(slug="team1", name="Team1")
        cls.team2 = Team.objects.create(slug="team2", name="Team2")

        Player.objects.bulk_create(
            [
                Player(team=cls.team1, name="t1-p1"),
                Player(team=cls.team1, name="t1-p2"),
                Player(team=cls.team2, name="t2-p1"),
                Player(team=cls.team2, name="t2-p2"),
            ]
        )

    def test_filtering_no_team_in_context(self):
        self.assertEqual(Player.objects.count(), 4)
        self.assertEqual(Player.for_team.count(), 0)

    @override_settings(STRICT_TEAM_CONTEXT=True)
    def test_filtering_no_team_in_context_strict(self):
        self.assertEqual(Player.objects.count(), 4)
        with self.assertRaises(EmptyTeamContextException):
            Player.for_team.count()

    def test_filtering_with_team_in_context(self):
        with current_team(self.team1):
            self.assertEqual(Player.objects.count(), 4)
            self.assertEqual(Player.for_team.count(), 2)

            self.assertQuerySetEqual(
                Player.for_team.values_list("name", flat=True),
                ["t1-p1", "t1-p2"],
                ordered=False,
            )

        with current_team(self.team2):
            self.assertQuerySetEqual(
                Player.for_team.values_list("name", flat=True),
                ["t2-p1", "t2-p2"],
                ordered=False,
            )
