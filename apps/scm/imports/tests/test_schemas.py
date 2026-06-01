"""Tests for Pydantic import schemas."""

from django.test import TestCase

from apps.scm.imports.models import ImportJob
from apps.scm.imports.schemas import ContainerImportSchema, validate_row_data


class ContainerImportSchemaTest(TestCase):
    def test_valid_data_passes(self):
        schema = ContainerImportSchema.model_validate({"container_number": "CSQU3054187", "equipment_type": "22G1"})
        self.assertEqual(schema.container_number, "CSQU3054187")

    def test_container_number_normalised_uppercase(self):
        schema = ContainerImportSchema.model_validate({"container_number": "csqu3054187"})
        self.assertEqual(schema.container_number, "CSQU3054187")

    def test_container_number_whitespace_stripped(self):
        schema = ContainerImportSchema.model_validate({"container_number": "  CSQU3054187  "})
        self.assertEqual(schema.container_number, "CSQU3054187")

    def test_container_number_spaces_removed(self):
        schema = ContainerImportSchema.model_validate({"container_number": "CSQU 305418 7"})
        self.assertEqual(schema.container_number, "CSQU3054187")

    def test_missing_container_number_raises(self):
        validated, errors = validate_row_data(ImportJob.ImportType.CONTAINERS, {"equipment_type": "22G1"})
        self.assertTrue(len(errors) > 0)
        self.assertEqual(validated, {})

    def test_empty_container_number_raises(self):
        validated, errors = validate_row_data(
            ImportJob.ImportType.CONTAINERS, {"container_number": "", "equipment_type": "22G1"}
        )
        self.assertTrue(len(errors) > 0)

    def test_equipment_type_normalised_uppercase(self):
        schema = ContainerImportSchema.model_validate({"container_number": "CSQU3054187", "equipment_type": "22g1"})
        self.assertEqual(schema.equipment_type, "22G1")

    def test_optional_fields_none_when_absent(self):
        schema = ContainerImportSchema.model_validate({"container_number": "CSQU3054187"})
        self.assertIsNone(schema.equipment_type)
        self.assertIsNone(schema.seal_number)

    def test_whitespace_only_optional_becomes_none(self):
        schema = ContainerImportSchema.model_validate({"container_number": "CSQU3054187", "notes": "   "})
        self.assertIsNone(schema.notes)

    def test_validate_row_data_returns_dict(self):
        data, errors = validate_row_data(
            ImportJob.ImportType.CONTAINERS,
            {"container_number": "CSQU3054187", "equipment_type": "22G1"},
        )
        self.assertEqual(errors, [])
        self.assertIn("container_number", data)

    def test_unknown_import_type_passes_through(self):
        data, errors = validate_row_data("unknown_type", {"anything": "value"})
        self.assertEqual(errors, [])
        self.assertEqual(data, {"anything": "value"})
