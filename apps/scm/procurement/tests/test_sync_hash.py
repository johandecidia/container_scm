"""Tests for the deterministic purchase order sync hash."""

from datetime import date

from django.test import SimpleTestCase, TestCase

from apps.scm.procurement.models import PurchaseOrder
from apps.scm.procurement.services import (
    compute_purchase_order_sync_hash,
    upsert_purchase_orders,
)
from apps.teams.models import Team


def _po(**overrides):
    data = {
        "external_id": "e1",
        "po_number": "PO1",
        "supplier_no": "S1",
        "supplier_name": "Supplier",
        "status": "open",
        "order_date": date(2026, 1, 1),
        "currency": "USD",
        "lines": [
            {"external_id": "l1", "line_no": "10000", "item_no": "A", "ordered_qty": 25, "unit_price": "1940.0"},
        ],
    }
    data.update(overrides)
    return data


class SyncHashDeterminismTest(SimpleTestCase):
    def test_stable_for_same_content(self):
        self.assertEqual(compute_purchase_order_sync_hash(_po()), compute_purchase_order_sync_hash(_po()))

    def test_decimal_representation_normalised(self):
        a = _po(lines=[{"external_id": "l1", "line_no": "1", "item_no": "A", "ordered_qty": 25}])
        b = _po(lines=[{"external_id": "l1", "line_no": "1", "item_no": "A", "ordered_qty": "25.000"}])
        self.assertEqual(compute_purchase_order_sync_hash(a), compute_purchase_order_sync_hash(b))

    def test_line_order_does_not_matter(self):
        l1 = {"external_id": "l1", "line_no": "1", "item_no": "A", "ordered_qty": 1}
        l2 = {"external_id": "l2", "line_no": "2", "item_no": "B", "ordered_qty": 2}
        self.assertEqual(
            compute_purchase_order_sync_hash(_po(lines=[l1, l2])),
            compute_purchase_order_sync_hash(_po(lines=[l2, l1])),
        )

    def test_local_and_technical_fields_do_not_affect_hash(self):
        base = compute_purchase_order_sync_hash(_po())
        with_noise = compute_purchase_order_sync_hash(
            _po(
                source_last_modified="2026-01-02T00:00:00Z",
                raw_payload={"anything": "here"},
                source_active=False,
            )
        )
        self.assertEqual(base, with_noise)

    def test_header_change_changes_hash(self):
        self.assertNotEqual(
            compute_purchase_order_sync_hash(_po()),
            compute_purchase_order_sync_hash(_po(supplier_name="Different")),
        )

    def test_line_change_changes_hash(self):
        changed = _po(lines=[{"external_id": "l1", "line_no": "10000", "item_no": "A", "ordered_qty": 30}])
        self.assertNotEqual(compute_purchase_order_sync_hash(_po()), compute_purchase_order_sync_hash(changed))


class SyncHashUpsertTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="hash", slug="hash")

    def test_hash_stored_and_drives_unchanged(self):
        upsert_purchase_orders(self.team, [_po()])
        po = PurchaseOrder.objects.get(team=self.team, external_id="e1")
        self.assertTrue(po.sync_hash)
        self.assertTrue(po.lines.first().sync_hash)

        result = upsert_purchase_orders(self.team, [_po()])
        self.assertEqual(result.unchanged, 1)
        self.assertEqual(result.updated, 0)

    def test_header_change_counts_as_updated(self):
        upsert_purchase_orders(self.team, [_po()])
        result = upsert_purchase_orders(self.team, [_po(supplier_name="New")])
        self.assertEqual(result.updated, 1)
        self.assertEqual(result.unchanged, 0)

    def test_line_added_counts_as_updated(self):
        upsert_purchase_orders(self.team, [_po()])
        two_lines = _po(
            lines=[
                {"external_id": "l1", "line_no": "10000", "item_no": "A", "ordered_qty": 25, "unit_price": "1940.0"},
                {"external_id": "l2", "line_no": "20000", "item_no": "B", "ordered_qty": 5},
            ]
        )
        result = upsert_purchase_orders(self.team, [two_lines])
        self.assertEqual(result.updated, 1)
