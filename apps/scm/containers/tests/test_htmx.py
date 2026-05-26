"""
Kort 4 & 5 — HTMX-test.
Acceptanskriterier:
  - HTMX-request returnerar partial template (inte hel sida).
  - Templates finns på disk.
"""
from pathlib import Path

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.scm.containers.models import Container
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


class ContainerTemplateFilesTest(SimpleTestCase):
    def test_container_templates_exist(self):
        templates = [
            "templates/scm/base.html",
            "templates/scm/containers/pages/container_list.html",
            "templates/scm/containers/partials/container_table.html",
            "templates/scm/containers/partials/container_row.html",
            "templates/scm/containers/partials/container_form.html",
            "templates/scm/containers/partials/container_filters.html",
        ]
        for template in templates:
            self.assertTrue(Path(template).exists(), f"Template missing: {template}")


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerHtmxTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="HTMX Team", slug="htmx-team")
        cls.user = CustomUser.objects.create_user(username="htmx@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def test_container_list_htmx_returns_partial(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(
            reverse("containers:list"),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        template_names = [t.name for t in response.templates]
        self.assertIn("scm/containers/partials/container_table.html", template_names)
        # Should NOT render the full page template
        self.assertNotIn("scm/containers/pages/container_list.html", template_names)

    def test_container_list_full_page_without_htmx(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:list"))
        self.assertEqual(response.status_code, 200)
        template_names = [t.name for t in response.templates]
        self.assertIn("scm/containers/pages/container_list.html", template_names)

    def test_container_create_htmx_returns_partial(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(
            reverse("containers:create"),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        template_names = [t.name for t in response.templates]
        self.assertIn("scm/containers/partials/container_form.html", template_names)

    def test_container_update_htmx_returns_row_on_success(self):
        container = Container.objects.create(
            team=self.team, container_number="HTMXUPD0001", status="planned"
        )
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("containers:update", kwargs={"container_id": container.pk}),
            data={"container_number": "HTMXUPD0001", "status": "in_transit", "carrier": ""},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        template_names = [t.name for t in response.templates]
        self.assertIn("scm/containers/partials/container_row.html", template_names)
