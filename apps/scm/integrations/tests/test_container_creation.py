"""Controlled container creation from Business Central item ledger entries (operation B) and its UI.

No Business Central calls; entries come from the stage-1a dummy fixture.
"""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.intake import create_or_get_container
from apps.scm.containers.models import Container, ContainerMovement
from apps.scm.integrations.business_systems.business_central.container_creation import (
    CREATE_APPROVED,
    MATCH_ONLY,
    REVIEW,
    create_approved_containers,
    get_container_creation_mode,
    get_creation_candidates,
    set_container_creation_mode,
)
from apps.scm.integrations.business_systems.business_central.container_links import match_item_ledger_entries
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralConfigurationError
from apps.scm.integrations.business_systems.business_central.item_ledger_sync import upsert_item_ledger_entries
from apps.scm.integrations.models import BusinessCentralItemLedgerEntry, Integration
from apps.scm.integrations.tests.test_container_links import fixture_rows
from apps.scm.integrations.tests.test_item_rules import NOT_CONTAINER, PREFIX, bc_integration, equipment_type, rule
from apps.scm.tracking.models import TrackingSubscription
from apps.teams.models import Membership
from apps.teams.roles import ROLE_ADMIN, ROLE_MEMBER
from apps.users.models import CustomUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-creation"}}
_COMPANY = "782d7133-5033-4c4b-9a29-83f7f44f062d"
Entry = BusinessCentralItemLedgerEntry
S = Entry.LinkStatus


def setup_integration(slug: str) -> tuple[Integration, CustomUser]:
    integration = bc_integration(slug)
    user = CustomUser.objects.create_user(username=f"{slug}@example.com", email=f"{slug}@example.com", password="pw")
    Membership.objects.create(team=integration.team, user=user, role=ROLE_ADMIN)
    t20 = equipment_type("22G1")
    rule(integration, "CON", match_type=PREFIX, eq=t20)
    rule(integration, "CONHYRA", NOT_CONTAINER)
    rule(integration, "MAFI-CW", eq=t20)
    upsert_item_ledger_entries(integration, _COMPANY, fixture_rows())
    create_or_get_container(team=integration.team, user=user, number="MCUU2901785", attributes={"equipment_type": t20})
    create_or_get_container(team=integration.team, user=user, number="MSCU7654329", attributes={"equipment_type": t20})
    match_item_ledger_entries(integration)
    return integration, user


@override_settings(CACHES=_LOCMEM)
class CreationModeTest(TestCase):
    def test_default_is_match_only(self):
        integration = bc_integration("mode-default")
        self.assertEqual(get_container_creation_mode(integration), MATCH_ONLY)
        integration.config = {"resources": {"item_ledger_entries": {"container_creation": "everything"}}}
        self.assertEqual(get_container_creation_mode(integration), MATCH_ONLY)

    def test_set_mode_keeps_other_settings(self):
        integration = bc_integration("mode-set")
        integration.config = {"resources": {"item_ledger_entries": {"enabled": True}}, "sync_enabled": True}
        set_container_creation_mode(integration, REVIEW)
        integration.refresh_from_db()
        self.assertEqual(
            integration.config,
            {
                "resources": {"item_ledger_entries": {"enabled": True, "container_creation": REVIEW}},
                "sync_enabled": True,
            },
        )
        with self.assertRaises(ValueError):
            set_container_creation_mode(integration, "auto")


@override_settings(CACHES=_LOCMEM)
class CandidatesAndCreationTest(TestCase):
    def setUp(self):
        self.integration, self.user = setup_integration("create")

    def tearDown(self):
        cache.clear()

    def _create(self, serials):
        return create_approved_containers(self.integration, serials, self.user)

    def test_candidates_are_valid_container_items_not_in_pegasus(self):
        candidates = get_creation_candidates(self.integration)
        self.assertEqual([c.serial_no for c in candidates], ["TCLU1234568", "CRTU4101684"])
        self.assertEqual({c.equipment_type.pk for c in candidates}, {"22G1"})

    def test_nothing_is_created_without_create_approved_mode(self):
        for mode in (MATCH_ONLY, REVIEW):
            set_container_creation_mode(self.integration, mode)
            with self.subTest(mode=mode), self.assertRaises(BusinessCentralConfigurationError):
                self._create(["TCLU1234568"])
        self.assertFalse(Container.objects.filter(serial_number="123456").exists())

    def test_approved_container_is_created_linked_and_has_no_side_effects(self):
        set_container_creation_mode(self.integration, CREATE_APPROVED)
        movements = ContainerMovement.objects.count()
        result = self._create(["tclu1234568"])
        self.assertEqual(result.created, ["TCLU1234568"])
        container = Container.objects.get(team=self.integration.team, owner_code="TCL", serial_number="123456")
        self.assertEqual(container.equipment_type_id, "22G1")
        self.assertIsNone(container.current_location)
        self.assertEqual(ContainerMovement.objects.count(), movements)
        self.assertFalse(TrackingSubscription.objects.exists())
        entry = Entry.objects.get(integration=self.integration, entry_no=92059)
        self.assertEqual((entry.link_status, entry.container), (S.LINKED, container))
        self.assertEqual(Container.objects.filter(team=self.integration.team).count(), 3)  # only the approved one

    def test_refused_serials(self):
        set_container_creation_mode(self.integration, CREATE_APPROVED)
        result = self._create(["TCLU1234567", "MCUU2901785", "CONX0001", "MSCU7654329", "ABCU1234560"])
        self.assertEqual(result.created, [])
        self.assertEqual(set(result.refused), {"TCLU1234567", "MCUU2901785", "CONX0001", "MSCU7654329", "ABCU1234560"})
        self.assertEqual(Container.objects.filter(team=self.integration.team).count(), 2)

    def test_missing_equipment_type_is_not_a_candidate(self):
        self.integration.item_rules.filter(item_no="MAFI-CW").update(equipment_type=None)
        match_item_ledger_entries(self.integration)
        self.assertNotIn("CRTU4101684", [c.serial_no for c in get_creation_candidates(self.integration)])
        set_container_creation_mode(self.integration, CREATE_APPROVED)
        self.assertIn("CRTU4101684", self._create(["CRTU4101684"]).refused)

    def test_ambiguous_equipment_type_is_refused(self):
        t40 = equipment_type("45G1", 40)
        rule(self.integration, "CON45", match_type=PREFIX, eq=t40)
        upsert_item_ledger_entries(
            self.integration, _COMPANY, [{"entryNo": 99100, "itemNo": "CON45G1", "serialNo": "TCLU1234568"}]
        )
        match_item_ledger_entries(self.integration)
        candidate = next(c for c in get_creation_candidates(self.integration) if c.serial_no == "TCLU1234568")
        self.assertTrue(candidate.ambiguous)
        set_container_creation_mode(self.integration, CREATE_APPROVED)
        self.assertIn("TCLU1234568", self._create(["TCLU1234568"]).refused)
        self.assertFalse(Container.objects.filter(serial_number="123456").exists())

    def test_repeated_and_concurrent_approval_never_duplicates(self):
        set_container_creation_mode(self.integration, CREATE_APPROVED)
        self.assertEqual(self._create(["TCLU1234568"]).created, ["TCLU1234568"])
        self.assertIn("TCLU1234568", self._create(["TCLU1234568"]).refused)  # now linked, no longer a candidate
        # Someone registers the container between the re-check and the creation.
        with mock.patch(
            "apps.scm.integrations.business_systems.business_central.container_creation.match_item_ledger_entries"
        ):
            Container.objects.create(
                team=self.integration.team,
                owner_code="CRT",
                category_id="U",
                serial_number="410168",
                check_digit=4,
                equipment_type_id="22G1",
            )
            result = self._create(["CRTU4101684"])
        self.assertEqual(result.existing, ["CRTU4101684"])
        self.assertEqual(Container.objects.filter(team=self.integration.team, serial_number="410168").count(), 1)

    def test_team_isolation(self):
        other, other_user = setup_integration("create-other")
        set_container_creation_mode(self.integration, CREATE_APPROVED)
        self._create(["TCLU1234568"])
        self.assertFalse(Container.objects.filter(team=other.team, serial_number="123456").exists())
        self.assertIn("TCLU1234568", [c.serial_no for c in get_creation_candidates(other)])


@override_settings(CACHES=_LOCMEM)
class ContainerLinksViewTest(TestCase):
    def setUp(self):
        self.integration, self.admin = setup_integration("links-ui")
        self.member = CustomUser.objects.create_user(username="lm@example.com", email="lm@example.com", password="pw")
        Membership.objects.create(team=self.integration.team, user=self.member, role=ROLE_MEMBER)

    def tearDown(self):
        cache.clear()

    def _login(self, user, team=None):
        self.client.force_login(user)
        session = self.client.session
        session["team"] = (team or self.integration.team).pk
        session.save()

    def _url(self, name="integrations:container_links"):
        return reverse(name, args=[self.integration.pk])

    def test_admin_sees_summary_and_no_candidates_in_match_only(self):
        self._login(self.admin)
        resp = self.client.get(self._url())
        self.assertContains(resp, "Linked to a container")
        self.assertNotContains(resp, "Creation candidates")
        resp = self.client.get(self._url(), {"status": S.INVALID_SERIAL})
        self.assertContains(resp, "CONX0001")

    def test_review_lists_candidates_without_checkboxes(self):
        set_container_creation_mode(self.integration, REVIEW)
        self._login(self.admin)
        resp = self.client.get(self._url())
        self.assertContains(resp, "TCLU1234568")
        self.assertNotContains(resp, 'name="serials"')

    def test_admin_changes_mode_and_creates_selected(self):
        self._login(self.admin)
        self.client.post(self._url("integrations:container_links_mode"), {"mode": CREATE_APPROVED})
        self.assertEqual(get_container_creation_mode(Integration.objects.get(pk=self.integration.pk)), CREATE_APPROVED)
        resp = self.client.get(self._url())
        self.assertContains(resp, 'name="serials" value="TCLU1234568"')
        self.client.post(self._url("integrations:container_links_create"), {"serials": ["TCLU1234568"]})
        self.assertTrue(Container.objects.filter(team=self.integration.team, serial_number="123456").exists())

    def test_create_in_review_mode_creates_nothing(self):
        set_container_creation_mode(self.integration, REVIEW)
        self._login(self.admin)
        self.client.post(self._url("integrations:container_links_create"), {"serials": ["TCLU1234568"]})
        self.assertFalse(Container.objects.filter(serial_number="123456").exists())

    def test_invalid_approval_input_is_refused(self):
        set_container_creation_mode(self.integration, CREATE_APPROVED)
        self._login(self.admin)
        for serials in ([], ["not-a-container"], ["TCLU1234568"] * 501):
            with (
                self.subTest(n=len(serials)),
                mock.patch("apps.scm.integrations.item_views.create_approved_containers") as create,
            ):
                self.client.post(self._url("integrations:container_links_create"), {"serials": serials})
            create.assert_not_called()

    def test_run_matching_queues_the_task(self):
        self._login(self.admin)
        with mock.patch(
            "apps.scm.integrations.item_views.match_business_central_item_ledger_entries_task.delay"
        ) as delay:
            self.client.post(self._url("integrations:container_links_match"))
        delay.assert_called_once_with(self.integration.pk)

    def test_member_gets_404_everywhere(self):
        set_container_creation_mode(self.integration, CREATE_APPROVED)
        self._login(self.member)
        for method, name, data in (
            ("get", "integrations:container_links", {}),
            ("post", "integrations:container_links_match", {}),
            ("post", "integrations:container_links_mode", {"mode": CREATE_APPROVED}),
            ("post", "integrations:container_links_create", {"serials": ["TCLU1234568"]}),
        ):
            with self.subTest(name=name):
                self.assertEqual(getattr(self.client, method)(self._url(name), data).status_code, 404)
        self.assertFalse(Container.objects.filter(serial_number="123456").exists())

    def test_admin_of_another_team_gets_404(self):
        other, other_admin = setup_integration("links-ui-other")
        self._login(other_admin, other.team)
        self.assertEqual(self.client.get(self._url()).status_code, 404)
        self.assertEqual(
            self.client.post(
                self._url("integrations:container_links_create"), {"serials": ["TCLU1234568"]}
            ).status_code,
            404,
        )
