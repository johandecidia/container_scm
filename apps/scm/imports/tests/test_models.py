"""Tests for import models."""

from django.test import TestCase

from apps.scm.imports.models import ImportError as ImportJobError
from apps.scm.imports.models import ImportJob, ImportRow, ImportTemplate
from apps.teams.models import BaseTeamModel

from .helpers import make_import_job, make_team, make_user


class ImportJobModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team()
        cls.user = make_user()
        cls.team.members.add(cls.user)

    def test_extends_base_team_model(self):
        self.assertTrue(issubclass(ImportJob, BaseTeamModel))

    def test_create_import_job(self):
        job = make_import_job(self.team, self.user)
        self.assertEqual(job.team, self.team)
        self.assertEqual(job.created_by, self.user)
        self.assertEqual(job.status, ImportJob.Status.UPLOADED)
        self.assertEqual(job.import_type, ImportJob.ImportType.CONTAINERS)

    def test_str_contains_pk_and_type(self):
        job = make_import_job(self.team, self.user)
        s = str(job)
        self.assertIn(str(job.pk), s)
        self.assertIn("containers", s)

    def test_default_counters_are_zero(self):
        job = make_import_job(self.team, self.user)
        self.assertEqual(job.total_rows, 0)
        self.assertEqual(job.valid_rows, 0)
        self.assertEqual(job.invalid_rows, 0)

    def test_has_timestamps(self):
        field_names = [f.name for f in ImportJob._meta.fields]
        self.assertIn("created_at", field_names)
        self.assertIn("updated_at", field_names)


class ImportRowModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="row-team")
        cls.user = make_user("row@example.com")
        cls.team.members.add(cls.user)
        cls.job = make_import_job(cls.team, cls.user)

    def test_create_import_row(self):
        row = ImportRow.objects.create(
            import_job=self.job,
            row_number=1,
            raw_data={"A": "1"},
        )
        self.assertEqual(row.import_job, self.job)
        self.assertEqual(row.row_number, 1)
        self.assertEqual(row.status, ImportRow.Status.PENDING)

    def test_row_linked_to_job(self):
        row = ImportRow.objects.create(import_job=self.job, row_number=2, raw_data={})
        self.assertEqual(self.job.rows.filter(pk=row.pk).count(), 1)

    def test_str_contains_row_number(self):
        row = ImportRow.objects.create(import_job=self.job, row_number=3, raw_data={})
        self.assertIn("3", str(row))


class ImportTemplateModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="tmpl-team")

    def test_create_template(self):
        mapping = {"Container No": "container_number"}
        tmpl = ImportTemplate.objects.create(
            team=self.team,
            name="My Template",
            import_type=ImportJob.ImportType.CONTAINERS,
            mapping=mapping,
            is_default=True,
        )
        self.assertEqual(tmpl.mapping, mapping)
        self.assertTrue(tmpl.is_default)

    def test_global_template_has_no_team(self):
        tmpl = ImportTemplate.objects.create(
            name="Global",
            import_type=ImportJob.ImportType.CONTAINERS,
            mapping={},
        )
        self.assertIsNone(tmpl.team)


class ImportErrorModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="err-model-team")
        cls.user = make_user("err-model@example.com")
        cls.team.members.add(cls.user)
        cls.job = make_import_job(cls.team, cls.user)
        cls.row = ImportRow.objects.create(import_job=cls.job, row_number=1, raw_data={})

    def test_create_error_linked_to_job(self):
        err = ImportJobError.objects.create(
            import_job=self.job,
            code="INVALID_FORMAT",
            message="Container number format is invalid.",
        )
        self.assertIsNotNone(err.pk)
        self.assertEqual(err.import_job, self.job)
        self.assertEqual(err.severity, ImportJobError.Severity.ERROR)

    def test_create_error_linked_to_row(self):
        err = ImportJobError.objects.create(
            import_job=self.job,
            import_row=self.row,
            code="MISSING_FIELD",
            message="Required field owner_code is missing.",
        )
        self.assertEqual(err.import_row, self.row)
        self.assertIn(err, self.row.import_errors.all())

    def test_warning_severity(self):
        err = ImportJobError.objects.create(
            import_job=self.job,
            code="WARN_DUPLICATE",
            message="Possible duplicate entry.",
            severity=ImportJobError.Severity.WARNING,
        )
        self.assertEqual(err.severity, ImportJobError.Severity.WARNING)

    def test_str_contains_code(self):
        err = ImportJobError.objects.create(
            import_job=self.job,
            code="E_STR_TEST",
            message="Test error message for str.",
        )
        self.assertIn("E_STR_TEST", str(err))

    def test_error_reverse_relation_on_job(self):
        err = ImportJobError.objects.create(
            import_job=self.job,
            code="E_REV",
            message="Reverse relation test.",
        )
        self.assertIn(err, self.job.import_errors.all())


class ImportCascadeDeleteTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="cascade-import-team")
        cls.user = make_user("cascade-import@example.com")
        cls.team.members.add(cls.user)

    def test_deleting_job_cascades_rows(self):
        job = make_import_job(self.team, self.user)
        ImportRow.objects.create(import_job=job, row_number=1, raw_data={})
        pk = job.pk
        job.delete()
        self.assertEqual(ImportRow.objects.filter(import_job_id=pk).count(), 0)

    def test_deleting_job_cascades_errors(self):
        job = make_import_job(self.team, self.user)
        ImportJobError.objects.create(import_job=job, code="E_CASCADE", message="Cascade test.")
        pk = job.pk
        job.delete()
        self.assertEqual(ImportJobError.objects.filter(import_job_id=pk).count(), 0)

    def test_deleting_row_cascades_row_errors(self):
        job = make_import_job(self.team, self.user)
        row = ImportRow.objects.create(import_job=job, row_number=1, raw_data={})
        ImportJobError.objects.create(import_job=job, import_row=row, code="E_ROW", message="Row error.")
        row_pk = row.pk
        row.delete()
        self.assertEqual(ImportJobError.objects.filter(import_row_id=row_pk).count(), 0)
