"""Tests for Purchase Order XML import pipeline.

Covers:
- Upload form: XML accepted for purchase_orders, rejected for other types
- Parsing: BC PO XML is flattened to flat row dicts
- Full pipeline: XML → parse → validate → confirm → POs/lines created
- Idempotency: re-import same XML produces no duplicates
- Tenant isolation: Team A cannot see Team B's imported POs
- Error cases: invalid XML, missing required fields
"""

import contextlib
import io
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path
from typing import cast

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils.datastructures import MultiValueDict

from apps.scm.imports.forms import ImportUploadForm
from apps.scm.imports.models import ImportJob, ImportRow
from apps.scm.imports.parsers import _parse_xml, parse_file
from apps.scm.imports.services import confirm_import_job, parse_import_job, validate_import_job
from apps.scm.procurement.models import PurchaseOrder

from .helpers import make_team, make_user

FIXTURES_DIR = Path(__file__).parent / "fixtures"
BC_XML_FIXTURE = FIXTURES_DIR / "business_central_purchase_order_sample.xml"


def _load_xml_fixture(filename: str = "business_central_purchase_order_sample.xml") -> SimpleUploadedFile:
    path = FIXTURES_DIR / filename
    return SimpleUploadedFile(filename, path.read_bytes(), content_type="text/xml")


def _make_xml_job(team, user, filename: str = "business_central_purchase_order_sample.xml") -> ImportJob:
    f = _load_xml_fixture(filename)
    return ImportJob.objects.create(
        team=team,
        created_by=user,
        file=f,
        original_filename=filename,
        import_type=ImportJob.ImportType.PURCHASE_ORDERS,
        status=ImportJob.Status.UPLOADED,
    )


# ---------------------------------------------------------------------------
# Form validation
# ---------------------------------------------------------------------------


class XMLUploadFormTest(TestCase):
    def _form(self, filename: str, content: bytes, import_type: str) -> ImportUploadForm:
        f = SimpleUploadedFile(filename, content)
        return ImportUploadForm(
            data={"import_type": import_type},
            files=cast(MultiValueDict, {"file": f}),
        )

    def test_xml_accepted_for_purchase_orders(self):
        form = self._form("orders.xml", b"<root/>", ImportJob.ImportType.PURCHASE_ORDERS)
        # Cross-field validation doesn't fire for bad XML content — only extension check
        self.assertTrue(form.is_valid(), form.errors)

    def test_xml_rejected_for_containers(self):
        form = self._form("orders.xml", b"<root/>", ImportJob.ImportType.CONTAINERS)
        self.assertFalse(form.is_valid())
        self.assertTrue(any("XML" in str(e) for e in form.non_field_errors()))

    def test_xml_rejected_for_container_movements(self):
        form = self._form("orders.xml", b"<root/>", ImportJob.ImportType.CONTAINER_MOVEMENTS)
        self.assertFalse(form.is_valid())

    def test_csv_still_accepted_for_purchase_orders(self):
        form = self._form("data.csv", b"a,b\n1,2", ImportJob.ImportType.PURCHASE_ORDERS)
        self.assertTrue(form.is_valid(), form.errors)

    def test_unsupported_extension_rejected(self):
        form = self._form("data.txt", b"data", ImportJob.ImportType.PURCHASE_ORDERS)
        self.assertFalse(form.is_valid())
        self.assertIn("file", form.errors)


# ---------------------------------------------------------------------------
# XML parsing (flat row extraction)
# ---------------------------------------------------------------------------


class XMLParserFlatRowTest(TestCase):
    """Verify that _parse_xml flattens BC PO XML to the expected flat row format."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="xml-parse-team")
        cls.user = make_user("xml-parse@example.com")

    def _make_job(self) -> ImportJob:
        return _make_xml_job(self.team, self.user)

    def test_xml_parse_returns_three_rows(self):
        """Fixture has 1 PO with 3 item lines (1 text line is skipped)."""
        job = self._make_job()
        rows = parse_file(job)
        self.assertEqual(len(rows), 3)

    def test_rows_contain_po_number(self):
        job = self._make_job()
        rows = parse_file(job)
        for row in rows:
            self.assertEqual(row["po_number"], "PO-2026-001")

    def test_rows_contain_supplier_fields(self):
        job = self._make_job()
        rows = parse_file(job)
        self.assertEqual(rows[0]["supplier_no"], "SUPP-0001")
        self.assertEqual(rows[0]["supplier_name"], "Anon Supplier Ltd")

    def test_rows_contain_iso_dates(self):
        job = self._make_job()
        rows = parse_file(job)
        self.assertEqual(rows[0]["order_date"], "2026-03-15")
        self.assertEqual(rows[0]["expected_receipt_date"], "2026-06-01")

    def test_rows_contain_currency(self):
        job = self._make_job()
        rows = parse_file(job)
        self.assertEqual(rows[0]["currency"], "USD")

    def test_rows_contain_line_fields(self):
        job = self._make_job()
        rows = parse_file(job)
        self.assertEqual(rows[0]["item_no"], "ITM-4210")
        self.assertEqual(rows[0]["description"], "Container Floor Board 20ft")
        self.assertEqual(rows[0]["ordered_qty"], "50")

    def test_second_line_qty(self):
        job = self._make_job()
        rows = parse_file(job)
        self.assertEqual(rows[1]["ordered_qty"], "100")

    def test_third_line_comma_decimal_qty(self):
        """'15,000' in XML must be stored as '15.000' string (not 15000)."""
        job = self._make_job()
        rows = parse_file(job)
        self.assertEqual(rows[2]["ordered_qty"], "15.000")

    def test_line_numbers_derived_correctly(self):
        job = self._make_job()
        rows = parse_file(job)
        self.assertEqual([r["line_no"] for r in rows], ["20000", "30000", "40000"])

    def test_parse_file_helper_for_xml(self):
        """`parse_file` dispatches to XML parser based on .xml extension."""
        job = self._make_job()
        rows = parse_file(job)
        self.assertIsInstance(rows, list)
        self.assertGreater(len(rows), 0)

    def test_parse_xml_empty_root_returns_empty_list(self):
        result = _parse_xml(io.BytesIO(b"<ReportDataSet><DataItems/></ReportDataSet>"))
        self.assertEqual(result, [])

    def test_parse_xml_invalid_xml_raises(self):
        with self.assertRaises(ET.ParseError):
            _parse_xml(io.BytesIO(b"not xml at all <<<"))


# ---------------------------------------------------------------------------
# Full pipeline: upload → parse → validate → confirm
# ---------------------------------------------------------------------------


class XMLPOImportPipelineTest(TestCase):
    """End-to-end pipeline test using the BC XML fixture."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="xml-pipeline-team")
        cls.user = make_user("xml-pipeline@example.com")

    def _run_pipeline(self) -> ImportJob:
        job = _make_xml_job(self.team, self.user)
        parse_import_job(job)
        validate_import_job(job)
        confirm_import_job(job)
        return job

    def test_job_status_is_completed(self):
        job = self._run_pipeline()
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)

    def test_three_rows_created(self):
        job = self._run_pipeline()
        self.assertEqual(job.rows.count(), 3)

    def test_one_purchase_order_created(self):
        self._run_pipeline()
        self.assertEqual(PurchaseOrder.objects.filter(team=self.team).count(), 1)

    def test_purchase_order_has_correct_po_number(self):
        self._run_pipeline()
        self.assertTrue(PurchaseOrder.objects.filter(team=self.team, po_number="PO-2026-001").exists())

    def test_purchase_order_has_correct_supplier(self):
        self._run_pipeline()
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.supplier_no, "SUPP-0001")
        self.assertEqual(po.supplier_name, "Anon Supplier Ltd")

    def test_purchase_order_has_correct_currency(self):
        self._run_pipeline()
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.currency, "USD")

    def test_three_po_lines_created(self):
        self._run_pipeline()
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.lines.count(), 3)

    def test_first_line_correct_item_and_qty(self):
        self._run_pipeline()
        po = PurchaseOrder.objects.get(team=self.team)
        line = po.lines.get(line_no="20000")
        self.assertEqual(line.item_no, "ITM-4210")
        self.assertEqual(line.ordered_qty, Decimal("50"))

    def test_third_line_comma_decimal_qty_imported_correctly(self):
        self._run_pipeline()
        po = PurchaseOrder.objects.get(team=self.team)
        line = po.lines.get(line_no="40000")
        self.assertEqual(line.ordered_qty, Decimal("15.000"))

    def test_all_rows_marked_imported(self):
        job = self._run_pipeline()
        statuses = list(job.rows.values_list("status", flat=True))
        self.assertTrue(all(s == ImportRow.Status.IMPORTED for s in statuses))

    def test_processed_rows_count_correct(self):
        job = self._run_pipeline()
        job.refresh_from_db()
        self.assertEqual(job.processed_rows, 3)


# ---------------------------------------------------------------------------
# Idempotency: re-import same XML twice → no duplicates
# ---------------------------------------------------------------------------


class XMLPOImportIdempotencyTest(TestCase):
    """Re-importing the same XML file must not create duplicate POs or lines."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="xml-idem-team")
        cls.user = make_user("xml-idem@example.com")

    def _run(self) -> ImportJob:
        job = _make_xml_job(self.team, self.user)
        parse_import_job(job)
        validate_import_job(job)
        confirm_import_job(job)
        return job

    def test_reimport_does_not_duplicate_pos(self):
        self._run()
        self._run()
        self.assertEqual(PurchaseOrder.objects.filter(team=self.team).count(), 1)

    def test_reimport_does_not_duplicate_lines(self):
        self._run()
        self._run()
        po = PurchaseOrder.objects.get(team=self.team)
        self.assertEqual(po.lines.count(), 3)

    def test_second_import_rows_are_skipped(self):
        self._run()
        job2 = self._run()
        skipped = job2.rows.filter(status=ImportRow.Status.SKIPPED).count()
        self.assertEqual(skipped, 3)


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class XMLPOImportTenantIsolationTest(TestCase):
    """Team A's imported POs must not be visible to Team B."""

    @classmethod
    def setUpTestData(cls):
        cls.team_a = make_team(name="Team Alpha", slug="xml-iso-team-a")
        cls.team_b = make_team(name="Team Beta", slug="xml-iso-team-b")
        cls.user = make_user("xml-iso@example.com")

    def test_team_a_pos_not_visible_to_team_b(self):
        job = _make_xml_job(self.team_a, self.user)
        parse_import_job(job)
        validate_import_job(job)
        confirm_import_job(job)

        self.assertTrue(PurchaseOrder.objects.filter(team=self.team_a).exists())
        self.assertFalse(PurchaseOrder.objects.filter(team=self.team_b).exists())

    def test_team_b_pos_not_visible_to_team_a(self):
        job = _make_xml_job(self.team_b, self.user)
        parse_import_job(job)
        validate_import_job(job)
        confirm_import_job(job)

        self.assertTrue(PurchaseOrder.objects.filter(team=self.team_b).exists())
        self.assertFalse(PurchaseOrder.objects.filter(team=self.team_a).exists())


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class XMLPOImportErrorTest(TestCase):
    """Invalid XML and malformed content produce clear errors."""

    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="xml-err-team")
        cls.user = make_user("xml-err@example.com")

    def _make_job_with_content(self, xml_bytes: bytes, filename: str = "bad.xml") -> ImportJob:
        f = SimpleUploadedFile(filename, xml_bytes, content_type="text/xml")
        return ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename=filename,
            import_type=ImportJob.ImportType.PURCHASE_ORDERS,
            status=ImportJob.Status.UPLOADED,
        )

    def test_invalid_xml_parse_fails_with_failed_status(self):
        job = self._make_job_with_content(b"<<< not xml >>>")
        with self.assertRaises(ET.ParseError):
            parse_import_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.FAILED)

    def test_invalid_xml_sets_parse_error_in_metadata(self):
        job = self._make_job_with_content(b"<<< not xml >>>")
        with contextlib.suppress(ET.ParseError):
            parse_import_job(job)
        job.refresh_from_db()
        self.assertIn("parse_error", job.metadata)

    def test_xml_with_po_but_no_lines_creates_zero_rows(self):
        """A valid PO header with no item lines (all text lines) → 0 rows."""
        xml = b"""\
<?xml version="1.0"?>
<ReportDataSet name="PEB Purchase Order">
  <DataItems>
    <DataItem name="Purchase_Header" tableId="38" systemId="guid-001">
      <DataItems>
        <DataItem name="CopyLoop" tableId="2000000026">
          <Columns>
            <Column name="EmailBodyTextLine4">Order No.: PO-EMPTY</Column>
            <Column name="Vendor_No">SUPP-X</Column>
            <Column name="Vendor_Name">Test Supplier</Column>
            <Column name="Order_Date">2026-01-01</Column>
          </Columns>
          <DataItems/>
        </DataItem>
      </DataItems>
    </DataItem>
  </DataItems>
</ReportDataSet>"""
        job = self._make_job_with_content(xml, "empty_lines.xml")
        parse_import_job(job)
        job.refresh_from_db()
        self.assertEqual(job.total_rows, 0)

    def test_xml_line_missing_item_no_is_skipped_by_parser(self):
        """A PO line without Column01 (item_no) is silently skipped by the XML parser.

        The BC parser design skips any line that has no item number, so no row
        is created — rather than creating an INVALID row.
        """
        xml = b"""\
<?xml version="1.0"?>
<ReportDataSet name="PEB Purchase Order">
  <DataItems>
    <DataItem name="Purchase_Header" tableId="38" systemId="guid-002">
      <DataItems>
        <DataItem name="CopyLoop" tableId="2000000026">
          <Columns>
            <Column name="EmailBodyTextLine4">Order No.: PO-NOID</Column>
            <Column name="Vendor_No">SUPP-Y</Column>
            <Column name="Vendor_Name">Supplier Y</Column>
            <Column name="Order_Date">2026-01-01</Column>
          </Columns>
          <DataItems>
            <DataItem name="TempPurchDocLine">
              <Columns>
                <Column name="Line01">10000100000000000000020000</Column>
                <Column name="Column02">Some description without item no</Column>
                <Column name="Column03">10</Column>
                <Column name="IsTextLine">False</Column>
              </Columns>
            </DataItem>
          </DataItems>
        </DataItem>
      </DataItems>
    </DataItem>
  </DataItems>
</ReportDataSet>"""
        job = self._make_job_with_content(xml, "missing_item.xml")
        parse_import_job(job)
        job.refresh_from_db()
        # Parser skips lines without item_no → 0 rows created
        self.assertEqual(job.total_rows, 0)


# ---------------------------------------------------------------------------
# Upload view: ?import_type=purchase_orders pre-selects correctly (XML context)
# ---------------------------------------------------------------------------


class XMLUploadViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="xml-view-team")
        cls.user = make_user("xml-view@example.com")
        cls.team.members.add(cls.user)

    def _client(self) -> Client:
        c = Client()
        c.force_login(self.user)
        session = c.session
        session["team_id"] = self.team.pk
        session.save()
        return c

    def test_xml_upload_creates_job_and_redirects(self):
        c = self._client()
        f = SimpleUploadedFile(
            "business_central_purchase_order_sample.xml",
            BC_XML_FIXTURE.read_bytes(),
            content_type="text/xml",
        )
        resp = c.post(
            reverse("imports:upload"),
            {"file": f, "import_type": ImportJob.ImportType.PURCHASE_ORDERS},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            ImportJob.objects.filter(team=self.team, import_type=ImportJob.ImportType.PURCHASE_ORDERS).exists()
        )
