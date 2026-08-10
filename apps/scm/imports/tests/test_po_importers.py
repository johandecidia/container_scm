"""Tests for PO import execution and end-to-end import pipeline."""

import os
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.scm.imports.importers import run_import
from apps.scm.imports.models import ImportJob, ImportRow
from apps.scm.imports.services import confirm_import_job, parse_import_job, validate_import_job
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine
from apps.teams.models import Team
from apps.users.models import CustomUser

from .helpers import make_team, make_user

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_fixture(name: str) -> SimpleUploadedFile:
    path = os.path.join(FIXTURES_DIR, name)
    with open(path, "rb") as f:
        return SimpleUploadedFile(name, f.read(), content_type="text/csv")


def _make_po_job(team, user, rows: list[dict]) -> ImportJob:
    headers = list(rows[0].keys())
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(row.get(h, "")) for h in headers))
    content = "\n".join(lines).encode("utf-8")
    f = SimpleUploadedFile("po_test.csv", content, content_type="text/csv")
    return ImportJob.objects.create(
        team=team,
        created_by=user,
        file=f,
        original_filename="po_test.csv",
        import_type=ImportJob.ImportType.PURCHASE_ORDERS,
        status=ImportJob.Status.UPLOADED,
    )


def _make_validated_row(job: ImportJob, validated_data: dict, row_number: int = 1) -> ImportRow:
    return ImportRow.objects.create(
        import_job=job,
        row_number=row_number,
        raw_data={},
        mapped_data={},
        validated_data=validated_data,
        status=ImportRow.Status.VALID,
    )


VALID_ROW_DATA = {
    "po_number": "PO-2026-001",
    "supplier_no": "SUPP-001",
    "supplier_name": "Acme Corp",
    "order_date": None,
    "expected_receipt_date": None,
    "currency": "USD",
    "line_no": "10000",
    "item_no": "ITM-001",
    "description": "Widget A",
    "ordered_qty": "50",  # stored as string in JSONField (model_dump(mode='json'))
    "po_external_id": "PO-2026-001",
    "line_external_id": "PO-2026-001-10000",
}


class ValidPOImportTest(TestCase):
    """Valid PO rows are imported correctly."""

    team: Team
    user: CustomUser

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="po-imp-team")
        cls.user = make_user("po-imp@example.com")

    def _run_job(self, validated_data_list: list[dict]) -> ImportJob:
        """Build a pre-validated job and run the importer."""
        f = SimpleUploadedFile("x.csv", b"col\nval", content_type="text/csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="x.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.VALIDATED,
        )
        for i, data in enumerate(validated_data_list, start=1):
            ImportRow.objects.create(
                import_job=job,
                row_number=i,
                validated_data=data,
                status=ImportRow.Status.VALID,
            )
        job.total_rows = len(validated_data_list)
        job.valid_rows = len(validated_data_list)
        job.save()
        return job

    def test_valid_purchase_orders_csv_imports_successfully(self):
        job = self._run_job([VALID_ROW_DATA])
        run_import(job)
        self.assertTrue(PurchaseOrder.objects.filter(team=self.team, external_id="PO-2026-001").exists())

    def test_valid_import_creates_purchase_order_lines(self):
        job = self._run_job([VALID_ROW_DATA])
        run_import(job)
        po = PurchaseOrder.objects.get(team=self.team, external_id="PO-2026-001")
        self.assertTrue(po.lines.filter(external_id="PO-2026-001-10000").exists())

    def test_valid_import_saves_supplier_correctly(self):
        job = self._run_job([VALID_ROW_DATA])
        run_import(job)
        po = PurchaseOrder.objects.get(team=self.team, external_id="PO-2026-001")
        self.assertEqual(po.supplier_no, "SUPP-001")
        self.assertEqual(po.supplier_name, "Acme Corp")

    def test_valid_import_saves_quantity_correctly(self):
        job = self._run_job([VALID_ROW_DATA])
        run_import(job)
        po = PurchaseOrder.objects.get(team=self.team, external_id="PO-2026-001")
        line = po.lines.get(external_id="PO-2026-001-10000")
        self.assertEqual(line.ordered_qty, Decimal("50.00"))

    def test_valid_import_saves_currency_correctly(self):
        job = self._run_job([VALID_ROW_DATA])
        run_import(job)
        po = PurchaseOrder.objects.get(team=self.team, external_id="PO-2026-001")
        self.assertEqual(po.currency, "USD")

    def test_valid_import_returns_success_summary(self):
        job = self._run_job([VALID_ROW_DATA])
        run_import(job)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)
        self.assertEqual(job.processed_rows, 1)

    def test_valid_import_marks_row_as_imported(self):
        job = self._run_job([VALID_ROW_DATA])
        run_import(job)
        row = job.rows.first()
        self.assertEqual(row.status, ImportRow.Status.IMPORTED)

    def test_valid_import_no_errors_on_success(self):
        job = self._run_job([VALID_ROW_DATA])
        run_import(job)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)

    def test_multiple_lines_same_po_create_one_po_multiple_lines(self):
        row2 = {
            **VALID_ROW_DATA,
            "line_no": "20000",
            "item_no": "ITM-002",
            "line_external_id": "PO-2026-001-20000",
            "ordered_qty": "25",
        }
        job = self._run_job([VALID_ROW_DATA, row2])
        run_import(job)
        po_count = PurchaseOrder.objects.filter(team=self.team, external_id="PO-2026-001").count()
        self.assertEqual(po_count, 1)
        po = PurchaseOrder.objects.get(team=self.team, external_id="PO-2026-001")
        self.assertEqual(po.lines.count(), 2)

    def test_two_different_pos_creates_two_po_records(self):
        row2 = {
            **VALID_ROW_DATA,
            "po_number": "PO-2026-002",
            "po_external_id": "PO-2026-002",
            "line_external_id": "PO-2026-002-10000",
            "supplier_no": "SUPP-002",
            "supplier_name": "Beta Supplies",
        }
        job = self._run_job([VALID_ROW_DATA, row2])
        run_import(job)
        self.assertEqual(PurchaseOrder.objects.filter(team=self.team).count(), 2)


class POImportAtomicRollbackTest(TestCase):
    """Import is wrapped in @transaction.atomic — a failure rolls back all rows."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="po-atomic-team")
        cls.user = make_user("po-atomic@example.com")

    def test_atomic_import_rolls_back_all_rows_when_importer_raises(self):
        """If the importer raises mid-job, no POs should be created.

        _IMPORTERS holds a reference to the function captured at module load time,
        so we patch the dict entry directly to inject the exception.
        """
        from unittest.mock import MagicMock

        import apps.scm.imports.importers as importers_module

        f = SimpleUploadedFile("x.csv", b"col\nval", content_type="text/csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="x.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.VALIDATED,
        )
        ImportRow.objects.create(
            import_job=job,
            row_number=1,
            validated_data=VALID_ROW_DATA,
            status=ImportRow.Status.VALID,
        )

        original = importers_module._IMPORTERS[ImportJob.ImportType.PURCHASE_ORDERS]
        importers_module._IMPORTERS[ImportJob.ImportType.PURCHASE_ORDERS] = MagicMock(side_effect=RuntimeError("boom"))
        try:
            with self.assertRaises(RuntimeError):
                run_import(job)
        finally:
            importers_module._IMPORTERS[ImportJob.ImportType.PURCHASE_ORDERS] = original

        self.assertEqual(PurchaseOrder.objects.filter(team=self.team).count(), 0)


class POImportDuplicateHandlingTest(TestCase):
    """Duplicate strategy: WARNING + skip (update_existing=False default).

    - Duplicate PO lines are SKIPPED when update_existing=False.
    - Duplicate PO lines are UPDATED when update_existing=True.
    - Re-importing the same file is idempotent.
    """

    team: Team
    user: CustomUser

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="po-dup-imp-team")
        cls.user = make_user("po-dup-imp@example.com")

    def _make_job(self, row_data: dict, row_number: int = 1, status: str = ImportRow.Status.VALID) -> ImportJob:
        f = SimpleUploadedFile("x.csv", b"col\nval", content_type="text/csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="x.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.VALIDATED,
            total_rows=1,
            valid_rows=1,
        )
        ImportRow.objects.create(
            import_job=job,
            row_number=row_number,
            validated_data=row_data,
            status=status,
        )
        return job

    def test_duplicate_po_lines_in_same_file_second_occurrence_is_skipped(self):
        """
        Two rows with the same PO+line in one file: first creates, second skips.
        This tests in-DB duplicate behaviour after the first row is processed.
        """
        f = SimpleUploadedFile("x.csv", b"col\nval", content_type="text/csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="x.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.VALIDATED,
            total_rows=2,
            valid_rows=2,
        )
        ImportRow.objects.create(
            import_job=job, row_number=1, validated_data=VALID_ROW_DATA, status=ImportRow.Status.VALID
        )
        ImportRow.objects.create(
            import_job=job, row_number=2, validated_data=VALID_ROW_DATA, status=ImportRow.Status.VALID
        )
        run_import(job)
        # Only one PO line should exist.
        po = PurchaseOrder.objects.get(team=self.team, external_id="PO-2026-001")
        self.assertEqual(po.lines.count(), 1)
        # Second row should be SKIPPED.
        rows = list(job.rows.order_by("row_number"))
        self.assertEqual(rows[0].status, ImportRow.Status.IMPORTED)
        self.assertEqual(rows[1].status, ImportRow.Status.SKIPPED)

    def test_duplicate_po_lines_against_existing_data_are_skipped(self):
        """Re-import of already-imported row is skipped by default."""
        job1 = self._make_job(VALID_ROW_DATA)
        run_import(job1)
        self.assertTrue(PurchaseOrder.objects.filter(team=self.team, external_id="PO-2026-001").exists())

        job2 = self._make_job(VALID_ROW_DATA)
        run_import(job2)
        row2 = job2.rows.first()
        self.assertEqual(row2.status, ImportRow.Status.SKIPPED)
        # Still only one PO line.
        po = PurchaseOrder.objects.get(team=self.team, external_id="PO-2026-001")
        self.assertEqual(po.lines.count(), 1)

    def test_reimporting_same_file_is_idempotent(self):
        """Fixture-based: reimporting valid_purchase_orders.csv twice gives same result."""
        f1 = _load_fixture("valid_purchase_orders.csv")
        job1 = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f1,
            original_filename="valid_purchase_orders.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.UPLOADED,
        )
        parse_import_job(job1)
        validate_import_job(job1)
        confirm_import_job(job1)

        po_count_after_first = PurchaseOrder.objects.filter(team=self.team).count()
        line_count_after_first = PurchaseOrderLine.objects.filter(team=self.team).count()

        f2 = _load_fixture("valid_purchase_orders.csv")
        job2 = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f2,
            original_filename="valid_purchase_orders.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.UPLOADED,
        )
        parse_import_job(job2)
        validate_import_job(job2)
        confirm_import_job(job2)

        self.assertEqual(PurchaseOrder.objects.filter(team=self.team).count(), po_count_after_first)
        self.assertEqual(PurchaseOrderLine.objects.filter(team=self.team).count(), line_count_after_first)

    def test_update_existing_updates_po_line(self):
        """update_existing=True overwrites an existing PO line."""
        job1 = self._make_job(VALID_ROW_DATA)
        run_import(job1)

        updated_data = {**VALID_ROW_DATA, "ordered_qty": "99"}
        job2 = self._make_job(updated_data)
        run_import(job2, update_existing=True)

        po = PurchaseOrder.objects.get(team=self.team, external_id="PO-2026-001")
        line = po.lines.get(external_id="PO-2026-001-10000")
        self.assertEqual(line.ordered_qty, Decimal("99"))
        row2 = job2.rows.first()
        self.assertEqual(row2.status, ImportRow.Status.IMPORTED)


class POImportInvalidRowsNotImportedTest(TestCase):
    """INVALID rows are never imported, regardless of their data."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="po-inv-team")
        cls.user = make_user("po-inv@example.com")

    def test_invalid_row_is_not_imported(self):
        f = SimpleUploadedFile("x.csv", b"col\nval", content_type="text/csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="x.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.VALIDATED,
        )
        ImportRow.objects.create(
            import_job=job,
            row_number=1,
            validated_data=VALID_ROW_DATA,
            status=ImportRow.Status.INVALID,
        )
        run_import(job)
        self.assertFalse(PurchaseOrder.objects.filter(team=self.team).exists())


class POImportFixtureTest(TestCase):
    """End-to-end pipeline tests using CSV fixture files."""

    team: Team
    user: CustomUser

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="po-fixture-team")
        cls.user = make_user("po-fixture@example.com")

    def _run_fixture(self, filename: str) -> ImportJob:
        f = _load_fixture(filename)
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename=filename,
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.UPLOADED,
        )
        parse_import_job(job)
        validate_import_job(job)
        confirm_import_job(job)
        return job

    def test_valid_purchase_orders_csv_imports_successfully(self):
        """valid_purchase_orders.csv: 3 rows → 2 POs, 3 lines, 0 errors."""
        job = self._run_fixture("valid_purchase_orders.csv")
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)
        self.assertEqual(PurchaseOrder.objects.filter(team=self.team).count(), 2)
        self.assertEqual(PurchaseOrderLine.objects.filter(team=self.team).count(), 3)

    def test_valid_fixture_import_creates_correct_po_numbers(self):
        self._run_fixture("valid_purchase_orders.csv")
        self.assertTrue(PurchaseOrder.objects.filter(team=self.team, po_number="PO-2026-001").exists())
        self.assertTrue(PurchaseOrder.objects.filter(team=self.team, po_number="PO-2026-002").exists())

    def test_valid_fixture_import_creates_correct_line_quantities(self):
        self._run_fixture("valid_purchase_orders.csv")
        po = PurchaseOrder.objects.get(team=self.team, po_number="PO-2026-001")
        line = po.lines.get(line_no="10000")
        self.assertEqual(line.ordered_qty, Decimal("50.000"))

    def test_missing_supplier_rows_are_now_valid(self):
        """invalid_missing_supplier.csv: supplier_no/supplier_name are optional — rows parse as VALID."""
        f = _load_fixture("invalid_missing_supplier.csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="invalid_missing_supplier.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.UPLOADED,
        )
        parse_import_job(job)
        job.refresh_from_db()
        self.assertEqual(job.rows.filter(status=ImportRow.Status.INVALID).count(), 0)
        self.assertEqual(job.rows.filter(status=ImportRow.Status.VALID).count(), 2)

    def test_missing_supplier_rows_import_successfully(self):
        """Rows without supplier info import without errors (supplier fields default to empty string)."""
        f = _load_fixture("invalid_missing_supplier.csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="invalid_missing_supplier.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.UPLOADED,
        )
        parse_import_job(job)
        validate_import_job(job)
        confirm_import_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)
        from apps.scm.procurement.models import PurchaseOrder

        self.assertEqual(PurchaseOrder.objects.filter(team=self.team).count(), 2)

    def test_invalid_quantities_rows_fail_with_clear_errors(self):
        """invalid_quantities.csv: text/negative/zero/empty quantities → INVALID rows."""
        f = _load_fixture("invalid_quantities.csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="invalid_quantities.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.UPLOADED,
        )
        parse_import_job(job)
        job.refresh_from_db()
        invalid_rows = job.rows.filter(status=ImportRow.Status.INVALID)
        # All 4 rows have invalid quantities
        self.assertEqual(invalid_rows.count(), 4)

    def test_import_fails_when_quantity_is_text(self):
        f = _load_fixture("invalid_quantities.csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="invalid_quantities.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.UPLOADED,
        )
        parse_import_job(job)
        invalid_rows = job.rows.filter(status=ImportRow.Status.INVALID)
        qty_errors = []
        for row in invalid_rows:
            qty_errors.extend([e for e in row.errors if e.get("field") == "ordered_qty"])
        self.assertTrue(qty_errors, "Expected ordered_qty errors for invalid quantities")

    def test_import_fails_when_quantity_is_negative(self):
        job_row_data = {
            "PO Number": "PO-2026-006",
            "Supplier No": "SUPP-001",
            "Supplier Name": "Acme Corp",
            "Order Date": "2026-03-15",
            "Expected Receipt Date": "2026-06-01",
            "Currency": "USD",
            "Line No": "10000",
            "Item No": "ITM-007",
            "Description": "Widget G",
            "Ordered Qty": "-10",
        }
        header = (
            "PO Number,Supplier No,Supplier Name,Order Date,"
            "Expected Receipt Date,Currency,Line No,Item No,Description,Ordered Qty\n"
        )
        content = header + ",".join(str(job_row_data[h]) for h in job_row_data)
        f = SimpleUploadedFile("neg.csv", content.encode(), content_type="text/csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="neg.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.UPLOADED,
        )
        parse_import_job(job)
        row = job.rows.first()
        self.assertEqual(row.status, ImportRow.Status.INVALID)
        qty_errors = [e for e in row.errors if e.get("field") == "ordered_qty"]
        self.assertTrue(qty_errors)

    def test_import_fails_when_quantity_is_empty(self):
        content = (
            "PO Number,Supplier No,Supplier Name,Order Date,"
            "Expected Receipt Date,Currency,Line No,Item No,Description,Ordered Qty\n"
            "PO-2026-008,SUPP-001,Acme Corp,2026-03-15,2026-06-01,USD,10000,ITM-009,Widget I,"
        )
        f = SimpleUploadedFile("empty_qty.csv", content.encode(), content_type="text/csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="empty_qty.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.UPLOADED,
        )
        parse_import_job(job)
        row = job.rows.first()
        self.assertEqual(row.status, ImportRow.Status.INVALID)

    def test_import_fails_when_delivery_date_is_invalid(self):
        content = (
            "PO Number,Supplier No,Supplier Name,Order Date,"
            "Expected Receipt Date,Currency,Line No,Item No,Description,Ordered Qty\n"
            "PO-2026-X01,SUPP-001,Acme Corp,not-a-date,2026-06-01,USD,10000,ITM-X01,Widget,10"
        )
        f = SimpleUploadedFile("bad_date.csv", content.encode(), content_type="text/csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="bad_date.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.UPLOADED,
        )
        parse_import_job(job)
        row = job.rows.first()
        self.assertEqual(row.status, ImportRow.Status.INVALID)
        date_errors = [
            e for e in row.errors if "date" in e.get("field", "").lower() or "date" in e.get("message", "").lower()
        ]
        self.assertTrue(date_errors)

    def test_duplicate_po_lines_in_same_fixture_second_occurrence_skipped(self):
        """
        Duplicate strategy: WARNING-based skip.
        duplicate_po_lines.csv has row 1 and row 2 with identical PO+line.
        After import: row 1 IMPORTED, row 2 SKIPPED, row 3 IMPORTED.
        """
        f = _load_fixture("duplicate_po_lines.csv")
        job = ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename="duplicate_po_lines.csv",
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.UPLOADED,
        )
        parse_import_job(job)
        validate_import_job(job)
        confirm_import_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)
        po = PurchaseOrder.objects.get(team=self.team, po_number="PO-2026-009")
        # Should have 2 unique lines (10000 and 20000), not 3
        self.assertEqual(po.lines.count(), 2)
