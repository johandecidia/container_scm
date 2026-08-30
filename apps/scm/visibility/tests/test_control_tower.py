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

* **Every KPI card is an action.** Arriving, Delayed and Exceptions are the numbers
  an operator acts on, and clicking them has to reach the same filtered board a URL
  would. The two totals — active shipments and containers — lead out to the lists
  they count, by route name rather than by a written-out path.
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
    get_visibility_overview,
)

from .factories import TEST_STORAGES, make_container, make_provider, make_user_and_team


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

    def test_the_kpi_cards_apply_the_filter_they_count(self):
        response = self.get()
        overview_url = reverse("visibility:overview")
        for query in ("?eta=7", "?delayed=1", "?exceptions=1"):
            with self.subTest(query=query):
                self.assertContains(response, self._kpi_card(f"{overview_url}{query}"))

    def test_the_active_shipments_kpi_leads_to_the_shipment_list(self):
        """A total is not a filter — it links to the list it counts."""
        self.assertContains(self.get(), self._kpi_card(reverse("shipments:list")))

    def test_the_containers_kpi_leads_to_the_container_list(self):
        self.assertContains(self.get(), self._kpi_card(reverse("containers:list")))

    def test_no_kpi_card_is_a_dead_total(self):
        """metric_card falls back to a bare `div.stat` without an href. None should.

        Asserting the absence of the unlinked branch is what keeps a card from
        quietly losing its href: a missing `{% url %}` would render one of these.
        """
        self.assertNotContains(self.get(), '<div class="stat bg-base-200')

    def test_the_exceptions_kpi_link_actually_filters(self):
        response = self.get(exceptions="1")
        self.assertContains(response, "SHP-HELD")
        self.assertNotContains(response, "SHP-LATE")

    def test_the_delayed_kpi_link_actually_filters(self):
        response = self.get(delayed="1")
        self.assertContains(response, "SHP-LATE")
        self.assertNotContains(response, "SHP-HELD")

    def test_the_arrivals_link_actually_filters(self):
        response = self.get(eta="7")
        self.assertContains(response, "SHP-HELD")
        self.assertNotContains(response, "SHP-LATE")

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
            Shipment.objects.create(
                team=team,
                shipment_number=number,
                carrier="Maersk",
                status=Shipment.Status.IN_TRANSIT,
                eta=timezone.localdate() + timedelta(days=12),
                original_eta=timezone.localdate() + timedelta(days=2),
            )

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
        labels = {
            obj.label for obj in get_visibility_overview(self.team_b, VisibilityFilters(delayed_only=True)).objects
        }
        self.assertEqual(labels, {"SHP-CT-B"})
