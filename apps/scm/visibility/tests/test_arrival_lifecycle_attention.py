"""The overdue arrival as an operational signal, and the Control Tower's arrival numbers.

Two things are being protected.

**One attention architecture.** "The ETA passed and nothing arrived at the
destination" joins the existing issue vocabulary beside the exception engine's codes
and the delay engine's verdict. It is not a second exception engine, it carries no
invented severity, and an object with several findings is still one row to work.

**It is only raised where it was actually checked.** Without a canonical destination
there is nowhere for the box to have failed to arrive, so the absence of a movement
is a gap in the data rather than a finding about the freight. Saying "not arrived"
there would be a claim the platform cannot support.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.choices import MovementType
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.visibility.selectors import get_visibility_overview
from apps.scm.visibility.work_queues import (
    ARRIVAL_OVERDUE_ISSUE,
    ExceptionQueueFilters,
    get_exception_queue,
)

from .arrival_scenarios import ArrivalScenarioTestCase, make_container
from .factories import make_user_and_team


class AttentionTestCase(ArrivalScenarioTestCase):
    def items(self) -> dict:
        return {item.object.label: item for item in get_exception_queue(self.team).items}

    def types(self, label: str) -> list[str]:
        item = self.items().get(label)
        return item.issue_types if item is not None else []


class OverdueArrivalIssueTest(AttentionTestCase):
    """The lifecycle's finding reaches the queue everything else reaches."""

    def test_an_overdue_arrival_raises_the_issue(self):
        self.linked(self.shipment("SHP-ATT", eta_days=-3), "220001")

        self.assertIn(ARRIVAL_OVERDUE_ISSUE, self.types("SHP-ATT"))

    def test_the_issue_names_the_place_it_checked(self):
        self.linked(self.shipment("SHP-ATT2", eta_days=-3), "220002")

        detail = next(
            issue.detail for issue in self.items()["SHP-ATT2"].issues if issue.issue_type == ARRIVAL_OVERDUE_ISSUE
        )

        self.assertIn("Oceanterminalen", detail)

    def test_the_issue_counts_the_containers_still_missing(self):
        shipment = self.shipment("SHP-ATT7", eta_days=-3)
        first = self.linked(shipment, "220008", sequence=0)
        self.linked(shipment, "220009", sequence=1)
        self.linked(shipment, "220010", sequence=2)
        self.arrive(first)

        detail = next(
            issue.detail for issue in self.items()["SHP-ATT7"].issues if issue.issue_type == ARRIVAL_OVERDUE_ISSUE
        )

        self.assertIn("2 containers", detail)

    def test_an_arrived_shipment_raises_no_arrival_issue(self):
        self.arrive(self.linked(self.shipment("SHP-ATT3", eta_days=-3), "220003"))

        self.assertNotIn(ARRIVAL_OVERDUE_ISSUE, self.types("SHP-ATT3"))

    def test_a_shipment_with_no_canonical_destination_raises_no_arrival_issue(self):
        """Nothing was checked, so "not arrived" would be a claim rather than a finding."""
        self.linked(self.shipment("SHP-ATT4", eta_days=-3, canonical=False), "220004")

        self.assertNotIn(ARRIVAL_OVERDUE_ISSUE, self.types("SHP-ATT4"))

    def test_a_future_eta_raises_no_arrival_issue(self):
        self.linked(self.shipment("SHP-ATT8", eta_days=4), "220011")

        self.assertNotIn(ARRIVAL_OVERDUE_ISSUE, self.types("SHP-ATT8"))

    def test_the_issue_can_be_filtered_for_like_any_other(self):
        self.linked(self.shipment("SHP-ATT5", eta_days=-3), "220005")
        self.arrive(self.linked(self.shipment("SHP-ATT5B", eta_days=-3), "220007"))

        queue = get_exception_queue(self.team, ExceptionQueueFilters(issue=ARRIVAL_OVERDUE_ISSUE))

        self.assertEqual([item.object.label for item in queue.items], ["SHP-ATT5"])

    def test_an_object_appears_once_however_many_findings_it_has(self):
        self.linked(self.shipment("SHP-ATT6", eta_days=-3), "220006")

        labels = [item.object.label for item in get_exception_queue(self.team).items]

        self.assertEqual(labels.count("SHP-ATT6"), 1)

    def test_the_issue_carries_no_severity(self):
        """LOC-3 adds an operational fact, not a High/Medium/Low the domain cannot defend."""
        self.linked(self.shipment("SHP-ATT9", eta_days=-3), "220012")

        item = self.items()["SHP-ATT9"]

        self.assertNotIn("high", [issue.band for issue in item.issues])
        self.assertEqual({issue.band for issue in item.issues}, {"delay"})

    def test_the_page_shows_the_finding(self):
        self.linked(self.shipment("SHP-ATT10", eta_days=-3), "220013")

        client = Client()
        client.force_login(self.user)

        self.assertContains(client.get(reverse("visibility:exceptions")), "Arrival overdue")


class ControlTowerTest(AttentionTestCase):
    """The arrival numbers are the lifecycle's, not an ETA comparison of their own."""

    def test_arriving_soon_excludes_what_has_already_arrived(self):
        self.linked(self.shipment("SHP-COMING", eta_days=2), "230001")
        self.arrive(self.linked(self.shipment("SHP-HERE", eta_days=2), "230002"))

        labels = [obj.label for obj in get_visibility_overview(self.team).arriving_soon]

        self.assertIn("SHP-COMING", labels)
        self.assertNotIn("SHP-HERE", labels)

    def test_awaiting_receipt_counts_what_landed_and_was_not_received(self):
        self.arrive(self.linked(self.shipment("SHP-WAIT", eta_days=2), "230003"))
        self.arrive(self.linked(self.shipment("SHP-BOOKED", eta_days=2), "230004"), MovementType.RECEIVED)

        labels = [obj.label for obj in get_visibility_overview(self.team).awaiting_receipt]

        self.assertEqual(labels, ["SHP-WAIT"])

    def test_overdue_arrivals_counts_what_nothing_delivered(self):
        self.linked(self.shipment("SHP-OD", eta_days=-2), "230005")
        self.arrive(self.linked(self.shipment("SHP-OD2", eta_days=-2), "230006"))

        labels = [obj.label for obj in get_visibility_overview(self.team).overdue_arrivals]

        self.assertEqual(labels, ["SHP-OD"])

    def test_the_board_shows_the_awaiting_receipt_number(self):
        self.arrive(self.linked(self.shipment("SHP-BOARD", eta_days=2), "230007"))

        client = Client()
        client.force_login(self.user)

        self.assertContains(client.get(reverse("visibility:overview")), "Awaiting receipt")

    def test_the_awaiting_receipt_card_links_into_the_arrivals_queue(self):
        self.arrive(self.linked(self.shipment("SHP-BOARD2", eta_days=2), "230008"))

        client = Client()
        client.force_login(self.user)
        html = client.get(reverse("visibility:overview")).content.decode()

        self.assertIn(f"{reverse('visibility:arrivals')}?window=30&amp;state=arrived", html)


class TenantIsolationTest(AttentionTestCase):
    """No team's arrival numbers may include another's."""

    def test_the_control_tower_only_counts_this_teams_overdue_arrivals(self):
        self.linked(self.shipment("SHP-MINE", eta_days=-2), "240001")

        _other_user, other_team = make_user_and_team("attother@example.com", "att-other")
        overdue = timezone.localdate() - timedelta(days=2)
        theirs = Shipment.objects.create(
            team=other_team,
            shipment_number="SHP-THEIRS",
            status=Shipment.Status.IN_TRANSIT,
            eta=overdue,
            original_eta=overdue,
        )
        ShipmentContainer.objects.create(shipment=theirs, container=make_container(other_team, "240002"), sequence=0)

        labels = [obj.label for obj in get_visibility_overview(self.team).overdue_arrivals]

        self.assertEqual(labels, ["SHP-MINE"])

    def test_another_teams_overdue_arrival_raises_no_issue_here(self):
        _other_user, other_team = make_user_and_team("attother2@example.com", "att-other2")
        overdue = timezone.localdate() - timedelta(days=2)
        theirs = Shipment.objects.create(
            team=other_team,
            shipment_number="SHP-THEIRS2",
            status=Shipment.Status.IN_TRANSIT,
            destination_location=self.terminal,
            eta=overdue,
            original_eta=overdue,
        )
        ShipmentContainer.objects.create(shipment=theirs, container=make_container(other_team, "240003"), sequence=0)

        self.assertNotIn("SHP-THEIRS2", self.items())
