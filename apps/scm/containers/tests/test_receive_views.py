"""Receive containers: the HTTP seam. The rules are in ``test_receive.py``."""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.choices import MovementType
from apps.scm.containers.models import Container, ContainerLocation, ContainerMovement, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "receive-views"}}

REPORT = """
| Prefix | Unit Number | Iso Size | Iso Type | Gate Date Time In | Site |
| PSLU | 2913030 | 22 | 10 | 2026-09-17 16:00:51 | MCR AB - Oceanterminalen |
| PSLU | 2913529 | 22 | G1 | 2026-09-11 13:31:04 | MCR AB - Oceanterminalen |
Support
"""


@override_settings(STORAGES=_TEST_STORAGES, CACHES=_LOCMEM)
class ReceiveViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Receive View", slug="receive-view")
        cls.other_team = Team.objects.create(name="Other", slug="receive-view-other")
        cls.user = CustomUser.objects.create_user(username="receiver@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.terminal = ContainerLocation.objects.create(team=cls.team, name="Oceanterminalen")
        cls.foreign = ContainerLocation.objects.create(team=cls.other_team, name="Oceanterminalen")
        equipment = EquipmentType.objects.get_or_create(
            iso_code="22G1", defaults={"category": "GP", "length_ft": 20, "description": "20' GP"}
        )[0]
        cls.box = Container.objects.create(
            team=cls.team,
            owner_code="PSL",
            category_id="U",
            serial_number="291303",
            check_digit=calculate_check_digit("PSL", "U", "291303"),
            equipment_type=equipment,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_page_offers_only_this_teams_active_locations(self):
        ContainerLocation.objects.create(team=self.team, name="Retired Yard", is_active=False)

        response = self.client.get(reverse("containers:receive"))

        self.assertEqual(response.status_code, 200)
        choices = list(response.context["form"].fields["location"].queryset)
        self.assertEqual(choices, [self.terminal])

    def test_the_container_list_links_to_receive(self):
        response = self.client.get(reverse("containers:list"))

        self.assertContains(response, reverse("containers:receive"))

    def test_preview_writes_nothing_and_shows_each_row(self):
        response = self.client.post(
            reverse("containers:receive_preview"),
            {"location": self.terminal.pk, "text": REPORT},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PSLU2913030")
        self.assertContains(response, "Container not found")
        self.assertContains(response, "Warning")  # Imported 2210 against a 22G1 container.
        self.assertContains(response, reverse("containers:receive_confirm"))
        self.assertFalse(ContainerMovement.objects.exists())

    def test_confirm_receives_and_reports_partial_success(self):
        response = self.client.post(
            reverse("containers:receive_confirm"),
            {"location": self.terminal.pk, "text": REPORT},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2 rows processed")
        self.assertContains(response, "Partially completed")
        movement = ContainerMovement.objects.get(container=self.box)
        self.assertEqual((movement.movement_type, movement.to_location), (MovementType.GATE_IN, self.terminal))

    def test_confirming_twice_records_one_movement(self):
        data = {"location": self.terminal.pk, "text": REPORT}

        self.client.post(reverse("containers:receive_confirm"), data, HTTP_HX_REQUEST="true")
        response = self.client.post(reverse("containers:receive_confirm"), data, HTTP_HX_REQUEST="true")

        self.assertContains(response, "Already received")
        self.assertEqual(ContainerMovement.objects.count(), 1)

    def test_another_teams_location_is_refused(self):
        response = self.client.post(
            reverse("containers:receive_confirm"), {"location": self.foreign.pk, "text": REPORT}, HTTP_HX_REQUEST="true"
        )

        self.assertContains(response, "alert-error")
        self.assertFalse(ContainerMovement.objects.exists())

    def test_text_without_rows_is_refused(self):
        response = self.client.post(
            reverse("containers:receive_preview"), {"location": self.terminal.pk, "text": "Support\nContact"}
        )

        self.assertContains(response, "No receive rows were found")

    def test_receive_requires_login_and_post(self):
        anonymous = Client()

        self.assertEqual(anonymous.get(reverse("containers:receive")).status_code, 302)
        self.assertEqual(self.client.get(reverse("containers:receive_confirm")).status_code, 405)
