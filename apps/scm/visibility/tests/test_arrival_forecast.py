"""The Control Tower's arrival forecast: unique outstanding containers, by period and week.

The forecast adds no rule of its own about arrival or ETA. A container is counted
while its own lifecycle says it is outstanding, on its object's ``current_eta``, in
the windows :func:`~apps.scm.visibility.selectors.filter_by_eta_window` already
defines — so these tests are mostly about counting: containers rather than rows, once
rather than twice, and never in a period that cannot hold them.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.test import SimpleTestCase
from django.urls import reverse

from apps.scm.containers.choices import MovementType
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.visibility.selectors import list_visibility_objects
from apps.scm.visibility.work_queues import get_arrival_forecast, parse_arrival_queue_filters
from apps.scm.visibility.work_queues.arrivals import weekly_arrivals

from .arrival_scenarios import ArrivalScenarioTestCase, make_container
from .factories import make_user_and_team


class ArrivalForecastTest(ArrivalScenarioTestCase):
    def forecast(self, team=None):
        return get_arrival_forecast(list_visibility_objects(team or self.team))

    def test_outstanding_containers_are_counted(self):
        shipment = self.shipment("SHP-OUT", eta_days=3)
        self.linked(shipment, "310001")
        self.linked(shipment, "310002", sequence=1)
        self.assertEqual(self.forecast().next_7_days, 2)

    def test_received_containers_are_not_counted(self):
        self.arrive(self.linked(self.shipment("SHP-RECV", eta_days=3), "310003"), MovementType.RECEIVED)
        self.assertEqual(self.forecast().next_63_days, 0)

    def test_a_partly_arrived_shipment_counts_only_what_is_still_coming(self):
        shipment = self.shipment("SHP-PART", eta_days=3)
        self.arrive(self.linked(shipment, "310004"), MovementType.RECEIVED)
        self.arrive(self.linked(shipment, "310005", sequence=1))
        self.linked(shipment, "310006", sequence=2)
        self.assertEqual(self.forecast().next_7_days, 1)

    def test_a_container_on_two_active_shipments_is_counted_once_on_its_current_one(self):
        """The container's most recent shipment decides — not whichever ETA is earliest."""
        container = make_container(self.team, "310007")
        ShipmentContainer.objects.create(shipment=self.shipment("SHP-OLD", eta_days=3), container=container)
        ShipmentContainer.objects.create(shipment=self.shipment("SHP-NEW", eta_days=20), container=container)
        forecast = self.forecast()
        self.assertEqual(forecast.next_7_days, 0)
        self.assertEqual(forecast.next_28_days, 1)
        self.assertEqual(forecast.ambiguous, 0)

    def test_a_container_whose_current_shipment_is_none_of_them_is_reported_not_counted(self):
        container = make_container(self.team, "310008")
        ShipmentContainer.objects.create(shipment=self.shipment("SHP-A", eta_days=3), container=container)
        ShipmentContainer.objects.create(shipment=self.shipment("SHP-B", eta_days=5), container=container)
        draft = Shipment.objects.create(team=self.team, shipment_number="SHP-DRAFT", status=Shipment.Status.DRAFT)
        ShipmentContainer.objects.create(shipment=draft, container=container)
        forecast = self.forecast()
        self.assertEqual(forecast.next_63_days, 0)
        self.assertEqual(forecast.ambiguous, 1)

    def test_containers_without_an_eta_are_counted_apart(self):
        shipment = self.shipment("SHP-NOETA")
        Shipment.objects.filter(pk=shipment.pk).update(eta=None, original_eta=None)
        self.linked(shipment, "310009")
        forecast = self.forecast()
        self.assertEqual(forecast.no_eta, 1)
        self.assertEqual(forecast.next_63_days, 0)
        self.assertEqual(sum(week.count for week in forecast.weekly_arrivals), 0)

    def test_overdue_containers_are_counted_apart(self):
        self.linked(self.shipment("SHP-LATE", eta_days=-2), "310010")
        forecast = self.forecast()
        self.assertEqual(forecast.overdue, 1)
        self.assertEqual(forecast.next_7_days, 0)
        self.assertEqual(sum(week.count for week in forecast.weekly_arrivals), 0)

    def test_the_periods_are_cumulative(self):
        for serial, days in (("310011", 0), ("310012", 10), ("310013", 20), ("310014", 50), ("310015", 70)):
            self.linked(self.shipment(f"SHP-{days}", eta_days=days), serial)
        forecast = self.forecast()
        self.assertEqual(
            [count for _days, count in forecast.periods],
            [1, 2, 3, 4],
        )

    def test_a_shipment_without_containers_is_reported_and_not_counted(self):
        self.shipment("SHP-EMPTY", eta_days=3)
        forecast = self.forecast()
        self.assertEqual(forecast.shipments_without_containers, 1)
        self.assertEqual(forecast.next_7_days, 0)

    def test_another_teams_containers_are_not_counted(self):
        self.linked(self.shipment("SHP-MINE", eta_days=3), "310016")
        _user, other_team = make_user_and_team("forecast-other@example.com", "forecast-other")
        self.assertEqual(self.forecast(other_team).next_7_days, 0)
        self.assertEqual(self.forecast().next_7_days, 1)


class ArrivalForecastOnTheControlTowerTest(ArrivalScenarioTestCase):
    """The forecast on the page: the team's whole inbound population, whatever the view."""

    def setUp(self):
        self.client.force_login(self.user)
        # Untracked, so the board's default Tracking view does not list it.
        self.linked(self.shipment("SHP-UNTRACKED", eta_days=3), "320001")

    def test_the_forecast_does_not_follow_the_selected_view(self):
        url = reverse("visibility:overview")
        for params in ({}, {"view": "tracking"}, {"view": "exceptions"}, {"view": "delayed"}, {"search": "nothing"}):
            with self.subTest(params=params):
                response = self.client.get(url, params)
                self.assertEqual(response.context["forecast"].next_7_days, 1)

    def test_the_htmx_board_carries_the_forecast(self):
        response = self.client.get(reverse("visibility:overview"), {"view": "delayed"}, headers={"hx-request": "true"})
        self.assertEqual(response.context["forecast"].next_7_days, 1)
        self.assertContains(response, "Arrival timeline")

    def test_each_period_opens_the_arrivals_queue_for_that_window(self):
        response = self.client.get(reverse("visibility:overview"))
        for days in ("7", "14", "28", "63"):
            with self.subTest(days=days):
                self.assertContains(response, f'href="{reverse("visibility:arrivals")}?window={days}"')
                self.assertEqual(parse_arrival_queue_filters({"window": days}).window, days)


class WeeklyArrivalsTest(SimpleTestCase):
    """Calendar weeks, Monday to Sunday, starting with today's."""

    def test_nine_consecutive_iso_weeks_starting_this_week(self):
        weeks = weekly_arrivals([], date(2026, 10, 28))  # a Wednesday in W44
        self.assertEqual([week.iso_week for week in weeks], [44, 45, 46, 47, 48, 49, 50, 51, 52])
        self.assertEqual(weeks[0].monday, date(2026, 10, 26))
        self.assertEqual(weeks[0].sunday, date(2026, 11, 1))
        self.assertTrue(all(b.monday - a.monday == timedelta(weeks=1) for a, b in zip(weeks, weeks[1:], strict=False)))

    def test_weeks_break_between_sunday_and_monday(self):
        weeks = weekly_arrivals([date(2026, 11, 1), date(2026, 11, 2)], date(2026, 10, 28))
        self.assertEqual([week.count for week in weeks[:2]], [1, 1])

    def test_weekly_counts_are_not_cumulative(self):
        weeks = weekly_arrivals([date(2026, 10, 29), date(2026, 11, 10), date(2026, 11, 11)], date(2026, 10, 28))
        self.assertEqual([week.count for week in weeks], [1, 0, 2, 0, 0, 0, 0, 0, 0])
        self.assertEqual([week.percent for week in weeks[:3]], [50, 0, 100])

    def test_the_year_end_runs_through_week_53_into_week_1(self):
        weeks = weekly_arrivals([date(2027, 1, 4)], date(2026, 12, 30))
        self.assertEqual([week.iso_week for week in weeks], [53, 1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(weeks[1].monday, date(2027, 1, 4))
        self.assertEqual(weeks[1].count, 1)

    def test_earlier_this_week_is_overdue_not_this_weeks_arrival(self):
        weeks = weekly_arrivals([date(2026, 10, 26)], date(2026, 10, 28))
        self.assertEqual(weeks[0].count, 0)

    def test_beyond_nine_weeks_is_not_shown(self):
        weeks = weekly_arrivals([date(2027, 1, 1)], date(2026, 10, 28))
        self.assertEqual(sum(week.count for week in weeks), 0)
