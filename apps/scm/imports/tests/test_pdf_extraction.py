"""Tests for PDF purchase order extraction.

Covers:
- Upload form: PDF accepted for purchase_orders, rejected for other types.
- Parsing: PDF extractor is called for .pdf purchase_order jobs.
- Error handling: timeout / HTTP errors mark job as FAILED and set extract_error.
- Row conversion: canonical JSON from the API becomes correct ImportRows.
- Pipeline: existing validate + confirm flow works unaffected after PDF extraction.
- Bug fix: duplicate-skip does NOT overwrite PO header when update_existing=False.
"""

from decimal import Decimal
from typing import cast
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils.datastructures import MultiValueDict

from apps.scm.imports.extractors.clients.pdf_fastapi import PDFExtractionError
from apps.scm.imports.forms import ImportUploadForm
from apps.scm.imports.importers import run_import
from apps.scm.imports.models import ImportJob, ImportRow
from apps.scm.imports.services import confirm_import_job, parse_import_job, validate_import_job
from apps.scm.procurement.models import PurchaseOrder

from .helpers import make_team, make_user

# Canonical row dict that the FastAPI service would return for a single PO line.
CANONICAL_ROW = {
    "po_number": "PO-PDF-001",
    "supplier_no": "SUPP-PDF",
    "supplier_name": "PDF Supplier AB",
    "order_date": "2026-03-01",
    "expected_receipt_date": "2026-06-15",
    "currency": "USD",
    "line_no": "10000",
    "item_no": "ITM-PDF-001",
    "description": "PDF Widget",
    "ordered_qty": "75",
}


def _make_pdf_upload_form(import_type: str) -> ImportUploadForm:
    f = SimpleUploadedFile("orders.pdf", b"%PDF-1.4 fake content")
    return ImportUploadForm(
        data={"import_type": import_type},
        files=cast(MultiValueDict, {"file": f}),
    )


def _make_pdf_job(team, user) -> ImportJob:
    """Create an ImportJob for a PDF purchase orders file (not yet parsed)."""
    f = SimpleUploadedFile("orders.pdf", b"%PDF-1.4 fake content")
    return ImportJob.objects.create(
        team=team,
        created_by=user,
        file=f,
        original_filename="orders.pdf",
        import_type=ImportJob.ImportType.PURCHASE_ORDERS,
        status=ImportJob.Status.UPLOADED,
    )


class PDFUploadFormTest(TestCase):
    """Form-level acceptance/rejection of .pdf files."""

    def test_pdf_accepted_for_purchase_orders(self):
        form = _make_pdf_upload_form(ImportJob.ImportType.PURCHASE_ORDERS)
        self.assertTrue(form.is_valid(), form.errors)

    def test_pdf_rejected_for_containers(self):
        form = _make_pdf_upload_form(ImportJob.ImportType.CONTAINERS)
        self.assertFalse(form.is_valid())
        self.assertTrue(form.non_field_errors())

    def test_pdf_rejected_for_shipments(self):
        form = _make_pdf_upload_form(ImportJob.ImportType.SHIPMENTS)
        self.assertFalse(form.is_valid())
        self.assertTrue(form.non_field_errors())


class PDFExtractorCalledTest(TestCase):
    """parse_file() calls the PDF extractor for .pdf purchase_orders jobs."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="pdf-call-team")
        cls.user = make_user("pdf-call@example.com")

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_extractor_called_for_pdf_job(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: [CANONICAL_ROW])

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)

        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        self.assertIn("/v1/purchase-orders/extract", call_url)

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_source_format_set_in_metadata(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: [CANONICAL_ROW])

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)

        job.refresh_from_db()
        self.assertEqual(job.metadata.get("source_format"), "pdf")


class PDFExtractorErrorHandlingTest(TestCase):
    """Errors from the FastAPI client mark the job as FAILED and set extract_error."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="pdf-err-team")
        cls.user = make_user("pdf-err@example.com")

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_timeout_sets_failed_status(self, mock_post):
        import requests as req_lib

        mock_post.side_effect = req_lib.Timeout()

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"), self.assertRaises(PDFExtractionError):
            parse_import_job(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.FAILED)
        self.assertIn("extract_error", job.metadata)
        self.assertIn("timed out", job.metadata["extract_error"])

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_http_500_sets_failed_status(self, mock_post):
        mock_post.return_value = MagicMock(status_code=500, text="Internal Server Error")

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"), self.assertRaises(PDFExtractionError):
            parse_import_job(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.FAILED)
        self.assertIn("extract_error", job.metadata)

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_http_422_sets_failed_status(self, mock_post):
        mock_post.return_value = MagicMock(status_code=422, text="Unprocessable Entity")

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"), self.assertRaises(PDFExtractionError):
            parse_import_job(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.FAILED)

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_invalid_json_sets_failed_status(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(side_effect=ValueError("No JSON")),
        )

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"), self.assertRaises(PDFExtractionError):
            parse_import_job(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.FAILED)

    def test_missing_base_url_sets_failed_status(self):
        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL=""), self.assertRaises(PDFExtractionError):
            parse_import_job(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.FAILED)


class PDFRowConversionTest(TestCase):
    """Canonical JSON from the API is turned into correctly structured ImportRows."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="pdf-rows-team")
        cls.user = make_user("pdf-rows@example.com")

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_api_rows_become_import_rows(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: [CANONICAL_ROW])

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)

        self.assertEqual(job.rows.count(), 1)
        row = job.rows.first()
        # raw_data should contain the canonical row
        self.assertEqual(row.raw_data.get("po_number"), "PO-PDF-001")

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_multi_row_api_response_creates_multiple_import_rows(self, mock_post):
        row2 = {**CANONICAL_ROW, "line_no": "20000", "item_no": "ITM-PDF-002"}
        mock_post.return_value = MagicMock(status_code=200, json=lambda: [CANONICAL_ROW, row2])

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)

        self.assertEqual(job.rows.count(), 2)

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_pydantic_validation_runs_on_pdf_rows(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: [CANONICAL_ROW])

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)

        row = job.rows.first()
        # Pydantic validation should have run and set validated_data
        self.assertIn("po_number", row.validated_data)
        self.assertEqual(row.status, ImportRow.Status.VALID)


class PDFFullPipelineTest(TestCase):
    """End-to-end: validate + confirm work correctly after PDF extraction."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="pdf-e2e-team")
        cls.user = make_user("pdf-e2e@example.com")

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_validate_and_confirm_after_pdf_extraction(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: [CANONICAL_ROW])

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)
        validate_import_job(job)
        confirm_import_job(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)
        self.assertTrue(PurchaseOrder.objects.filter(team=self.team, po_number="PO-PDF-001").exists())
        po = PurchaseOrder.objects.get(team=self.team, po_number="PO-PDF-001")
        self.assertTrue(po.lines.filter(line_no="10000").exists())
        line = po.lines.get(line_no="10000")
        self.assertEqual(line.ordered_qty, Decimal("75"))


class PODuplicateSkipBugFixTest(TestCase):
    """Duplicate-skip (update_existing=False) must NOT modify the PO header."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="po-dupfix-team")
        cls.user = make_user("po-dupfix@example.com")

    def _make_validated_job(self, row_data: dict) -> ImportJob:
        f = SimpleUploadedFile("x.csv", b"col\nval")
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
            row_number=1,
            validated_data=row_data,
            status=ImportRow.Status.VALID,
        )
        return job

    def test_skipped_duplicate_does_not_overwrite_po_header(self):
        original_data = {
            "po_number": "PO-DUP-001",
            "supplier_no": "SUPP-ORIG",
            "supplier_name": "Original Supplier",
            "order_date": None,
            "expected_receipt_date": None,
            "currency": "EUR",
            "line_no": "10000",
            "item_no": "ITM-001",
            "description": "Widget",
            "ordered_qty": "10",
            "po_external_id": "PO-DUP-001",
            "line_external_id": "PO-DUP-001-10000",
        }
        # First import: creates PO with original supplier.
        job1 = self._make_validated_job(original_data)
        run_import(job1)
        po = PurchaseOrder.objects.get(team=self.team, external_id="PO-DUP-001")
        self.assertEqual(po.supplier_name, "Original Supplier")

        # Second import: same line, different supplier name — should skip without touching header.
        modified_data = {**original_data, "supplier_name": "SHOULD NOT APPEAR"}
        job2 = self._make_validated_job(modified_data)
        run_import(job2, update_existing=False)

        row2 = job2.rows.first()
        self.assertEqual(row2.status, ImportRow.Status.SKIPPED)

        # PO header must be unchanged.
        po.refresh_from_db()
        self.assertEqual(po.supplier_name, "Original Supplier")
