"""Tests for container conditions as team-owned master data.

Conditions used to be a ``TextChoices`` enum. What these tests pin down is the part
that changed when they became rows: that they belong to one team, that renaming one
is safe, that retiring one stops it being offered without rewriting history, and that
the 0009 → 0011 migration staging really does carry the old strings across.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import ProtectedError
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from apps.scm.containers.conditions import DEFAULT_CONTAINER_CONDITIONS, ensure_default_conditions
from apps.scm.containers.forms import ContainerAttributesForm, ContainerForm
from apps.scm.containers.models import Container, ContainerCondition, EquipmentType
from apps.scm.containers.selectors import filter_containers, get_condition_options, get_default_condition
from apps.scm.containers.utils import calculate_check_digit
from apps.teams.models import Team
from apps.teams.roles import ROLE_ADMIN, ROLE_MEMBER
from apps.users.models import CustomUser

OWNER = "CSQ"
CAT = "U"
SERIAL = "305418"
CHECK = calculate_check_digit(OWNER, CAT, SERIAL)
VALID_ID = f"{OWNER}{CAT}{SERIAL}{CHECK}"


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team, condition=None, owner=OWNER, serial=SERIAL) -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id=CAT,
        serial_number=serial,
        check_digit=calculate_check_digit(owner, CAT, serial),
        equipment_type=_et(),
        condition=condition,
    )


class DefaultConditionsTest(TestCase):
    """A new team starts with a usable list, and seeding is safe to repeat."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Defaults", slug="condition-defaults")

    def test_a_new_team_gets_the_default_set(self):
        codes = list(ContainerCondition.objects.filter(team=self.team).values_list("code", flat=True))
        self.assertEqual(codes, [code for code, _name in DEFAULT_CONTAINER_CONDITIONS])

    def test_defaults_are_in_the_order_they_are_declared(self):
        names = list(ContainerCondition.objects.filter(team=self.team).values_list("name", flat=True))
        self.assertEqual(names, [name for _code, name in DEFAULT_CONTAINER_CONDITIONS])

    def test_seeding_again_creates_nothing_and_keeps_renames(self):
        condition = ContainerCondition.objects.get(team=self.team, code="AI")
        condition.name = "Sold as seen"
        condition.save()

        ensure_default_conditions(self.team)

        self.assertEqual(ContainerCondition.objects.filter(team=self.team).count(), len(DEFAULT_CONTAINER_CONDITIONS))
        condition.refresh_from_db()
        self.assertEqual(condition.name, "Sold as seen")

    def test_seeding_again_does_not_revive_a_retired_condition(self):
        condition = ContainerCondition.objects.get(team=self.team, code="WW")
        condition.is_active = False
        condition.save()

        ensure_default_conditions(self.team)

        condition.refresh_from_db()
        self.assertFalse(condition.is_active)

    def test_default_condition_is_the_first_active_one(self):
        self.assertEqual(get_default_condition(self.team).code, "NEW")

    def test_default_condition_skips_retired_ones(self):
        ContainerCondition.objects.filter(team=self.team, code="NEW").update(is_active=False)
        self.assertEqual(get_default_condition(self.team).code, "IICL")

    def test_no_default_when_the_team_has_retired_everything(self):
        ContainerCondition.objects.filter(team=self.team).update(is_active=False)
        self.assertIsNone(get_default_condition(self.team))


class ConditionTeamIsolationTest(TestCase):
    """One team's vocabulary is invisible to another, and codes do not collide across."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Alpha", slug="condition-alpha")
        cls.other_team = Team.objects.create(name="Beta", slug="condition-beta")

    def test_conditions_are_team_isolated(self):
        ContainerCondition.objects.create(team=self.team, code="SCRAP", name="Scrap")

        self.assertTrue(get_condition_options(self.team).filter(code="SCRAP").exists())
        self.assertFalse(get_condition_options(self.other_team).filter(code="SCRAP").exists())

    def test_two_teams_may_use_the_same_code(self):
        """The point of scoping: 'CW' means whatever each team says it means."""
        mine = ContainerCondition.objects.get(team=self.team, code="CW")
        theirs = ContainerCondition.objects.get(team=self.other_team, code="CW")
        theirs.name = "Cargo worthy (surveyed)"
        theirs.save()

        mine.refresh_from_db()
        self.assertEqual(mine.name, "Cargo Worthy")
        self.assertNotEqual(mine.pk, theirs.pk)

    def test_one_team_cannot_use_the_same_code_twice(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContainerCondition.objects.create(team=self.team, code="CW", name="Cargo Worthy (duplicate)")

    def test_a_container_may_not_use_another_teams_condition(self):
        foreign = ContainerCondition.objects.get(team=self.other_team, code="CW")
        with self.assertRaises(ValidationError) as caught:
            _container(self.team, condition=foreign)
        self.assertIn("condition", caught.exception.message_dict)
        self.assertFalse(Container.objects.filter(team=self.team).exists())

    def test_a_containers_own_teams_condition_is_accepted(self):
        mine = ContainerCondition.objects.get(team=self.team, code="CW")
        container = _container(self.team, condition=mine)
        self.assertEqual(container.condition, mine)

    def test_a_container_needs_no_condition_at_all(self):
        self.assertIsNone(_container(self.team).condition)

    def test_the_edit_form_offers_only_this_teams_conditions(self):
        offered = ContainerForm(team=self.team).fields["condition"].queryset
        self.assertEqual({c.team_id for c in offered}, {self.team.pk})

    def test_the_intake_form_rejects_another_teams_condition(self):
        foreign = ContainerCondition.objects.get(team=self.other_team, code="CW")
        form = ContainerAttributesForm({"condition": foreign.pk}, team=self.team)
        self.assertFalse(form.is_valid())
        self.assertIn("condition", form.errors)


class ConditionRenameTest(TestCase):
    """Renaming is the whole point: the relation is to the row, not to the wording."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Rename", slug="condition-rename")

    def test_renaming_leaves_the_relation_alone(self):
        condition = ContainerCondition.objects.get(team=self.team, code="AI")
        container = _container(self.team, condition=condition)

        condition.name = "Sold as seen"
        condition.save()

        container.refresh_from_db()
        self.assertEqual(container.condition_id, condition.pk)
        self.assertEqual(container.condition.name, "Sold as seen")
        self.assertEqual(container.condition.code, "AI")

    def test_renaming_does_not_disturb_filtering_by_code(self):
        condition = ContainerCondition.objects.get(team=self.team, code="AI")
        container = _container(self.team, condition=condition)
        condition.name = "Sold as seen"
        condition.save()

        self.assertIn(container, filter_containers(self.team, condition="AI"))


class RetiredConditionTest(TestCase):
    """Retiring stops a condition being chosen. It does not rewrite what it graded."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Retire", slug="condition-retire")
        cls.user = CustomUser.objects.create_user(username="retire@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_ADMIN})
        cls.retired = ContainerCondition.objects.create(
            team=cls.team, code="GOOD", name="Good", sort_order=90, is_active=False
        )
        cls.container = _container(cls.team, condition=cls.retired)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_a_retired_condition_is_not_offered_for_a_new_container(self):
        offered = ContainerAttributesForm(team=self.team).fields["condition"].queryset
        self.assertNotIn(self.retired, offered)

    def test_a_retired_condition_is_not_offered_as_a_filter(self):
        self.assertNotIn(self.retired, get_condition_options(self.team))

    def test_a_retired_condition_stays_selectable_on_the_container_carrying_it(self):
        """Otherwise opening the edit form would propose clearing a value nobody touched."""
        offered = ContainerForm(instance=self.container, team=self.team).fields["condition"].queryset
        self.assertIn(self.retired, offered)

    def test_a_retired_condition_is_not_selectable_on_a_container_without_it(self):
        other = _container(self.team, serial="999999")
        offered = ContainerForm(instance=other, team=self.team).fields["condition"].queryset
        self.assertNotIn(self.retired, offered)

    def test_a_retired_condition_still_shows_on_the_container_list(self):
        response = self.client.get(reverse("containers:list"))
        self.assertContains(response, "Good")

    def test_a_retired_condition_still_shows_on_the_container_workspace(self):
        response = self.client.get(reverse("containers:detail", kwargs={"container_id": self.container.pk}))
        self.assertContains(response, "Good")

    def test_containers_graded_with_it_can_still_be_filtered_for(self):
        self.assertIn(self.container, filter_containers(self.team, condition="GOOD"))


class ConditionProtectionTest(TestCase):
    """A condition in use cannot be deleted. Retiring is the only way to stop using it."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Protect", slug="condition-protect")

    def test_a_condition_in_use_is_protected(self):
        condition = ContainerCondition.objects.get(team=self.team, code="CW")
        _container(self.team, condition=condition)
        with self.assertRaises(ProtectedError):
            condition.delete()
        self.assertTrue(ContainerCondition.objects.filter(pk=condition.pk).exists())

    def test_an_unused_condition_can_still_be_deleted(self):
        condition = ContainerCondition.objects.get(team=self.team, code="CW")
        condition.delete()
        self.assertFalse(ContainerCondition.objects.filter(pk=condition.pk).exists())


class ConditionSettingsPageTest(TestCase):
    """The Settings page: list, add, rename, retire. No delete."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Settings", slug="condition-settings")
        cls.user = CustomUser.objects.create_user(username="settings@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_ADMIN})

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_list_shows_the_teams_conditions(self):
        response = self.client.get(reverse("containers:condition_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cargo Worthy")
        self.assertContains(response, "Wind &amp; Water Tight")

    def test_the_list_shows_how_many_containers_use_each(self):
        _container(self.team, condition=ContainerCondition.objects.get(team=self.team, code="CW"))
        response = self.client.get(reverse("containers:condition_list"))
        counts = {c.code: c.container_count for c in response.context["conditions"]}
        self.assertEqual(counts["CW"], 1)
        self.assertEqual(counts["NEW"], 0)

    def test_the_list_does_not_show_another_teams_conditions(self):
        other = Team.objects.create(name="Elsewhere", slug="condition-elsewhere")
        ContainerCondition.objects.create(team=other, code="SECRET", name="Their Own Grade")
        response = self.client.get(reverse("containers:condition_list"))
        self.assertNotContains(response, "Their Own Grade")

    def test_adding_a_condition(self):
        response = self.client.post(
            reverse("containers:condition_create"),
            data={"name": "Scrap", "code": "scrap", "sort_order": 50, "is_active": "on"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        condition = ContainerCondition.objects.get(team=self.team, code="SCRAP")
        self.assertEqual(condition.name, "Scrap")

    def test_a_duplicate_code_is_a_form_error_not_a_database_fault(self):
        response = self.client.post(
            reverse("containers:condition_create"),
            data={"name": "Cargo Worthy Again", "code": "CW", "sort_order": 60, "is_active": "on"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already uses that code")
        self.assertEqual(ContainerCondition.objects.filter(team=self.team, code="CW").count(), 1)

    def test_another_teams_code_is_not_a_duplicate(self):
        other = Team.objects.create(name="Elsewhere", slug="condition-elsewhere-2")
        ContainerCondition.objects.create(team=other, code="SCRAP", name="Scrap")
        self.client.post(
            reverse("containers:condition_create"),
            data={"name": "Scrap", "code": "SCRAP", "sort_order": 50, "is_active": "on"},
            HTTP_HX_REQUEST="true",
        )
        self.assertTrue(ContainerCondition.objects.filter(team=self.team, code="SCRAP").exists())

    def test_renaming_through_the_page(self):
        condition = ContainerCondition.objects.get(team=self.team, code="AI")
        self.client.post(
            reverse("containers:condition_update", kwargs={"condition_id": condition.pk}),
            data={"name": "Sold as seen", "code": "AI", "sort_order": condition.sort_order, "is_active": "on"},
            HTTP_HX_REQUEST="true",
        )
        condition.refresh_from_db()
        self.assertEqual(condition.name, "Sold as seen")

    def test_deactivating_and_reactivating(self):
        condition = ContainerCondition.objects.get(team=self.team, code="AI")
        url = reverse("containers:condition_deactivate", kwargs={"condition_id": condition.pk})

        self.client.post(url, HTTP_HX_REQUEST="true")
        condition.refresh_from_db()
        self.assertFalse(condition.is_active)

        self.client.post(url, HTTP_HX_REQUEST="true")
        condition.refresh_from_db()
        self.assertTrue(condition.is_active)

    def test_another_teams_condition_cannot_be_edited(self):
        other = Team.objects.create(name="Elsewhere", slug="condition-elsewhere-3")
        foreign = ContainerCondition.objects.create(team=other, code="SCRAP", name="Scrap")
        response = self.client.post(
            reverse("containers:condition_update", kwargs={"condition_id": foreign.pk}),
            data={"name": "Hijacked", "code": "SCRAP", "sort_order": 0, "is_active": "on"},
        )
        self.assertEqual(response.status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.name, "Scrap")

    def test_the_page_requires_login(self):
        anonymous = Client()
        response = anonymous.get(reverse("containers:condition_list"))
        self.assertEqual(response.status_code, 302)

    def test_a_member_without_admin_rights_cannot_reach_or_change_conditions(self):
        """Conditions are Settings → Container settings, and Settings is admin-only.

        The grading vocabulary is master data the whole team reads, so retiring or
        renaming a value is not an operator's change to make.
        """
        member = CustomUser.objects.create_user(username="ops@example.com", password="pass")
        self.team.members.add(member, through_defaults={"role": ROLE_MEMBER})
        condition = ContainerCondition.objects.get(team=self.team, code="CW")
        client = Client()
        client.force_login(member)

        routes = [
            ("get", reverse("containers:condition_list")),
            ("post", reverse("containers:condition_create")),
            ("post", reverse("containers:condition_update", kwargs={"condition_id": condition.pk})),
            ("post", reverse("containers:condition_deactivate", kwargs={"condition_id": condition.pk})),
        ]
        for method, url in routes:
            with self.subTest(url=url):
                self.assertEqual(getattr(client, method)(url).status_code, 404)

        condition.refresh_from_db()
        self.assertTrue(condition.is_active)
        self.assertEqual(condition.name, "Cargo Worthy")


class ContainerConditionMigrationTest(TransactionTestCase):
    """The 0009 → 0011 staging carries the strings that are actually in the database.

    Run against the real migration chain rather than a hand-rolled imitation of it,
    because what is being tested is the staging: a nullable FK added beside the old
    column, filled from the distinct values found, and only then swapped in. The
    values used here are the original vocabulary — ``GOOD`` was the old default and is
    the one a live database is most likely to be full of.
    """

    # scm_containers is migrated backwards and forwards here, so the surrounding
    # transaction wrapping of TestCase would not survive it.
    available_apps = None

    APP = "scm_containers"
    BEFORE = "0009_containercondition"
    AFTER = "0011_container_condition_fk"

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([(self.APP, target)])
        executor.loader.build_graph()
        return executor.loader.project_state([(self.APP, target)]).apps

    def setUp(self):
        # Whatever happens, leave the database on the latest migration: the rest of
        # the suite shares it.
        self.addCleanup(self._migrate, self.AFTER)

    def test_existing_condition_strings_become_rows_and_nothing_loses_its_grade(self):
        apps = self._migrate(self.BEFORE)
        Team = apps.get_model("teams", "Team")
        Container = apps.get_model(self.APP, "Container")
        EquipmentTypeModel = apps.get_model(self.APP, "EquipmentType")

        equipment_type = EquipmentTypeModel.objects.create(
            iso_code="22G1", category="GP", length_ft=20, high_cube=False, description="20' GP"
        )
        # Two teams, so the migration has to key its rows by team and not merge them.
        one = Team.objects.create(name="Migration One", slug="condition-migration-one")
        two = Team.objects.create(name="Migration Two", slug="condition-migration-two")
        empty = Team.objects.create(name="Migration Empty", slug="condition-migration-empty")

        for team, serial, condition in (
            (one, "100001", "GOOD"),
            (one, "100002", "NEW"),
            (two, "100003", "GOOD"),
            (two, "100004", "DAMAGED"),
        ):
            Container.objects.create(
                team=team,
                owner_code="CSQ",
                category_id="U",
                serial_number=serial,
                check_digit=calculate_check_digit("CSQ", "U", serial),
                equipment_type=equipment_type,
                condition=condition,
            )

        apps = self._migrate(self.AFTER)
        Container = apps.get_model(self.APP, "Container")
        ContainerConditionModel = apps.get_model(self.APP, "ContainerCondition")

        graded = {
            (c.team_id, c.serial_number): c.condition
            for c in Container.objects.filter(team_id__in=[one.pk, two.pk]).select_related("condition")
        }
        self.assertEqual(len(graded), 4)
        # Nothing lost its grade, and every grade kept its name.
        self.assertIsNotNone(graded[(one.pk, "100001")])
        self.assertEqual(graded[(one.pk, "100001")].name, "Good")
        self.assertEqual(graded[(one.pk, "100002")].name, "New")
        self.assertEqual(graded[(two.pk, "100004")].name, "Damaged")

        # Each team got its own row for GOOD — same code, different teams.
        self.assertNotEqual(graded[(one.pk, "100001")].pk, graded[(two.pk, "100003")].pk)
        self.assertEqual(graded[(two.pk, "100003")].code, "GOOD")

        # The replaced vocabulary is retired rather than re-offered.
        self.assertFalse(graded[(one.pk, "100001")].is_active)
        self.assertFalse(graded[(two.pk, "100004")].is_active)

        # The current default set is active and present for every team, including one
        # that has no containers at all.
        for team in (one, two, empty):
            active = set(
                ContainerConditionModel.objects.filter(team_id=team.pk, is_active=True).values_list("code", flat=True)
            )
            self.assertEqual(active, {code for code, _name in DEFAULT_CONTAINER_CONDITIONS})
