"""The Control Tower: what it says, what it links to, and what it must not rebuild.

The visibility overview became the Control Tower in UX-1 — a presentation change on
top of the same read layer. Three things are worth a test rather than a comment:

* **The attention queue is composed, not computed.** ``needs_attention`` joins the
  exception engine's findings to the delay engine's. If it ever starts deciding for
  itself what is wrong, the platform has two answers to that question.

* **The map is not inside the swap.** The whole reason the board and the map are
  separate elements is that a filter change must replace the map's data without
  recreating a WebGL context. A layout change that quietly moved the map into the
  board would look fine and break panning, so the partial is asserted to contain the
  source URL and no map element.

* **Every KPI card is an action.** Tracking, Delayed and Exceptions are the numbers
  an operator acts on, and since TRACK-UX clicking one selects the matching view of
  this board — the same board a ``?view=`` URL would reach. Arriving still drills
  into the Arrivals queue, and Active shipments leads out to the list it counts, by
  route name rather than by a written-out path.

* **Tracking is the default view.** The board opens on what the platform is actively
  watching. Every fixture here therefore has a live watch on it: under TRACK-UX a
  shipment with carrier events and no subscription is not something we are tracking,
  and a fixture without one would be testing an empty board.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.tracking.delay_detection import DelayReport
from apps.scm.tracking.exception_detection import ExceptionReport
from apps.scm.tracking.models import TrackingEvent
from apps.scm.visibility.read_models import ObjectKind, VisibilityObject
from apps.scm.visibility.selectors import (
    VisibilityFilters,
    VisibilityOverview,
    VisibilityView,
    get_visibility_overview,
)

from .factories import TEST_STORAGES, make_container, make_provider, make_user_and_team, watch_container


def _object(pk: int, *, delayed: bool = False, exception: bool = False) -> VisibilityObject:
    """A visibility object with only the health facts this module is about."""
    return VisibilityObject(
        kind=ObjectKind.SHIPMENT,
        shipment=Shipment(pk=pk, shipment_number=f"SHP-{pk}"),
        delay=DelayReport(is_delayed=delayed, reason="ETA moved forward", eta_drift_days=4 if delayed else 0),
        exceptions=ExceptionReport(
            has_exception=exception,
            exception_types=["customs_hold"] if exception else [],
            details=["Customs hold at Gothenburg"] if exception else [],
        ),
    )


class NeedsAttentionTest(TestCase):
    """Ordering and de-duplication of the attention queue."""

    def test_exceptions_come_before_delays(self):
        """An exception has happened; a delay is a date that moved."""
        overview = VisibilityOverview(objects=[_object(1, delayed=True), _object(2, exception=True)])
        self.assertEqual([obj.shipment.pk for obj in overview.needs_attention], [2, 1])

    def test_something_both_delayed_and_excepted_appears_once(self):
        overview = VisibilityOverview(objects=[_object(1, delayed=True, exception=True)])
        self.assertEqual(len(overview.needs_attention), 1)

    def test_a_healthy_board_has_an_empty_queue(self):
        overview = VisibilityOverview(objects=[_object(1)])
        self.assertEqual(overview.needs_attention, [])

    def test_the_queue_is_drawn_only_from_the_filtered_objects(self):
        """Filtering the board narrows what needs attention with it."""
        overview = VisibilityOverview(objects=[])
        self.assertEqual(overview.needs_attention, [])


@override_settings(STORAGES=TEST_STORAGES)
class ControlTowerPageTest(TestCase):
    """One team, one delayed shipment and one shipment on customs hold."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("ct@example.com", "ct-team")
        cls.delayed = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-LATE",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            eta=timezone.localdate() + timedelta(days=12),
            original_eta=timezone.localdate() + timedelta(days=4),
        )
        cls.held = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-HELD",
            carrier="MSC",
            status=Shipment.Status.IN_TRANSIT,
            eta=timezone.localdate() + timedelta(days=3),
            original_eta=timezone.localdate() + timedelta(days=3),
        )
        cls.container = make_container(cls.team)
        ShipmentContainer.objects.create(shipment=cls.held, container=cls.container)
        TrackingEvent.objects.create(
            team=cls.team,
            provider=make_provider(),
            shipment=cls.held,
            container=cls.container,
            event_type=TrackingEvent.EventType.CUSTOMS_HOLD,
            event_datetime=timezone.now() - timedelta(hours=6),
            location_name="Gothenburg",
            description="Customs hold",
        )
        # A live watch on each shipment, so both are on the default Tracking board.
        cls.late_container = make_container(cls.team, "MSKU0000006")
        ShipmentContainer.objects.create(shipment=cls.delayed, container=cls.late_container)
        for shipment, container in ((cls.held, cls.container), (cls.delayed, cls.late_container)):
            watch_container(cls.team, container, shipment=shipment)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def get(self, **params):
        return self.client.get(reverse("visibility:overview"), params)

    # -- naming ------------------------------------------------------------

    def test_the_page_is_called_the_control_tower(self):
        response = self.get()
        self.assertContains(response, "Control Tower")
        self.assertContains(response, "Supply chain status and what needs your attention.")

    def test_the_old_visibility_url_is_still_the_one_serving_it(self):
        """The label changed. The route, the app and the read models did not."""
        self.assertEqual(reverse("visibility:overview"), "/scm/visibility/")

    # -- attention ---------------------------------------------------------

    def test_the_attention_queue_lists_both_engines_findings(self):
        overview = get_visibility_overview(self.team)
        labels = [obj.label for obj in overview.needs_attention]
        self.assertEqual(labels, ["SHP-HELD", "SHP-LATE"])

    def test_the_attention_queue_shows_the_domains_own_reason(self):
        response = self.get()
        self.assertContains(response, "Needs attention")
        self.assertContains(response, "Customs hold at Gothenburg")
        self.assertContains(response, "ETA moved forward")

    def test_an_attention_row_links_to_the_object_it_is_about(self):
        self.assertContains(self.get(), reverse("shipments:detail", args=[self.held.pk]))

    # -- KPI strip ---------------------------------------------------------

    def _kpi_card(self, url: str) -> str:
        """The opening tag metric_card renders for a linked card.

        The anchor is matched along with the href on purpose: both list URLs are
        also in the sidebar, so a bare href assertion would pass whether or not
        the card itself is a link.
        """
        return f'<a href="{url}" class="stat bg-base-200'

    def test_the_three_operational_kpi_cards_select_this_boards_views(self):
        """Tracking, Delayed and Exceptions are the board's own views.

        A change from UX-3, where all three left for a work queue: they now have a
        view here to switch to, so a click narrows the list and the map beside it
        rather than navigating away. The queues are still one click from the section
        headers, which is what the drill-down was for.
        """
        overview_url = reverse("visibility:overview")
        response = self.get()
        for view in VisibilityView.values:
            with self.subTest(view=view):
                self.assertContains(response, self._kpi_card(f"{overview_url}?view={view}"))

    def test_the_arrival_kpi_cards_still_drill_into_the_arrivals_queue(self):
        """Untouched by TRACK-UX: arrivals are worked in their own queue."""
        self.assertContains(self.get(), self._kpi_card(reverse("visibility:arrivals")))

    def test_the_attention_panel_offers_the_queue_it_summarises(self):
        self.assertContains(self.get(), f'href="{reverse("visibility:exceptions")}" class="text-xs link')

    def test_the_arrivals_panel_offers_the_queue_it_summarises(self):
        self.assertContains(self.get(), f'href="{reverse("visibility:arrivals")}" class="text-xs link')

    def test_the_active_shipments_kpi_leads_to_the_shipment_list(self):
        """A total is not a filter — it links to the list it counts."""
        self.assertContains(self.get(), self._kpi_card(reverse("shipments:list")))

    def test_the_tracking_kpi_counts_distinct_containers_under_a_live_watch(self):
        """One box with two sources is one tracked container, not two."""
        from .factories import make_aggregator_subscription

        make_aggregator_subscription(self.team, self.container, shipment=self.held)

        overview = get_visibility_overview(self.team)

        self.assertEqual(overview.tracking_container_count, 2)

    def test_no_kpi_card_is_a_dead_total(self):
        """metric_card falls back to a bare `div.stat` without an href. None should.

        Asserting the absence of the unlinked branch is what keeps a card from
        quietly losing its href: a missing `{% url %}` would render one of these.
        """
        self.assertNotContains(self.get(), '<div class="stat bg-base-200')

    # -- the views ----------------------------------------------------------

    def test_the_board_opens_on_tracking(self):
        """No query parameter is the Tracking view, and the control shows it chosen."""
        response = self.get()
        self.assertEqual(response.context["overview"].view, VisibilityView.TRACKING)
        # The one selected button in the view group, rendered server-side.
        html = response.content.decode()
        self.assertEqual(html.count('class="btn join-item btn-sm btn-primary"'), 1)
        selected = html.split('class="btn join-item btn-sm btn-primary"', 1)[1]
        self.assertIn('value="tracking"', selected.split("</label>", 1)[0])

    def test_asking_for_tracking_explicitly_shows_the_same_board(self):
        default = {obj.key for obj in self.get().context["overview"].objects}
        explicit = {obj.key for obj in self.get(view="tracking").context["overview"].objects}
        self.assertEqual(default, explicit)

    def test_the_exceptions_view_uses_the_exception_engines_own_findings(self):
        response = self.get(view="exceptions")
        keys = {obj.key for obj in response.context["overview"].objects}
        attention = {obj.key for obj in get_visibility_overview(self.team).needs_attention}
        self.assertTrue(keys)
        self.assertTrue(keys <= attention)
        self.assertTrue(all(obj.has_exception for obj in response.context["overview"].objects))

    def test_the_delayed_view_uses_the_delay_engines_own_verdict(self):
        response = self.get(view="delayed")
        objects = response.context["overview"].objects
        self.assertTrue(objects)
        self.assertTrue(all(obj.is_delayed for obj in objects))

    def test_the_three_views_are_three_separate_datasets(self):
        keys = {
            view: {obj.key for obj in self.get(view=view).context["overview"].objects} for view in VisibilityView.values
        }
        self.assertNotEqual(keys[VisibilityView.EXCEPTIONS], keys[VisibilityView.DELAYED])
        self.assertNotEqual(keys[VisibilityView.TRACKING], keys[VisibilityView.EXCEPTIONS])

    # -- backwards compatibility -------------------------------------------
    #
    # The KPI cards and the navigation stopped pointing at the flag URLs in UX-3, but
    # they are in bookmarks and in links people have shared. They keep working, now
    # by selecting the equivalent view.

    def test_the_old_exceptions_filter_url_still_filters_the_board(self):
        response = self.get(exceptions="1")
        self.assertContains(response, "SHP-HELD")
        self.assertNotContains(response, "SHP-LATE")

    def test_the_old_delayed_filter_url_still_filters_the_board(self):
        response = self.get(delayed="1")
        self.assertContains(response, "SHP-LATE")
        self.assertNotContains(response, "SHP-HELD")

    def test_the_old_eta_filter_url_still_filters_the_board(self):
        response = self.get(eta="7")
        self.assertContains(response, "SHP-HELD")
        self.assertNotContains(response, "SHP-LATE")

    # -- no duplicate definitions ------------------------------------------

    def test_the_queues_and_the_control_tower_agree_about_what_needs_attention(self):
        """One definition, two presentations. Two definitions would be the bug."""
        from apps.scm.visibility.work_queues import get_exception_queue

        overview = get_visibility_overview(self.team)
        self.assertEqual(
            {obj.key for obj in overview.needs_attention},
            {item.key for item in get_exception_queue(self.team).items},
        )

    def test_the_queues_and_the_control_tower_agree_about_what_is_arriving(self):
        from apps.scm.visibility.work_queues import get_arrival_queue

        overview = get_visibility_overview(self.team)
        self.assertEqual(
            {obj.key for obj in overview.arriving_soon},
            {obj.key for obj in get_arrival_queue(self.team).objects},
        )

    # -- HTMX and the map --------------------------------------------------

    def test_an_htmx_filter_change_returns_only_the_board(self):
        response = self.client.get(reverse("visibility:overview"), {"delayed": "1"}, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="visibility-board"')
        self.assertNotContains(response, "Supply chain status and what needs your attention.")

    @override_settings(MAPBOX_PUBLIC_TOKEN="pk.test-token")
    def test_the_swapped_board_carries_no_map_element(self):
        """Swapping a map element in would recreate the WebGL context every filter."""
        response = self.client.get(reverse("visibility:overview"), headers={"hx-request": "true"})
        self.assertNotContains(response, "data-scm-map=")
        self.assertContains(response, "data-scm-map-source=")

    @override_settings(MAPBOX_PUBLIC_TOKEN="pk.test-token")
    def test_the_full_page_keeps_the_map_outside_the_board(self):
        html = self.get().content.decode()
        self.assertIn("data-scm-map", html)
        self.assertLess(html.index("data-scm-map"), html.index('id="visibility-board"'))

    def test_the_map_source_url_follows_the_active_filters(self):
        response = self.get(exceptions="1", carrier="MSC")
        source = f'data-scm-map-source="{reverse("visibility:map_data")}?'
        self.assertContains(response, source)
        html = response.content.decode()
        carried = html.split(source, 1)[1].split('"', 1)[0]
        self.assertIn("exceptions=1", carried)
        self.assertIn("carrier=MSC", carried)

    def test_the_map_data_endpoint_honours_the_same_filters(self):
        response = self.client.get(reverse("visibility:map_data"), {"exceptions": "1"})
        labels = {feature["properties"]["label"] for feature in response.json()["features"]}
        self.assertNotIn("SHP-LATE", labels)

    # -- filters -----------------------------------------------------------

    def test_the_filter_state_survives_in_the_url(self):
        """`hx-push-url` is what makes a filtered board linkable."""
        self.assertContains(self.get(), 'hx-push-url="true"')

    def test_the_local_search_is_a_filter_and_says_so(self):
        self.assertContains(self.get(), "Filter by container, shipment, vessel")

    # -- rendering ---------------------------------------------------------

    def test_no_template_syntax_reaches_the_browser(self):
        """Django's ``{# #}`` is single-line only.

        A multi-line one is not a comment: the first line disappears and the rest
        is printed on the page. It happened while this layout was being built, it
        renders as plausible-looking prose, and no assertion about content would
        have noticed.
        """
        html = self.get().content.decode()
        for token in ("{#", "#}", "{%", "%}"):
            with self.subTest(token=token):
                self.assertNotIn(token, html)


@override_settings(STORAGES=TEST_STORAGES)
class ControlTowerIsolationTest(TestCase):
    """The attention queue is team data like everything else on the page."""

    @classmethod
    def setUpTestData(cls):
        cls.user_a, cls.team_a = make_user_and_team("ct-a@example.com", "ct-team-a")
        cls.user_b, cls.team_b = make_user_and_team("ct-b@example.com", "ct-team-b")
        for team, number in ((cls.team_a, "SHP-CT-A"), (cls.team_b, "SHP-CT-B")):
            shipment = Shipment.objects.create(
                team=team,
                shipment_number=number,
                carrier="Maersk",
                status=Shipment.Status.IN_TRANSIT,
                eta=timezone.localdate() + timedelta(days=12),
                original_eta=timezone.localdate() + timedelta(days=2),
            )
            # A live watch, so each team's shipment is on its own default board.
            container = make_container(team)
            ShipmentContainer.objects.create(shipment=shipment, container=container)
            watch_container(team, container, shipment=shipment)

    def test_the_attention_queue_is_scoped_to_the_callers_team(self):
        labels = {obj.label for obj in get_visibility_overview(self.team_a).needs_attention}
        self.assertEqual(labels, {"SHP-CT-A"})

    def test_another_teams_delay_is_not_on_the_page(self):
        client = Client()
        client.force_login(self.user_a)
        response = client.get(reverse("visibility:overview"))
        self.assertContains(response, "SHP-CT-A")
        self.assertNotContains(response, "SHP-CT-B")

    def test_the_filtered_board_is_scoped_too(self):
        """A filter must never be a way to widen the query beyond the team."""
        client = Client()
        client.force_login(self.user_a)
        response = client.get(reverse("visibility:overview"), {"delayed": "1"})
        self.assertNotContains(response, "SHP-CT-B")

    def test_a_filtered_visibility_overview_for_the_other_team_shows_only_its_own(self):
        filters = VisibilityFilters(view=VisibilityView.DELAYED)
        labels = {obj.label for obj in get_visibility_overview(self.team_b, filters).objects}
        self.assertEqual(labels, {"SHP-CT-B"})
