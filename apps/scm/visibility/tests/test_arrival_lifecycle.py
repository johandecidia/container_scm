"""The arrival lifecycle: expected, approaching, here, received — and whose arrival it is.

The interpreter is a reading of evidence, so these tests are mostly about what it
refuses to conclude:

* An ETA that has passed does not mean the box arrived. It means EXPECTED and
  overdue, and only an accepted physical movement says otherwise.
* An arrival at the port containing the destination is not an arrival at the
  destination. Containment runs one way.
* A gate-in from before the shipment's own cycle is somebody else's arrival, and a
  gate-in recorded against another shipment is that shipment's.
* An arrival stays an arrival. A box gated in, received, and trucked out again did
  not become expected again.

Dates are relative to ``timezone.now()`` at test time, so the suite does not drift
into failing on a particular calendar day.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.test.utils import override_settings
from django.utils import timezone

from apps.scm.containers.choices import LocationSource, LocationType, MovementType
from apps.scm.containers.models import Container
from apps.scm.containers.movements import record_container_movement
from apps.scm.containers.services import create_location
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.visibility.arrival_lifecycle import (
    ArrivalState,
    arrival_cycle_start,
    get_container_arrival_lifecycle,
    get_shipment_arrival_progress,
)

from .arrival_scenarios import ArrivalScenarioTestCase, make_container
from .factories import make_user_and_team


class LifecycleTestCase(ArrivalScenarioTestCase):
    """The shared hierarchy, plus the one question these tests ask of it."""

    def lifecycle(self, container: Container, shipment: Shipment | None):
        return get_container_arrival_lifecycle(self.team, container, shipment)


class StateFromEvidenceTest(LifecycleTestCase):
    """The four states, and what each one takes."""

    def test_a_routed_container_with_no_arrival_is_expected(self):
        shipment = self.shipment("SHP-EXP")
        container = self.linked(shipment, "100001")

        lifecycle = self.lifecycle(container, shipment)

        self.assertEqual(lifecycle.state, ArrivalState.EXPECTED)
        self.assertFalse(lifecycle.has_arrived)
        self.assertIsNone(lifecycle.arrived_at)
        self.assertTrue(lifecycle.is_evaluable)

    def test_an_eta_inside_the_arrival_window_is_arriving(self):
        shipment = self.shipment("SHP-ARV", eta_days=1)
        container = self.linked(shipment, "100002")

        self.assertEqual(self.lifecycle(container, shipment).state, ArrivalState.ARRIVING)

    def test_an_eta_beyond_the_arrival_window_is_only_expected(self):
        shipment = self.shipment("SHP-FAR", eta_days=9)
        container = self.linked(shipment, "100003")

        self.assertEqual(self.lifecycle(container, shipment).state, ArrivalState.EXPECTED)

    @override_settings(SCM_ARRIVAL_WINDOW_HOURS=24 * 14)
    def test_the_arrival_window_is_configurable(self):
        """Widening it moves the EXPECTED/ARRIVING boundary and nothing else."""
        shipment = self.shipment("SHP-WIDE", eta_days=9)
        container = self.linked(shipment, "100004")

        self.assertEqual(self.lifecycle(container, shipment).state, ArrivalState.ARRIVING)

    def test_a_gate_in_at_the_destination_is_arrived(self):
        shipment = self.shipment("SHP-GATE")
        container = self.linked(shipment, "100005")
        occurred = timezone.now() - timedelta(hours=2)

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=occurred,
        )

        lifecycle = self.lifecycle(container, shipment)
        self.assertEqual(lifecycle.state, ArrivalState.ARRIVED)
        self.assertEqual(lifecycle.arrived_at, occurred)
        self.assertIsNone(lifecycle.received_at)
        self.assertTrue(lifecycle.is_awaiting_receipt)

    def test_a_receipt_at_the_destination_is_received(self):
        shipment = self.shipment("SHP-RECV")
        container = self.linked(shipment, "100006")
        occurred = timezone.now() - timedelta(hours=1)

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.RECEIVED,
            to_location=self.terminal,
            occurred_at=occurred,
            source=LocationSource.DEPOT,
        )

        lifecycle = self.lifecycle(container, shipment)
        self.assertEqual(lifecycle.state, ArrivalState.RECEIVED)
        self.assertEqual(lifecycle.received_at, occurred)
        self.assertFalse(lifecycle.is_awaiting_receipt)

    def test_a_receipt_is_arrival_evidence_too(self):
        """A box somebody received is a box that got there, whether a gate move was recorded."""
        shipment = self.shipment("SHP-RECV2")
        container = self.linked(shipment, "100007")

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.RECEIVED,
            to_location=self.terminal,
            source=LocationSource.DEPOT,
        )

        self.assertTrue(self.lifecycle(container, shipment).has_arrived)

    def test_arrival_time_is_the_first_evidence_not_the_latest(self):
        shipment = self.shipment("SHP-FIRST")
        container = self.linked(shipment, "100008")
        gate_in = timezone.now() - timedelta(days=2)

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=gate_in,
        )
        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.RECEIVED,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(days=1),
            source=LocationSource.DEPOT,
        )

        self.assertEqual(self.lifecycle(container, shipment).arrived_at, gate_in)

    def test_a_container_with_no_shipment_has_no_arrival_cycle(self):
        """Nothing to be inbound to, so nothing to have arrived at."""
        container = make_container(self.team, "100009")

        lifecycle = self.lifecycle(container, None)

        self.assertFalse(lifecycle.is_evaluable)
        self.assertIsNone(lifecycle.destination)
        self.assertEqual(lifecycle.state, ArrivalState.EXPECTED)

    def test_a_shipment_with_no_canonical_destination_can_never_arrive(self):
        """The lifecycle says so rather than reading the booking text as a place."""
        shipment = self.shipment("SHP-TEXT", destination=None)
        Shipment.objects.filter(pk=shipment.pk).update(destination_location=None)
        shipment.refresh_from_db()
        container = self.linked(shipment, "100010")

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
        )

        lifecycle = self.lifecycle(container, shipment)
        self.assertFalse(lifecycle.is_evaluable)
        self.assertFalse(lifecycle.has_arrived)


class OverdueTest(LifecycleTestCase):
    """An ETA that has passed is a fact about the date, not about the box."""

    def test_a_passed_eta_with_no_arrival_is_expected_and_overdue(self):
        shipment = self.shipment("SHP-LATE", eta_days=-4)
        container = self.linked(shipment, "110001")

        lifecycle = self.lifecycle(container, shipment)

        self.assertEqual(lifecycle.state, ArrivalState.EXPECTED)
        self.assertTrue(lifecycle.is_overdue)

    def test_a_passed_eta_does_not_imply_arrival(self):
        """The clock passing 14:00 is not the box reaching the gate."""
        shipment = self.shipment("SHP-LATE2", eta_days=-1)
        container = self.linked(shipment, "110002")

        lifecycle = self.lifecycle(container, shipment)

        self.assertFalse(lifecycle.has_arrived)
        self.assertIsNone(lifecycle.arrived_at)

    def test_an_arrived_container_is_not_overdue_however_late_it_was(self):
        shipment = self.shipment("SHP-LATE3", eta_days=-4)
        container = self.linked(shipment, "110003")

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
        )

        lifecycle = self.lifecycle(container, shipment)
        self.assertEqual(lifecycle.state, ArrivalState.ARRIVED)
        self.assertFalse(lifecycle.is_overdue)

    def test_no_eta_is_not_overdue(self):
        shipment = self.shipment("SHP-NOETA")
        Shipment.objects.filter(pk=shipment.pk).update(eta=None)
        shipment.refresh_from_db()
        container = self.linked(shipment, "110004")

        lifecycle = self.lifecycle(container, shipment)
        self.assertFalse(lifecycle.is_overdue)
        self.assertEqual(lifecycle.state, ArrivalState.EXPECTED)


class DestinationHierarchyTest(LifecycleTestCase):
    """Containment runs one way: into the destination, never out of it."""

    def test_an_arrival_at_a_descendant_of_the_destination_counts(self):
        """A bay inside Oceanterminalen is Oceanterminalen."""
        shipment = self.shipment("SHP-BAY")
        container = self.linked(shipment, "120001")

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.bay,
        )

        self.assertEqual(self.lifecycle(container, shipment).state, ArrivalState.ARRIVED)

    def test_an_arrival_at_the_parent_of_the_destination_does_not_count(self):
        """Göteborg contains terminals this shipment was not routed to."""
        shipment = self.shipment("SHP-PORT")
        container = self.linked(shipment, "120002")

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.port,
        )

        lifecycle = self.lifecycle(container, shipment)
        self.assertEqual(lifecycle.state, ArrivalState.EXPECTED)
        self.assertFalse(lifecycle.has_arrived)

    def test_an_arrival_at_an_unrelated_place_does_not_count(self):
        shipment = self.shipment("SHP-ELSE")
        container = self.linked(shipment, "120003")

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.elsewhere,
        )

        self.assertFalse(self.lifecycle(container, shipment).has_arrived)

    def test_a_shipment_to_the_port_is_satisfied_by_its_terminal(self):
        """The other direction: routed to Göteborg, gated into a terminal inside it."""
        shipment = self.shipment("SHP-INTO", destination=self.port)
        container = self.linked(shipment, "120004")

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
        )

        self.assertEqual(self.lifecycle(container, shipment).state, ArrivalState.ARRIVED)


class HistoricalStabilityTest(LifecycleTestCase):
    """Arrival is an event in the inbound process, not the box's current position."""

    def test_gate_in_receive_gate_out_stays_received(self):
        shipment = self.shipment("SHP-HIST")
        container = self.linked(shipment, "130001")
        received = timezone.now() - timedelta(days=2)

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(days=3),
        )
        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.RECEIVED,
            to_location=self.terminal,
            occurred_at=received,
            source=LocationSource.DEPOT,
        )
        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_OUT,
            from_location=self.terminal,
            occurred_at=timezone.now() - timedelta(days=1),
        )

        container.refresh_from_db()
        self.assertIsNone(container.current_location)

        lifecycle = self.lifecycle(container, shipment)
        self.assertEqual(lifecycle.state, ArrivalState.RECEIVED)
        self.assertEqual(lifecycle.received_at, received)

    def test_a_departure_does_not_make_an_arrived_container_expected_again(self):
        shipment = self.shipment("SHP-HIST2")
        container = self.linked(shipment, "130002")

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(days=2),
        )
        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_OUT,
            occurred_at=timezone.now() - timedelta(days=1),
        )

        self.assertEqual(self.lifecycle(container, shipment).state, ArrivalState.ARRIVED)

    def test_a_transfer_onward_inside_the_port_does_not_undo_the_arrival(self):
        shipment = self.shipment("SHP-HIST3")
        container = self.linked(shipment, "130003")

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(days=2),
        )
        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.TRANSFER,
            from_location=self.terminal,
            to_location=self.elsewhere,
            occurred_at=timezone.now() - timedelta(days=1),
        )

        self.assertTrue(self.lifecycle(container, shipment).has_arrived)


class ShipmentAssociationTest(LifecycleTestCase):
    """Whose arrival is this? The question LOC-3 exists to answer."""

    def test_a_gate_in_from_before_the_shipment_departed_does_not_count(self):
        """The depot has handled this box before. That was not this booking."""
        shipment = self.shipment("SHP-OLD", departed_days=5)
        container = self.linked(shipment, "140001")

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(days=120),
        )

        lifecycle = self.lifecycle(container, shipment)
        self.assertEqual(lifecycle.state, ArrivalState.EXPECTED)
        self.assertIsNone(lifecycle.arrived_at)

    def test_a_gate_in_after_the_shipment_departed_counts(self):
        shipment = self.shipment("SHP-NEW", departed_days=5)
        container = self.linked(shipment, "140002")

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(days=1),
        )

        self.assertEqual(self.lifecycle(container, shipment).state, ArrivalState.ARRIVED)

    def test_a_movement_linked_to_another_shipment_does_not_count(self):
        """Explicit linkage decides both ways, and it decides against this one."""
        first = self.shipment("SHP-A")
        second = self.shipment("SHP-B")
        container = self.linked(second, "140003")

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            related_shipment=first,
        )

        self.assertEqual(self.lifecycle(container, second).state, ArrivalState.EXPECTED)

    def test_a_movement_linked_to_this_shipment_counts_whenever_it_happened(self):
        """Somebody stated the association; a derived time bound must not overrule it."""
        shipment = self.shipment("SHP-LINKED", departed_days=1)
        container = self.linked(shipment, "140004")

        record_container_movement(
            team=self.team,
            container=container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
            occurred_at=timezone.now() - timedelta(days=200),
            related_shipment=shipment,
        )

        self.assertEqual(self.lifecycle(container, shipment).state, ArrivalState.ARRIVED)

    def test_the_cycle_start_prefers_the_actual_departure(self):
        shipment = self.shipment("SHP-ANCHOR")
        self.assertEqual(arrival_cycle_start(shipment), shipment.actual_departure_at)

    def test_the_cycle_start_falls_back_to_the_planned_departure(self):
        shipment = self.shipment("SHP-ANCHOR2")
        Shipment.objects.filter(pk=shipment.pk).update(actual_departure_at=None)
        shipment.refresh_from_db()

        start = arrival_cycle_start(shipment)

        self.assertIsNotNone(start)
        self.assertEqual(timezone.localdate(start), shipment.etd)

    def test_the_cycle_start_falls_back_to_when_the_shipment_was_recorded(self):
        """Not a departure, but a floor: a September booking was not met in June."""
        shipment = self.shipment("SHP-ANCHOR3")
        Shipment.objects.filter(pk=shipment.pk).update(actual_departure_at=None, etd=None)
        shipment.refresh_from_db()

        self.assertEqual(arrival_cycle_start(shipment), shipment.created_at)

    def test_a_container_with_no_shipment_has_no_cycle_start(self):
        self.assertIsNone(arrival_cycle_start(None))


class ShipmentProgressTest(LifecycleTestCase):
    """Four boxes, four states, and one summary that does not overstate."""

    def _mixed(self) -> Shipment:
        shipment = self.shipment("SHP-MIX")
        containers = []
        for index in range(4):
            container = make_container(self.team, f"15000{index}")
            ShipmentContainer.objects.create(shipment=shipment, container=container, sequence=index)
            containers.append(container)

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

    def test_the_counts_describe_every_container(self):
        progress = get_shipment_arrival_progress(self.team, self._mixed())

        self.assertEqual(progress.total, 4)
        self.assertEqual(progress.arrived, 3)
        self.assertEqual(progress.received, 2)
        self.assertEqual(progress.outstanding, 1)
        self.assertEqual(progress.awaiting_receipt, 1)

    def test_the_percentages_are_derived_from_the_counts(self):
        progress = get_shipment_arrival_progress(self.team, self._mixed())

        self.assertEqual(progress.arrived_percent, 75)
        self.assertEqual(progress.received_percent, 50)

    def test_one_outstanding_container_keeps_the_shipment_outstanding(self):
        progress = get_shipment_arrival_progress(self.team, self._mixed())

        self.assertEqual(progress.state, ArrivalState.EXPECTED)
        self.assertFalse(progress.has_arrived)

    def test_a_shipment_is_arrived_only_when_every_container_is(self):
        shipment = self.shipment("SHP-ALL")
        for index in range(2):
            container = make_container(self.team, f"16000{index}")
            ShipmentContainer.objects.create(shipment=shipment, container=container, sequence=index)
            record_container_movement(
                team=self.team,
                container=container,
                movement_type=MovementType.GATE_IN,
                to_location=self.terminal,
            )

        progress = get_shipment_arrival_progress(self.team, shipment)
        self.assertEqual(progress.state, ArrivalState.ARRIVED)
        self.assertTrue(progress.has_arrived)
        self.assertTrue(progress.is_awaiting_receipt)

    def test_a_shipment_is_received_only_when_every_container_is(self):
        shipment = self.shipment("SHP-ALLRECV")
        for index in range(2):
            container = make_container(self.team, f"17000{index}")
            ShipmentContainer.objects.create(shipment=shipment, container=container, sequence=index)
            record_container_movement(
                team=self.team,
                container=container,
                movement_type=MovementType.RECEIVED,
                to_location=self.terminal,
                source=LocationSource.DEPOT,
            )

        progress = get_shipment_arrival_progress(self.team, shipment)
        self.assertEqual(progress.state, ArrivalState.RECEIVED)
        self.assertTrue(progress.is_received)
        self.assertFalse(progress.is_awaiting_receipt)

    def test_a_shipment_with_no_containers_is_still_an_expectation(self):
        progress = get_shipment_arrival_progress(self.team, self.shipment("SHP-EMPTY"))

        self.assertEqual(progress.total, 0)
        self.assertFalse(progress.has_arrived)
        self.assertEqual(progress.state, ArrivalState.EXPECTED)
        self.assertEqual(progress.arrived_percent, 0)

    def test_a_shipment_with_no_containers_can_still_be_arriving(self):
        progress = get_shipment_arrival_progress(self.team, self.shipment("SHP-EMPTY2", eta_days=1))

        self.assertEqual(progress.state, ArrivalState.ARRIVING)

    def test_the_first_arrival_and_the_last_receipt_are_reported(self):
        shipment = self._mixed()
        progress = get_shipment_arrival_progress(self.team, shipment)

        self.assertIsNotNone(progress.first_arrived_at)
        self.assertIsNotNone(progress.last_received_at)
        self.assertIsNotNone(progress.latest_relevant_movement)


class TenantIsolationTest(TestCase):
    """No destination, movement or lifecycle answer may cross a team boundary."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("lcmine@example.com", "lc-mine")
        cls.other_user, cls.other_team = make_user_and_team("lctheirs@example.com", "lc-theirs")
        cls.terminal = create_location(cls.team, {"name": "Oceanterminalen", "location_type": LocationType.TERMINAL})
        cls.other_terminal = create_location(
            cls.other_team, {"name": "Oceanterminalen", "location_type": LocationType.TERMINAL}
        )

        cls.shipment = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-MINE",
            status=Shipment.Status.IN_TRANSIT,
            destination_location=cls.terminal,
            actual_departure_at=timezone.now() - timedelta(days=5),
            eta=timezone.localdate() + timedelta(days=3),
        )
        cls.container = make_container(cls.team, "180001")
        ShipmentContainer.objects.create(shipment=cls.shipment, container=cls.container, sequence=0)

    def test_another_teams_arrival_at_a_same_named_place_does_not_count(self):
        other_container = make_container(self.other_team, "180002")
        record_container_movement(
            team=self.other_team,
            container=other_container,
            movement_type=MovementType.GATE_IN,
            to_location=self.other_terminal,
        )

        lifecycle = get_container_arrival_lifecycle(self.team, self.container, self.shipment)
        self.assertFalse(lifecycle.has_arrived)

    def test_reading_a_lifecycle_as_the_wrong_team_finds_no_evidence(self):
        record_container_movement(
            team=self.team,
            container=self.container,
            movement_type=MovementType.GATE_IN,
            to_location=self.terminal,
        )

        self.assertTrue(get_container_arrival_lifecycle(self.team, self.container, self.shipment).has_arrived)
        # The same question asked by the other team: the destination is not theirs,
        # so it resolves to no subtree and no movement of theirs can match it.
        self.assertFalse(get_container_arrival_lifecycle(self.other_team, self.container, self.shipment).has_arrived)


class QueryBehaviourTest(LifecycleTestCase):
    """A fixed number of queries, whatever the number of containers."""

    def test_the_query_count_does_not_grow_with_the_number_of_containers(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        small = self.shipment("SHP-Q1")
        for index in range(2):
            self.linked(small, f"19000{index}")

        large = self.shipment("SHP-Q2")
        for index in range(12):
            container = make_container(self.team, f"1910{index:02d}")
            ShipmentContainer.objects.create(shipment=large, container=container, sequence=index)

        with CaptureQueriesContext(connection) as few:
            get_shipment_arrival_progress(self.team, small)
        with CaptureQueriesContext(connection) as many:
            get_shipment_arrival_progress(self.team, large)

        self.assertEqual(len(few.captured_queries), len(many.captured_queries))
