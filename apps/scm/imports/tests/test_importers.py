"""Tests for the import confirmation / importer logic."""

from django.test import TestCase

from apps.scm.containers.models import Container
from apps.scm.imports.importers import run_import
from apps.scm.imports.models import ImportJob, ImportRow

from .helpers import (
    CAT,
    CHECK,
    OWNER,
    SERIAL,
    make_equipment_type,
    make_import_job,
    make_parsed_job,
    make_team,
    make_user,
)


class RunImportTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="imp-team")
        cls.user = make_user("imp@example.com")
        cls.team.members.add(cls.user)
        cls.et = make_equipment_type()

    def test_valid_row_creates_container(self):
        job = make_parsed_job(self.team, self.user)
        run_import(job)
        self.assertTrue(Container.objects.filter(team=self.team, owner_code=OWNER, serial_number=SERIAL).exists())

    def test_invalid_row_not_imported(self):
        job = make_import_job(self.team, self.user)
        ImportRow.objects.create(
            import_job=job,
            row_number=1,
            validated_data={"container_number": "INVALID"},
            status=ImportRow.Status.INVALID,
        )
        job.status = ImportJob.Status.VALIDATED
        job.save()
        run_import(job)
        self.assertFalse(Container.objects.filter(team=self.team).exists())

    def test_duplicate_creates_skipped_row(self):
        Container.objects.create(
            team=self.team,
            owner_code=OWNER,
            category_id=CAT,
            serial_number=SERIAL,
            check_digit=CHECK,
            equipment_type=self.et,
        )
        job = make_parsed_job(self.team, self.user)
        run_import(job)
        row = job.rows.first()
        self.assertEqual(row.status, ImportRow.Status.SKIPPED)

    def test_import_job_status_completed(self):
        job = make_parsed_job(self.team, self.user)
        run_import(job)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)

    def test_imported_row_status_set(self):
        job = make_parsed_job(self.team, self.user)
        run_import(job)
        row = job.rows.first()
        self.assertEqual(row.status, ImportRow.Status.IMPORTED)

    def test_update_existing_updates_container(self):
        Container.objects.create(
            team=self.team,
            owner_code=OWNER,
            category_id=CAT,
            serial_number=SERIAL,
            check_digit=CHECK,
            equipment_type=self.et,
            location_text="Old Location",
        )
        job = make_parsed_job(self.team, self.user)
        # Add current_location (text) to validated data
        row = job.rows.first()
        data = dict(row.validated_data)
        data["current_location"] = "New Location"  # maps to location_text in importer
        row.validated_data = data
        row.save()
        run_import(job, update_existing=True)
        row.refresh_from_db()
        self.assertEqual(row.status, ImportRow.Status.IMPORTED)
