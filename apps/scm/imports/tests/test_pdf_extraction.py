"""Tests for PDF purchase order extraction.

Covers:
- Upload form: PDF accepted for purchase_orders, rejected for other types.
- Parsing: PDF extractor is called for .pdf purchase_order jobs.
- Error handling: timeout / HTTP errors mark job as FAILED and set extract_error.
- Row conversion: API response dict is flattened into correct ImportRows.
- Pipeline: existing validate + confirm flow works unaffected after PDF extraction.
- Bug fix: duplicate-skip does NOT overwrite PO header when update_existing=False.
"""

from decimal import Decimal
from typing import Any, cast
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils.datastructures import MultiValueDict

from apps.scm.imports.extractors.clients.pdf_fastapi import PDFExtractionError
from apps.scm.imports.forms import ImportUploadForm
from apps.scm.imports.importers import run_import
from apps.scm.imports.models import ImportError, ImportJob, ImportRow
from apps.scm.imports.services import confirm_import_job, parse_import_job, validate_import_job
from apps.scm.procurement.models import PurchaseOrder
from apps.teams.models import Team
from apps.users.models import CustomUser

from .helpers import make_team, make_user

# Canonical API response dict matching the real FastAPI extraction service shape.
CANONICAL_API_RESPONSE: dict[str, Any] = {
    "confidence": 0.95,
    "status": "completed",
    "requires_review": False,
    "warnings": [],
    "data": {
        "purchase_order_number": "PO-PDF-001",
        "vendor_number": "SUPP-PDF",
        "vendor": {"company": "PDF Supplier AB"},
        "order_date": "2026-03-01",
        "currency": "USD",
        "line_items": [
            {
                "item_no": "ITM-PDF-001",
                "description": "PDF Widget",
                "quantity": "75",
            }
        ],
    },
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
        mock_post.return_value = MagicMock(status_code=200, json=lambda: CANONICAL_API_RESPONSE)

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)

        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        self.assertIn("/v1/purchase-orders/extract", call_url)

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_source_format_set_in_metadata(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: CANONICAL_API_RESPONSE)

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
    """API response dict is flattened into correctly structured ImportRows."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="pdf-rows-team")
        cls.user = make_user("pdf-rows@example.com")

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_api_response_becomes_import_rows(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: CANONICAL_API_RESPONSE)

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)

        self.assertEqual(job.rows.count(), 1)
        row = job.rows.first()
        self.assertEqual(row.raw_data.get("po_number"), "PO-PDF-001")
        self.assertEqual(row.raw_data.get("supplier_no"), "SUPP-PDF")
        self.assertEqual(row.raw_data.get("supplier_name"), "PDF Supplier AB")
        self.assertEqual(row.raw_data.get("line_no"), "10000")
        self.assertEqual(row.raw_data.get("ordered_qty"), "75")

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_multiple_line_items_create_multiple_import_rows(self, mock_post):
        two_line_response = {
            **CANONICAL_API_RESPONSE,
            "data": {
                **CANONICAL_API_RESPONSE["data"],
                "line_items": [
                    {"item_no": "ITM-PDF-001", "description": "Widget A", "quantity": "10"},
                    {"item_no": "ITM-PDF-002", "description": "Widget B", "quantity": "20"},
                ],
            },
        }
        mock_post.return_value = MagicMock(status_code=200, json=lambda: two_line_response)

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)

        self.assertEqual(job.rows.count(), 2)
        line_nos = list(job.rows.values_list("raw_data__line_no", flat=True).order_by("row_number"))
        self.assertEqual(line_nos, ["10000", "20000"])

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_pydantic_validation_runs_on_pdf_rows(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: CANONICAL_API_RESPONSE)

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)

        row = job.rows.first()
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
        mock_post.return_value = MagicMock(status_code=200, json=lambda: CANONICAL_API_RESPONSE)

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

    team: Team
    user: CustomUser

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


# Real service response for a Swedish-language Business Central purchase order:
# HTTP 200, but the layout was not recognised — no PO number and no line items.
UNRECOGNISED_LAYOUT_RESPONSE = {
    "status": "needs_review",
    "confidence": 0.7,
    "requires_review": True,
    "warnings": ["Purchase order number not found", "No line items found"],
    "data": {
        "document_type": "purchase_order",
        "purchase_order_number": "UNKNOWN",
        "vendor_number": None,
        "order_date": None,
        "currency": "SEK",
        "vendor": None,
        "ship_to": None,
        "purchaser": None,
        "line_items": [],
        "amount_excl_vat": "41833.60",
    },
}

# The service could not read the document at all.
NULL_DATA_RESPONSE = {
    "status": "failed",
    "confidence": 0.0,
    "requires_review": True,
    "warnings": ["Could not extract text from document"],
    "data": None,
}

# Lines were found, but the service is not fully confident about them.
NEEDS_REVIEW_WITH_ROWS_RESPONSE = {
    "status": "needs_review",
    "confidence": 0.62,
    "requires_review": True,
    "warnings": ["Vendor number not found", "Order date could not be parsed"],
    "data": {
        **CANONICAL_API_RESPONSE["data"],
        "vendor_number": "",
    },
}


class PDFEmptyExtractionTest(TestCase):
    """An extraction yielding no rows fails the job instead of completing it empty.

    The service answers HTTP 200 for an unsupported layout, so the old pipeline
    silently produced a COMPLETED job with 0 rows and told the user the import
    had succeeded.
    """

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="pdf-empty-team")
        cls.user = make_user("pdf-empty@example.com")

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_unrecognised_layout_marks_job_failed(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: UNRECOGNISED_LAYOUT_RESPONSE)

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"), self.assertRaises(PDFExtractionError):
            parse_import_job(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.FAILED)
        self.assertEqual(job.total_rows, 0)

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_unrecognised_layout_creates_no_rows(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: UNRECOGNISED_LAYOUT_RESPONSE)

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"), self.assertRaises(PDFExtractionError):
            parse_import_job(job)

        self.assertEqual(job.rows.count(), 0)

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_error_message_includes_service_warnings(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: UNRECOGNISED_LAYOUT_RESPONSE)

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"), self.assertRaises(PDFExtractionError):
            parse_import_job(job)

        job.refresh_from_db()
        extract_error = job.metadata["extract_error"]
        self.assertIn("no purchase order lines", extract_error)
        self.assertIn("needs_review", extract_error)
        self.assertIn("No line items found", extract_error)

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_null_data_marks_job_failed(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: NULL_DATA_RESPONSE)

        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"), self.assertRaises(PDFExtractionError):
            parse_import_job(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.FAILED)
        self.assertIn("no purchase order data", job.metadata["extract_error"])


class PDFExtractionMetadataTest(TestCase):
    """Extraction-quality fields are persisted and surfaced as warnings."""

    team: Team
    user: CustomUser

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="pdf-meta-team")
        cls.user = make_user("pdf-meta@example.com")

    def _parse(self, mock_post, response) -> ImportJob:
        mock_post.return_value = MagicMock(status_code=200, json=lambda: response)
        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)
        job.refresh_from_db()
        return job

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_quality_fields_stored_in_metadata(self, mock_post):
        job = self._parse(mock_post, NEEDS_REVIEW_WITH_ROWS_RESPONSE)

        self.assertEqual(job.metadata["extraction_status"], "needs_review")
        self.assertEqual(job.metadata["extraction_confidence"], 0.62)
        self.assertTrue(job.metadata["extraction_requires_review"])
        self.assertEqual(
            job.metadata["extraction_warnings"],
            ["Vendor number not found", "Order date could not be parsed"],
        )

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_clean_extraction_records_confident_metadata(self, mock_post):
        job = self._parse(mock_post, CANONICAL_API_RESPONSE)

        self.assertEqual(job.metadata["extraction_status"], "completed")
        self.assertEqual(job.metadata["extraction_confidence"], 0.95)
        self.assertFalse(job.metadata["extraction_requires_review"])
        self.assertEqual(job.metadata["extraction_warnings"], [])

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_service_warnings_recorded_as_job_level_warnings(self, mock_post):
        job = self._parse(mock_post, NEEDS_REVIEW_WITH_ROWS_RESPONSE)

        warnings = ImportError.objects.filter(import_job=job, code="extraction_warning")
        self.assertEqual(warnings.count(), 2)
        self.assertTrue(all(w.severity == ImportError.Severity.WARNING for w in warnings))
        self.assertTrue(all(w.import_row_id is None for w in warnings))

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_requires_review_recorded_as_job_level_warning(self, mock_post):
        job = self._parse(mock_post, NEEDS_REVIEW_WITH_ROWS_RESPONSE)

        review = ImportError.objects.filter(import_job=job, code="extraction_requires_review")
        self.assertEqual(review.count(), 1)
        self.assertEqual(review.first().severity, ImportError.Severity.WARNING)
        self.assertIn("0.62", review.first().message)

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_confident_extraction_records_no_warnings(self, mock_post):
        job = self._parse(mock_post, CANONICAL_API_RESPONSE)

        self.assertEqual(ImportError.objects.filter(import_job=job).count(), 0)

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_extraction_warnings_survive_validation(self, mock_post):
        """Row-level validation must not wipe the job-level extraction warnings."""
        job = self._parse(mock_post, NEEDS_REVIEW_WITH_ROWS_RESPONSE)
        validate_import_job(job)

        self.assertEqual(
            ImportError.objects.filter(import_job=job, import_row__isnull=True).count(),
            3,  # 2 service warnings + 1 requires-review notice
        )

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_requires_review_does_not_block_the_import(self, mock_post):
        """A flagged-for-review extraction still yields valid, importable rows."""
        job = self._parse(mock_post, NEEDS_REVIEW_WITH_ROWS_RESPONSE)

        self.assertEqual(job.status, ImportJob.Status.PARSED)
        self.assertEqual(job.rows.filter(status=ImportRow.Status.VALID).count(), 1)


class PDFReparseClearsStaleErrorsTest(TestCase):
    """A retried parse must not show the previous attempt's failure."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="pdf-retry-team")
        cls.user = make_user("pdf-retry@example.com")

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_successful_reparse_clears_previous_extract_error(self, mock_post):
        job = _make_pdf_job(self.team, self.user)

        mock_post.return_value = MagicMock(status_code=200, json=lambda: UNRECOGNISED_LAYOUT_RESPONSE)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"), self.assertRaises(PDFExtractionError):
            parse_import_job(job)
        job.refresh_from_db()
        self.assertIn("extract_error", job.metadata)

        mock_post.return_value = MagicMock(status_code=200, json=lambda: CANONICAL_API_RESPONSE)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)

        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.PARSED)
        self.assertNotIn("extract_error", job.metadata)
        self.assertNotIn("parse_error", job.metadata)


class ImportDetailAlertRenderingTest(TestCase):
    """The detail page must show why an import produced nothing."""

    team: Team
    user: CustomUser

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="pdf-alerts-team")
        cls.user = make_user("pdf-alerts@example.com")
        cls.team.members.add(cls.user)

    def _client(self) -> Client:
        c = Client()
        c.force_login(self.user)
        session = c.session
        session["team_id"] = self.team.pk
        session.save()
        return c

    def _detail(self, job: ImportJob):
        return self._client().get(reverse("imports:detail", kwargs={"pk": job.pk}))

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_failed_extraction_shows_reason(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: UNRECOGNISED_LAYOUT_RESPONSE)
        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"), self.assertRaises(PDFExtractionError):
            parse_import_job(job)

        resp = self._detail(job)
        self.assertContains(resp, "Document extraction failed")
        self.assertContains(resp, "No line items found")

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_needs_review_extraction_shows_warning(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: NEEDS_REVIEW_WITH_ROWS_RESPONSE)
        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)

        resp = self._detail(job)
        self.assertContains(resp, "Extraction needs review")
        self.assertContains(resp, "Vendor number not found")

    def test_parsed_job_with_zero_rows_shows_warning(self):
        """Defensive: a legacy job that reached a post-parse state with no rows."""
        job = _make_pdf_job(self.team, self.user)
        job.status = ImportJob.Status.COMPLETED
        job.total_rows = 0
        job.save(update_fields=["status", "total_rows"])

        resp = self._detail(job)
        self.assertContains(resp, "No rows were read from this file")

    @patch("apps.scm.imports.extractors.clients.pdf_fastapi.requests.post")
    def test_confident_extraction_shows_no_alerts(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: CANONICAL_API_RESPONSE)
        job = _make_pdf_job(self.team, self.user)
        with self.settings(SCM_PDF_FASTAPI_BASE_URL="http://localhost:9000"):
            parse_import_job(job)

        resp = self._detail(job)
        self.assertNotContains(resp, "Document extraction failed")
        self.assertNotContains(resp, "Extraction needs review")
        self.assertNotContains(resp, "No rows were read from this file")
