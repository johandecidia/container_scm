"""Linking Business Central item ledger entries to existing Pegasus containers (operation A).

Entries come from the stage-1a dummy fixture (11 entries). No Business Central calls.
"""

from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.scm.containers.intake import create_or_get_container
from apps.scm.containers.models import Container, ContainerMovement
from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.container_links import (
    match_item_ledger_entries,
    matching_lock_name,
)
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralSyncInProgressError
from apps.scm.integrations.business_systems.business_central.item_ledger_sync import upsert_item_ledger_entries
from apps.scm.integrations.business_systems.business_central.locks import cache_lock_key
from apps.scm.integrations.models import BusinessCentralItemLedgerEntry, BusinessCentralItemRule
from apps.scm.integrations.selectors import get_item_link_summary
from apps.scm.integrations.tasks import match_business_central_item_ledger_entries_task
from apps.scm.integrations.tests.test_item_rules import NOT_CONTAINER, PREFIX, bc_integration, equipment_type, rule
from apps.scm.tracking.models import TrackingSubscription
from apps.users.models import CustomUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-links"}}
_COMPANY = "782d7133-5033-4c4b-9a29-83f7f44f062d"
Entry = BusinessCentralItemLedgerEntry
S = Entry.LinkStatus


def fixture_rows() -> list[dict]:
    return BusinessCentralClient(use_dummy=True)._load_fixture("business_central_item_ledger_entries.json")


@override_settings(CACHES=_LOCMEM)
class MatchingTest(TestCase):
    def setUp(self):
        self.integration = bc_integration("links")
        self.team = self.integration.team
        self.user = CustomUser.objects.create_user(username="links@example.com", email="links@example.com")
        self.t20 = equipment_type("22G1")
        self.t40 = equipment_type("45G1", 40)
        rule(self.integration, "CON", match_type=PREFIX, eq=self.t20)
        rule(self.integration, "CONHYRA", NOT_CONTAINER)
        rule(self.integration, "MAFI-CW", eq=self.t20)
        upsert_item_ledger_entries(self.integration, _COMPANY, fixture_rows())
        self.mcuu, _ = create_or_get_container(
            team=self.team, user=self.user, number="MCUU2901785", attributes={"equipment_type": self.t20}
        )
        self.mscu, _ = create_or_get_container(
            team=self.team, user=self.user, number="MSCU7654329", attributes={"equipment_type": self.t40}
        )

    def tearDown(self):
        cache.clear()

    def _status(self, entry_no):
        return Entry.objects.get(integration=self.integration, entry_no=entry_no)

    def test_statuses_after_a_pass(self):
        result = match_item_ledger_entries(self.integration)
        self.assertEqual(result.checked, 11)
        expected = {
            92058: S.LINKED,
            92300: S.LINKED,
            92059: S.CONTAINER_NOT_FOUND,  # valid ISO, container item, not in Pegasus
            92101: S.CONTAINER_NOT_FOUND,  # MAFI-CW is a container item by explicit rule
            92060: S.INVALID_SERIAL,  # wrong check digit
            92201: S.INVALID_SERIAL,  # not ISO format
            92061: S.MISSING_SERIAL,
            92200: S.MISSING_SERIAL,
            92062: S.EQUIPMENT_TYPE_CONFLICT,  # mapped 22G1, container is 45G1
            92063: S.EQUIPMENT_TYPE_CONFLICT,
            92064: S.EQUIPMENT_TYPE_CONFLICT,
        }
        self.assertEqual(
            {e.entry_no: e.link_status for e in Entry.objects.filter(integration=self.integration)}, expected
        )

    def test_one_container_many_entries_and_items(self):
        match_item_ledger_entries(self.integration)
        self.assertEqual(set(self.mcuu.bc_item_ledger_entries.values_list("entry_no", flat=True)), {92058, 92300})
        mscu_entries = self.mscu.bc_item_ledger_entries.all()
        self.assertEqual(set(mscu_entries.values_list("item_no", flat=True)), {"CON22G1-WW", "CON22G1"})
        self.assertEqual(Container.objects.filter(team=self.team).count(), 2)

    def test_conflict_keeps_the_container_type_and_links(self):
        match_item_ledger_entries(self.integration)
        self.mscu.refresh_from_db()
        self.assertEqual(self.mscu.equipment_type, self.t40)
        self.assertEqual(self._status(92064).container, self.mscu)

    def test_matching_creates_and_changes_no_containers(self):
        before = list(Container.objects.values("pk", "updated_at", "equipment_type", "current_location", "status"))
        movements = ContainerMovement.objects.count()
        match_item_ledger_entries(self.integration)
        self.assertEqual(
            list(Container.objects.values("pk", "updated_at", "equipment_type", "current_location", "status")), before
        )
        self.assertEqual(ContainerMovement.objects.count(), movements)
        self.assertFalse(TrackingSubscription.objects.exists())

    def test_repeated_pass_is_idempotent(self):
        match_item_ledger_entries(self.integration)
        again = match_item_ledger_entries(self.integration)
        self.assertEqual((again.checked, again.changed), (11, 0))

    def test_changed_rules_take_effect_on_the_next_pass(self):
        match_item_ledger_entries(self.integration)
        BusinessCentralItemRule.objects.filter(item_no="MAFI-CW").delete()
        rule(self.integration, "CON22G1", NOT_CONTAINER)
        result = match_item_ledger_entries(self.integration)
        self.assertGreater(result.changed, 0)
        self.assertEqual(self._status(92101).link_status, S.UNCLASSIFIED_ITEM)
        unlinked = self._status(92058)
        self.assertEqual((unlinked.link_status, unlinked.container), (S.NON_CONTAINER_ITEM, None))
        self.assertEqual(unlinked.item_classification, NOT_CONTAINER)

    def test_registering_a_container_later_links_on_the_next_pass(self):
        match_item_ledger_entries(self.integration)
        create_or_get_container(
            team=self.team, user=self.user, number="TCLU1234568", attributes={"equipment_type": self.t20}
        )
        match_item_ledger_entries(self.integration)
        self.assertEqual(self._status(92059).link_status, S.LINKED)

    def test_missing_equipment_type_only_blocks_creation(self):
        BusinessCentralItemRule.objects.filter(item_no="MAFI-CW").update(equipment_type=None)
        BusinessCentralItemRule.objects.filter(item_no="CON").update(equipment_type=None)
        match_item_ledger_entries(self.integration)
        self.assertEqual(self._status(92101).link_status, S.MISSING_EQUIPMENT_TYPE)
        self.assertEqual(self._status(92058).link_status, S.LINKED)  # existing container still links
        self.assertEqual(self._status(92064).link_status, S.LINKED)  # no mapped type → no conflict

    def test_non_container_item_with_valid_serial_is_not_linked(self):
        upsert_item_ledger_entries(
            self.integration, _COMPANY, [{"entryNo": 99000, "itemNo": "CONHYRA", "serialNo": "MCUU2901785"}]
        )
        match_item_ledger_entries(self.integration)
        entry = self._status(99000)
        self.assertEqual((entry.link_status, entry.container), (S.NON_CONTAINER_ITEM, None))

    def test_variant_unresolved(self):
        rule(self.integration, "CONVAR", variant="A", eq=self.t20)
        upsert_item_ledger_entries(
            self.integration, _COMPANY, [{"entryNo": 99001, "itemNo": "CONVAR", "serialNo": "MCUU2901785"}]
        )
        match_item_ledger_entries(self.integration)
        self.assertEqual(self._status(99001).link_status, S.VARIANT_UNRESOLVED)

    def test_only_selected_serials(self):
        result = match_item_ledger_entries(self.integration, serial_numbers=["MCUU2901785"])
        self.assertEqual(result.checked, 2)
        self.assertEqual(self._status(92059).link_status, S.PENDING)

    def test_parallel_pass_is_refused(self):
        cache.add(cache_lock_key(matching_lock_name(self.integration)), "held", 600)
        with self.assertRaises(BusinessCentralSyncInProgressError):
            match_item_ledger_entries(self.integration)

    def test_deleting_a_container_keeps_the_commercial_history(self):
        match_item_ledger_entries(self.integration)
        self.mcuu.delete()
        entry = self._status(92058)
        self.assertIsNone(entry.container)
        self.assertEqual(Entry.objects.filter(integration=self.integration).count(), 11)

    def test_resync_leaves_the_link_alone(self):
        match_item_ledger_entries(self.integration)
        changed = [{**row, "remainingQuantity": 0} for row in fixture_rows()]
        upsert_item_ledger_entries(self.integration, _COMPANY, changed)
        entry = self._status(92058)
        self.assertEqual((entry.link_status, entry.container), (S.LINKED, self.mcuu))

    def test_link_summary(self):
        match_item_ledger_entries(self.integration)
        summary = {status: count for status, _label, count in get_item_link_summary(self.integration)}
        self.assertEqual(summary[S.LINKED], 2)
        self.assertEqual(summary[S.EQUIPMENT_TYPE_CONFLICT], 3)
        self.assertNotIn(S.PENDING, summary)

    def test_task_and_command(self):
        self.assertEqual(match_business_central_item_ledger_entries_task(self.integration.pk)["checked"], 11)
        out = StringIO()
        call_command("bc_match_item_ledger_entries", integration=self.integration.pk, stdout=out)
        self.assertIn("Checked 11 entries, 0 changed", out.getvalue())


@override_settings(CACHES=_LOCMEM)
class TeamIsolationTest(TestCase):
    def test_a_container_of_another_team_is_never_linked(self):
        mine, theirs = bc_integration("links-mine"), bc_integration("links-theirs")
        user = CustomUser.objects.create_user(username="iso@example.com", email="iso@example.com")
        t20 = equipment_type("22G1")
        for integration in (mine, theirs):
            rule(integration, "CON", match_type=PREFIX, eq=t20)
            upsert_item_ledger_entries(integration, _COMPANY, fixture_rows())
        other_container, _ = create_or_get_container(
            team=theirs.team, user=user, number="MCUU2901785", attributes={"equipment_type": t20}
        )
        match_item_ledger_entries(mine)
        match_item_ledger_entries(theirs)
        mine_entry = Entry.objects.get(integration=mine, entry_no=92058)
        self.assertEqual((mine_entry.link_status, mine_entry.container), (S.CONTAINER_NOT_FOUND, None))
        self.assertEqual(Entry.objects.get(integration=theirs, entry_no=92058).container, other_container)
        self.assertFalse(Entry.objects.filter(integration=mine, container__team=theirs.team).exists())
