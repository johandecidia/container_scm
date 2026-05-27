"""
Kort 5 — Selector-test.
Acceptanskriterier: Selectors returnerar bara det egna teamets data (team-isolering).
"""

from django.test import TestCase

from apps.scm.containers.models import Container
from apps.scm.containers.selectors import (
    filter_team_containers,
    get_container_by_id,
    list_team_containers,
)
from apps.teams.models import Team


class ListTeamContainersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Team A", slug="team-a")
        cls.other_team = Team.objects.create(name="Team B", slug="team-b")
        cls.own = Container.objects.create(team=cls.team, container_number="OWN000001")
        Container.objects.create(team=cls.other_team, container_number="OTHER00001")

    def test_list_team_containers_only_returns_team_data(self):
        results = list_team_containers(team=self.team)
        self.assertIn(self.own, results)
        self.assertEqual(results.count(), 1)

    def test_list_team_containers_excludes_other_team(self):
        results = list_team_containers(team=self.team)
        container_numbers = list(results.values_list("container_number", flat=True))
        self.assertNotIn("OTHER00001", container_numbers)


class FilterTeamContainersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Filter Team", slug="filter-team")
        cls.c1 = Container.objects.create(
            team=cls.team, container_number="MSCU1111111", carrier="MSC", status="in_transit"
        )
        cls.c2 = Container.objects.create(
            team=cls.team, container_number="EVGR2222222", carrier="Evergreen", status="available"
        )

    def test_no_filters_returns_all(self):
        results = filter_team_containers(team=self.team)
        self.assertEqual(results.count(), 2)

    def test_filter_by_container_number(self):
        results = filter_team_containers(team=self.team, query_params={"q": "MSCU"})
        self.assertEqual(results.count(), 1)
        self.assertEqual(results.first(), self.c1)

    def test_filter_by_status(self):
        results = filter_team_containers(team=self.team, query_params={"status": "available"})
        self.assertEqual(results.count(), 1)
        self.assertEqual(results.first(), self.c2)

    def test_filter_by_carrier(self):
        results = filter_team_containers(team=self.team, query_params={"carrier": "msc"})
        self.assertEqual(results.count(), 1)
        self.assertEqual(results.first(), self.c1)

    def test_filter_no_match_returns_empty(self):
        results = filter_team_containers(team=self.team, query_params={"q": "NOTEXIST"})
        self.assertEqual(results.count(), 0)


class GetContainerByIdTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Lookup Team", slug="lookup-team")
        cls.other_team = Team.objects.create(name="Other Lookup Team", slug="other-lookup-team")
        cls.container = Container.objects.create(team=cls.team, container_number="LOOK0000001")
        cls.other_container = Container.objects.create(team=cls.other_team, container_number="OTHR0000001")

    def test_get_container_by_id_own_team(self):
        result = get_container_by_id(team=self.team, container_id=self.container.pk)
        self.assertEqual(result, self.container)

    def test_get_container_by_id_other_team_raises(self):
        from django.core.exceptions import ObjectDoesNotExist

        with self.assertRaises(ObjectDoesNotExist):
            get_container_by_id(team=self.team, container_id=self.other_container.pk)
