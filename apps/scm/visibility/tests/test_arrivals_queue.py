"""The Arrivals work queue: the window, the grouping, and what it refuses to invent.

Three things are worth protecting here.

* **The window is the page.** Arrivals defaults to the next seven days and reuses
  the Control Tower's own window rule, so "the next seven days" cannot come to mean
  two different things in two places. A hand-edited window falls back to the
  default rather than quietly showing everything.

* **Shipment and standalone container are both first-class.** A shipment says how
  many boxes it carries; a container tracked on its own says what kind of box it is.
  No shipment is invented to make a lone container fit a row shape.

* **Nothing is given a date, a time or a destination it does not have.** The ETA the
  domain holds is a date, an hour exists only where a carrier forecast supplied one,
  and where there is no booking there is no destination — the box's current location
  is not promoted into one.

Dates are all relative to ``timezone.localdate()`` at test time, so the suite does
not drift into failing on a particular calendar day.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.models import Container
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingSubscription
from apps.scm.visibility.read_models import Health, ObjectKind
from apps.scm.visibility.work_queues import (
    ARRIVAL_WINDOWS,
    DEFAULT_ARRIVAL_WINDOW,
    ArrivalQueueFilters,
    get_arrival_queue,
    parse_arrival_queue_filters,
)

from .factories import equipment_type, make_provider, make_user_and_team


def _container(team, serial: str, *, iso: str = "22G1", description: str = "20' GP") -> Container:
    from apps.scm.containers.models import EquipmentType

    equipment = (
        equipment_type()
        if iso == "22G1"
        else EquipmentType.objects.get_or_create(
            iso_code=iso,
            defaults={"category": "GP", "length_ft": 40, "high_cube": True, "description": description},
        )[0]
    )
    return Container.objects.create(
        team=team,
        owner_code="MSK",
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit("MSK", "U", serial),
        equipment_type=equipment,
    )


def _tracked(team, container, provider: TrackingProvider, shipment=None) -> TrackingSubscription:
    """A live watch, which is what makes a container visible on its own."""
    return TrackingSubscription.objects.create(
        team=team,
        provider=provider,
        container=container,
        shipment=shipment,
        tracking_reference=container.container_id,
        status=TrackingSubscription.Status.ACTIVE,
        tracking_status=TrackingSubscription.TrackingStatus.TRACKING,
    )


class ArrivalWindowTest(TestCase):
    """Which objects the window lets through."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("arr@example.com", "arr-team")
        today = timezone.localdate()
        cls.days = {
            "today": today,
            "soon": today + timedelta(days=3),
            "far": today + timedelta(days=20),
            "beyond": today + timedelta(days=90),
            "past": today - timedelta(days=4),
        }
        for name, day in cls.days.items():
            Shipment.objects.create(
                team=cls.team,
                shipment_number=f"SHP-{name.upper()}",
                carrier="CMA CGM",
                status=Shipment.Status.IN_TRANSIT,
                destination_port="Gothenburg",
                eta=day,
                original_eta=day,
            )

    def labels(self, **params):
        queue = get_arrival_queue(self.team, parse_arrival_queue_filters(params))
        return [obj.label for obj in queue.objects]

    def test_the_default_window_is_the_next_seven_days(self):
        self.assertEqual(DEFAULT_ARRIVAL_WINDOW, "7")
        self.assertEqual(set(self.labels()), {"SHP-TODAY", "SHP-SOON"})

    def test_the_today_window_is_only_today(self):
        self.assertEqual(self.labels(window="today"), ["SHP-TODAY"])

    def test_the_thirty_day_window_reaches_further(self):
        self.assertEqual(set(self.labels(window="30")), {"SHP-TODAY", "SHP-SOON", "SHP-FAR"})

    def test_objects_beyond_the_window_are_excluded(self):
        self.assertNotIn("SHP-BEYOND", self.labels(window="30"))

    def test_a_past_eta_is_not_in_a_forward_window(self):
        """An overdue arrival is not "arriving in the next seven days"."""
        self.assertNotIn("SHP-PAST", self.labels(window="7"))

    def test_the_overdue_window_shows_what_has_slipped(self):
        self.assertEqual(self.labels(window="overdue"), ["SHP-PAST"])

    def test_an_unrecognised_window_falls_back_to_the_default(self):
        """A hand-edited URL must not turn a planning view into a full list."""
        filters = parse_arrival_queue_filters({"window": "everything"})
        self.assertEqual(filters.window, DEFAULT_ARRIVAL_WINDOW)

    def test_a_missing_window_falls_back_to_the_default(self):
        self.assertEqual(parse_arrival_queue_filters({}).window, DEFAULT_ARRIVAL_WINDOW)

    def test_the_overdue_window_says_so_on_the_page(self):
        client = Client()
        client.force_login(self.user)
        self.assertContains(client.get(reverse("visibility:arrivals"), {"window": "overdue"}), "overdue")

    def test_every_offered_window_is_one_the_parser_accepts(self):
        for value, _label in ARRIVAL_WINDOWS:
            with self.subTest(window=value):
                self.assertEqual(parse_arrival_queue_filters({"window": value}).window, value)


class ArrivalGroupingTest(TestCase):
    """Days, in order, and what each day says about itself."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("arg@example.com", "arg-team")
        today = timezone.localdate()
        cls.today_shipment = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-NOW",
            carrier="CMA CGM",
            status=Shipment.Status.IN_TRANSIT,
            destination_port="Gothenburg",
            eta=today,
            original_eta=today,
        )
        cls.tomorrow_shipment = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-NEXT",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            destination_port="Rotterdam",
            eta=today + timedelta(days=1),
            original_eta=today + timedelta(days=1),
        )
        for index in range(3):
            ShipmentContainer.objects.create(
                shipment=cls.today_shipment, container=_container(cls.team, f"20000{index}")
            )
        ShipmentContainer.objects.create(shipment=cls.tomorrow_shipment, container=_container(cls.team, "300001"))

    def groups(self, **params):
        return get_arrival_queue(self.team, parse_arrival_queue_filters(params)).groups

    def test_arrivals_are_grouped_by_day(self):
        self.assertEqual(len(self.groups()), 2)

    def test_the_earliest_day_comes_first(self):
        days = [group.day for group in self.groups()]
        self.assertEqual(days, sorted(days))

    def test_the_first_two_days_are_named(self):
        first, second = self.groups()
        self.assertEqual(first.label, "Today")
        self.assertEqual(second.label, "Tomorrow")
        self.assertTrue(first.is_today)
        self.assertTrue(second.is_tomorrow)

    def test_a_later_day_is_shown_as_a_date_rather_than_named(self):
        Shipment.objects.create(
            team=self.team,
            shipment_number="SHP-LATER",
            carrier="MSC",
            status=Shipment.Status.IN_TRANSIT,
            eta=timezone.localdate() + timedelta(days=5),
            original_eta=timezone.localdate() + timedelta(days=5),
        )
        later = self.groups()[-1]
        self.assertEqual(later.label, "")
        self.assertFalse(later.is_today)
        self.assertFalse(later.is_tomorrow)

    def test_a_day_counts_the_containers_arriving_on_it(self):
        self.assertEqual(self.groups()[0].container_count, 3)

    def test_a_past_day_knows_it_is_overdue(self):
        Shipment.objects.create(
            team=self.team,
            shipment_number="SHP-GONE",
            carrier="MSC",
            status=Shipment.Status.IN_TRANSIT,
            eta=timezone.localdate() - timedelta(days=2),
            original_eta=timezone.localdate() - timedelta(days=2),
        )
        self.assertTrue(self.groups(window="overdue")[0].is_overdue)

    def test_the_queue_counts_objects_and_containers_separately(self):
        queue = get_arrival_queue(self.team)
        self.assertEqual(queue.total, 2)
        self.assertEqual(queue.container_count, 4)


class ShipmentAndContainerTest(TestCase):
    """Both kinds of arrival, each described in its own terms."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("arb@example.com", "arb-team")
        today = timezone.localdate()
        provider = make_provider()

        cls.shipment = Shipment.objects.create(
            team=cls.team,
            shipment_number="SH-260081",
            carrier="CMA CGM",
            status=Shipment.Status.IN_TRANSIT,
            origin_port="Shanghai",
            destination_port="Gothenburg",
            eta=today + timedelta(days=2),
            original_eta=today + timedelta(days=2),
        )
        for index in range(12):
            container = _container(cls.team, f"40000{index}" if index < 10 else f"4000{index}")
            ShipmentContainer.objects.create(shipment=cls.shipment, container=container)

        # A box tracked on its own, whose only shipment is finished — so it stands
        # alone on the queue but still knows where it was booked to.
        cls.finished = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-DONE",
            carrier="CMA CGM",
            status=Shipment.Status.DELIVERED,
            origin_port="Shanghai",
            destination_port="Gothenburg",
            eta=today + timedelta(days=4),
            original_eta=today + timedelta(days=4),
        )
        cls.standalone = _container(cls.team, "500001", iso="45G1", description="40' High Cube")
        ShipmentContainer.objects.create(shipment=cls.finished, container=cls.standalone)
        _tracked(cls.team, cls.standalone, provider, shipment=cls.finished)

        # And a box with no shipment at all, so it has no destination to show.
        cls.orphan = _container(cls.team, "600001")
        _tracked(cls.team, cls.orphan, provider)
        # A carrier forecast, which is the only thing that gives a lone box an ETA —
        # and the only thing that gives any arrival an hour rather than just a date.
        cls.orphan_eta_at = timezone.now() + timedelta(days=3)
        TrackingEvent.objects.create(
            team=cls.team,
            provider=provider,
            container=cls.orphan,
            event_type=TrackingEvent.EventType.ETA_UPDATED,
            event_time_type=TrackingEvent.EventTimeType.ESTIMATED,
            event_datetime=cls.orphan_eta_at,
            location_name="Singapore",
        )

    def objects(self, **params):
        return {obj.label: obj for obj in get_arrival_queue(self.team, parse_arrival_queue_filters(params)).objects}

    def test_a_shipment_arrival_carries_its_container_count(self):
        shipment = self.objects(window="30")["SH-260081"]
        self.assertEqual(shipment.kind, ObjectKind.SHIPMENT)
        self.assertEqual(shipment.container_count, 12)

    def test_a_standalone_container_is_its_own_arrival(self):
        standalone = self.objects(window="30")[self.standalone.container_id]
        self.assertEqual(standalone.kind, ObjectKind.CONTAINER)
        self.assertEqual(standalone.container_count, 1)

    def test_a_standalone_container_keeps_the_destination_it_was_booked_to(self):
        standalone = self.objects(window="30")[self.standalone.container_id]
        self.assertEqual(standalone.destination, "Gothenburg")
        self.assertEqual(standalone.origin, "Shanghai")

    def test_a_container_with_no_shipment_has_no_destination_rather_than_a_guess(self):
        """It is in Singapore. That is where it is, not where it is going."""
        orphan = self.objects(window="30")[self.orphan.container_id]
        self.assertEqual(orphan.destination, "")
        self.assertEqual(orphan.route_label, "")

    def test_the_shipment_eta_is_the_one_shown(self):
        self.assertEqual(self.objects(window="30")["SH-260081"].current_eta, self.shipment.eta)

    def test_a_shipment_arrival_shows_a_date_and_no_invented_hour(self):
        """The booked ETA is a date. Printing a time for it would be made up."""
        shipment = self.objects(window="30")["SH-260081"]
        self.assertEqual(shipment.eta_source, "shipment")
        self.assertIsNone(shipment.current_eta_at)

    def test_a_carrier_forecast_keeps_the_hour_it_came_with(self):
        orphan = self.objects(window="30")[self.orphan.container_id]
        self.assertEqual(orphan.eta_source, "tracking")
        self.assertEqual(orphan.current_eta_at, self.orphan_eta_at)

    def test_the_page_describes_a_shipment_by_its_boxes_and_a_container_by_its_type(self):
        client = Client()
        client.force_login(self.user)
        html = client.get(reverse("visibility:arrivals"), {"window": "30"}).content.decode()
        self.assertIn("12 containers", html)
        self.assertIn("40&#x27; High Cube", html)


class ArrivalHealthTest(TestCase):
    """Health is the visibility layer's own, unchanged and explicit about not knowing."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("arh@example.com", "arh-team")
        today = timezone.localdate()
        cls.on_time = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-OK",
            carrier="MSC",
            status=Shipment.Status.IN_TRANSIT,
            eta=today + timedelta(days=2),
            original_eta=today + timedelta(days=2),
        )
        cls.delayed = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-SLIP",
            carrier="MSC",
            status=Shipment.Status.IN_TRANSIT,
            eta=today + timedelta(days=5),
            original_eta=today + timedelta(days=1),
        )
        cls.excepted = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-HOLD",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            eta=today + timedelta(days=3),
            original_eta=today + timedelta(days=3),
        )
        container = _container(cls.team, "700001")
        ShipmentContainer.objects.create(shipment=cls.excepted, container=container)
        TrackingEvent.objects.create(
            team=cls.team,
            provider=make_provider(),
            shipment=cls.excepted,
            container=container,
            event_type=TrackingEvent.EventType.CUSTOMS_HOLD,
            event_datetime=timezone.now() - timedelta(hours=3),
            location_name="Rotterdam",
        )

    def health(self, **params):
        queue = get_arrival_queue(self.team, parse_arrival_queue_filters(params))
        return {obj.label: obj.health for obj in queue.objects}

    def test_an_untroubled_arrival_is_on_time(self):
        self.assertEqual(self.health()["SHP-OK"], Health.ON_TIME)

    def test_a_slipped_arrival_stays_delayed_on_this_page(self):
        self.assertEqual(self.health()["SHP-SLIP"], Health.DELAYED)

    def test_an_excepted_arrival_stays_an_exception_on_this_page(self):
        self.assertEqual(self.health()["SHP-HOLD"], Health.EXCEPTION)

    def test_filtering_by_health(self):
        self.assertEqual(list(self.health(health=Health.DELAYED)), ["SHP-SLIP"])

    def test_the_page_offers_no_invented_condition(self):
        client = Client()
        client.force_login(self.user)
        html = client.get(reverse("visibility:arrivals")).content.decode().lower()
        self.assertNotIn("at risk", html)


class ArrivalFilterTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("arf@example.com", "arf-team")
        today = timezone.localdate()
        for number, carrier, destination in (
            ("SHP-GOT", "CMA CGM", "Gothenburg"),
            ("SHP-ROT", "Maersk", "Rotterdam"),
        ):
            Shipment.objects.create(
                team=cls.team,
                shipment_number=number,
                carrier=carrier,
                status=Shipment.Status.IN_TRANSIT,
                destination_port=destination,
                eta=today + timedelta(days=2),
                original_eta=today + timedelta(days=2),
            )

    def labels(self, **params):
        queue = get_arrival_queue(self.team, parse_arrival_queue_filters(params))
        return [obj.label for obj in queue.objects]

    def test_filtering_by_destination(self):
        self.assertEqual(self.labels(destination="Rotterdam"), ["SHP-ROT"])

    def test_filtering_by_carrier(self):
        self.assertEqual(self.labels(carrier="CMA CGM"), ["SHP-GOT"])

    def test_filtering_by_object_type(self):
        self.assertEqual(self.labels(kind=ObjectKind.CONTAINER), [])

    def test_searching(self):
        self.assertEqual(self.labels(search="rot"), ["SHP-ROT"])

    def test_the_filter_choices_come_from_the_whole_window(self):
        """Choosing a carrier must not remove the other carriers from the dropdown."""
        queue = get_arrival_queue(self.team, parse_arrival_queue_filters({"carrier": "CMA CGM"}))
        self.assertEqual(queue.carrier_choices, ["CMA CGM", "Maersk"])
        self.assertEqual(queue.destination_choices, ["Gothenburg", "Rotterdam"])

    def test_a_filter_that_matches_nothing_still_appears_in_its_dropdown(self):
        """A filtered link can outlive the state behind it.

        The filter applies regardless; what must not happen is the page reading
        "Any carrier" while the list is in fact filtered down to nothing, because
        then the empty state's advice to clear a filter points at nothing visible.
        """
        queue = get_arrival_queue(self.team, parse_arrival_queue_filters({"carrier": "Hapag-Lloyd"}))
        self.assertEqual(queue.objects, [])
        self.assertIn("Hapag-Lloyd", queue.carrier_choices)

    def test_a_destination_that_matches_nothing_still_appears_too(self):
        queue = get_arrival_queue(self.team, parse_arrival_queue_filters({"destination": "Felixstowe"}))
        self.assertEqual(queue.objects, [])
        self.assertIn("Felixstowe", queue.destination_choices)

    def test_the_default_window_alone_is_not_an_active_filter(self):
        """Otherwise the page offers to clear filters nobody applied."""
        self.assertFalse(ArrivalQueueFilters().is_active)

    def test_a_chosen_window_is_an_active_filter(self):
        self.assertTrue(ArrivalQueueFilters(window="today").is_active)

    def test_each_other_filter_marks_the_state_active(self):
        for field in ("destination", "carrier", "health", "kind", "search"):
            with self.subTest(field=field):
                self.assertTrue(ArrivalQueueFilters(**{field: "x"}).is_active)


class ArrivalsPageTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("arp@example.com", "arp-team")
        today = timezone.localdate()
        cls.shipment = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-PAGE",
            carrier="CMA CGM",
            status=Shipment.Status.IN_TRANSIT,
            origin_port="Shanghai",
            destination_port="Gothenburg",
            eta=today + timedelta(days=2),
            original_eta=today + timedelta(days=2),
        )
        ShipmentContainer.objects.create(shipment=cls.shipment, container=_container(cls.team, "800001"))
        cls.today_shipment = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-TODAY",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            destination_port="Rotterdam",
            eta=today,
            original_eta=today,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def get(self, **params):
        return self.client.get(reverse("visibility:arrivals"), params)

    def test_the_page_returns_200(self):
        self.assertEqual(self.get().status_code, 200)

    def test_the_route_is_under_the_visibility_namespace(self):
        self.assertEqual(reverse("visibility:arrivals"), "/scm/visibility/arrivals/")

    def test_it_requires_a_login(self):
        self.client.logout()
        self.assertNotEqual(self.client.get(reverse("visibility:arrivals")).status_code, 200)

    def test_the_page_says_what_it_is_for(self):
        response = self.get()
        self.assertContains(response, "Arrivals")
        self.assertContains(response, "Upcoming containers and shipments.")

    def test_the_default_page_says_how_many_are_coming_and_when(self):
        self.assertContains(self.get(), "arriving in the next 7 days")

    def test_the_today_window_says_today(self):
        self.assertContains(self.get(window="today"), "arriving today")

    def test_the_page_shows_the_destination_and_the_eta(self):
        response = self.get()
        self.assertContains(response, "Gothenburg")
        self.assertContains(response, self.shipment.eta.strftime("%d %b %Y"))

    def test_a_row_links_to_the_object(self):
        self.assertContains(self.get(), reverse("shipments:detail", args=[self.shipment.pk]))

    def test_the_window_buttons_are_links_so_they_can_be_bookmarked(self):
        html = self.get().content.decode()
        for value, _label in ARRIVAL_WINDOWS:
            with self.subTest(window=value):
                self.assertIn(f"{reverse('visibility:arrivals')}?window={value}", html)

    def test_the_filter_state_is_pushed_to_the_url(self):
        self.assertContains(self.get(), 'hx-push-url="true"')

    def test_the_window_travels_with_the_other_filters(self):
        """Changing a carrier must not silently reset the day range."""
        self.assertContains(self.get(window="30"), '<input type="hidden" name="window" value="30">')

    def test_an_htmx_request_returns_only_the_queue(self):
        response = self.client.get(reverse("visibility:arrivals"), headers={"hx-request": "true"})
        self.assertContains(response, 'id="arrivals-queue"')
        self.assertNotContains(response, "Upcoming containers and shipments.")

    def test_an_empty_period_suggests_a_wider_range(self):
        """Nothing is overdue here, so the overdue window is the empty one."""
        response = self.get(window="overdue")
        self.assertContains(response, "No arrivals in this period")
        self.assertContains(response, "Try a wider date range")

    def test_an_empty_period_caused_by_a_filter_says_that_instead(self):
        """Widening the date range would not help — the carrier is what excluded it."""
        response = self.get(carrier="Nobody")
        self.assertContains(response, "No arrivals match these filters")
        self.assertNotContains(response, "No arrivals in this period")


class TeamIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("armine@example.com", "armine-team")
        cls.other_user, cls.other_team = make_user_and_team("artheirs@example.com", "artheirs-team")
        today = timezone.localdate()
        for team, number, serial in ((cls.team, "SHP-MINE", "900001"), (cls.other_team, "SHP-THEIRS", "900002")):
            shipment = Shipment.objects.create(
                team=team,
                shipment_number=number,
                carrier="MSC",
                status=Shipment.Status.IN_TRANSIT,
                destination_port="Gothenburg",
                eta=today + timedelta(days=2),
                original_eta=today + timedelta(days=2),
            )
            ShipmentContainer.objects.create(shipment=shipment, container=_container(team, serial))

    def test_the_queue_only_contains_this_teams_arrivals(self):
        labels = [obj.label for obj in get_arrival_queue(self.team).objects]
        self.assertEqual(labels, ["SHP-MINE"])

    def test_the_page_does_not_render_another_teams_arrival(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("visibility:arrivals"))
        self.assertContains(response, "SHP-MINE")
        self.assertNotContains(response, "SHP-THEIRS")

    def test_no_filter_or_window_can_reach_another_teams_arrival(self):
        for params in (
            {"search": "SHP-THEIRS"},
            {"carrier": "MSC"},
            {"destination": "Gothenburg"},
            {"window": "30"},
        ):
            with self.subTest(params=params):
                queue = get_arrival_queue(self.team, parse_arrival_queue_filters(params))
                self.assertNotIn("SHP-THEIRS", [obj.label for obj in queue.objects])

    def test_the_filter_choices_do_not_leak_another_teams_destinations(self):
        Shipment.objects.filter(team=self.other_team).update(destination_port="Felixstowe")
        self.assertEqual(get_arrival_queue(self.team).destination_choices, ["Gothenburg"])


class QueryBehaviourTest(TestCase):
    """One pass over the team's visibility objects, whatever the window holds."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("arqq@example.com", "arqq-team")
        today = timezone.localdate()
        for index in range(6):
            shipment = Shipment.objects.create(
                team=cls.team,
                shipment_number=f"SHP-A{index}",
                carrier="MSC",
                status=Shipment.Status.IN_TRANSIT,
                destination_port="Gothenburg",
                eta=today + timedelta(days=index % 5),
                original_eta=today + timedelta(days=index % 5),
            )
            ShipmentContainer.objects.create(shipment=shipment, container=_container(cls.team, f"11000{index}"))

    def test_the_query_count_does_not_grow_with_the_number_of_arrivals(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as many:
            self.assertEqual(get_arrival_queue(self.team, ArrivalQueueFilters(window="30")).total, 6)

        Shipment.objects.filter(shipment_number__in=["SHP-A3", "SHP-A4", "SHP-A5"]).delete()
        with CaptureQueriesContext(connection) as fewer:
            get_arrival_queue(self.team, ArrivalQueueFilters(window="30"))

        self.assertEqual(len(many.captured_queries), len(fewer.captured_queries))
