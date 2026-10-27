"""Received quantities, variants and locations on synced Business Central order lines.

Payloads follow the live sandbox response for purchase order 100021 line 10000
(verified 2026-10-25): 49 ordered, 3 received, variant RAL5013, location L005, with
``itemVariant``/``location`` expanded and ``null`` when a line has none.
"""

from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.mapper import BusinessCentralMapper
from apps.scm.integrations.business_systems.business_central.sync import (
    sync_purchase_orders_from_business_central,
)
from apps.scm.integrations.models import Integration, IntegrationSyncRun
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine
from apps.teams.models import Team

_PO = {
    "id": "acb56aaf-a799-f111-8072-6045bdaa5148",
    "number": "100021",
    "vendorNumber": "L00040",
    "vendorName": "Bullbox / CYNSA S.A BKBKESMM",
    "status": "Draft",
    "fullyReceived": False,
    "orderDate": "2026-08-16",
    "requestedReceiptDate": "0001-01-01",
    "currencyCode": "EUR",
    "lastModifiedDateTime": "2026-10-02T11:39:07.397Z",
}

_LINE = {
    "id": "19b968df-a799-f111-8072-6045bdaa5148",
    "documentId": "acb56aaf-a799-f111-8072-6045bdaa5148",
    "sequence": 10000,
    "lineType": "Item",
    "lineObjectNumber": "CON22G1",
    "description": "NY 20’fots DRY VAN container, ventilerad (gått en lastad resa)",
    "unitOfMeasureCode": "ST",
    "quantity": 49,
    "receivedQuantity": 3,
    "invoicedQuantity": 0,
    "directUnitCost": 1650,
    "expectedReceiptDate": "2026-12-14",
    "itemVariant": {"code": "RAL5013"},
    "location": {"code": "L005", "displayName": "Lager MCR05 - Lager Oceantermialen i JohnEvans"},
}


def _line(**changes) -> dict:
    return {**_LINE, **changes}


class LineMappingTest(SimpleTestCase):
    def _map(self, line: dict):
        return BusinessCentralMapper().map_purchase_order(_PO, [line]).lines[0]

    def test_received_quantity_is_mapped(self):
        line = self._map(_LINE)
        self.assertEqual(line.ordered_qty, Decimal("49"))
        self.assertEqual(line.received_qty, Decimal("3"))

    def test_decimal_received_quantity_is_exact(self):
        self.assertEqual(self._map(_line(receivedQuantity=2.5)).received_qty, Decimal("2.5"))

    def test_missing_received_quantity_is_unknown_not_zero(self):
        line = _line()
        del line["receivedQuantity"]
        self.assertIsNone(self._map(line).received_qty)
        self.assertIsNone(self._map(_line(receivedQuantity=None)).received_qty)

    def test_invalid_received_quantity_fails_the_record(self):
        with self.assertRaises(ValueError):
            self._map(_line(receivedQuantity="three"))

    def test_expanded_variant_and_location(self):
        line = self._map(_LINE)
        self.assertEqual(line.variant_code, "RAL5013")
        self.assertEqual(line.location_code, "L005")
        self.assertEqual(line.location_name, "Lager MCR05 - Lager Oceantermialen i JohnEvans")

    def test_null_navigation_means_none(self):
        line = self._map(_line(itemVariant=None, location=None))
        self.assertEqual(line.variant_code, "")
        self.assertEqual(line.location_code, "")
        self.assertEqual(line.location_name, "")

    def test_absent_navigation_means_unknown(self):
        line = _line()
        del line["itemVariant"], line["location"]
        mapped = self._map(line)
        self.assertIsNone(mapped.variant_code)
        self.assertIsNone(mapped.location_code)
        self.assertIsNone(mapped.location_name)


class _SyncCase(TestCase):
    """Runs the real sync with the dummy client's two fetches replaced."""

    def setUp(self):
        self.team = Team.objects.create(name="bc-qty", slug="bc-qty")
        self.integration = self._integration(self.team)

    @staticmethod
    def _integration(team: Team) -> Integration:
        return Integration.objects.create(
            team=team,
            name="BC",
            provider_code="business_central",
            provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
            config={"sync_enabled": True, "tenant_id": "t", "environment": "Production", "company_id": "c"},
        )

    def _sync(self, lines: list[dict], *, integration: Integration | None = None, po: dict | None = None):
        client = BusinessCentralClient(use_dummy=True)
        with (
            mock.patch.object(client, "fetch_purchase_orders", return_value=[po or _PO]),
            mock.patch.object(client, "fetch_purchase_order_lines", return_value=lines),
        ):
            return sync_purchase_orders_from_business_central(integration or self.integration, client=client)

    def _line_row(self, team: Team | None = None) -> PurchaseOrderLine:
        return PurchaseOrderLine.objects.get(team=team or self.team, external_id=_LINE["id"])


class ReceivedQuantitySyncTest(_SyncCase):
    def test_reference_order_quantities(self):
        run = self._sync([_LINE])
        self.assertEqual(run.status, IntegrationSyncRun.Status.COMPLETED)
        line = self._line_row()
        self.assertEqual(line.ordered_qty, Decimal("49"))
        self.assertEqual(line.received_qty, Decimal("3"))
        self.assertEqual(line.outstanding_qty, Decimal("46"))

    def test_nothing_received(self):
        self._sync([_line(receivedQuantity=0)])
        line = self._line_row()
        self.assertEqual(line.received_qty, Decimal("0"))
        self.assertEqual(line.outstanding_qty, Decimal("49"))

    def test_fully_received(self):
        self._sync([_line(receivedQuantity=49)])
        line = self._line_row()
        self.assertEqual(line.received_qty, Decimal("49"))
        self.assertEqual(line.outstanding_qty, Decimal("0"))

    def test_repeated_sync_is_unchanged_and_does_not_accumulate(self):
        self._sync([_LINE])
        run = self._sync([_LINE])
        self.assertEqual(run.records_unchanged, 1)
        self.assertEqual(run.records_updated, 0)
        self.assertEqual(self._line_row().received_qty, Decimal("3"))

    def test_changed_received_quantity_replaces_the_old_one(self):
        self._sync([_LINE])
        run = self._sync([_line(receivedQuantity=10)])
        self.assertEqual(run.records_updated, 1)
        line = self._line_row()
        self.assertEqual(line.received_qty, Decimal("10"))
        self.assertEqual(line.outstanding_qty, Decimal("39"))

    def test_missing_received_quantity_keeps_the_stored_value(self):
        self._sync([_LINE])
        line = _line()
        del line["receivedQuantity"]
        run = self._sync([line])
        self.assertEqual(run.status, IntegrationSyncRun.Status.COMPLETED)
        self.assertEqual(self._line_row().received_qty, Decimal("3"))

    def test_invalid_received_quantity_fails_in_isolation_and_keeps_the_stored_value(self):
        self._sync([_LINE])
        run = self._sync([_line(receivedQuantity="n/a")])
        self.assertEqual(run.records_failed, 1)
        self.assertEqual(run.status, IntegrationSyncRun.Status.PARTIALLY_COMPLETED)
        self.assertEqual(self._line_row().received_qty, Decimal("3"))

    def test_logistics_status_reads_the_received_quantity(self):
        from apps.scm.procurement.selectors import get_purchase_order_logistics_status

        self._sync([_LINE])
        po = PurchaseOrder.objects.get(team=self.team, external_id=_PO["id"])
        self.assertEqual(get_purchase_order_logistics_status(po), "partially_received")


class LineReferencesSyncTest(_SyncCase):
    def test_variant_and_location_are_stored(self):
        self._sync([_LINE])
        line = self._line_row()
        self.assertEqual(line.variant_code, "RAL5013")
        self.assertEqual(line.location_code, "L005")
        self.assertEqual(line.location_name, "Lager MCR05 - Lager Oceantermialen i JohnEvans")

    def test_line_without_variant_or_location_syncs(self):
        run = self._sync([_line(itemVariant=None, location=None)])
        self.assertEqual(run.status, IntegrationSyncRun.Status.COMPLETED)
        line = self._line_row()
        self.assertEqual(line.variant_code, "")
        self.assertEqual(line.location_code, "")

    def test_variant_removed_in_bc_is_cleared(self):
        self._sync([_LINE])
        self._sync([_line(itemVariant=None)])
        self.assertEqual(self._line_row().variant_code, "")

    def test_unexpanded_references_keep_the_stored_codes(self):
        self._sync([_LINE])
        line = _line()
        del line["itemVariant"], line["location"]
        run = self._sync([line])
        self.assertEqual(run.status, IntegrationSyncRun.Status.COMPLETED)
        stored = self._line_row()
        self.assertEqual(stored.variant_code, "RAL5013")
        self.assertEqual(stored.location_code, "L005")

    def test_location_code_does_not_touch_any_container(self):
        from apps.scm.containers.models import Container, ContainerMovement

        self._sync([_LINE])
        self.assertFalse(Container.objects.filter(team=self.team).exists())
        self.assertFalse(ContainerMovement.objects.exists())

    def test_teams_keep_their_own_lines(self):
        other = Team.objects.create(name="bc-qty-b", slug="bc-qty-b")
        other_integration = self._integration(other)
        self._sync([_LINE])
        self._sync([_line(receivedQuantity=7, itemVariant={"code": "RAL7013"})], integration=other_integration)
        self.assertEqual(self._line_row().received_qty, Decimal("3"))
        self.assertEqual(self._line_row().variant_code, "RAL5013")
        self.assertEqual(self._line_row(other).received_qty, Decimal("7"))
        self.assertEqual(self._line_row(other).variant_code, "RAL7013")
