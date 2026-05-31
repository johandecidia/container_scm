"""Tests for business-rule validation (validators.py)."""

from django.test import TestCase

from apps.scm.containers.models import Container
from apps.scm.imports.models import ImportError, ImportRow
from apps.scm.imports.validators import validate_import_row

from .helpers import CAT, CHECK, CONTAINER_ID, OWNER, SERIAL, make_equipment_type, make_import_job, make_team, make_user


class ContainerRowValidationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = make_team(slug="val-team")
        cls.user = make_user("val@example.com")
        cls.team.members.add(cls.user)
        cls.et = make_equipment_type()
        cls.job = make_import_job(cls.team, cls.user)

    def _row(self, validated_data: dict) -> ImportRow:
        row, _ = ImportRow.objects.get_or_create(
            import_job=self.job,
            row_number=ImportRow.objects.filter(import_job=self.job).count() + 1,
            defaults={"validated_data": validated_data},
        )
        row.validated_data = validated_data
        row.save()
        return row

    def test_valid_row_marked_valid(self):
        row = self._row({"container_number": CONTAINER_ID, "equipment_type": "22G1"})
        validate_import_row(row, self.team)
        row.refresh_from_db()
        self.assertEqual(row.status, ImportRow.Status.VALID)

    def test_invalid_container_number_marks_invalid(self):
        row = self._row({"container_number": "INVALID!!!", "equipment_type": "22G1"})
        validate_import_row(row, self.team)
        row.refresh_from_db()
        self.assertEqual(row.status, ImportRow.Status.INVALID)

    def test_wrong_check_digit_marks_invalid(self):
        bad_id = f"{OWNER}{CAT}{SERIAL}9"  # wrong check digit (unless 9 is correct)
        correct_check = CHECK
        if str(correct_check) == "9":
            bad_id = f"{OWNER}{CAT}{SERIAL}0"
        row = self._row({"container_number": bad_id, "equipment_type": "22G1"})
        validate_import_row(row, self.team)
        row.refresh_from_db()
        self.assertEqual(row.status, ImportRow.Status.INVALID)

    def test_missing_equipment_type_marks_invalid(self):
        row = self._row({"container_number": CONTAINER_ID})
        validate_import_row(row, self.team)
        row.refresh_from_db()
        self.assertEqual(row.status, ImportRow.Status.INVALID)

    def test_nonexistent_equipment_type_marks_invalid(self):
        row = self._row({"container_number": CONTAINER_ID, "equipment_type": "ZZZZ"})
        validate_import_row(row, self.team)
        row.refresh_from_db()
        self.assertEqual(row.status, ImportRow.Status.INVALID)

    def test_duplicate_container_is_warning_not_error(self):
        """Duplicate container gets a WARNING — row stays valid unless other errors exist."""
        Container.objects.create(
            team=self.team,
            owner_code=OWNER,
            category_id=CAT,
            serial_number=SERIAL,
            check_digit=CHECK,
            equipment_type=self.et,
        )
        row = self._row({"container_number": CONTAINER_ID, "equipment_type": "22G1"})
        validate_import_row(row, self.team)
        row.refresh_from_db()
        # Warning-only → row is still VALID
        self.assertEqual(row.status, ImportRow.Status.VALID)
        errors = ImportError.objects.filter(import_row=row, severity=ImportError.Severity.WARNING)
        self.assertEqual(errors.count(), 1)
        self.assertEqual(errors.first().code, "duplicate_container")

    def test_import_errors_persisted(self):
        row = self._row({"container_number": "BADINPUT", "equipment_type": "22G1"})
        validate_import_row(row, self.team)
        self.assertGreater(ImportError.objects.filter(import_row=row).count(), 0)
