from django.test import SimpleTestCase
from django.utils.functional import SimpleLazyObject

from apps.teams.context import current_team, get_current_team, set_current_team, unset_current_team
from apps.teams.models import Team


class TestTeamContext(SimpleTestCase):
    def setUp(self):
        unset_current_team()

    def tearDown(self):
        unset_current_team()

    def test_using_context_manager_reverts_team(self):
        team1 = _team("team1")
        team2 = _team("team2")

        set_current_team(team1)
        with current_team(team2):
            self.assertIs(get_current_team(), team2)

        self.assertIs(get_current_team(), team1)

    def test_team_context_lazy_object(self):
        team1 = _team("team1")
        team2 = _team("team2")

        self.assertIsNone(get_current_team())

        # test setting with `None` lazy object
        set_current_team(SimpleLazyObject(lambda: None))
        self.assertIsNone(get_current_team())

        set_current_team(SimpleLazyObject(lambda: team1))
        self.assertIs(get_current_team(), team1)

        set_current_team(SimpleLazyObject(lambda: team2))
        self.assertIs(get_current_team(), team2)

    def test_team_context_manager_reentry(self):
        team1 = _team("team1")
        team2 = _team("team2")

        self.assertIsNone(get_current_team())

        with current_team(team1):
            self.assertIs(get_current_team(), team1)

            with current_team(team2):
                self.assertIs(get_current_team(), team2)

            self.assertIs(get_current_team(), team1)

        self.assertIsNone(get_current_team())


def _team(slug):
    return Team(slug=slug, name=slug)
