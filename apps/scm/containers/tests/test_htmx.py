"""Tests for HTMX partial responses and template existence."""

from pathlib import Path

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

OWNER = "CSQ"
CAT = "U"
SERIAL = "305418"
CHECK = calculate_check_digit(OWNER, CAT, SERIAL)
VALID_ID = f"{OWNER}{CAT}{SERIAL}{CHECK}"


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _make_container(team, owner=OWNER, serial=SERIAL) -> Container:
    check = calculate_check_digit(owner, CAT, serial)
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id=CAT,
        serial_number=serial,
        check_digit=check,
        equipment_type=_et(),
    )


class ContainerTemplateFilesTest(SimpleTestCase):
    def test_container_templates_exist(self):
        templates = [
            "templates/scm/base.html",
            "templates/scm/containers/pages/container_list.html",
            "templates/scm/containers/pages/container_detail.html",
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

    def test_list_with_htmx_returns_partial(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:list"), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        template_names = [t.name for t in response.templates]
        self.assertIn("scm/containers/partials/container_table.html", template_names)
        self.assertNotIn("scm/containers/pages/container_list.html", template_names)

    def test_list_without_htmx_returns_full_page(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:list"))
        self.assertEqual(response.status_code, 200)
        template_names = [t.name for t in response.templates]
        self.assertIn("scm/containers/pages/container_list.html", template_names)

    def test_create_htmx_returns_form_partial(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:create"), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        template_names = [t.name for t in response.templates]
        self.assertIn("scm/containers/partials/container_form.html", template_names)

    def test_update_htmx_returns_row_on_success(self):
        container = _make_container(self.team)
        et = _et()
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("containers:update", kwargs={"container_id": container.pk}),
            data={
                "container_id_input": VALID_ID,
                "equipment_type": et.pk,
                "status": "BOOKED",
                "condition": "GOOD",
                "color_system": "UNKNOWN",
                "current_location": "Rotterdam",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        template_names = [t.name for t in response.templates]
        self.assertIn("scm/containers/partials/container_row.html", template_names)

    def test_empty_state_when_no_containers(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("containers:list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No containers yet")
