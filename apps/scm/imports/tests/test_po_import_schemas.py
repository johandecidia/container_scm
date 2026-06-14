"""Unit tests for PurchaseOrderImportRowSchema — pure Pydantic, no database."""

import datetime
from decimal import Decimal

from django.test import SimpleTestCase

from apps.scm.imports.models import ImportJob
from apps.scm.imports.schemas import PurchaseOrderImportRowSchema, validate_row_data

VALID_ROW = {
    "po_number": "PO-2026-001",
    "supplier_no": "SUPP-001",
    "supplier_name": "Acme Corp",
    "order_date": "2026-03-15",
    "expected_receipt_date": "2026-06-01",
    "currency": "USD",
    "line_no": "10000",
    "item_no": "ITM-001",
    "description": "Widget A",
    "ordered_qty": "50",
}


class PurchaseOrderImportRowSchemaValidTest(SimpleTestCase):
    """Schema accepts well-formed data."""

    def test_valid_row_schema_accepts_valid_data(self):
        schema = PurchaseOrderImportRowSchema.model_validate(VALID_ROW)
        self.assertEqual(schema.po_number, "PO-2026-001")
        self.assertEqual(schema.supplier_no, "SUPP-001")
        self.assertEqual(schema.supplier_name, "Acme Corp")
        self.assertEqual(schema.line_no, "10000")
        self.assertEqual(schema.item_no, "ITM-001")

    def test_valid_row_schema_parses_ordered_qty(self):
        schema = PurchaseOrderImportRowSchema.model_validate(VALID_ROW)
        self.assertEqual(schema.ordered_qty, Decimal("50"))

    def test_valid_row_schema_parses_delivery_date(self):
        schema = PurchaseOrderImportRowSchema.model_validate(VALID_ROW)
        self.assertEqual(schema.expected_receipt_date, datetime.date(2026, 6, 1))

    def test_valid_row_schema_parses_order_date(self):
        schema = PurchaseOrderImportRowSchema.model_validate(VALID_ROW)
        self.assertEqual(schema.order_date, datetime.date(2026, 3, 15))

    def test_valid_row_schema_parses_date_dd_mm_yyyy(self):
        row = {**VALID_ROW, "order_date": "15-03-2026"}
        schema = PurchaseOrderImportRowSchema.model_validate(row)
        self.assertEqual(schema.order_date, datetime.date(2026, 3, 15))

    def test_valid_row_schema_parses_date_dd_slash_mm_slash_yyyy(self):
        row = {**VALID_ROW, "order_date": "15/03/2026"}
        schema = PurchaseOrderImportRowSchema.model_validate(row)
        self.assertEqual(schema.order_date, datetime.date(2026, 3, 15))

    def test_valid_row_schema_normalises_currency_uppercase(self):
        row = {**VALID_ROW, "currency": "eur"}
        schema = PurchaseOrderImportRowSchema.model_validate(row)
        self.assertEqual(schema.currency, "EUR")

    def test_valid_row_schema_defaults_currency_to_eur_when_missing(self):
        row = {k: v for k, v in VALID_ROW.items() if k != "currency"}
        schema = PurchaseOrderImportRowSchema.model_validate(row)
        self.assertEqual(schema.currency, "EUR")

    def test_valid_row_schema_defaults_currency_to_eur_when_empty(self):
        row = {**VALID_ROW, "currency": ""}
        schema = PurchaseOrderImportRowSchema.model_validate(row)
        self.assertEqual(schema.currency, "EUR")

    def test_valid_row_schema_trims_whitespace_on_required_fields(self):
        row = {**VALID_ROW, "po_number": "  PO-2026-001  ", "supplier_no": " SUPP-001 "}
        schema = PurchaseOrderImportRowSchema.model_validate(row)
        self.assertEqual(schema.po_number, "PO-2026-001")
        self.assertEqual(schema.supplier_no, "SUPP-001")

    def test_valid_row_schema_trims_description(self):
        row = {**VALID_ROW, "description": "  Widget A  "}
        schema = PurchaseOrderImportRowSchema.model_validate(row)
        self.assertEqual(schema.description, "Widget A")

    def test_valid_row_schema_empty_description_allowed(self):
        row = {**VALID_ROW, "description": ""}
        schema = PurchaseOrderImportRowSchema.model_validate(row)
        self.assertEqual(schema.description, "")

    def test_valid_row_schema_optional_dates_accept_none(self):
        row = {**VALID_ROW, "order_date": "", "expected_receipt_date": ""}
        schema = PurchaseOrderImportRowSchema.model_validate(row)
        self.assertIsNone(schema.order_date)
        self.assertIsNone(schema.expected_receipt_date)

    def test_valid_row_schema_accepts_decimal_quantity(self):
        row = {**VALID_ROW, "ordered_qty": "12.5"}
        schema = PurchaseOrderImportRowSchema.model_validate(row)
        self.assertEqual(schema.ordered_qty, Decimal("12.5"))

    def test_valid_row_schema_accepts_comma_decimal_quantity(self):
        # European locale format
        row = {**VALID_ROW, "ordered_qty": "12,5"}
        schema = PurchaseOrderImportRowSchema.model_validate(row)
        self.assertEqual(schema.ordered_qty, Decimal("12.5"))


class PurchaseOrderImportRowSchemaRejectsInvalidTest(SimpleTestCase):
    """Schema raises validation errors for invalid data."""

    def _get_errors(self, row: dict) -> list[dict]:
        _, errors = validate_row_data(ImportJob.ImportType.PURCHASE_ORDERS, row)
        return errors

    def test_schema_rejects_missing_supplier_no(self):
        row = {k: v for k, v in VALID_ROW.items() if k != "supplier_no"}
        _, errors = validate_row_data(ImportJob.ImportType.PURCHASE_ORDERS, row)
        self.assertTrue(errors)
        fields = [e["field"] for e in errors]
        self.assertIn("supplier_no", fields)

    def test_schema_rejects_empty_supplier_no(self):
        row = {**VALID_ROW, "supplier_no": ""}
        errors = self._get_errors(row)
        self.assertTrue(errors)
        fields = [e["field"] for e in errors]
        self.assertIn("supplier_no", fields)

    def test_schema_rejects_whitespace_only_supplier_no(self):
        row = {**VALID_ROW, "supplier_no": "   "}
        errors = self._get_errors(row)
        self.assertTrue(errors)

    def test_schema_rejects_missing_supplier_name(self):
        row = {**VALID_ROW, "supplier_name": ""}
        errors = self._get_errors(row)
        self.assertTrue(errors)
        fields = [e["field"] for e in errors]
        self.assertIn("supplier_name", fields)

    def test_schema_rejects_missing_po_number(self):
        row = {**VALID_ROW, "po_number": ""}
        errors = self._get_errors(row)
        self.assertTrue(errors)
        fields = [e["field"] for e in errors]
        self.assertIn("po_number", fields)

    def test_schema_rejects_missing_item_no(self):
        row = {**VALID_ROW, "item_no": ""}
        errors = self._get_errors(row)
        self.assertTrue(errors)
        fields = [e["field"] for e in errors]
        self.assertIn("item_no", fields)

    def test_schema_rejects_missing_line_no(self):
        row = {**VALID_ROW, "line_no": ""}
        errors = self._get_errors(row)
        self.assertTrue(errors)
        fields = [e["field"] for e in errors]
        self.assertIn("line_no", fields)

    def test_schema_rejects_text_quantity(self):
        row = {**VALID_ROW, "ordered_qty": "abc"}
        errors = self._get_errors(row)
        self.assertTrue(errors)
        fields = [e["field"] for e in errors]
        self.assertIn("ordered_qty", fields)

    def test_schema_rejects_negative_quantity(self):
        row = {**VALID_ROW, "ordered_qty": "-10"}
        errors = self._get_errors(row)
        self.assertTrue(errors)
        fields = [e["field"] for e in errors]
        self.assertIn("ordered_qty", fields)

    def test_schema_rejects_zero_quantity(self):
        row = {**VALID_ROW, "ordered_qty": "0"}
        errors = self._get_errors(row)
        self.assertTrue(errors)
        fields = [e["field"] for e in errors]
        self.assertIn("ordered_qty", fields)

    def test_schema_rejects_empty_quantity(self):
        row = {**VALID_ROW, "ordered_qty": ""}
        errors = self._get_errors(row)
        self.assertTrue(errors)
        fields = [e["field"] for e in errors]
        self.assertIn("ordered_qty", fields)

    def test_schema_rejects_invalid_date(self):
        row = {**VALID_ROW, "order_date": "not-a-date"}
        errors = self._get_errors(row)
        self.assertTrue(errors)
        fields = [e["field"] for e in errors]
        self.assertIn("order_date", fields)

    def test_error_message_includes_field_name(self):
        row = {**VALID_ROW, "ordered_qty": "abc"}
        errors = self._get_errors(row)
        self.assertTrue(any(e["field"] == "ordered_qty" for e in errors))

    def test_validate_row_data_returns_empty_dict_on_error(self):
        row = {**VALID_ROW, "supplier_no": ""}
        validated, errors = validate_row_data(ImportJob.ImportType.PURCHASE_ORDERS, row)
        self.assertEqual(validated, {})
        self.assertTrue(errors)

    def test_validate_row_data_returns_populated_dict_on_success(self):
        validated, errors = validate_row_data(ImportJob.ImportType.PURCHASE_ORDERS, VALID_ROW)
        self.assertEqual(errors, [])
        self.assertIn("po_number", validated)
        self.assertIn("ordered_qty", validated)
        self.assertIn("supplier_no", validated)
