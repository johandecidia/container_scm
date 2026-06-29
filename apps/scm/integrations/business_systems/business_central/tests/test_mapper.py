"""Tests for the Business Central purchase order mapper."""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from apps.scm.integrations.business_systems.business_central.mapper import BusinessCentralMapper, _normalize_status
from apps.scm.integrations.business_systems.business_central.schemas import NormalizedPurchaseOrder

_RAW_PO = {
    "id": "bc-po-id-001",
    "number": "PO100245",
    "vendorNumber": "L00141",
    "vendorName": "Shanghai Containers Ltd",
    "orderDate": "2026-06-18",
    "expectedReceiptDate": "2026-08-15",
    "currencyCode": "USD",
    "status": "Open",
    "totalAmountExcludingTax": 48500.0,
}

_RAW_LINE = {
    "id": "bc-line-id-001",
    "sequence": 10000,
    "itemNumber": "40HC-NEW",
    "description": "40HC New Container",
    "quantity": 25,
    "unitOfMeasureCode": "PCS",
    "directUnitCost": 1940.0,
    "lineAmount": 48500.0,
    "expectedReceiptDate": "2026-08-15",
}


class NormalizeStatusTest(SimpleTestCase):
    def test_open(self):
        self.assertEqual(_normalize_status("Open"), "open")

    def test_released(self):
        self.assertEqual(_normalize_status("Released"), "released")

    def test_partially_received(self):
        self.assertEqual(_normalize_status("Partially Received"), "partially_received")

    def test_fully_received(self):
        self.assertEqual(_normalize_status("Fully Received"), "fully_received")

    def test_closed(self):
        self.assertEqual(_normalize_status("Closed"), "closed")

    def test_unknown_falls_back_to_open(self):
        self.assertEqual(_normalize_status("SomeUnknownStatus"), "open")

    def test_case_insensitive(self):
        self.assertEqual(_normalize_status("OPEN"), "open")
        self.assertEqual(_normalize_status("released"), "released")


class MapperTest(SimpleTestCase):
    def setUp(self):
        self.mapper = BusinessCentralMapper()

    def test_maps_purchase_order_header(self):
        result = self.mapper.map_purchase_order(_RAW_PO, [])
        self.assertIsInstance(result, NormalizedPurchaseOrder)
        self.assertEqual(result.external_id, "bc-po-id-001")
        self.assertEqual(result.po_number, "PO100245")
        self.assertEqual(result.supplier_no, "L00141")
        self.assertEqual(result.supplier_name, "Shanghai Containers Ltd")
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.status, "open")
        self.assertEqual(result.order_date, date(2026, 6, 18))
        self.assertEqual(result.expected_receipt_date, date(2026, 8, 15))
        self.assertEqual(result.source_system, "business_central")

    def test_raw_payload_preserved(self):
        result = self.mapper.map_purchase_order(_RAW_PO, [])
        self.assertEqual(result.raw_payload["id"], "bc-po-id-001")
        self.assertEqual(result.raw_payload["totalAmountExcludingTax"], 48500.0)

    def test_maps_purchase_order_lines(self):
        result = self.mapper.map_purchase_order(_RAW_PO, [_RAW_LINE])
        self.assertEqual(len(result.lines), 1)
        line = result.lines[0]
        self.assertEqual(line.external_id, "bc-line-id-001")
        self.assertEqual(line.line_no, "10000")
        self.assertEqual(line.item_no, "40HC-NEW")
        self.assertEqual(line.description, "40HC New Container")
        self.assertEqual(line.ordered_qty, Decimal("25"))
        self.assertEqual(line.unit_price, Decimal("1940.0"))
        self.assertEqual(line.expected_receipt_date, date(2026, 8, 15))

    def test_line_raw_payload_preserved(self):
        result = self.mapper.map_purchase_order(_RAW_PO, [_RAW_LINE])
        self.assertEqual(result.lines[0].raw_payload["lineAmount"], 48500.0)

    def test_no_lines_returns_empty_list(self):
        result = self.mapper.map_purchase_order(_RAW_PO, [])
        self.assertEqual(result.lines, [])

    def test_none_lines_treated_as_empty(self):
        result = self.mapper.map_purchase_order(_RAW_PO, None)
        self.assertEqual(result.lines, [])

    def test_status_released_mapped(self):
        raw = {**_RAW_PO, "status": "Released"}
        result = self.mapper.map_purchase_order(raw, [])
        self.assertEqual(result.status, "released")
