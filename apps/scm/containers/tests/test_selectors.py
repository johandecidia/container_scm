"""Tests for container selectors."""

from django.core.exceptions import ObjectDoesNotExist
from django.test import TestCase

from apps.scm.containers.choices import ContainerCondition, ContainerStatus
from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.selectors import (
    filter_containers,
    get_active_equipment_types,
    get_container_by_id,
    get_equipment_types,
    get_team_containers,
)
from apps.scm.containers.utils import calculate_check_digit
from apps.teams.models import Team


def _et(iso_code="20GP", length_ft=20, category="GP") -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code=iso_code,
        defaults={"category": category, "length_ft": length_ft, "high_cube": False, "description": iso_code},
    )[0]


def _container(team, owner="CSQ", serial="305418", **kwargs) -> Container:
    check = calculate_check_digit(owner, "U", serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check,
        equipment_type=_et(),
        **kwargs,
    )


class GetEquipmentTypesTest(TestCase):
    def test_returns_all(self):
        _et("20GP")
        _et("40GP", 40)
        qs = get_equipment_types()
        self.assertGreaterEqual(qs.count(), 2)

    def test_active_excludes_inactive(self):
        et = _et("20GP")
        et.is_active = False
        et.save()
        active_codes = list(get_active_equipment_types().values_list("iso_code", flat=True))
        self.assertNotIn("20GP", active_codes)


class GetTeamContainersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Team A", slug="team-a")
        cls.other = Team.objects.create(name="Team B", slug="team-b")
        cls.own = _container(cls.team)
        _container(cls.other, owner="MSC", serial="999999")

    def test_returns_only_team_containers(self):
        qs = get_team_containers(self.team)
        self.assertIn(self.own, qs)
        self.assertEqual(qs.count(), 1)


class GetContainerByIdTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Lookup Team", slug="lookup-team")
        cls.other = Team.objects.create(name="Other Lookup", slug="other-lookup")
        cls.container = _container(cls.team)
        cls.other_container = _container(cls.other, owner="MSC", serial="999999")

    def test_returns_own_container(self):
        result = get_container_by_id(self.team, self.container.pk)
        self.assertEqual(result, self.container)

    def test_raises_for_other_team_container(self):
        with self.assertRaises(ObjectDoesNotExist):
            get_container_by_id(self.team, self.other_container.pk)


class FilterContainersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Filter Team", slug="filter-team")
        et_40hc = _et("40HC", 40, "GP")
        et_40hc.high_cube = True
        et_40hc.save()

        cls.c1 = _container(
            cls.team,
            owner="CSQ",
            serial="305418",
            status=ContainerStatus.AVAILABLE,
            condition=ContainerCondition.NEW,
            location_text="Rotterdam",
            manufacturer="CIMC",
        )
        cls.c2 = Container.objects.create(
            team=cls.team,
            owner_code="MSC",
            category_id="U",
            serial_number="999999",
            check_digit=calculate_check_digit("MSC", "U", "999999"),
            equipment_type=et_40hc,
            status=ContainerStatus.IN_TRANSIT,
            condition=ContainerCondition.GOOD,
            location_text="Hamburg",
            manufacturer="Singamas",
        )

    def test_no_filters_returns_all(self):
        self.assertEqual(filter_containers(self.team).count(), 2)

    def test_filter_by_status(self):
        qs = filter_containers(self.team, status=ContainerStatus.AVAILABLE)
        self.assertEqual(qs.count(), 1)
        self.assertIn(self.c1, qs)

    def test_filter_by_condition(self):
        qs = filter_containers(self.team, condition=ContainerCondition.NEW)
        self.assertEqual(qs.count(), 1)
        self.assertIn(self.c1, qs)

    def test_filter_by_equipment_type(self):
        qs = filter_containers(self.team, equipment_type="40HC")
        self.assertEqual(qs.count(), 1)
        self.assertIn(self.c2, qs)

    def test_search_by_owner_code(self):
        qs = filter_containers(self.team, search="CSQ")
        self.assertEqual(qs.count(), 1)
        self.assertIn(self.c1, qs)

    def test_search_by_location(self):
        qs = filter_containers(self.team, search="Rotterdam")
        self.assertIn(self.c1, qs)

    def test_search_by_manufacturer(self):
        qs = filter_containers(self.team, search="Singamas")
        self.assertIn(self.c2, qs)

    def test_search_finds_a_container_by_its_whole_number(self):
        """The number printed on the box has to find the box.

        It is stored as four columns and composed on read, so `icontains` over those
        columns matches nothing for the number typed whole — which is exactly how
        somebody with a container in front of them types it.
        """
        qs = filter_containers(self.team, search=self.c1.container_id)
        self.assertEqual(list(qs), [self.c1])

    def test_search_tolerates_a_number_typed_with_spaces(self):
        number = self.c1.container_id
        spaced = f"{number[:4]} {number[4:10]} {number[10]}"
        self.assertEqual(list(filter_containers(self.team, search=spaced)), [self.c1])

    def test_search_by_a_partial_number_narrows_to_that_prefix(self):
        qs = filter_containers(self.team, search=self.c1.container_id[:8])
        self.assertEqual(list(qs), [self.c1])

    def test_search_by_a_whole_number_with_a_wrong_check_digit_matches_nothing(self):
        """The number as typed is what was asked for — it is not silently corrected."""
        number = self.c1.container_id
        wrong = f"{number[:10]}{(int(number[10]) + 1) % 10}"
        self.assertEqual(filter_containers(self.team, search=wrong).count(), 0)

    def test_sort_newest_default(self):
        # Just verify it doesn't error
        qs = filter_containers(self.team, sort="newest")
        self.assertEqual(qs.count(), 2)

    def test_sort_oldest(self):
        qs = filter_containers(self.team, sort="oldest")
        self.assertEqual(qs.count(), 2)

    def test_no_match_returns_empty(self):
        qs = filter_containers(self.team, search="NOTEXIST")
        self.assertEqual(qs.count(), 0)
