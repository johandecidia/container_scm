"""The lifecycle where operations reads it: the queue rows, the two workspaces, receiving.

The interpreter is tested in ``test_arrival_lifecycle``; these are about the pages
telling the truth with it:

* An arrivals row carries its state, whether it is overdue, and the movement the
  state rests on — so the queue can be interrogated without opening the box.
* The Container Workspace shows inbound progress *beside* physical location and
  tracking, never merged into either, and shows nothing at all for a box that is not
  inbound to anywhere.
* The Shipment Workspace summarises several containers as counts, because one badge
  cannot describe four boxes in three states.
* Receiving is a movement an operator records, through the existing service. It is
  its own event, not something a gate-in implies.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.choices import LocationSource, MovementType
from apps.scm.containers.models import Container, ContainerMovement
from apps.scm.containers.movements import record_container_movement
from apps.scm.shipments.models import Shipment
from apps.scm.visibility.arrival_lifecycle import ArrivalState, get_container_arrival_lifecycle

from .arrival_scenarios import ArrivalScenarioTestCase, make_container
from .factories import make_user_and_team


class WorkspaceTestCase(ArrivalScenarioTestCase):
    """The shared hierarchy, with a logged-in client for the pages."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)


class ArrivalsRowTest(WorkspaceTestCase):
    """What a queue row says now, in the page's own words."""

    def test_a_row_shows_its_lifecycle_state(self):
        self.linked(self.shipment("SHP-PAGE", eta_days=1), "240001")

        self.assertContains(self.client.get(reverse("visibility:arrivals")), "Arriving")

    def test_an_overdue_row_says_so(self):
        self.linked(self.shipment("SHP-PAGELATE", eta_days=-2), "240002")

        response = self.client.get(reverse("visibility:arrivals"), {"window": "overdue"})

        self.assertContains(response, "Overdue")

    def test_the_state_filter_is_offered(self):
        self.linked(self.shipment("SHP-PAGE2"), "240003")

        response = self.client.get(reverse("visibility:arrivals"))

        self.assertContains(response, 'name="state"')
        self.assertContains(response, "Any state")

    def test_an_arrived_row_shows_the_movement_it_rests_on(self):
        self.arrive(self.linked(self.shipment("SHP-PAGEIN"), "240004"))

        response = self.client.get(reverse("visibility:arrivals"), {"state": ArrivalState.ARRIVED})

        self.assertContains(response, "SHP-PAGEIN")
        self.assertContains(response, "Gate In")

    def test_a_part_arrived_shipment_says_how_far(self):
        shipment = self.shipment("SHP-PAGEPART")
        first = self.linked(shipment, "240005", sequence=0)
        self.linked(shipment, "240006", sequence=1)
        self.arrive(first)

        self.assertContains(self.client.get(reverse("visibility:arrivals")), "1/2 arrived")


class ContainerWorkspaceTest(WorkspaceTestCase):
    """A compact inbound section, kept apart from physical location and tracking."""

    def get(self, container: Container):
        return self.client.get(reverse("containers:detail", args=[container.pk]))

    def test_an_inbound_container_shows_its_arrival_section(self):
        container = self.linked(self.shipment("SHP-CW1"), "300001")

        response = self.get(container)

        self.assertContains(response, "Inbound arrival")
        self.assertContains(response, "Oceanterminalen")
        self.assertContains(response, "Expected")

    def test_the_section_shows_the_arrival_and_receipt_times(self):
        container = self.linked(self.shipment("SHP-CW2"), "300002")
        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.RECEIVED,
            to_location=self.terminal,
            source=LocationSource.DEPOT,
        )

        response = self.get(container)

        self.assertContains(response, "Arrived at")
        self.assertContains(response, "Received at")
        self.assertContains(response, "Received")

    def test_an_arrived_container_is_prompted_to_be_received(self):
        container = self.linked(self.shipment("SHP-CW3"), "300003")
        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
        )

        self.assertContains(self.get(container), "not yet received")

    def test_a_container_with_no_shipment_shows_no_arrival_section(self):
        """It is not inbound to anywhere, and an empty panel would suggest it were."""
        self.assertNotContains(self.get(make_container(self.team, "300004")), "Inbound arrival")

    def test_a_container_whose_shipment_has_no_canonical_destination_shows_none_either(self):
        container = self.linked(self.shipment("SHP-CW5", canonical=False), "300005")

        self.assertNotContains(self.get(container), "Inbound arrival")

    def test_the_arrival_section_does_not_replace_the_physical_state_panel(self):
        """Two answers, both on the page: where the box is, and how far it has got."""
        container = self.linked(self.shipment("SHP-CW6"), "300006")

        response = self.get(container)

        self.assertContains(response, "Physical location")
        self.assertContains(response, "Inbound arrival")


class ShipmentWorkspaceTest(WorkspaceTestCase):
    """Four boxes, three states, and counts rather than one misleading badge."""

    def _mixed(self) -> Shipment:
        shipment = self.shipment("SHP-SW1")
        containers = [self.linked(shipment, f"31000{index}", sequence=index) for index in range(4)]
        for container in containers[:2]:
            record_container_movement(
                team=self.team,
                container=container,
                movement_type=MovementType.RECEIVED,
                to_location=self.terminal,
                source=LocationSource.DEPOT,
            )
        record_container_movement(
            team=self.team,
            container=containers[2],
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
        )
        return shipment

    def get(self, shipment: Shipment):
        return self.client.get(reverse("shipments:detail", args=[shipment.pk]))

    def test_the_page_shows_arrival_progress(self):
        response = self.get(self._mixed())

        self.assertContains(response, "Arrival progress")
        self.assertContains(response, "Outstanding")

    def test_the_progress_is_derived_and_not_rounded_up_to_arrived(self):
        response = self.get(self._mixed())

        self.assertContains(response, "75%")
        self.assertContains(response, "50%")
        self.assertContains(response, "awaiting receipt")

    def test_a_shipment_with_no_canonical_destination_shows_no_progress_panel(self):
        shipment = self.shipment("SHP-SW2", canonical=False)
        self.linked(shipment, "320001")

        self.assertNotContains(self.get(shipment), "Arrival progress")

    def test_a_shipment_with_no_containers_says_so_rather_than_reading_as_complete(self):
        response = self.get(self.shipment("SHP-SW3"))

        self.assertContains(response, "Arrival progress")
        self.assertContains(response, "nothing to arrive")


class ReceiveActionTest(WorkspaceTestCase):
    """Receiving goes through the existing movement service, and stays its own event."""

    def test_the_physical_panel_offers_a_receive_action(self):
        container = self.linked(self.shipment("SHP-RA1"), "330001")

        response = self.client.get(reverse("containers:detail", args=[container.pk]))

        self.assertContains(response, "type=received")
        self.assertContains(response, "Receive")

    def test_the_receipt_modal_preselects_the_canonical_destination(self):
        container = self.linked(self.shipment("SHP-RA2"), "330002")

        response = self.client.get(reverse("containers:record_movement", args=[container.pk]), {"type": "received"})

        self.assertContains(response, f'<option value="{self.terminal.pk}" selected>', html=False)

    def test_a_gate_in_is_not_given_a_preselected_destination(self):
        """A gate-in can happen anywhere on the way; offering the booking would guess."""
        container = self.linked(self.shipment("SHP-RA3"), "330003")

        response = self.client.get(reverse("containers:record_movement", args=[container.pk]), {"type": "gate_in"})

        self.assertNotContains(response, f'<option value="{self.terminal.pk}" selected>')

    def test_recording_a_receipt_advances_the_lifecycle(self):
        shipment = self.shipment("SHP-RA4")
        container = self.linked(shipment, "330004")

        response = self.client.post(
            reverse("containers:record_movement", args=[container.pk]),
            {
                "movement_type": MovementType.RECEIVED,
                "to_location": self.terminal.pk,
                "occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "notes": "",
                "gate_name": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        lifecycle = get_container_arrival_lifecycle(self.team, container, shipment)
        self.assertEqual(lifecycle.state, ArrivalState.RECEIVED)

    def test_recording_a_receipt_creates_one_movement_and_not_a_gate_in_too(self):
        """Keep events honest: a receipt is a receipt, not a receipt plus an invented gate move."""
        shipment = self.shipment("SHP-RA5")
        container = self.linked(shipment, "330005")

        self.client.post(
            reverse("containers:record_movement", args=[container.pk]),
            {
                "movement_type": MovementType.RECEIVED,
                "to_location": self.terminal.pk,
                "occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "notes": "",
                "gate_name": "",
            },
        )

        movements = ContainerMovement.objects.filter(team=self.team, container=container)
        self.assertEqual([movement.movement_type for movement in movements], [MovementType.RECEIVED])

    def test_receiving_a_container_with_no_shipment_still_works(self):
        """Depot receiving is not conditional on a booking existing."""
        container = make_container(self.team, "330006")

        response = self.client.get(reverse("containers:record_movement", args=[container.pk]), {"type": "received"})

        self.assertEqual(response.status_code, 200)


class TeamIsolationTest(WorkspaceTestCase):
    """No workspace may render another team's arrival."""

    def test_another_teams_container_workspace_is_not_reachable(self):
        _other_user, other_team = make_user_and_team("wsother@example.com", "ws-other")
        other_container = make_container(other_team, "340001")

        response = self.client.get(reverse("containers:detail", args=[other_container.pk]))

        self.assertEqual(response.status_code, 404)

    def test_another_teams_shipment_workspace_is_not_reachable(self):
        _other_user, other_team = make_user_and_team("wsother2@example.com", "ws-other2")
        other_shipment = Shipment.objects.create(
            team=other_team,
            shipment_number="SHP-THEIRS",
            status=Shipment.Status.IN_TRANSIT,
            eta=timezone.localdate() + timedelta(days=2),
        )

        response = self.client.get(reverse("shipments:detail", args=[other_shipment.pk]))

        self.assertEqual(response.status_code, 404)
