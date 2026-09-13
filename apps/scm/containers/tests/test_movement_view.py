"""The Gate In / Gate Out action: the HTTP seam, not the rules.

The rules are tested in ``test_physical_movements.py``. What matters here is that
the view is only a seam — it validates a form, calls the service and renders. In
particular a domain rejection must reach the person as a form error rather than a
500, because "this gate-out has no origin" is something they can fix.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.choices import LocationSource, LocationType, MovementType
from apps.scm.containers.models import Container, ContainerLocation, ContainerMovement, EquipmentType
from apps.scm.containers.movements import record_container_movement
from apps.scm.containers.utils import calculate_check_digit
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _equipment() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


@override_settings(STORAGES=_TEST_STORAGES)
class RecordMovementViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Gate View", slug="loc2-gate-view")
        cls.user = CustomUser.objects.create_user(username="gate@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.terminal = ContainerLocation.objects.create(
            team=cls.team, name="Oceanterminalen", location_type=LocationType.DEPOT
        )
        cls.depot = ContainerLocation.objects.create(team=cls.team, name="MCR Depot", location_type=LocationType.DEPOT)

    def setUp(self):
        self.container = Container.objects.create(
            team=self.team,
            owner_code="XIN",
            category_id="U",
            serial_number="200930",
            check_digit=calculate_check_digit("XIN", "U", "200930"),
            equipment_type=_equipment(),
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("containers:record_movement", kwargs={"container_id": self.container.pk})

    def _when(self) -> str:
        return timezone.localtime().strftime("%Y-%m-%dT%H:%M")

    def test_the_modal_opens(self):
        response = self.client.get(f"{self.url}?type=gate_in", HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Record movement")

    def test_posting_a_gate_in_moves_the_container(self):
        response = self.client.post(
            self.url,
            data={
                "movement_type": MovementType.GATE_IN,
                "to_location": self.terminal.pk,
                "occurred_at": self._when(),
                "gate_name": "John Evans",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.container.refresh_from_db()
        self.assertEqual(self.container.current_location, self.terminal)
        movement = ContainerMovement.objects.get(container=self.container)
        self.assertEqual(movement.gate_name, "John Evans")
        self.assertEqual(movement.source, LocationSource.MANUAL)

    def test_posting_a_gate_out_clears_the_container(self):
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(hours=2),
        )
        response = self.client.post(
            self.url,
            data={"movement_type": MovementType.GATE_OUT, "occurred_at": self._when()},
        )

        self.assertEqual(response.status_code, 302)
        self.container.refresh_from_db()
        self.assertIsNone(self.container.current_location)

    def test_an_htmx_post_asks_the_page_to_reload(self):
        response = self.client.post(
            self.url,
            data={
                "movement_type": MovementType.GATE_IN,
                "to_location": self.terminal.pk,
                "occurred_at": self._when(),
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Refresh"], "true")

    def test_a_gate_in_without_a_destination_is_a_form_error(self):
        response = self.client.post(
            self.url,
            data={"movement_type": MovementType.GATE_IN, "occurred_at": self._when()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ContainerMovement.objects.filter(container=self.container).exists())

    def test_a_gate_out_with_nowhere_to_leave_is_a_form_error_not_a_crash(self):
        """The container has never been placed, so the service rejects it."""
        response = self.client.post(
            self.url,
            data={"movement_type": MovementType.GATE_OUT, "occurred_at": self._when()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ContainerMovement.objects.filter(container=self.container).exists())

    def test_a_transfer_to_the_same_place_is_a_form_error(self):
        response = self.client.post(
            self.url,
            data={
                "movement_type": MovementType.TRANSFER,
                "from_location": self.terminal.pk,
                "to_location": self.terminal.pk,
                "occurred_at": self._when(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ContainerMovement.objects.filter(container=self.container).exists())

    def test_only_this_teams_locations_are_offered(self):
        other_team = Team.objects.create(name="Other", slug="loc2-gate-other")
        theirs = ContainerLocation.objects.create(team=other_team, name="Their Depot", location_type=LocationType.DEPOT)
        response = self.client.post(
            self.url,
            data={
                "movement_type": MovementType.GATE_IN,
                "to_location": theirs.pk,
                "occurred_at": self._when(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ContainerMovement.objects.filter(container=self.container).exists())

    def test_another_teams_container_is_not_reachable(self):
        other_team = Team.objects.create(name="Outsider", slug="loc2-gate-outsider")
        other_user = CustomUser.objects.create_user(username="outsider@example.com", password="pass")
        other_team.members.add(other_user, through_defaults={"role": ROLE_MEMBER})

        client = Client()
        client.force_login(other_user)
        response = client.get(self.url)
        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_TEST_STORAGES)
class ContainerWorkspacePhysicalStateTest(TestCase):
    """The Overview panel says where the box is and what that rests on."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Physical Panel", slug="loc2-panel")
        cls.user = CustomUser.objects.create_user(username="panel@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.terminal = ContainerLocation.objects.create(
            team=cls.team, name="Oceanterminalen", location_type=LocationType.DEPOT
        )

    def setUp(self):
        self.container = Container.objects.create(
            team=self.team,
            owner_code="XIN",
            category_id="U",
            serial_number="200930",
            check_digit=calculate_check_digit("XIN", "U", "200930"),
            equipment_type=_equipment(),
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("containers:detail", kwargs={"container_id": self.container.pk})

    def test_an_unplaced_container_says_so(self):
        response = self.client.get(self.url)
        self.assertContains(response, "No physical location has been recorded")

    def test_a_gated_in_container_shows_the_place_and_the_movement(self):
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            gate_name="John Evans",
        )
        response = self.client.get(self.url)
        self.assertContains(response, "Oceanterminalen")
        self.assertContains(response, "Gate In")
        self.assertContains(response, "John Evans")

    def test_a_gated_out_container_reads_as_departed_not_unknown(self):
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(hours=2),
        )
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_OUT,
        )
        response = self.client.get(self.url)
        self.assertContains(response, "Departed")
