"""Tests for business-rule validation of PO import rows."""

from decimal import Decimal

from django.test import TestCase

from apps.scm.imports.models import ImportError, ImportJob, ImportRow
from apps.scm.imports.validators import validate_import_row
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine

from .helpers import make_team, make_user


def _make_po_import_job(team, user) -> ImportJob:
    from django.core.files.uploadedfile import SimpleUploadedFile

    f = SimpleUploadedFile("test_po.csv", b"PO Number\nPO-001", content_type="text/csv")
    return ImportJob.objects.create(
        team=team,
        created_by=user,
        file=f,
        original_filename="test_po.csv",
        import_type=ImportJob.ImportType.PURCHASE_ORDERS,
        status=ImportJob.Status.UPLOADED,
    )


def _make_po_row(job: ImportJob, validated_data: dict, row_number: int = 1) -> ImportRow:
    return ImportRow.objects.create(
        import_job=job,
        row_number=row_number,
        raw_data={},
        mapped_data={},
        validated_data=validated_data,
        status=ImportRow.Status.PENDING,
    )


VALID_PO_DATA = {
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


class POValidationNoDuplicateTest(TestCase):
    """Validator marks new PO rows as VALID when no duplicates exist."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="po-val-team")
        cls.user = make_user("po-val@example.com")

    def test_new_po_row_is_marked_valid(self):
        job = _make_po_import_job(self.team, self.user)
        row = _make_po_row(job, VALID_PO_DATA)
        validate_import_row(row, self.team)
        row.refresh_from_db()
        self.assertEqual(row.status, ImportRow.Status.VALID)

    def test_new_po_row_has_no_errors(self):
        job = _make_po_import_job(self.team, self.user)
        row = _make_po_row(job, VALID_PO_DATA)
        validate_import_row(row, self.team)
        row.refresh_from_db()
        self.assertEqual(row.errors, [])

    def test_no_import_errors_created_for_valid_row(self):
        job = _make_po_import_job(self.team, self.user)
        row = _make_po_row(job, VALID_PO_DATA)
        validate_import_row(row, self.team)
        self.assertEqual(ImportError.objects.filter(import_row=row).count(), 0)

    def test_validator_stores_po_external_id_in_validated_data(self):
        """Validator derives and persists po_external_id for the importer."""
        job = _make_po_import_job(self.team, self.user)
        row = _make_po_row(job, VALID_PO_DATA)
        validate_import_row(row, self.team)
        row.refresh_from_db()
        self.assertEqual(row.validated_data.get("po_external_id"), "PO-2026-001")

    def test_validator_stores_line_external_id_in_validated_data(self):
        job = _make_po_import_job(self.team, self.user)
        row = _make_po_row(job, VALID_PO_DATA)
        validate_import_row(row, self.team)
        row.refresh_from_db()
        self.assertEqual(row.validated_data.get("line_external_id"), "PO-2026-001-10000")


class POValidationDuplicateTest(TestCase):
    """Duplicate PO lines produce a WARNING — row stays VALID (skip strategy)."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="po-dup-team")
        cls.user = make_user("po-dup@example.com")
        cls.existing_po = PurchaseOrder.objects.create(
            team=cls.team,
            external_id="PO-2026-001",
            po_number="PO-2026-001",
            supplier_no="SUPP-001",
            supplier_name="Acme Corp",
            status="open",
        )
        cls.existing_line = PurchaseOrderLine.objects.create(
            team=cls.team,
            purchase_order=cls.existing_po,
            external_id="PO-2026-001-10000",
            line_no="10000",
            item_no="ITM-001",
            description="Widget A",
            ordered_qty=Decimal("50"),
            shipped_qty=Decimal("0"),
            received_qty=Decimal("0"),
        )

    def test_duplicate_po_line_against_existing_data_gets_warning(self):
        """
        Duplicate strategy: WARNING-based skip.
        Rows with existing PO lines get a WARNING but remain VALID.
        The importer will skip them unless update_existing=True.
        """
        job = _make_po_import_job(self.team, self.user)
        row = _make_po_row(job, VALID_PO_DATA)
        validate_import_row(row, self.team)
        row.refresh_from_db()
        self.assertEqual(row.status, ImportRow.Status.VALID)

    def test_duplicate_po_line_warning_has_correct_code(self):
        job = _make_po_import_job(self.team, self.user)
        row = _make_po_row(job, VALID_PO_DATA)
        validate_import_row(row, self.team)
        row.refresh_from_db()
        error_codes = [e["code"] for e in row.errors]
        self.assertIn("duplicate_po_line", error_codes)

    def test_duplicate_warning_has_warning_severity_not_error(self):
        job = _make_po_import_job(self.team, self.user)
        row = _make_po_row(job, VALID_PO_DATA)
        validate_import_row(row, self.team)
        row.refresh_from_db()
        for error in row.errors:
            if error.get("code") == "duplicate_po_line":
                self.assertEqual(error["severity"], ImportError.Severity.WARNING)

    def test_duplicate_warning_is_persisted_to_import_error_model(self):
        job = _make_po_import_job(self.team, self.user)
        row = _make_po_row(job, VALID_PO_DATA)
        validate_import_row(row, self.team)
        errors = ImportError.objects.filter(import_row=row, code="duplicate_po_line")
        self.assertEqual(errors.count(), 1)
        self.assertEqual(errors.first().severity, ImportError.Severity.WARNING)

    def test_duplicate_warning_message_contains_po_number_and_line_no(self):
        job = _make_po_import_job(self.team, self.user)
        row = _make_po_row(job, VALID_PO_DATA)
        validate_import_row(row, self.team)
        row.refresh_from_db()
        dup_errors = [e for e in row.errors if e.get("code") == "duplicate_po_line"]
        self.assertTrue(dup_errors)
        self.assertIn("PO-2026-001", dup_errors[0]["message"])
        self.assertIn("10000", dup_errors[0]["message"])

    def test_new_line_on_existing_po_is_valid_no_warning(self):
        """A new line number on an already-imported PO is fine."""
        data = {**VALID_PO_DATA, "line_no": "20000", "line_external_id": "PO-2026-001-20000"}
        job = _make_po_import_job(self.team, self.user)
        row = _make_po_row(job, data)
        validate_import_row(row, self.team)
        row.refresh_from_db()
        self.assertEqual(row.status, ImportRow.Status.VALID)
        dup_errors = [e for e in row.errors if e.get("code") == "duplicate_po_line"]
        self.assertEqual(dup_errors, [])

    def test_different_team_duplicate_is_not_flagged(self):
        """Team isolation: duplicate check is scoped to the current team."""
        from .helpers import make_team

        other_team = make_team(name="Other Team", slug="other-po-team")
        job = _make_po_import_job(other_team, self.user)
        row = _make_po_row(job, VALID_PO_DATA)
        validate_import_row(row, other_team)
        row.refresh_from_db()
        self.assertEqual(row.status, ImportRow.Status.VALID)
        dup_errors = [e for e in row.errors if e.get("code") == "duplicate_po_line"]
        self.assertEqual(dup_errors, [])
