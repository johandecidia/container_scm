"""Unit tests for the Business Central PO XML parser."""

import io
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase

from apps.scm.imports.bc_xml_parser import (
    _derive_line_no,
    _extract_currency,
    _extract_po_number,
    _parse_date,
    _parse_qty,
    parse_bc_po_xml,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "business_central_purchase_order_sample.xml"


def _open_fixture() -> io.BytesIO:
    return io.BytesIO(FIXTURE_PATH.read_bytes())


class ParseBcPoXmlFixtureTests(SimpleTestCase):
    """Tests against the anonymized BC fixture file."""

    def setUp(self):
        self.pos = parse_bc_po_xml(_open_fixture())

    def test_returns_one_purchase_order(self):
        self.assertEqual(len(self.pos), 1)

    def test_external_id(self):
        self.assertEqual(self.pos[0]["external_id"], "aaaaaaaa-0001-0001-0001-000000000001")

    def test_po_number(self):
        self.assertEqual(self.pos[0]["po_number"], "PO-2026-001")

    def test_supplier_no(self):
        self.assertEqual(self.pos[0]["supplier_no"], "SUPP-0001")

    def test_supplier_name(self):
        self.assertEqual(self.pos[0]["supplier_name"], "Anon Supplier Ltd")

    def test_order_date(self):
        from datetime import date

        self.assertEqual(self.pos[0]["order_date"], date(2026, 3, 15))

    def test_expected_receipt_date(self):
        from datetime import date

        self.assertEqual(self.pos[0]["expected_receipt_date"], date(2026, 6, 1))

    def test_currency_from_email_body(self):
        self.assertEqual(self.pos[0]["currency"], "USD")

    def test_status_defaults_to_open(self):
        self.assertEqual(self.pos[0]["status"], "open")

    def test_three_item_lines_returned(self):
        """Text-only line must be excluded; only real item lines returned."""
        self.assertEqual(len(self.pos[0]["lines"]), 3)

    def test_first_line_item_no(self):
        self.assertEqual(self.pos[0]["lines"][0]["item_no"], "ITM-4210")

    def test_first_line_description(self):
        self.assertEqual(self.pos[0]["lines"][0]["description"], "Container Floor Board 20ft")

    def test_first_line_qty(self):
        self.assertEqual(self.pos[0]["lines"][0]["ordered_qty"], Decimal("50"))

    def test_second_line_qty(self):
        self.assertEqual(self.pos[0]["lines"][1]["ordered_qty"], Decimal("100"))

    def test_third_line_qty_comma_decimal(self):
        """Column03 value '15,000' must be parsed as 15.000, not 15000."""
        self.assertEqual(self.pos[0]["lines"][2]["ordered_qty"], Decimal("15.000"))

    def test_line_external_ids_present(self):
        for line in self.pos[0]["lines"]:
            self.assertTrue(line["external_id"], "external_id must not be empty")

    def test_line_nos_derived(self):
        expected = ["20000", "30000", "40000"]
        actual = [line["line_no"] for line in self.pos[0]["lines"]]
        self.assertEqual(actual, expected)

    def test_shipped_and_received_default_to_zero(self):
        for line in self.pos[0]["lines"]:
            self.assertEqual(line["shipped_qty"], Decimal("0"))
            self.assertEqual(line["received_qty"], Decimal("0"))


class ParseBcPoXmlEdgeCaseTests(SimpleTestCase):
    """Tests for edge cases using inline XML strings."""

    def _parse(self, xml_str: str):
        return parse_bc_po_xml(io.BytesIO(xml_str.encode()))

    def test_empty_data_items_returns_empty_list(self):
        xml = """\
<?xml version="1.0"?>
<ReportDataSet name="PEB Purchase Order">
  <DataItems/>
</ReportDataSet>"""
        self.assertEqual(self._parse(xml), [])

    def test_missing_system_id_skips_header(self):
        xml = """\
<?xml version="1.0"?>
<ReportDataSet>
  <DataItems>
    <DataItem name="Purchase_Header" tableId="38">
      <DataItems>
        <DataItem name="CopyLoop" tableId="2000000026">
          <Columns>
            <Column name="EmailBodyTextLine4">Order No.: PO-NOID</Column>
          </Columns>
          <DataItems/>
        </DataItem>
      </DataItems>
    </DataItem>
  </DataItems>
</ReportDataSet>"""
        self.assertEqual(self._parse(xml), [])

    def test_missing_copy_loop_skips_header(self):
        xml = """\
<?xml version="1.0"?>
<ReportDataSet>
  <DataItems>
    <DataItem name="Purchase_Header" tableId="38" systemId="some-guid">
      <DataItems/>
    </DataItem>
  </DataItems>
</ReportDataSet>"""
        self.assertEqual(self._parse(xml), [])

    def test_all_text_lines_returns_empty_lines_list(self):
        xml = """\
<?xml version="1.0"?>
<ReportDataSet>
  <DataItems>
    <DataItem name="Purchase_Header" tableId="38" systemId="guid-001">
      <DataItems>
        <DataItem name="CopyLoop" tableId="2000000026">
          <Columns>
            <Column name="EmailBodyTextLine4">Order No.: PO-X</Column>
          </Columns>
          <DataItems>
            <DataItem name="TempPurchDocLine">
              <Columns>
                <Column name="Column02">Just a comment</Column>
                <Column name="IsTextLine">True</Column>
              </Columns>
            </DataItem>
          </DataItems>
        </DataItem>
      </DataItems>
    </DataItem>
  </DataItems>
</ReportDataSet>"""
        result = self._parse(xml)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["lines"], [])

    def test_currency_defaults_to_eur_when_not_found(self):
        xml = """\
<?xml version="1.0"?>
<ReportDataSet>
  <DataItems>
    <DataItem name="Purchase_Header" tableId="38" systemId="guid-002">
      <DataItems>
        <DataItem name="CopyLoop" tableId="2000000026">
          <Columns/>
          <DataItems/>
        </DataItem>
      </DataItems>
    </DataItem>
  </DataItems>
</ReportDataSet>"""
        result = self._parse(xml)
        self.assertEqual(result[0]["currency"], "EUR")

    def test_bom_prefix_is_stripped(self):
        """BOM-prefixed XML bytes (UTF-8-SIG) must be parsed without error."""
        xml_bytes = '\ufeff<?xml version="1.0"?><ReportDataSet><DataItems/></ReportDataSet>'.encode("utf-8-sig")
        self.assertEqual(parse_bc_po_xml(io.BytesIO(xml_bytes)), [])


class ParseQtyTests(SimpleTestCase):
    """Unit tests for the quantity parser helper."""

    def test_integer_string(self):
        self.assertEqual(_parse_qty("50"), Decimal("50"))

    def test_comma_decimal(self):
        self.assertEqual(_parse_qty("15,000"), Decimal("15.000"))

    def test_space_thousands_separator(self):
        self.assertEqual(_parse_qty("1 615,00"), Decimal("1615.00"))

    def test_nbsp_thousands_separator(self):
        self.assertEqual(_parse_qty("1\xa0800,00"), Decimal("1800.00"))

    def test_empty_string_returns_zero(self):
        self.assertEqual(_parse_qty(""), Decimal("0"))

    def test_invalid_string_returns_zero(self):
        self.assertEqual(_parse_qty("n/a"), Decimal("0"))


class ParseDateTests(SimpleTestCase):
    """Unit tests for the date parser helper."""

    def test_iso_format(self):
        from datetime import date

        self.assertEqual(_parse_date("2026-03-15"), date(2026, 3, 15))

    def test_dmy_dash_format(self):
        from datetime import date

        self.assertEqual(_parse_date("15-03-2026"), date(2026, 3, 15))

    def test_none_input(self):
        self.assertIsNone(_parse_date(None))

    def test_empty_string(self):
        self.assertIsNone(_parse_date(""))

    def test_unrecognised_format(self):
        self.assertIsNone(_parse_date("March 15, 2026"))


class ExtractPoNumberTests(SimpleTestCase):
    """Unit tests for the PO number extraction helper."""

    def test_standard_pattern(self):
        cols = {"EmailBodyTextLine4": "Order No.: PO-2026-001"}
        self.assertEqual(_extract_po_number(cols), "PO-2026-001")

    def test_numeric_po_number(self):
        cols = {"EmailBodyTextLine4": "Order No.: 100002"}
        self.assertEqual(_extract_po_number(cols), "100002")

    def test_missing_line4_returns_empty(self):
        self.assertEqual(_extract_po_number({}), "")

    def test_po_number_column_fallback(self):
        cols = {"PO_Number": "DIRECT-001"}
        self.assertEqual(_extract_po_number(cols), "DIRECT-001")


class ExtractCurrencyTests(SimpleTestCase):
    """Unit tests for the currency extraction helper."""

    def test_explicit_currency_code_column(self):
        cols = {"Currency_Code": "SEK"}
        self.assertEqual(_extract_currency(cols), "SEK")

    def test_email_body_pattern(self):
        cols = {"EmailBodyTextLine5": "Amount: 3 415,00 USD excl. VAT"}
        self.assertEqual(_extract_currency(cols), "USD")

    def test_fallback_to_eur(self):
        self.assertEqual(_extract_currency({}), "EUR")


class DeriveLineNoTests(SimpleTestCase):
    """Unit tests for the line number derivation helper."""

    def test_standard_bc_external_id(self):
        self.assertEqual(_derive_line_no("10000100000000000000020000"), "20000")

    def test_short_id_returned_as_is(self):
        self.assertEqual(_derive_line_no("123"), "123")

    def test_empty_string(self):
        self.assertEqual(_derive_line_no(""), "")
