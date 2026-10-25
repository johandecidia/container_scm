"""Item ledger entry sync: initial/incremental import, resumption, idempotency, isolation.

Uses the dummy fixture (11 entries, served 3 per page) presented as a live source.
No Business Central calls.
"""

from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scm.containers.models import Container
from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import (
    BusinessCentralConfigurationError,
    BusinessCentralConnectionError,
    BusinessCentralResponseError,
    BusinessCentralSyncInProgressError,
)
from apps.scm.integrations.business_systems.business_central.item_ledger_sync import (
    sync_item_ledger_entries_from_business_central,
    upsert_item_ledger_entries,
)
from apps.scm.integrations.business_systems.business_central.locks import cache_lock_key, sync_lock_name
from apps.scm.integrations.business_systems.business_central.sync import sync_purchase_orders_from_business_central
from apps.scm.integrations.models import BusinessCentralItemLedgerEntry, Integration, IntegrationSyncRun
from apps.scm.integrations.services import get_due_business_central_syncs
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-ile-sync"}}
_COMPANY = "782d7133-5033-4c4b-9a29-83f7f44f062d"
_ILE = IntegrationSyncRun.ResourceType.ITEM_LEDGER_ENTRIES
Entry = BusinessCentralItemLedgerEntry
SerialStatus = Entry.SerialStatus


class _SourceClient(BusinessCentralClient):
    """Dummy fixtures presented as a live source."""

    def __init__(self, environment: str = "sandbox"):
        super().__init__(use_dummy=True)
        self.environment = environment

    def source_identity(self) -> dict:
        return {"mode": "live", "tenant_id": "tenant", "environment": self.environment, "company_id": _COMPANY}


def _integration(slug: str, **ile_config) -> Integration:
    team = Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]
    return Integration.objects.create(
        team=team,
        name="BC",
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        is_active=True,
        config={"company_id": _COMPANY, "resources": {_ILE: {"enabled": True, **ile_config}}},
    )


def _sync(integration, client=None):
    return sync_item_ledger_entries_from_business_central(
        integration, client=client or _SourceClient(), trigger_type=IntegrationSyncRun.TriggerType.MANUAL
    )


def _rows(integration):
    return Entry.objects.filter(integration=integration)


@override_settings(CACHES=_LOCMEM)
class InitialImportTest(TestCase):
    def tearDown(self):
        cache.clear()

    def test_imports_every_entry_with_counts_quality_and_cursor(self):
        integration = _integration("ile-initial")
        run = _sync(integration)
        self.assertEqual(run.status, IntegrationSyncRun.Status.COMPLETED)
        self.assertEqual((run.records_fetched, run.records_created, run.records_failed), (11, 11, 0))
        self.assertEqual(run.metadata["cursor_to"], 92300)
        self.assertFalse(run.metadata["has_more"])
        self.assertEqual(
            run.metadata["quality"], {"valid_iso": 7, "invalid_iso": 2, "missing_serial": 2, "unclassified": 11}
        )
        self.assertEqual(run.metadata["scope"], {"item_no_prefixes": [], "serial_only": False})
        self.assertEqual(_rows(integration).count(), 11)
        self.assertTrue(all(e.team_id == integration.team_id and e.company_id == _COMPANY for e in _rows(integration)))

    def test_serial_classification_is_stored(self):
        integration = _integration("ile-serials")
        _sync(integration)
        by_no = {e.entry_no: e for e in _rows(integration)}
        self.assertEqual(by_no[92059].serial_no_raw, " TCLU1234568 ")
        self.assertEqual((by_no[92059].serial_no, by_no[92059].serial_status), ("TCLU1234568", SerialStatus.VALID_ISO))
        self.assertEqual(by_no[92060].serial_status, SerialStatus.INVALID_ISO)  # wrong check digit, not corrected
        self.assertEqual(by_no[92201].serial_status, SerialStatus.INVALID_ISO)  # CONX0001: not ISO format
        self.assertEqual(by_no[92061].serial_status, SerialStatus.MISSING_SERIAL)

    def test_non_container_item_is_stored_unclassified(self):
        integration = _integration("ile-art")
        _sync(integration)
        art = _rows(integration).get(entry_no=92200)
        self.assertEqual(art.item_no, "ART20020")
        self.assertEqual(art.item_classification, Entry.ItemClassification.UNCLASSIFIED)
        self.assertEqual(set(_rows(integration).values_list("item_classification", flat=True)), {"unclassified"})

    def test_same_serial_in_several_entries_and_on_several_items(self):
        integration = _integration("ile-same-serial")
        _sync(integration)
        entries = _rows(integration).filter(serial_no="MSCU7654329")
        self.assertEqual(entries.count(), 3)
        self.assertEqual(set(entries.values_list("item_no", flat=True)), {"CON22G1", "CON22G1-WW"})
        self.assertEqual(_rows(integration).filter(serial_no="MCUU2901785").count(), 2)

    def test_no_operational_side_effects(self):
        from apps.scm.shipments.models import Shipment
        from apps.scm.tracking.models import TrackingSubscription

        integration = _integration("ile-no-side-effects")
        _sync(integration)
        self.assertFalse(Container.objects.exists())
        self.assertFalse(Shipment.objects.exists())
        self.assertFalse(TrackingSubscription.objects.exists())


@override_settings(CACHES=_LOCMEM)
class IncrementalAndResumeTest(TestCase):
    def tearDown(self):
        cache.clear()

    def test_batched_initial_import_resumes_from_the_cursor(self):
        integration = _integration("ile-batched", max_entries_per_run=3)
        first = _sync(integration)
        self.assertEqual(
            (first.records_fetched, first.metadata["cursor_to"], first.metadata["has_more"]), (3, 92060, True)
        )
        runs = [first]
        while runs[-1].metadata["has_more"]:
            runs.append(_sync(integration))
        self.assertEqual(sum(r.records_created for r in runs), 11)
        self.assertEqual(_rows(integration).count(), 11)
        self.assertEqual(runs[1].metadata["cursor_from"], 92060)

    def test_repeated_run_after_completion_fetches_nothing_and_keeps_the_cursor(self):
        integration = _integration("ile-repeat")
        _sync(integration)
        again = _sync(integration)
        self.assertEqual(again.records_fetched, 0)
        self.assertEqual(again.metadata["cursor_from"], 92300)
        self.assertEqual(again.metadata["cursor_to"], 92300)
        self.assertEqual(_rows(integration).count(), 11)

    def test_incremental_run_only_requests_newer_entries(self):
        integration = _integration("ile-incremental")
        _sync(integration)
        with mock.patch.object(_SourceClient, "iter_item_ledger_entry_pages", return_value=iter([])) as pages:
            _sync(integration)
        self.assertEqual(pages.call_args.kwargs["after_entry_no"], 92300)

    def test_interrupted_run_keeps_stored_pages_and_resumes_without_duplicates(self):
        integration = _integration("ile-interrupted")
        real = BusinessCentralClient.iter_item_ledger_entry_pages

        def failing_after_first_page(self, **kwargs):
            pages = real(self, **kwargs)
            yield next(pages)
            raise BusinessCentralConnectionError("connection dropped")

        with (
            mock.patch.object(_SourceClient, "iter_item_ledger_entry_pages", failing_after_first_page),
            self.assertRaises(BusinessCentralConnectionError),
        ):
            _sync(integration)
        failed = integration.sync_runs.get()
        self.assertEqual(failed.status, IntegrationSyncRun.Status.FAILED)
        self.assertNotIn("cursor_to", failed.metadata)
        self.assertEqual(_rows(integration).count(), 3)  # the stored page survives

        resumed = _sync(integration)
        self.assertNotIn("cursor_from", resumed.metadata)  # the failed run did not move the cursor
        self.assertEqual((resumed.records_created, resumed.records_unchanged), (8, 3))
        self.assertEqual(_rows(integration).count(), 11)

    def test_record_without_entry_no_holds_the_cursor(self):
        integration = _integration("ile-bad-record")
        rows = BusinessCentralClient(use_dummy=True)._load_fixture("business_central_item_ledger_entries.json")
        rows.insert(2, {"itemNo": "CON22G1", "serialNo": "MCUU2901785"})
        with mock.patch.object(_SourceClient, "iter_item_ledger_entry_pages", return_value=iter([rows])):
            run = _sync(integration)
        self.assertEqual(run.status, IntegrationSyncRun.Status.PARTIALLY_COMPLETED)
        self.assertEqual((run.records_created, run.records_failed), (11, 1))
        self.assertNotIn("cursor_to", run.metadata)
        self.assertIn("entryNo", run.metadata["errors"][0]["error"])
        self.assertIsNone(
            IntegrationSyncRun.objects.filter(metadata__has_key="cursor_to", integration=integration).first()
        )

    def test_out_of_order_response_fails_the_run(self):
        integration = _integration("ile-order")
        pages = iter([[{"entryNo": 5}, {"entryNo": 4}]])
        with (
            mock.patch.object(_SourceClient, "iter_item_ledger_entry_pages", return_value=pages),
            self.assertRaises(BusinessCentralResponseError),
        ):
            _sync(integration)
        self.assertEqual(integration.sync_runs.get().status, IntegrationSyncRun.Status.FAILED)

    def test_changing_the_filter_restarts_from_the_beginning_of_the_new_scope(self):
        integration = _integration("ile-scope", item_no_prefixes=["CON"])
        narrow = _sync(integration)
        self.assertEqual(narrow.records_fetched, 9)
        integration.config["resources"][_ILE]["item_no_prefixes"] = []
        integration.save()
        wide = _sync(integration)
        self.assertNotIn("cursor_from", wide.metadata)
        self.assertEqual((wide.records_created, wide.records_unchanged), (2, 9))

    def test_prefix_and_serial_filters(self):
        integration = _integration("ile-filters", item_no_prefixes=["MAFI", "CON"], serial_only=True)
        _sync(integration)
        self.assertEqual(
            sorted(_rows(integration).values_list("entry_no", flat=True)),
            [92058, 92059, 92060, 92062, 92063, 92064, 92101, 92201, 92300],
        )


@override_settings(CACHES=_LOCMEM)
class UpsertTest(TestCase):
    def setUp(self):
        self.integration = _integration("ile-upsert")
        self.raw = {"entryNo": 1, "itemNo": "CON22G1", "serialNo": "MCUU2901785", "remainingQuantity": 1}

    def test_unchanged_row_is_not_rewritten(self):
        upsert_item_ledger_entries(self.integration, _COMPANY, [self.raw])
        before = Entry.objects.get()
        counts = upsert_item_ledger_entries(self.integration, _COMPANY, [dict(self.raw)])
        after = Entry.objects.get()
        self.assertEqual((counts.created, counts.updated, counts.unchanged), (0, 0, 1))
        self.assertEqual(after.updated_at, before.updated_at)
        self.assertGreaterEqual(after.synced_at, before.synced_at)

    def test_changed_bc_row_updates_the_local_copy(self):
        upsert_item_ledger_entries(self.integration, _COMPANY, [self.raw])
        counts = upsert_item_ledger_entries(self.integration, _COMPANY, [{**self.raw, "remainingQuantity": 0}])
        self.assertEqual(counts.updated, 1)
        self.assertEqual(Entry.objects.get().raw_payload["remainingQuantity"], 0)
        self.assertEqual(Entry.objects.count(), 1)

    def test_repeated_entry_in_one_page_is_stored_once(self):
        counts = upsert_item_ledger_entries(self.integration, _COMPANY, [self.raw, {**self.raw, "itemNo": "X"}])
        self.assertEqual(counts.created, 1)
        self.assertEqual(Entry.objects.get().item_no, "X")

    def test_same_entry_no_in_another_company_is_separate(self):
        upsert_item_ledger_entries(self.integration, _COMPANY, [self.raw])
        upsert_item_ledger_entries(self.integration, "other-company", [self.raw])
        self.assertEqual(Entry.objects.filter(entry_no=1).count(), 2)


@override_settings(CACHES=_LOCMEM)
class SafetyTest(TestCase):
    def tearDown(self):
        cache.clear()

    def test_disabled_by_default(self):
        integration = _integration("ile-default")
        integration.config = {"company_id": _COMPANY}
        integration.save()
        with self.assertRaisesMessage(BusinessCentralConfigurationError, "not enabled"):
            _sync(integration)
        self.assertFalse(Entry.objects.exists())

    def test_not_scheduled_by_default(self):
        integration = _integration("ile-sched")
        integration.config = {"sync_enabled": True, "scheduled_sync_enabled": True}
        integration.save()
        self.assertEqual([r for i, r in get_due_business_central_syncs() if i == integration], ["purchase_orders"])

    def test_invalid_options_are_refused(self):
        configs = (
            {"item_no_prefixes": "CON"},
            {"item_no_prefixes": ["CON') or ('1"]},
            {"serial_only": "yes"},
            {"max_entries_per_run": 0},
            {"max_entries_per_run": "100"},
        )
        for index, config in enumerate(configs):
            with self.subTest(config=config), self.assertRaises(BusinessCentralConfigurationError):
                _sync(_integration(f"ile-opt-{index}", **config))
        self.assertFalse(IntegrationSyncRun.objects.exists())

    def test_parallel_run_of_the_same_resource_is_refused(self):
        integration = _integration("ile-locked")
        cache.add(cache_lock_key(sync_lock_name(integration, _ILE)), "held", 600)
        with self.assertRaises(BusinessCentralSyncInProgressError):
            _sync(integration)
        self.assertFalse(integration.sync_runs.exists())

    def test_source_binding_applies_to_item_ledger_entries(self):
        integration = _integration("ile-binding")
        sync_purchase_orders_from_business_central(integration, client=_SourceClient("production"))
        with self.assertRaisesMessage(BusinessCentralConfigurationError, "separate Team"):
            _sync(integration, _SourceClient("sandbox"))
        self.assertFalse(Entry.objects.exists())

    def test_teams_are_isolated(self):
        first, second = _integration("ile-team-a"), _integration("ile-team-b")
        _sync(first)
        _sync(second)
        self.assertEqual(Entry.objects.filter(team=first.team).count(), 11)
        self.assertEqual(Entry.objects.filter(team=second.team).count(), 11)
        self.assertFalse(Entry.objects.filter(team=first.team, integration=second).exists())


@override_settings(CACHES=_LOCMEM)
class CommandAndMonitoringTest(TestCase):
    def tearDown(self):
        cache.clear()

    def test_command_until_complete(self):
        integration = _integration("ile-command", max_entries_per_run=4)
        out = StringIO()
        call_command(
            "bc_sync_item_ledger_entries", integration=integration.pk, dummy=True, until_complete=True, stdout=out
        )
        self.assertEqual(_rows(integration).count(), 11)
        self.assertEqual(integration.sync_runs.count(), 3)
        self.assertIn("Run 3 completed", out.getvalue())

    def test_resource_status_shows_quality(self):
        from apps.scm.integrations.selectors import get_resource_sync_statuses

        integration = _integration("ile-status")
        _sync(integration)
        status = next(s for s in get_resource_sync_statuses(integration) if s.resource_type == _ILE)
        self.assertTrue(status.implemented and status.enabled)
        self.assertEqual(status.last_finished_run.metadata["quality"]["valid_iso"], 7)
        self.assertLessEqual(status.last_finished_run.finished_at, timezone.now())

    def test_detail_page_shows_quality_counts(self):
        from django.urls import reverse

        from apps.teams.models import Membership
        from apps.users.models import CustomUser

        integration = _integration("ile-ui", max_entries_per_run=3)
        _sync(integration)
        user = CustomUser.objects.create_user(username="ile-ui@example.com", email="ile-ui@example.com", password="pw")
        Membership.objects.create(team=integration.team, user=user, role="admin")
        self.client.force_login(user)
        session = self.client.session
        session["team"] = integration.team.pk
        session.save()
        resp = self.client.get(reverse("integrations:detail", args=[integration.pk]))
        self.assertContains(resp, "Valid ISO 2")
        self.assertContains(resp, "More to import")
