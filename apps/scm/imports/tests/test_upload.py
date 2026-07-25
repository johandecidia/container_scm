"""Tests for import file upload (forms and services)."""

from typing import cast

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils.datastructures import MultiValueDict

from apps.scm.imports.forms import ImportUploadForm
from apps.scm.imports.models import ImportJob
from apps.scm.imports.services import create_import_job

from .helpers import make_csv_file, make_team, make_user


class BCPOXLSXRemovedTest(TestCase):
    """Regression: BC_PO_XLSX import type must no longer exist in the codebase."""

    def test_bc_po_xlsx_not_in_import_type_values(self):
        self.assertNotIn("bc_po_xlsx", ImportJob.ImportType.values)

    def test_upload_form_choices_do_not_include_bc_po_xlsx(self):
        f = SimpleUploadedFile("x.csv", b"a,b")
        form = ImportUploadForm(
            data={"import_type": ImportJob.ImportType.CONTAINERS},
            files=cast(MultiValueDict, {"file": f}),
        )
        choice_values = [v for v, _ in form.fields["import_type"].choices]
        self.assertNotIn("bc_po_xlsx", choice_values)

    def test_form_rejects_bc_po_xlsx_import_type(self):
        f = SimpleUploadedFile("x.csv", b"a,b")
        form = ImportUploadForm(
            data={"import_type": "bc_po_xlsx"},
            files=cast(MultiValueDict, {"file": f}),
        )
        self.assertFalse(form.is_valid())
        self.assertIn("import_type", form.errors)

    def test_xlsx_accepted_for_purchase_orders(self):
        """Standard .xlsx still works under the purchase_orders import type."""
        f = SimpleUploadedFile("orders.xlsx", b"PK\x03\x04fake-xlsx-bytes")
        form = ImportUploadForm(
            data={"import_type": ImportJob.ImportType.PURCHASE_ORDERS},
            files=cast(MultiValueDict, {"file": f}),
        )
        # The file is valid at form level (content validation happens at parse time).
        self.assertNotIn("import_type", form.errors)


class ImportUploadFormTest(TestCase):
    def _form(self, filename: str, content: bytes = b"a,b\n1,2", import_type: str = ImportJob.ImportType.CONTAINERS):
        f = SimpleUploadedFile(filename, content)
        return ImportUploadForm(
            data={"import_type": import_type},
            files=cast(MultiValueDict, {"file": f}),
        )

    def test_valid_csv_accepted(self):
        form = self._form("data.csv")
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_xlsx_accepted(self):
        import io

        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Container No"])
        ws.append(["MSCU1234560"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        f = SimpleUploadedFile(
            "data.xlsx", buf.read(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        form = ImportUploadForm(data={"import_type": ImportJob.ImportType.CONTAINERS}, files={"file": f})
        self.assertTrue(form.is_valid(), form.errors)

    def test_unknown_extension_rejected(self):
        form = self._form("data.txt")
        self.assertFalse(form.is_valid())
        self.assertIn("file", form.errors)

    def test_pdf_accepted_for_purchase_orders(self):
        form = self._form("data.pdf", b"%PDF-1.4", import_type=ImportJob.ImportType.PURCHASE_ORDERS)
        self.assertTrue(form.is_valid(), form.errors)

    def test_pdf_rejected_for_containers(self):
        # PDF is only valid for purchase_orders; containers should trigger a non-field error.
        form = self._form("data.pdf", b"%PDF-1.4", import_type=ImportJob.ImportType.CONTAINERS)
        self.assertFalse(form.is_valid())
        self.assertTrue(form.non_field_errors())

    def test_oversized_file_rejected(self):
        big = b"a,b\n" + b"1,2\n" * (11 * 1024 * 1024 // 4)
        form = self._form("big.csv", big)
        self.assertFalse(form.is_valid())
        self.assertIn("file", form.errors)


class CreateImportJobServiceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="upload-team")
        cls.user = make_user("upload@example.com")
        cls.team.members.add(cls.user)

    def test_creates_import_job(self):
        f = make_csv_file([{"Container No": "CSQU3054187"}])
        job = create_import_job(self.team, self.user, f, ImportJob.ImportType.CONTAINERS)
        self.assertIsNotNone(job.pk)
        self.assertEqual(job.team, self.team)
        self.assertEqual(job.created_by, self.user)
        self.assertEqual(job.import_type, ImportJob.ImportType.CONTAINERS)

    def test_status_is_uploaded(self):
        f = make_csv_file([{"A": "1"}])
        job = create_import_job(self.team, self.user, f, ImportJob.ImportType.CONTAINERS)
        self.assertEqual(job.status, ImportJob.Status.UPLOADED)

    def test_original_filename_saved(self):
        f = make_csv_file([], filename="my_data.csv")
        job = create_import_job(self.team, self.user, f, ImportJob.ImportType.CONTAINERS)
        self.assertEqual(job.original_filename, "my_data.csv")


class ImportUploadViewPreselectionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="presel-team")
        cls.user = make_user("presel@example.com")
        cls.team.members.add(cls.user)

    def _client(self):
        c = Client()
        c.force_login(self.user)
        session = c.session
        session["team_id"] = self.team.pk
        session.save()
        return c

    def _get_initial_import_type(self, response) -> str:
        return response.context["form"].initial.get("import_type", "")

    def test_purchase_orders_param_preselects_purchase_orders(self):
        c = self._client()
        resp = c.get(reverse("imports:upload") + "?import_type=purchase_orders")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._get_initial_import_type(resp), ImportJob.ImportType.PURCHASE_ORDERS)

    def test_containers_param_preselects_containers(self):
        c = self._client()
        resp = c.get(reverse("imports:upload") + "?import_type=containers")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._get_initial_import_type(resp), ImportJob.ImportType.CONTAINERS)

    def test_no_param_uses_empty_initial(self):
        c = self._client()
        resp = c.get(reverse("imports:upload"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._get_initial_import_type(resp), "")

    def test_invalid_param_falls_back_safely(self):
        c = self._client()
        resp = c.get(reverse("imports:upload") + "?import_type=invalid_type")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._get_initial_import_type(resp), "")

    def test_purchase_orders_page_import_link_includes_param(self):
        c = self._client()
        resp = c.get(reverse("procurement:purchase_order_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "import_type=purchase_orders")


class UploadFormErrorRenderingTest(TestCase):
    """Form-wide validation errors must be visible on the upload page.

    The template previously rendered only per-field errors, so a PDF uploaded
    under the wrong import type was rejected with no message at all.
    """

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="upload-errors-team")
        cls.user = make_user("upload-errors@example.com")
        cls.team.members.add(cls.user)

    def _client(self) -> Client:
        c = Client()
        c.force_login(self.user)
        session = c.session
        session["team_id"] = self.team.pk
        session.save()
        return c

    def test_pdf_with_wrong_import_type_shows_error_message(self):
        f = SimpleUploadedFile("orders.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        resp = self._client().post(
            reverse("imports:upload"),
            {"file": f, "import_type": ImportJob.ImportType.CONTAINERS},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "PDF files are only supported for Purchase Orders imports.")
        self.assertFalse(ImportJob.objects.filter(team=self.team).exists())

    def test_xml_with_wrong_import_type_shows_error_message(self):
        f = SimpleUploadedFile("orders.xml", b"<?xml version='1.0'?><a/>", content_type="text/xml")
        resp = self._client().post(
            reverse("imports:upload"),
            {"file": f, "import_type": ImportJob.ImportType.CONTAINERS},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "XML files are only supported for Purchase Orders imports.")
