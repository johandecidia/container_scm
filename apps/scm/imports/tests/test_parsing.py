"""Tests for CSV and XLSX file parsing."""

from django.test import TestCase

from apps.scm.imports.models import ImportJob
from apps.scm.imports.parsers import create_import_rows, parse_file
from apps.scm.imports.services import parse_import_job

from .helpers import CONTAINER_ID, make_csv_file, make_team, make_user, make_xlsx_file


class CsvParsingTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="csv-team")
        cls.user = make_user("csv@example.com")

    def _job_from_csv(self, rows, filename="test.csv"):
        f = make_csv_file(rows, filename)
        return ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename=filename,
            import_type=ImportJob.ImportType.CONTAINERS,
        )

    def test_csv_rows_returned(self):
        job = self._job_from_csv([{"Container No": CONTAINER_ID, "Equipment Type": "22G1"}])
        rows = parse_file(job)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Container No"], CONTAINER_ID)

    def test_empty_rows_ignored(self):
        job = self._job_from_csv([{"Container No": CONTAINER_ID}, {"Container No": ""}])
        rows = parse_file(job)
        self.assertEqual(len(rows), 1)

    def test_create_import_rows_sets_total(self):
        job = self._job_from_csv([{"Container No": CONTAINER_ID}])
        raw_rows = parse_file(job)
        create_import_rows(job, raw_rows)
        job.refresh_from_db()
        self.assertEqual(job.total_rows, 1)

    def test_import_rows_created(self):
        job = self._job_from_csv([{"Container No": CONTAINER_ID}, {"Container No": "MSCU1234560"}])
        raw_rows = parse_file(job)
        create_import_rows(job, raw_rows)
        self.assertEqual(job.rows.count(), 2)

    def test_parse_service_updates_status(self):
        job = self._job_from_csv([{"Container No": CONTAINER_ID, "Equipment Type": "22G1"}])
        parse_import_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.PARSED)


class XlsxParsingTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="xlsx-team")
        cls.user = make_user("xlsx@example.com")

    def _job_from_xlsx(self, rows, filename="test.xlsx"):
        f = make_xlsx_file(rows, filename)
        return ImportJob.objects.create(
            team=self.team,
            created_by=self.user,
            file=f,
            original_filename=filename,
            import_type=ImportJob.ImportType.CONTAINERS,
        )

    def test_xlsx_rows_returned(self):
        job = self._job_from_xlsx([{"Container No": CONTAINER_ID}])
        rows = parse_file(job)
        self.assertEqual(len(rows), 1)

    def test_xlsx_empty_rows_ignored(self):
        job = self._job_from_xlsx([{"Container No": CONTAINER_ID}, {"Container No": ""}])
        rows = parse_file(job)
        self.assertEqual(len(rows), 1)

    def test_xlsx_total_rows_updated(self):
        job = self._job_from_xlsx([{"Container No": CONTAINER_ID}])
        raw = parse_file(job)
        create_import_rows(job, raw)
        job.refresh_from_db()
        self.assertEqual(job.total_rows, 1)
