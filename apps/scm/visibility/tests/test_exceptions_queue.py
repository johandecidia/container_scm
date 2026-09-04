"""The Exceptions work queue: what it lists, in what order, and what it must not become.

Four things here are worth a test rather than a comment.

* **It has no second opinion about what is wrong.** Every row comes from the
  exception engine or the delay engine. If the queue ever starts deciding for
  itself, the platform has two answers to the most important question it asks.

* **One object, one row.** A box that is both held and running late is one problem
  to work. The Control Tower's attention queue already de-duplicates that way and
  the queue must not undo it.

* **Bands are not severity.** Rows are ordered by where the finding came from — a
  carrier event, a date that moved, an absence of data — and then by how soon the
  thing arrives. Nothing invents high/medium/low.

* **No workflow.** No acknowledge, assign, resolve or snooze, because none of that
  state exists. A row leaves the queue when the supply chain changes.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.models import Container
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.delay_detection import DelayReport
from apps.scm.tracking.exception_detection import ExceptionIssue, ExceptionReport
from apps.scm.tracking.models import TrackingEvent, TrackingSubscription
from apps.scm.visibility.read_models import ObjectKind, VisibilityObject
from apps.scm.visibility.work_queues import (
    DELAY_ISSUE,
    ExceptionQueueFilters,
    IssueBand,
    QueueIssue,
    get_exception_queue,
    parse_exception_queue_filters,
)

from .factories import equipment_type, make_container, make_provider, make_user_and_team

QUEUE_URL = "/scm/visibility/exceptions/"


def _container(team, serial: str) -> Container:
    """A container with a valid ISO number — Container.save runs full_clean."""
    return Container.objects.create(
        team=team,
        owner_code="MSK",
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit("MSK", "U", serial),
        equipment_type=equipment_type(),
    )


def _object(label: str, *, issues=(), delayed=False, eta=None) -> VisibilityObject:
    """A visibility object carrying only the findings these tests are about."""
    return VisibilityObject(
        kind=ObjectKind.SHIPMENT,
        shipment=Shipment(pk=len(label), shipment_number=label, eta=eta),
        delay=DelayReport(is_delayed=delayed, reason="ETA moved forward", eta_drift_days=3 if delayed else 0),
        exceptions=ExceptionReport(
            has_exception=bool(issues),
            exception_types=[t for t, _detail in issues],
            details=[d for _t, d in issues],
            issues=[ExceptionIssue(t, d) for t, d in issues],
        ),
    )


class IssueBandingTest(TestCase):
    """Where a finding came from is the only ranking the domain can defend."""

    def test_a_carrier_reported_exception_is_in_the_exception_band(self):
        for issue_type in ("customs_hold", "rolled", "port_congestion"):
            with self.subTest(issue_type=issue_type):
                self.assertEqual(QueueIssue(issue_type, "").band, IssueBand.EXCEPTION)

    def test_a_moved_date_is_in_the_delay_band(self):
        self.assertEqual(QueueIssue(DELAY_ISSUE, "").band, IssueBand.DELAY)

    def test_an_absence_of_data_is_in_the_tracking_band(self):
        """A stale feed is a gap in what we know, not something a carrier reported."""
        self.assertEqual(QueueIssue("missing_event", "").band, IssueBand.TRACKING)

    def test_an_unrecognised_exception_code_still_counts_as_an_exception(self):
        """A finding must not disappear because the label table is out of date."""
        issue = QueueIssue("some_new_carrier_code", "")
        self.assertEqual(issue.band, IssueBand.EXCEPTION)
        self.assertEqual(issue.label, "Some New Carrier Code")

    def test_no_issue_type_is_given_a_severity(self):
        """Prioritisation the domain cannot justify must not appear in the labels."""
        labels = " ".join(QueueIssue(t, "").label.lower() for t in ("customs_hold", DELAY_ISSUE, "missing_event"))
        for word in ("high", "medium", "low", "critical", "at risk"):
            self.assertNotIn(word, labels)


class QueueCompositionTest(TestCase):
    """Rows, de-duplication and ordering, over objects with known findings."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("exq@example.com", "exq-team")
        today = timezone.localdate()
        cls.held = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-HELD",
            carrier="MSC",
            status=Shipment.Status.IN_TRANSIT,
            destination_port="Gothenburg",
            eta=today + timedelta(days=3),
            original_eta=today + timedelta(days=3),
        )
        cls.late = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-LATE",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            destination_port="Rotterdam",
            eta=today + timedelta(days=12),
            original_eta=today + timedelta(days=4),
        )
        cls.both = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-BOTH",
            carrier="MSC",
            status=Shipment.Status.IN_TRANSIT,
            eta=today + timedelta(days=20),
            original_eta=today + timedelta(days=2),
        )
        cls.healthy = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-FINE",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            eta=today + timedelta(days=5),
            original_eta=today + timedelta(days=5),
        )
        provider = make_provider()
        for shipment, number, location in (
            (cls.held, None, "Gothenburg"),
            (cls.both, "000006", "Rotterdam"),
        ):
            container = make_container(cls.team) if number is None else _container(cls.team, number)
            ShipmentContainer.objects.create(shipment=shipment, container=container)
            TrackingEvent.objects.create(
                team=cls.team,
                provider=provider,
                shipment=shipment,
                container=container,
                event_type=TrackingEvent.EventType.CUSTOMS_HOLD,
                event_datetime=timezone.now() - timedelta(hours=6),
                location_name=location,
                description="Customs hold",
            )

    def queue(self, **params):
        return get_exception_queue(self.team, parse_exception_queue_filters(params))

    def labels(self, **params):
        return [item.object.label for item in self.queue(**params).items]

    def test_an_object_with_an_exception_is_in_the_queue(self):
        self.assertIn("SHP-HELD", self.labels())

    def test_an_object_that_is_only_delayed_is_in_the_queue(self):
        """The delay engine's findings belong here too, or half the work is invisible."""
        self.assertIn("SHP-LATE", self.labels())

    def test_a_healthy_object_is_not_in_the_queue(self):
        self.assertNotIn("SHP-FINE", self.labels())

    def test_an_object_with_both_an_exception_and_a_delay_appears_once(self):
        self.assertEqual(self.labels().count("SHP-BOTH"), 1)

    def test_such_an_object_leads_with_its_exception_and_keeps_the_delay(self):
        item = next(item for item in self.queue().items if item.object.label == "SHP-BOTH")
        self.assertEqual(item.primary_issue.issue_type, "customs_hold")
        self.assertIn(DELAY_ISSUE, [issue.issue_type for issue in item.other_issues])

    def test_exceptions_come_before_delays(self):
        labels = self.labels()
        self.assertLess(labels.index("SHP-HELD"), labels.index("SHP-LATE"))

    def test_the_detail_is_the_engines_own_words(self):
        item = next(item for item in self.queue().items if item.object.label == "SHP-HELD")
        self.assertEqual(item.primary_issue.detail, "Customs hold at Gothenburg")

    def test_a_delay_detail_carries_the_drift_the_engine_measured(self):
        item = next(item for item in self.queue().items if item.object.label == "SHP-LATE")
        self.assertIn("ETA moved forward", item.primary_issue.detail)
        self.assertIn("+8d", item.primary_issue.detail)

    def test_a_row_links_to_the_object_it_is_about(self):
        item = next(item for item in self.queue().items if item.object.label == "SHP-HELD")
        self.assertEqual(item.object.detail_url, reverse("shipments:detail", args=[self.held.pk]))

    def test_the_total_counts_the_queue_before_filters(self):
        queue = self.queue(carrier="MSC")
        self.assertEqual(queue.total, 3)
        self.assertLess(len(queue.items), queue.total)

    # -- filters -----------------------------------------------------------

    def test_filtering_by_issue_type(self):
        self.assertEqual(set(self.labels(issue="customs_hold")), {"SHP-HELD", "SHP-BOTH"})

    def test_filtering_by_the_delay_issue_finds_objects_that_are_also_excepted(self):
        """SHP-BOTH is delayed as well as held — a delay filter must not lose it."""
        self.assertEqual(set(self.labels(issue=DELAY_ISSUE)), {"SHP-LATE", "SHP-BOTH"})

    def test_filtering_by_carrier(self):
        self.assertEqual(set(self.labels(carrier="Maersk")), {"SHP-LATE"})

    def test_filtering_by_object_type(self):
        self.assertEqual(self.labels(kind=ObjectKind.CONTAINER), [])
        self.assertTrue(self.labels(kind=ObjectKind.SHIPMENT))

    def test_filtering_by_eta_window(self):
        self.assertEqual(self.labels(eta="7"), ["SHP-HELD"])

    def test_searching_by_destination(self):
        self.assertEqual(self.labels(search="rotterdam"), ["SHP-LATE"])

    def test_the_issue_filter_only_offers_issues_that_are_present(self):
        """A choice that can only return nothing is a dead end, not a filter."""
        offered = dict(self.queue().issue_choices)
        self.assertIn("customs_hold", offered)
        self.assertNotIn("rolled", offered)

    def test_the_carrier_filter_offers_the_carriers_in_the_queue(self):
        self.assertEqual(self.queue().carrier_choices, ["MSC", "Maersk"])

    def test_an_issue_filter_that_matches_nothing_is_still_offered(self):
        """The Control Tower's Delayed card links here whether or not anything is delayed.

        The filter applies either way. What must not happen is the select reading
        "Any issue" while the list is filtered to nothing: the empty state then
        advises clearing a filter that nothing on the page shows.
        """
        queue = self.queue(issue="rolled")
        self.assertEqual(queue.items, [])
        self.assertIn("rolled", dict(queue.issue_choices))

    def test_a_carrier_filter_that_matches_nothing_is_still_offered(self):
        queue = self.queue(carrier="Hapag-Lloyd")
        self.assertEqual(queue.items, [])
        self.assertIn("Hapag-Lloyd", queue.carrier_choices)

    def test_an_unknown_issue_code_is_not_invented_into_a_choice(self):
        """Only issue types the queue has words for. A typo must not become a filter."""
        self.assertNotIn("not_a_real_issue", dict(self.queue(issue="not_a_real_issue").issue_choices))


class QueueSortOrderTest(TestCase):
    """Band first, then the soonest arrival. Documented, so it can be relied on."""

    def _sorted(self, objects):
        from apps.scm.visibility.work_queues.issues import build_queue_item

        items = sorted((build_queue_item(obj) for obj in objects), key=lambda item: item.sort_key)
        return [item.object.label for item in items]

    def test_the_soonest_arrival_leads_within_a_band(self):
        today = timezone.localdate()
        order = self._sorted(
            [
                _object("FAR", issues=[("customs_hold", "held")], eta=today + timedelta(days=30)),
                _object("SOON", issues=[("customs_hold", "held")], eta=today + timedelta(days=2)),
            ]
        )
        self.assertEqual(order, ["SOON", "FAR"])

    def test_an_object_with_no_eta_sorts_last_in_its_band(self):
        """No ETA is an unknown, not an urgency."""
        today = timezone.localdate()
        order = self._sorted(
            [
                _object("NO-ETA", issues=[("customs_hold", "held")]),
                _object("DATED", issues=[("customs_hold", "held")], eta=today + timedelta(days=40)),
            ]
        )
        self.assertEqual(order, ["DATED", "NO-ETA"])

    def test_a_tracking_gap_sorts_below_a_delay(self):
        today = timezone.localdate()
        order = self._sorted(
            [
                _object("STALE", issues=[("missing_event", "No tracking update for 9 days")], eta=today),
                _object("LATE", delayed=True, eta=today + timedelta(days=25)),
            ]
        )
        self.assertEqual(order, ["LATE", "STALE"])

    def test_a_carrier_exception_sorts_above_both(self):
        today = timezone.localdate()
        order = self._sorted(
            [
                _object("STALE", issues=[("missing_event", "stale")], eta=today),
                _object("LATE", delayed=True, eta=today),
                _object("HELD", issues=[("customs_hold", "held")], eta=today + timedelta(days=60)),
            ]
        )
        self.assertEqual(order, ["HELD", "LATE", "STALE"])


class ExceptionsPageTest(TestCase):
    """The page: status, rendering, HTMX and the empty state."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("exp@example.com", "exp-team")
        cls.shipment = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-PAGE",
            carrier="CMA CGM",
            status=Shipment.Status.IN_TRANSIT,
            destination_port="Gothenburg",
            eta=timezone.localdate() + timedelta(days=4),
            original_eta=timezone.localdate() + timedelta(days=4),
        )
        cls.container = make_container(cls.team)
        ShipmentContainer.objects.create(shipment=cls.shipment, container=cls.container)
        TrackingEvent.objects.create(
            team=cls.team,
            provider=make_provider(),
            shipment=cls.shipment,
            container=cls.container,
            event_type=TrackingEvent.EventType.CUSTOMS_HOLD,
            event_datetime=timezone.now() - timedelta(hours=2),
            location_name="Rotterdam",
            description="Customs hold",
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def get(self, **params):
        return self.client.get(reverse("visibility:exceptions"), params)

    def test_the_page_returns_200(self):
        self.assertEqual(self.get().status_code, 200)

    def test_the_route_is_under_the_visibility_namespace(self):
        self.assertEqual(reverse("visibility:exceptions"), QUEUE_URL)

    def test_it_requires_a_login(self):
        self.client.logout()
        self.assertNotEqual(self.client.get(reverse("visibility:exceptions")).status_code, 200)

    def test_the_page_says_what_it_is_for(self):
        response = self.get()
        self.assertContains(response, "Exceptions")
        self.assertContains(response, "Supply chain issues that need attention.")

    def test_the_count_does_not_claim_a_workflow_state(self):
        """Nothing here is opened or closed, so the header must not say "open"."""
        response = self.get()
        self.assertContains(response, "need attention")
        self.assertNotContains(response, "open issues")

    def test_the_row_shows_the_issue_and_the_reason(self):
        response = self.get()
        self.assertContains(response, "Customs hold")
        self.assertContains(response, "Customs hold at Rotterdam")

    def test_the_row_shows_the_object_and_links_to_it(self):
        response = self.get()
        self.assertContains(response, "SHP-PAGE")
        self.assertContains(response, reverse("shipments:detail", args=[self.shipment.pk]))

    def test_the_page_offers_no_workflow_actions(self):
        """Acknowledge, assign, resolve and snooze have no state behind them."""
        html = self.get().content.decode().lower()
        for word in ("acknowledge", "assign", "resolve", "snooze", "due date"):
            with self.subTest(word=word):
                self.assertNotIn(word, html)

    def test_the_filter_state_is_pushed_to_the_url(self):
        self.assertContains(self.get(), 'hx-push-url="true"')

    def test_an_htmx_request_returns_only_the_queue(self):
        response = self.client.get(reverse("visibility:exceptions"), headers={"hx-request": "true"})
        self.assertContains(response, 'id="exceptions-queue"')
        self.assertNotContains(response, "Supply chain issues that need attention.")

    def test_a_filter_that_matches_nothing_says_so_in_terms_of_the_filter(self):
        response = self.get(carrier="Nobody")
        self.assertContains(response, "No exceptions match these filters")
        self.assertNotContains(response, "No current exceptions")

    def test_an_empty_queue_says_nothing_needs_attention(self):
        TrackingEvent.objects.filter(team=self.team).delete()
        self.shipment.delete()
        response = self.get()
        self.assertContains(response, "No current exceptions")


class TeamIsolationTest(TestCase):
    """Another team's problems are not this team's queue."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("mine@example.com", "mine-team")
        cls.other_user, cls.other_team = make_user_and_team("theirs@example.com", "theirs-team")
        provider = make_provider()
        for team, number, label in ((cls.team, "000006", "SHP-MINE"), (cls.other_team, "000109", "SHP-THEIRS")):
            shipment = Shipment.objects.create(
                team=team,
                shipment_number=label,
                carrier="MSC",
                status=Shipment.Status.IN_TRANSIT,
                eta=timezone.localdate() + timedelta(days=3),
                original_eta=timezone.localdate() + timedelta(days=3),
            )
            container = _container(team, number)
            ShipmentContainer.objects.create(shipment=shipment, container=container)
            TrackingSubscription.objects.create(
                team=team,
                provider=provider,
                container=container,
                shipment=shipment,
                tracking_reference=container.container_id,
                status=TrackingSubscription.Status.ACTIVE,
            )
            TrackingEvent.objects.create(
                team=team,
                provider=provider,
                shipment=shipment,
                container=container,
                event_type=TrackingEvent.EventType.CUSTOMS_HOLD,
                event_datetime=timezone.now() - timedelta(hours=1),
                location_name="Gothenburg",
            )

    def test_the_queue_only_contains_this_teams_objects(self):
        labels = [item.object.label for item in get_exception_queue(self.team).items]
        self.assertEqual(labels, ["SHP-MINE"])

    def test_the_page_does_not_render_another_teams_object(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("visibility:exceptions"))
        self.assertContains(response, "SHP-MINE")
        self.assertNotContains(response, "SHP-THEIRS")

    def test_no_filter_can_reach_another_teams_object(self):
        """Filters narrow a team-scoped list; they are not a way back out of it."""
        for params in ({"search": "SHP-THEIRS"}, {"carrier": "MSC"}, {"issue": "customs_hold"}):
            with self.subTest(params=params):
                queue = get_exception_queue(self.team, parse_exception_queue_filters(params))
                self.assertNotIn("SHP-THEIRS", [item.object.label for item in queue.items])


class QueryBehaviourTest(TestCase):
    """The queue must not grow a query per row."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("exqq@example.com", "exqq-team")
        provider = make_provider()
        for index in range(6):
            shipment = Shipment.objects.create(
                team=cls.team,
                shipment_number=f"SHP-Q{index}",
                carrier="MSC",
                status=Shipment.Status.IN_TRANSIT,
                eta=timezone.localdate() + timedelta(days=3),
                original_eta=timezone.localdate() + timedelta(days=1),
            )
            container = _container(cls.team, f"10000{index}")
            ShipmentContainer.objects.create(shipment=shipment, container=container)
            TrackingEvent.objects.create(
                team=cls.team,
                provider=provider,
                shipment=shipment,
                container=container,
                event_type=TrackingEvent.EventType.CUSTOMS_HOLD,
                event_datetime=timezone.now() - timedelta(hours=1),
                location_name="Gothenburg",
            )

    def test_the_queue_is_built_from_one_pass(self):
        """Six queued shipments must cost what one costs.

        Asserted as an equality between two runs rather than a magic number, so a
        legitimate change to the visibility selectors does not fail this test while
        an N+1 still does.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as many:
            get_exception_queue(self.team)
        self.assertEqual(len(get_exception_queue(self.team).items), 6)

        Shipment.objects.filter(shipment_number__in=["SHP-Q3", "SHP-Q4", "SHP-Q5"]).delete()
        with CaptureQueriesContext(connection) as fewer:
            get_exception_queue(self.team)

        self.assertEqual(len(many.captured_queries), len(fewer.captured_queries))


class FilterStateTest(TestCase):
    def test_unset_filters_are_not_active(self):
        self.assertFalse(ExceptionQueueFilters().is_active)

    def test_each_filter_marks_the_state_active(self):
        for field in ("issue", "carrier", "kind", "eta_window", "search"):
            with self.subTest(field=field):
                self.assertTrue(ExceptionQueueFilters(**{field: "x"}).is_active)

    def test_parsing_trims_whitespace(self):
        filters = parse_exception_queue_filters({"search": "  MSKU  ", "carrier": " MSC "})
        self.assertEqual(filters.search, "MSKU")
        self.assertEqual(filters.carrier, "MSC")
