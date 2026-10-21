"""Sandbox/production isolation and source ownership for the BC purchase-order sync.

A Business Central sandbox is often a copy of production: same company id, same
record GUIDs. These tests pin down that data from one source can never overwrite
another's, and that watermarks are never shared between sources.
"""

from datetime import UTC, datetime
from unittest import mock

from django.test import TestCase, override_settings

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import (
    BusinessCentralConfigurationError,
    BusinessCentralConnectionError,
)
from apps.scm.integrations.business_systems.business_central.reconcile import reconcile_purchase_orders
from apps.scm.integrations.business_systems.business_central.sync import sync_purchase_orders_from_business_central
from apps.scm.integrations.models import Integration, IntegrationSyncRun
from apps.scm.integrations.services import get_last_successful_watermark
from apps.scm.procurement.models import ContainerLoad, PurchaseOrder, PurchaseOrderSource
from apps.scm.procurement.tests.test_container_links import _container
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-isolation"}}
_COMPANY = "782d7133-5033-4c4b-9a29-83f7f44f062d"
_RESOURCE = IntegrationSyncRun.ResourceType.PURCHASE_ORDERS


def _source(environment: str) -> dict:
    return {"mode": "live", "tenant_id": "tenant", "environment": environment.lower(), "company_id": _COMPANY}


class _FixtureClient(BusinessCentralClient):
    """Fixture data presented as if it came from a given live environment."""

    def __init__(self, environment: str = "sandbox_eligra_261002"):
        super().__init__(use_dummy=True)
        self.environment = environment

    def source_identity(self) -> dict:
        return _source(self.environment)


def _integration(slug: str) -> Integration:
    team = Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]
    return Integration.objects.create(
        team=team,
        name="BC",
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        is_active=True,
        config={"environment": "Sandbox_eligra_261002", "company_id": _COMPANY, "tenant_id": "tenant"},
    )


def _sync(integration, client=None):
    return sync_purchase_orders_from_business_central(
        integration, client=client or _FixtureClient(), trigger_type=IntegrationSyncRun.TriggerType.MANUAL
    )


@override_settings(CACHES=_LOCMEM)
class SourceBindingTest(TestCase):
    def test_run_records_its_source(self):
        integration = _integration("iso-record")
        run = _sync(integration)
        self.assertEqual(run.metadata["source"], _source("sandbox_eligra_261002"))

    def test_po_records_canonical_company(self):
        integration = _integration("iso-company")
        _sync(integration)
        self.assertEqual(
            set(PurchaseOrder.objects.filter(team=integration.team).values_list("source_company_id", flat=True)),
            {_COMPANY},
        )

    def test_switching_environment_is_refused(self):
        integration = _integration("iso-switch")
        _sync(integration)
        with self.assertRaisesMessage(BusinessCentralConfigurationError, "separate Team"):
            _sync(integration, _FixtureClient("production"))
        # Nothing was written by the refused run.
        self.assertEqual(integration.sync_runs.count(), 1)

    def test_legacy_run_without_source_blocks_live_sync(self):
        integration = _integration("iso-legacy")
        IntegrationSyncRun.objects.create(
            team=integration.team, integration=integration, status=IntegrationSyncRun.Status.COMPLETED
        )
        with self.assertRaises(BusinessCentralConfigurationError):
            _sync(integration)

    def test_dummy_data_blocks_live_sync(self):
        integration = _integration("iso-dummy")
        _sync(integration, BusinessCentralClient(use_dummy=True))
        with self.assertRaises(BusinessCentralConfigurationError):
            _sync(integration)

    def test_failed_run_does_not_bind(self):
        integration = _integration("iso-failed")
        with (
            mock.patch.object(_FixtureClient, "fetch_purchase_orders", side_effect=BusinessCentralConnectionError()),
            self.assertRaises(BusinessCentralConnectionError),
        ):
            _sync(integration, _FixtureClient("typo_environment"))
        run = _sync(integration)  # corrected environment
        self.assertEqual(run.status, IntegrationSyncRun.Status.COMPLETED)

    def test_same_records_in_separate_teams_do_not_collide(self):
        sandbox = _integration("iso-sandbox-team")
        production = _integration("iso-production-team")
        _sync(sandbox, _FixtureClient("sandbox_eligra_261002"))
        _sync(production, _FixtureClient("production"))
        self.assertEqual(PurchaseOrder.objects.filter(team=sandbox.team).count(), 2)
        self.assertEqual(PurchaseOrder.objects.filter(team=production.team).count(), 2)
        other = Integration.objects.get(pk=production.pk)
        self.assertEqual(other.sync_runs.get().metadata["source"]["environment"], "production")


@override_settings(CACHES=_LOCMEM)
class WatermarkIsolationTest(TestCase):
    def test_watermark_from_other_source_is_not_reused(self):
        integration = _integration("iso-wm")
        IntegrationSyncRun.objects.create(
            team=integration.team,
            integration=integration,
            status=IntegrationSyncRun.Status.FAILED,  # failed: does not bind, but carries a watermark
            watermark_to=datetime(2030, 1, 1, tzinfo=UTC),
            metadata={"source": _source("production")},
        )
        self.assertIsNone(get_last_successful_watermark(integration, _RESOURCE, source=_source("sandbox")))

    def test_watermark_scoped_by_source(self):
        integration = _integration("iso-wm2")
        IntegrationSyncRun.objects.create(
            team=integration.team,
            integration=integration,
            status=IntegrationSyncRun.Status.COMPLETED,
            watermark_to=datetime(2030, 1, 1, tzinfo=UTC),
            metadata={"source": _source("production")},
        )
        sandbox = _source("sandbox_eligra_261002")
        self.assertIsNone(get_last_successful_watermark(integration, _RESOURCE, source=sandbox))
        self.assertIsNotNone(get_last_successful_watermark(integration, _RESOURCE, source=_source("production")))

    def test_incremental_run_uses_own_watermark(self):
        integration = _integration("iso-wm3")
        first = _sync(integration)
        with mock.patch.object(_FixtureClient, "fetch_purchase_orders", return_value=[]) as fetch:
            second = _sync(integration)
        self.assertEqual(second.watermark_from, first.watermark_to)
        self.assertEqual(fetch.call_args.kwargs["modified_since"], "2026-06-21T14:00:00Z")  # minus 5 min overlap


@override_settings(CACHES=_LOCMEM)
class OwnershipTest(TestCase):
    def test_record_owned_by_other_source_is_untouched(self):
        integration = _integration("iso-owner")
        manual = PurchaseOrder.objects.create(
            team=integration.team,
            external_id="bc-po-id-001",
            po_number="MANUAL-1",
            supplier_no="S",
            supplier_name="Manual supplier",
            source_system=PurchaseOrderSource.MANUAL,
        )
        run = _sync(integration)
        manual.refresh_from_db()
        self.assertEqual(manual.po_number, "MANUAL-1")
        self.assertEqual(manual.source_system, PurchaseOrderSource.MANUAL)
        self.assertEqual(run.status, IntegrationSyncRun.Status.PARTIALLY_COMPLETED)
        self.assertEqual(run.records_failed, 1)
        self.assertIsNone(run.watermark_to)

    def test_record_from_other_company_is_untouched(self):
        integration = _integration("iso-other-company")
        foreign = PurchaseOrder.objects.create(
            team=integration.team,
            external_id="bc-po-id-001",
            po_number="PO-OTHER",
            supplier_no="S",
            supplier_name="Other company",
            source_system=PurchaseOrderSource.BUSINESS_CENTRAL,
            source_company_id="99999999-9999-9999-9999-999999999999",
        )
        run = _sync(integration)
        foreign.refresh_from_db()
        self.assertEqual(foreign.po_number, "PO-OTHER")
        self.assertEqual(run.records_failed, 1)

    def test_container_links_survive_resync(self):
        integration = _integration("iso-links")
        _sync(integration)
        po = PurchaseOrder.objects.get(team=integration.team, po_number="PO100245")
        line = po.lines.get()
        load = ContainerLoad.objects.create(
            team=integration.team, purchase_order_line=line, container=_container(po.team)
        )
        changed = [
            {**p, "vendorName": "Renamed"} for p in BusinessCentralClient(use_dummy=True).fetch_purchase_orders()
        ]
        with mock.patch.object(_FixtureClient, "fetch_purchase_orders", return_value=changed):
            run = _sync(integration)
        self.assertEqual(run.records_updated, 2)
        self.assertTrue(ContainerLoad.objects.filter(pk=load.pk, purchase_order_line_id=line.pk).exists())


@override_settings(CACHES=_LOCMEM)
class MappingFailureTest(TestCase):
    def test_malformed_record_fails_in_isolation(self):
        integration = _integration("iso-malformed")
        pos = BusinessCentralClient(use_dummy=True).fetch_purchase_orders()
        pos[0] = {**pos[0], "id": ""}
        with mock.patch.object(_FixtureClient, "fetch_purchase_orders", return_value=pos):
            run = _sync(integration)
        self.assertEqual(run.status, IntegrationSyncRun.Status.PARTIALLY_COMPLETED)
        self.assertEqual(run.records_created, 1)
        self.assertEqual(run.records_failed, 1)
        self.assertIsNone(run.watermark_to)
        self.assertIn("missing 'id'", run.metadata["errors"][0]["error"])
        self.assertEqual(run.metadata["source"], _source("sandbox_eligra_261002"))

    def test_line_fetch_failure_fails_run_without_writes(self):
        integration = _integration("iso-linefail")
        with (
            mock.patch.object(
                _FixtureClient, "fetch_purchase_order_lines", side_effect=BusinessCentralConnectionError("down")
            ),
            self.assertRaises(BusinessCentralConnectionError),
        ):
            _sync(integration)
        self.assertFalse(PurchaseOrder.objects.filter(team=integration.team).exists())
        self.assertEqual(integration.sync_runs.get().status, IntegrationSyncRun.Status.FAILED)


@override_settings(CACHES=_LOCMEM)
class ReconcileIsolationTest(TestCase):
    def test_reconcile_requires_prior_sync_from_same_source(self):
        integration = _integration("iso-recon-none")
        with self.assertRaisesMessage(BusinessCentralConfigurationError, "completed purchase order sync"):
            reconcile_purchase_orders(integration, client=_FixtureClient())

    def test_reconcile_refuses_other_source(self):
        integration = _integration("iso-recon-other")
        _sync(integration)
        with self.assertRaises(BusinessCentralConfigurationError):
            reconcile_purchase_orders(integration, client=_FixtureClient("production"))
        self.assertTrue(
            all(PurchaseOrder.objects.filter(team=integration.team).values_list("source_active", flat=True))
        )
