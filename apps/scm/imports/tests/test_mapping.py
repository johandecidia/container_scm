"""Tests for column mapping logic."""

from django.test import TestCase

from apps.scm.imports.mappings import apply_mapping, get_default_mapping
from apps.scm.imports.models import ImportJob


class ApplyMappingTest(TestCase):
    def setUp(self):
        self.mapping = get_default_mapping(ImportJob.ImportType.CONTAINERS)

    def test_known_column_mapped(self):
        result = apply_mapping({"Container No": "CSQU3054187"}, self.mapping)
        self.assertEqual(result["container_number"], "CSQU3054187")

    def test_equipment_type_mapped(self):
        result = apply_mapping({"Equipment Type": "22G1"}, self.mapping)
        self.assertEqual(result["equipment_type"], "22G1")

    def test_unknown_column_ignored(self):
        result = apply_mapping({"Unknown Column": "abc", "Container No": "CSQU3054187"}, self.mapping)
        self.assertNotIn("Unknown Column", result)
        self.assertIn("container_number", result)

    def test_multiple_headers_for_same_target(self):
        # "Container No" and "Container Number" both map to container_number
        result = apply_mapping({"Container No": "CSQU3054187", "Container Number": "MSCU1234560"}, self.mapping)
        # One of them should win
        self.assertIn("container_number", result)

    def test_empty_mapping_returns_empty(self):
        result = apply_mapping({"Container No": "X"}, {})
        self.assertEqual(result, {})
