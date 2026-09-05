"""What the lifecycle changes about the Expected Arrivals queue.

The interpreter is tested in ``test_arrival_lifecycle``; this is about the queue
reading that one answer instead of a rule of its own:

* Expected Arrivals is what is *outstanding*. Arrived and received rows leave it,
  and can be found again by asking for them explicitly.
* A part-arrived shipment stays, because the box nobody has seen is the one worth
  planning for.
* A shipment with no canonical destination stays too: nothing can be shown to have
  arrived, and dropping it would lose a real expectation.
"""

from __future__ import annotations

from apps.scm.containers.choices import MovementType
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.visibility.arrival_lifecycle import ArrivalState
from apps.scm.visibility.selectors import get_visibility_overview
from apps.scm.visibility.work_queues import (
    ArrivalQueueFilters,
    get_arrival_queue,
    parse_arrival_queue_filters,
)

from .arrival_scenarios import ArrivalScenarioTestCase, make_container


class QueueTestCase(ArrivalScenarioTestCase):
    """The shared hierarchy, plus the one question these tests ask of it."""

    def numbers(self, **params) -> set[str]:
        queue = get_arrival_queue(self.team, parse_arrival_queue_filters(params))
        return {obj.label for obj in queue.objects}


class ExpectedQueueIsOutstandingTest(QueueTestCase):
    """The queue is what is still coming, decided by the lifecycle and nothing else."""

    def test_an_expected_shipment_is_on_the_queue(self):
        self.linked(self.shipment("SHP-EXP"), "200001")
        self.assertIn("SHP-EXP", self.numbers(window="30"))

    def test_an_arriving_shipment_is_on_the_queue(self):
        self.linked(self.shipment("SHP-ARV", eta_days=1), "200002")

        queue = get_arrival_queue(self.team, ArrivalQueueFilters(window="30"))
        states = {obj.label: obj.arrival_state for obj in queue.objects}
        self.assertEqual(states["SHP-ARV"], ArrivalState.ARRIVING)

    def test_an_arrived_shipment_leaves_the_queue(self):
        self.arrive(self.linked(self.shipment("SHP-IN"), "200003"))
        self.assertNotIn("SHP-IN", self.numbers(window="30"))

    def test_a_received_shipment_leaves_the_queue(self):
        self.arrive(self.linked(self.shipment("SHP-RECV"), "200004"), MovementType.RECEIVED)
        self.assertNotIn("SHP-RECV", self.numbers(window="30"))

    def test_a_partly_arrived_shipment_stays(self):
        """Nineteen in and one outstanding is still an arrival to work."""
        shipment = self.shipment("SHP-PART")
        first = self.linked(shipment, "200005")
        second = make_container(self.team, "200006")
        ShipmentContainer.objects.create(shipment=shipment, container=second, sequence=1)

        self.arrive(first)

        self.assertIn("SHP-PART", self.numbers(window="30"))

    def test_a_shipment_with_no_canonical_destination_stays(self):
        """Nothing can be shown to have arrived, so a real expectation is not dropped."""
        self.linked(self.shipment("SHP-TEXT", canonical=False), "200007")
        self.assertIn("SHP-TEXT", self.numbers(window="30"))

    def test_an_arrived_shipment_can_be_found_by_asking_for_it(self):
        self.arrive(self.linked(self.shipment("SHP-FIND"), "200008"))

        self.assertIn("SHP-FIND", self.numbers(window="30", state=ArrivalState.ARRIVED))

    def test_a_received_shipment_can_be_found_by_asking_for_it(self):
        self.arrive(self.linked(self.shipment("SHP-FIND2"), "200009"), MovementType.RECEIVED)

        self.assertIn("SHP-FIND2", self.numbers(window="30", state=ArrivalState.RECEIVED))

    def test_asking_for_arrived_does_not_also_show_the_expected(self):
        self.linked(self.shipment("SHP-STILL"), "200010")
        self.arrive(self.linked(self.shipment("SHP-DONE"), "200011"))

        found = self.numbers(window="30", state=ArrivalState.ARRIVED)
        self.assertIn("SHP-DONE", found)
        self.assertNotIn("SHP-STILL", found)

    def test_filtering_by_expected_narrows_within_the_outstanding_queue(self):
        self.linked(self.shipment("SHP-SOON", eta_days=1), "200012")
        self.linked(self.shipment("SHP-LATER", eta_days=5), "200013")

        self.assertEqual(self.numbers(window="30", state=ArrivalState.ARRIVING), {"SHP-SOON"})
        self.assertEqual(self.numbers(window="30", state=ArrivalState.EXPECTED), {"SHP-LATER"})

    def test_an_unrecognised_state_narrows_nothing(self):
        self.linked(self.shipment("SHP-ANY"), "200014")
        filters = parse_arrival_queue_filters({"state": "landed"})

        self.assertEqual(filters.state, "")
        self.assertIn("SHP-ANY", self.numbers(window="30", state="landed"))

    def test_a_chosen_state_marks_the_filter_state_active(self):
        self.assertTrue(ArrivalQueueFilters(state=ArrivalState.ARRIVED).is_active)
        self.assertTrue(ArrivalQueueFilters(state=ArrivalState.ARRIVED).has_narrowing_filters)


class OverdueQueueTest(QueueTestCase):
    """An overdue arrival stays on the queue and says so."""

    def test_an_overdue_shipment_is_in_the_overdue_window_and_flagged(self):
        self.linked(self.shipment("SHP-LATE", eta_days=-4), "210001")

        queue = get_arrival_queue(self.team, ArrivalQueueFilters(window="overdue"))
        overdue = {obj.label: obj for obj in queue.objects}

        self.assertIn("SHP-LATE", overdue)
        self.assertTrue(overdue["SHP-LATE"].is_arrival_overdue)
        self.assertEqual(queue.overdue_count, 1)

    def test_an_overdue_shipment_that_arrived_is_gone_from_the_queue(self):
        self.arrive(self.linked(self.shipment("SHP-LATEIN", eta_days=-4), "210002"))

        queue = get_arrival_queue(self.team, ArrivalQueueFilters(window="overdue"))
        self.assertEqual([obj.label for obj in queue.objects], [])


class QueryBehaviourTest(QueueTestCase):
    """The lifecycle costs the queue a fixed number of queries."""

    def test_the_query_count_does_not_grow_with_the_number_of_arrivals(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for index in range(6):
            shipment = self.shipment(f"SHP-Q{index}", eta_days=index % 5)
            self.linked(shipment, f"25000{index}")

        with CaptureQueriesContext(connection) as many:
            self.assertEqual(get_arrival_queue(self.team, ArrivalQueueFilters(window="30")).total, 6)

        Shipment.objects.filter(shipment_number__in=["SHP-Q3", "SHP-Q4", "SHP-Q5"]).delete()
        with CaptureQueriesContext(connection) as fewer:
            get_arrival_queue(self.team, ArrivalQueueFilters(window="30"))

        self.assertEqual(len(many.captured_queries), len(fewer.captured_queries))

    def test_the_lifecycle_adds_a_bounded_number_of_queries_to_the_control_tower(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for index in range(4):
            self.linked(self.shipment(f"SHP-CT{index}"), f"26000{index}")

        with CaptureQueriesContext(connection) as few:
            get_visibility_overview(self.team)

        for index in range(4, 10):
            self.linked(self.shipment(f"SHP-CT{index}"), f"26000{index}")

        with CaptureQueriesContext(connection) as more:
            get_visibility_overview(self.team)

        self.assertEqual(len(few.captured_queries), len(more.captured_queries))
