"""Business Central item classification: rule precedence, forms, admin-only UI, team isolation."""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.models import EquipmentType
from apps.scm.integrations.business_systems.business_central.item_rules import ItemRuleSet, resolve_item
from apps.scm.integrations.item_forms import ItemRuleForm
from apps.scm.integrations.models import BusinessCentralItemLedgerEntry, BusinessCentralItemRule, Integration
from apps.teams.models import Membership, Team
from apps.teams.roles import ROLE_ADMIN, ROLE_MEMBER
from apps.users.models import CustomUser

CONTAINER = BusinessCentralItemLedgerEntry.ItemClassification.CONTAINER
NOT_CONTAINER = BusinessCentralItemLedgerEntry.ItemClassification.NOT_CONTAINER
UNCLASSIFIED = BusinessCentralItemLedgerEntry.ItemClassification.UNCLASSIFIED
EXACT = BusinessCentralItemRule.MatchType.EXACT
PREFIX = BusinessCentralItemRule.MatchType.PREFIX


def equipment_type(iso_code: str, length: int = 20) -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code=iso_code, defaults={"category": "GP", "length_ft": length, "description": iso_code}
    )[0]


def bc_integration(slug: str) -> Integration:
    team = Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]
    return Integration.objects.create(
        team=team,
        name="BC",
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        is_active=True,
    )


def rule(integration, item_no, classification=CONTAINER, *, match_type=EXACT, variant="", eq=None):
    return BusinessCentralItemRule.objects.create(
        team=integration.team,
        integration=integration,
        match_type=match_type,
        item_no=item_no,
        variant_code=variant,
        classification=classification,
        equipment_type=eq,
    )


class RulePrecedenceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.integration = bc_integration("rules-precedence")
        cls.t20 = equipment_type("22G1")
        cls.t40 = equipment_type("45G1", 40)
        rule(cls.integration, "CON", match_type=PREFIX, eq=cls.t20)
        rule(cls.integration, "CON4", match_type=PREFIX, eq=cls.t40)
        rule(cls.integration, "CONHYRA", NOT_CONTAINER)
        rule(cls.integration, "MAFI-CW", eq=cls.t20)
        rule(cls.integration, "CON22U6", variant="RF", eq=cls.t40)
        rule(cls.integration, "CONVAR", variant="A", eq=cls.t20)
        rule(cls.integration, "CONVAR", variant="B", eq=cls.t40)
        cls.rules = ItemRuleSet.for_integration(cls.integration)

    def test_exact_item(self):
        resolution = self.rules.resolve("MAFI-CW")
        self.assertEqual((resolution.classification, resolution.equipment_type), (CONTAINER, self.t20))

    def test_prefix(self):
        self.assertEqual(self.rules.resolve("CON22G1").equipment_type, self.t20)

    def test_longest_prefix_wins(self):
        self.assertEqual(self.rules.resolve("CON45G1").equipment_type, self.t40)

    def test_explicit_not_container_beats_prefix(self):
        resolution = self.rules.resolve("CONHYRA")
        self.assertEqual((resolution.classification, resolution.equipment_type), (NOT_CONTAINER, None))

    def test_variant_rule_wins_for_that_variant(self):
        self.assertEqual(self.rules.resolve("CON22U6", "RF").equipment_type, self.t40)

    def test_item_with_only_variant_rules_needs_a_variant(self):
        resolution = self.rules.resolve("CONVAR")
        self.assertTrue(resolution.needs_variant)
        self.assertEqual(resolution.classification, UNCLASSIFIED)
        self.assertEqual(self.rules.resolve("CONVAR", "B").equipment_type, self.t40)

    def test_unknown_item_is_unclassified(self):
        for item in ("ART20020", "", "P1"):
            with self.subTest(item=item):
                self.assertEqual(self.rules.resolve(item).classification, UNCLASSIFIED)

    def test_case_insensitive(self):
        self.assertEqual(self.rules.resolve("mafi-cw").classification, CONTAINER)

    def test_resolution_is_deterministic_regardless_of_rule_order(self):
        reversed_rules = ItemRuleSet(list(BusinessCentralItemRule.objects.filter(integration=self.integration))[::-1])
        for item in ("CON45G1", "CONHYRA", "CON22G1", "CONVAR"):
            with self.subTest(item=item):
                self.assertEqual(reversed_rules.resolve(item), self.rules.resolve(item))

    def test_rules_are_per_integration(self):
        other = bc_integration("rules-other-team")
        self.assertEqual(resolve_item(other, "MAFI-CW").classification, UNCLASSIFIED)


class ItemRuleFormTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.integration = bc_integration("rules-form")
        cls.t20 = equipment_type("22G1")

    def _form(self, **data):
        base = {"match_type": EXACT, "item_no": "con22g1", "variant_code": "", "classification": CONTAINER}
        return ItemRuleForm({**base, **data}, integration=self.integration)

    def test_valid_rule_is_saved_uppercase_for_the_integration(self):
        form = self._form(equipment_type=self.t20.pk)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(
            (saved.item_no, saved.integration, saved.team), ("CON22G1", self.integration, self.integration.team)
        )

    def test_invalid_item_number(self):
        self.assertIn("item_no", self._form(item_no="CON 22'; DROP").errors)

    def test_variant_only_on_exact_rules(self):
        self.assertIn("variant_code", self._form(match_type=PREFIX, variant_code="A").errors)

    def test_equipment_type_only_for_containers(self):
        self.assertIn("equipment_type", self._form(classification=NOT_CONTAINER, equipment_type=self.t20.pk).errors)

    def test_duplicate_rule_is_refused(self):
        rule(self.integration, "CON22G1")
        self.assertFalse(self._form().is_valid())

    def test_unclassified_is_not_a_rule_choice(self):
        self.assertIn("classification", self._form(classification=UNCLASSIFIED).errors)


class ItemRulesViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.integration = bc_integration("rules-ui")
        cls.t20 = equipment_type("22G1")
        cls.admin = cls._user("rules-admin@example.com", ROLE_ADMIN)
        cls.member = cls._user("rules-member@example.com", ROLE_MEMBER)
        for entry_no, item in ((1, "CON22G1"), (2, "CON22G1"), (3, "ART20020")):
            BusinessCentralItemLedgerEntry.objects.create(
                team=cls.integration.team,
                integration=cls.integration,
                company_id="c",
                entry_no=entry_no,
                item_no=item,
                serial_status=BusinessCentralItemLedgerEntry.SerialStatus.MISSING_SERIAL,
                sync_hash="h",
                synced_at=timezone.now(),
            )

    @classmethod
    def _user(cls, email, role):
        user = CustomUser.objects.create_user(username=email, email=email, password="pw")
        Membership.objects.create(team=cls.integration.team, user=user, role=role)
        return user

    def _login(self, user, team=None):
        self.client.force_login(user)
        session = self.client.session
        session["team"] = (team or self.integration.team).pk
        session.save()

    def _url(self, name="integrations:item_rules", *args):
        return reverse(name, args=[self.integration.pk, *args])

    def test_admin_sees_rules_and_unclassified_items(self):
        self._login(self.admin)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "CON22G1")
        self.assertContains(resp, "Unclassified")

    def test_admin_adds_edits_and_removes_a_rule(self):
        self._login(self.admin)
        data = {"match_type": EXACT, "item_no": "CON22G1", "variant_code": "", "classification": CONTAINER}
        self.client.post(self._url(), {**data, "equipment_type": self.t20.pk})
        saved = BusinessCentralItemRule.objects.get(integration=self.integration)
        self.assertEqual(saved.equipment_type, self.t20)

        self.client.post(
            self._url("integrations:item_rule_update", saved.pk), {**data, "classification": NOT_CONTAINER}
        )
        saved.refresh_from_db()
        self.assertEqual((saved.classification, saved.equipment_type), (NOT_CONTAINER, None))

        self.client.post(self._url("integrations:item_rule_delete", saved.pk))
        self.assertFalse(BusinessCentralItemRule.objects.exists())

    def test_member_gets_404_everywhere(self):
        existing = rule(self.integration, "CON22G1")
        self._login(self.member)
        for method, url in (
            ("get", self._url()),
            ("post", self._url()),
            ("get", self._url("integrations:item_rule_update", existing.pk)),
            ("post", self._url("integrations:item_rule_update", existing.pk)),
            ("post", self._url("integrations:item_rule_delete", existing.pk)),
        ):
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(self.client, method)(url).status_code, 404)
        self.assertTrue(BusinessCentralItemRule.objects.filter(pk=existing.pk).exists())

    def test_admin_of_another_team_cannot_reach_this_integration(self):
        other = bc_integration("rules-ui-other")
        outsider = CustomUser.objects.create_user(username="out@example.com", email="out@example.com", password="pw")
        Membership.objects.create(team=other.team, user=outsider, role=ROLE_ADMIN)
        existing = rule(self.integration, "CON22G1")
        self._login(outsider, other.team)
        self.assertEqual(self.client.get(self._url()).status_code, 404)
        self.assertEqual(self.client.post(self._url("integrations:item_rule_delete", existing.pk)).status_code, 404)
        self.assertTrue(BusinessCentralItemRule.objects.filter(pk=existing.pk).exists())
