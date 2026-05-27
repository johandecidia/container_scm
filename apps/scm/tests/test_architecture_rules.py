"""
Kort 3 — Arkitekturregler
Acceptanskriterier: Varje SCM-app har standardfiler och ARCHITECTURE.md finns.
"""

from pathlib import Path

from django.test import SimpleTestCase

SCM_APPS = [
    "containers",
    "shipments",
    "rates",
    "imports",
    "integrations",
    "analytics",
]

REQUIRED_FILES = [
    "models.py",
    "views.py",
    "urls.py",
    "forms.py",
    "selectors.py",
    "services.py",
    "admin.py",
    "apps.py",
]


class ScmArchitectureTest(SimpleTestCase):
    def test_scm_architecture_doc_exists(self):
        self.assertTrue(Path("apps/scm/ARCHITECTURE.md").exists())

    def test_scm_apps_have_required_files(self):
        for app in SCM_APPS:
            for filename in REQUIRED_FILES:
                path = Path(f"apps/scm/{app}/{filename}")
                self.assertTrue(path.exists(), f"Missing: apps/scm/{app}/{filename}")
