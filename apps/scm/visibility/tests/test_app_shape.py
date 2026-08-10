"""The visibility app must stay a composition layer.

It reads other apps' data and owns none of its own. Locking that down in a test
rather than a comment, because the pressure to "just add a small model here" is
constant, and the moment this app stores something it becomes a second source of
truth for data that already has one.
"""

from __future__ import annotations

from pathlib import Path

from django.apps import apps
from django.test import SimpleTestCase

APP_DIR = Path("apps/scm/visibility")


class VisibilityAppShapeTest(SimpleTestCase):
    def test_the_app_is_installed(self):
        self.assertTrue(apps.is_installed("apps.scm.visibility"))

    def test_it_owns_no_models(self):
        self.assertEqual(list(apps.get_app_config("scm_visibility").get_models()), [])

    def test_it_has_no_models_module_to_grow_one_in(self):
        self.assertFalse((APP_DIR / "models.py").exists())

    def test_it_has_no_services_module(self):
        """No writes. A visibility page that mutates domain data is a bug."""
        self.assertFalse((APP_DIR / "services.py").exists())

    def test_it_has_the_files_it_does_need(self):
        for name in ("apps.py", "selectors.py", "read_models.py", "geojson.py", "views.py", "urls.py"):
            with self.subTest(name=name):
                self.assertTrue((APP_DIR / name).exists(), f"Missing apps/scm/visibility/{name}")
