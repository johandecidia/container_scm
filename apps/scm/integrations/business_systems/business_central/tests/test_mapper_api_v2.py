"""Mapper tests against the real Business Central API v2.0 response structure.

Payloads follow the documented purchaseOrder / purchaseOrderLine resources: the
``@odata.etag`` annotation, GUID ids, ``lineObjectNumber``, a header
``requestedReceiptDate``, Business Central's ``0001-01-01`` blank date, and the
API's own status enum.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from django.test import SimpleTestCase

from apps.scm.integrations.business_systems.business_central.mapper import BusinessCentralMapper

_API_PO = {
    "@odata.etag": 'W/"JzE5OzEyMzQ1Njc4OTAxMjM0NTY3ODkwMTsn"',
    "id": "5d115c9c-44e3-ea11-bb43-000d3a2feca1",
    "number": "106001",
    "vendorNumber": "30000",
    "vendorName": "CoolWood Technologies",
    "status": "Open",
    "fullyReceived": False,
    "orderDate": "2026-09-14",
    "requestedReceiptDate": "0001-01-01",
    "currencyCode": "",
    "lastModifiedDateTime": "2026-09-14T08:12:44.137Z",
}

_API_LINE = {
    "@odata.etag": 'W/"JzE5OzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDsn"',
    "id": "6f115c9c-44e3-ea11-bb43-000d3a2feca1",
    "documentId": "5d115c9c-44e3-ea11-bb43-000d3a2feca1",
    "sequence": 10000,
    "lineType": "Item",
    "itemId": "96a5b0b3-44e3-ea11-bb43-000d3a2feca1",
    "lineObjectNumber": "CON22G1",
    "description": "20ft Dry Container",
    "unitOfMeasureCode": "PCS",
    "quantity": 50,
    "receivedQuantity": 0,
    "invoicedQuantity": 0,
    "directUnitCost": 2140.35,
    "expectedReceiptDate": "2026-11-02",
}

_COMMENT_LINE = {
    "id": "7f115c9c-44e3-ea11-bb43-000d3a2feca1",
    "sequence": 20000,
    "lineType": "Comment",
    "lineObjectNumber": "",
    "description": "Deliver to depot 3",
    "quantity": 0,
    "directUnitCost": 0,
    "expectedReceiptDate": "0001-01-01",
}


class ApiV2MappingTest(SimpleTestCase):
    def setUp(self):
        self.mapper = BusinessCentralMapper()

    def test_header_mapping(self):
        po = self.mapper.map_purchase_order(_API_PO, [_API_LINE])
        self.assertEqual(po.external_id, "5d115c9c-44e3-ea11-bb43-000d3a2feca1")
        self.assertEqual(po.po_number, "106001")
        self.assertEqual(po.supplier_no, "30000")
        self.assertEqual(po.supplier_name, "CoolWood Technologies")
        self.assertEqual(po.status, "released")  # API "Open" == released document
        self.assertEqual(po.order_date, date(2026, 9, 14))

    def test_blank_bc_date_is_none(self):
        po = self.mapper.map_purchase_order(_API_PO, [])
        self.assertIsNone(po.expected_receipt_date)

    def test_requested_receipt_date_used_for_header(self):
        po = self.mapper.map_purchase_order({**_API_PO, "requestedReceiptDate": "2026-10-30"}, [])
        self.assertEqual(po.expected_receipt_date, date(2026, 10, 30))

    def test_empty_currency_falls_back_to_default(self):
        self.assertEqual(self.mapper.map_purchase_order(_API_PO, []).currency, "EUR")

    def test_last_modified_with_fraction_is_aware_utc(self):
        po = self.mapper.map_purchase_order(_API_PO, [])
        self.assertEqual(po.source_last_modified, datetime(2026, 9, 14, 8, 12, 44, 137000, tzinfo=UTC))

    def test_offset_datetime_converted_to_utc(self):
        po = self.mapper.map_purchase_order({**_API_PO, "lastModifiedDateTime": "2026-09-14T10:00:00+02:00"}, [])
        self.assertEqual(po.source_last_modified, datetime(2026, 9, 14, 8, 0, tzinfo=UTC))

    def test_naive_datetime_treated_as_utc(self):
        po = self.mapper.map_purchase_order({**_API_PO, "lastModifiedDateTime": "2026-09-14T08:00:00"}, [])
        self.assertEqual(po.source_last_modified, datetime(2026, 9, 14, 8, 0, tzinfo=UTC))

    def test_line_mapping(self):
        line = self.mapper.map_purchase_order(_API_PO, [_API_LINE]).lines[0]
        self.assertEqual(line.external_id, "6f115c9c-44e3-ea11-bb43-000d3a2feca1")
        self.assertEqual(line.line_no, "10000")
        self.assertEqual(line.item_no, "CON22G1")
        self.assertEqual(line.ordered_qty, Decimal("50"))
        self.assertEqual(line.unit_price, Decimal("2140.35"))
        self.assertEqual(line.expected_receipt_date, date(2026, 11, 2))
        self.assertIsNone(line.source_last_modified)  # not a v2.0 line property

    def test_raw_payload_preserved(self):
        po = self.mapper.map_purchase_order(_API_PO, [_API_LINE])
        self.assertEqual(po.raw_payload["@odata.etag"], _API_PO["@odata.etag"])
        self.assertEqual(po.lines[0].raw_payload["lineType"], "Item")

    def test_comment_line_maps_without_item(self):
        line = self.mapper.map_purchase_order(_API_PO, [_COMMENT_LINE]).lines[0]
        self.assertEqual(line.item_no, "")
        self.assertEqual(line.ordered_qty, Decimal("0"))
        self.assertIsNone(line.expected_receipt_date)

    def test_missing_optional_line_values(self):
        line = self.mapper.map_purchase_order(_API_PO, [{"id": "x", "sequence": 30000}]).lines[0]
        self.assertEqual(line.item_no, "")
        self.assertEqual(line.ordered_qty, Decimal("0"))
        self.assertIsNone(line.unit_price)

    def test_null_optional_header_values(self):
        raw = {"id": "a", "number": "PO1", "vendorName": None, "status": None, "orderDate": None}
        po = self.mapper.map_purchase_order(raw, [])
        self.assertEqual(po.supplier_name, "")
        self.assertEqual(po.status, "open")
        self.assertIsNone(po.order_date)

    def test_decimal_precision_kept(self):
        line = self.mapper.map_purchase_order(_API_PO, [{**_API_LINE, "quantity": 12.5, "directUnitCost": 0.1}]).lines[
            0
        ]
        self.assertEqual(line.ordered_qty, Decimal("12.5"))
        self.assertEqual(line.unit_price, Decimal("0.1"))

    def test_unexpected_status_is_open_and_logged(self):
        with self.assertLogs("apps.scm.integrations.business_systems.business_central.mapper", "WARNING"):
            po = self.mapper.map_purchase_order({**_API_PO, "status": "Pending Prepayment"}, [])
        self.assertEqual(po.status, "open")
        self.assertEqual(po.raw_payload["status"], "Pending Prepayment")

    def test_missing_po_id_raises(self):
        with self.assertRaisesMessage(ValueError, "missing 'id'"):
            self.mapper.map_purchase_order({**_API_PO, "id": None}, [])

    def test_missing_po_number_raises(self):
        with self.assertRaisesMessage(ValueError, "missing 'number'"):
            self.mapper.map_purchase_order({**_API_PO, "number": ""}, [])

    def test_missing_line_id_raises(self):
        with self.assertRaisesMessage(ValueError, "line is missing 'id'"):
            self.mapper.map_purchase_order(_API_PO, [{**_API_LINE, "id": ""}])

    def test_non_numeric_quantity_raises(self):
        with self.assertRaisesMessage(ValueError, "quantity"):
            self.mapper.map_purchase_order(_API_PO, [{**_API_LINE, "quantity": "fifty"}])
