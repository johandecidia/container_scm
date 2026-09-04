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
    "tracking",
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


class ScmTemplateTagTest(SimpleTestCase):
    """No SCM template tag may be wrapped across lines.

    Django's template lexer matches `{% ... %}` without DOTALL, so a tag broken over
    two lines is not a tag: it renders as its own source text. It fails silently —
    the page still returns 200, with a template tag printed where the component
    should have been — which is why it is asserted rather than trusted to review.
    """

    def test_no_template_tag_is_wrapped_across_lines(self):
        offenders = []
        for path in sorted(Path("templates/scm").rglob("*.html")):
            in_comment = False
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                # Usage examples inside {% comment %} blocks are discarded before
                # rendering, so a wrapped tag there is documentation, not a bug.
                if "{% comment %}" in line:
                    in_comment = True
                if "{% endcomment %}" in line:
                    in_comment = False
                    continue
                if in_comment:
                    continue
                if line.count("{%") > line.count("%}"):
                    offenders.append(f"{path}:{number}: {line.strip()}")

        self.assertEqual(offenders, [], "Template tags must open and close on one line:\n" + "\n".join(offenders))
