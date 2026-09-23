"""Reading a pasted gate-in report: what is a row, what is noise, what is an error."""

from datetime import datetime

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.scm.containers.receive_parser import parse_receive_text

MARKDOWN = """
John Evans — Gate In report

| Prefix | Unit Number | Iso Size | Iso Type | Gate Date Time In | Site |
|---|---|---|---|---|---|
| PSLU | 2913030 | 22 | 10 | 2026-09-17 16:00:51 | MCR AB - Oceanterminalen |
| PSLU | 2913792 | 22 | G1 | 2026-09-14 10:11:23 | MCR AB - Oceanterminalen |

Support | Contact us | Privacy
"""

TSV = (
    "PSLU\t2913030\t22\t10\t2026-09-17 16:00:51\tMCR AB - Oceanterminalen\n"
    "PSLU\t2913792\t22\tG1\t2026-09-14 10:11:23\tMCR AB - Oceanterminalen\n"
)


@override_settings(TIME_ZONE="Europe/Stockholm")
class ReceiveParserTest(SimpleTestCase):
    def test_markdown_table_rows_are_read(self):
        result = parse_receive_text(MARKDOWN)

        self.assertEqual([row.container_number for row in result.rows], ["PSLU2913030", "PSLU2913792"])
        self.assertEqual(result.errors, [])
        first = result.rows[0]
        self.assertEqual((first.iso_size, first.iso_type, first.iso_code), ("22", "10", "2210"))
        self.assertEqual(first.source_site, "MCR AB - Oceanterminalen")

    def test_tab_separated_rows_are_read(self):
        result = parse_receive_text(TSV)

        self.assertEqual([row.container_number for row in result.rows], ["PSLU2913030", "PSLU2913792"])
        self.assertEqual(result.rows[1].iso_code, "22G1")

    def test_runs_of_spaces_are_treated_as_column_gaps(self):
        text = "PSLU    2913030    22    10    2026-09-17 16:00:51    MCR AB - Oceanterminalen"

        (row,) = parse_receive_text(text).rows

        self.assertEqual(row.container_number, "PSLU2913030")
        self.assertEqual(row.source_site, "MCR AB - Oceanterminalen")

    def test_a_tsv_header_is_skipped_and_its_column_order_used(self):
        text = "Unit Number\tPrefix\tGate Date Time In\tSite\n2913030\tPSLU\t2026-09-17 16:00:51\tOcean\n"

        (row,) = parse_receive_text(text).rows

        self.assertEqual(row.container_number, "PSLU2913030")
        self.assertEqual(row.source_site, "Ocean")
        self.assertEqual(row.iso_code, "")

    def test_surrounding_whitespace_and_blank_lines_are_ignored(self):
        text = "\n\n   | PSLU |  2913030  | 22 | 10 |   2026-09-17 16:00:51 | Site |   \n\n"

        (row,) = parse_receive_text(text).rows

        self.assertEqual(row.container_number, "PSLU2913030")

    def test_footer_and_page_noise_are_neither_rows_nor_errors(self):
        text = "Menu | Home | Support\nPowered by John Evans\nPage 1 of 1\n"

        result = parse_receive_text(text)

        self.assertEqual((result.rows, result.errors), ([], []))

    def test_event_time_is_exact_and_in_the_active_timezone(self):
        (row,) = parse_receive_text(TSV.splitlines()[0]).rows

        expected = timezone.make_aware(datetime(2026, 9, 17, 16, 0, 51))
        self.assertEqual(row.occurred_at, expected)
        self.assertEqual(str(row.occurred_at.tzinfo), "Europe/Stockholm")

    def test_prefix_and_unit_number_are_normalised(self):
        (row,) = parse_receive_text("| pslu | 291 3030 | 22 | 10 | 2026-09-17 16:00:51 | x |").rows

        self.assertEqual(row.container_number, "PSLU2913030")
        self.assertEqual(row.parts["serial_number"], "291303")
        self.assertEqual(row.parts["check_digit"], 0)

    def test_a_wrong_check_digit_is_reported_not_dropped(self):
        result = parse_receive_text("| PSLU | 2913031 | 22 | 10 | 2026-09-17 16:00:51 | x |")

        self.assertEqual(result.rows, [])
        (error,) = result.errors
        self.assertEqual(error.container_number, "PSLU2913031")
        self.assertIn("check digit", error.error.lower())

    def test_a_malformed_unit_number_is_reported(self):
        result = parse_receive_text("| PSLU | 29130 | 22 | 10 | 2026-09-17 16:00:51 | x |")

        self.assertEqual(len(result.errors), 1)

    def test_an_invalid_datetime_is_reported(self):
        for value in ("yesterday", "2026-13-45 10:00:00", ""):
            with self.subTest(value=value):
                result = parse_receive_text(f"| PSLU | 2913030 | 22 | 10 | {value} | x |")
                self.assertEqual(result.rows, [])
                self.assertEqual(len(result.errors), 1)

    def test_good_rows_survive_a_bad_one(self):
        text = TSV + "PSLU\t2913031\t22\t10\t2026-09-17 16:00:51\tx\n"

        result = parse_receive_text(text)

        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.errors[0].line_number, 3)
