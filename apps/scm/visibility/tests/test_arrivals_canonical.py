"""Expected Arrivals, driven by canonical destinations instead of carrier text.

The question LOC-1 exists to make answerable is "what is expected at
Oceanterminalen", and the test that matters is that it can be answered *without*
matching any of the strings a carrier might use — "Gothenburg", "Goteborg",
"GOTHENBURG, SE", "SEGOT". So the fixtures here deliberately give the shipments
misleading destination text: a shipment bound for Oceanterminalen says "Gothenburg",
and one bound nowhere canonical says "Gothenburg" too. Only the canonical filter can
tell them apart.

Backwards compatibility is asserted in both directions: a shipment with no canonical
destination still appears on the unfiltered queue and can still be filtered by text,
and the canonical filter never invents one for it.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.choices import LocationType
from apps.scm.containers.models import Container
from apps.scm.containers.services import create_location
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment, ShipmentContainer
from apps.scm.visibility.work_queues import ArrivalQueueFilters, get_arrival_queue, parse_arrival_queue_filters

from .factories import equipment_type, make_user_and_team


def _container(team, serial: str) -> Container:
    return Container.objects.create(
        team=team,
        owner_code="MSK",
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit("MSK", "U", serial),
        equipment_type=equipment_type(),
    )


class CanonicalDestinationFilterTest(TestCase):
    """One port, one terminal inside it, and three shipments that all say "Gothenburg"."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("arc@example.com", "arc-team")
        today = timezone.localdate()

        cls.port = create_location(
            cls.team, {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT"}
        )
        cls.terminal = create_location(
            cls.team,
            {
                "name": "Oceanterminalen",
                "location_type": LocationType.DEPOT,
                "unlocode": "SEGOT",
                "parent_location": cls.port,
            },
        )
        cls.other_terminal = create_location(
            cls.team,
            {
                "name": "APM Terminals Gothenburg",
                "location_type": LocationType.TERMINAL,
                "unlocode": "SEGOT",
                "parent_location": cls.port,
            },
        )

        def shipment(number, destination_location, destination_port="Gothenburg"):
            created = Shipment.objects.create(
                team=cls.team,
                shipment_number=number,
                carrier="Maersk",
                status=Shipment.Status.IN_TRANSIT,
                destination_port=destination_port,
                destination_location=destination_location,
                eta=today + timedelta(days=3),
                original_eta=today + timedelta(days=3),
            )
            ShipmentContainer.objects.create(shipment=created, container=_container(cls.team, number[-6:]))
            return created

        # All three say "Gothenburg". Only the canonical column distinguishes them.
        cls.to_terminal = shipment("SHP-100001", cls.terminal)
        cls.to_other = shipment("SHP-200002", cls.other_terminal)
        cls.to_port = shipment("SHP-300003", cls.port)
        cls.text_only = shipment("SHP-400004", None)

    def labels(self, **params):
        queue = get_arrival_queue(self.team, parse_arrival_queue_filters(params))
        return sorted(obj.label for obj in queue.objects)

    def test_the_terminal_gets_exactly_what_is_routed_to_it(self):
        """The whole point: no other terminal's traffic, no text matching."""
        self.assertEqual(self.labels(destination_location=str(self.terminal.pk)), ["SHP-100001"])

    def test_a_sibling_terminals_traffic_is_not_included(self):
        self.assertNotIn("SHP-200002", self.labels(destination_location=str(self.terminal.pk)))

    def test_the_port_includes_the_terminals_inside_it(self):
        """A box bound for Oceanterminalen is arriving at the port that contains it."""
        self.assertEqual(
            self.labels(destination_location=str(self.port.pk)),
            ["SHP-100001", "SHP-200002", "SHP-300003"],
        )

    def test_a_terminal_does_not_inherit_the_ports_own_traffic(self):
        self.assertNotIn("SHP-300003", self.labels(destination_location=str(self.terminal.pk)))

    def test_carrier_text_cannot_get_a_shipment_into_a_canonical_filter(self):
        """SHP-400004 says "Gothenburg" and is bound nowhere we recorded."""
        for location in (self.port, self.terminal):
            with self.subTest(location=location.name):
                self.assertNotIn("SHP-400004", self.labels(destination_location=str(location.pk)))

    def test_the_text_filter_still_works_for_a_shipment_with_no_canonical_place(self):
        """Backwards compatibility: a legacy shipment is not stranded."""
        self.assertIn("SHP-400004", self.labels(destination="Gothenburg"))

    def test_an_unfiltered_queue_still_shows_everything(self):
        self.assertEqual(
            self.labels(),
            ["SHP-100001", "SHP-200002", "SHP-300003", "SHP-400004"],
        )

    def test_a_non_numeric_filter_narrows_nothing_rather_than_erroring(self):
        """A hand-edited URL should show the queue, not a 500."""
        self.assertEqual(len(self.labels(destination_location="oceanterminalen")), 4)

    def test_a_canonical_filter_counts_as_an_active_filter(self):
        """So the empty state offers to clear it, and the Clear button appears."""
        filters = ArrivalQueueFilters(destination_location=str(self.terminal.pk))
        self.assertTrue(filters.has_narrowing_filters)
        self.assertTrue(filters.is_active)

    def test_the_places_offered_are_the_ones_something_is_routed_to(self):
        queue = get_arrival_queue(self.team, parse_arrival_queue_filters({}))
        self.assertCountEqual(
            [location.name for location in queue.destination_location_choices],
            ["Göteborg", "Oceanterminalen", "APM Terminals Gothenburg"],
        )

    def test_a_selected_place_stays_in_its_dropdown_even_with_no_matches(self):
        """A filtered link can outlive the shipment that justified it."""
        empty = create_location(self.team, {"name": "Empty Depot"})
        queue = get_arrival_queue(self.team, parse_arrival_queue_filters({"destination_location": str(empty.pk)}))
        self.assertEqual(queue.objects, [])
        self.assertIn(empty, queue.destination_location_choices)

    def test_the_row_shows_the_canonical_name_over_the_carrier_text(self):
        queue = get_arrival_queue(
            self.team, parse_arrival_queue_filters({"destination_location": str(self.terminal.pk)})
        )
        row = queue.objects[0]
        self.assertEqual(row.destination_label, "Oceanterminalen")
        self.assertEqual(row.destination, "Gothenburg", "the carrier's text is still there")
        self.assertTrue(row.has_canonical_destination)

    def test_a_row_without_a_canonical_place_falls_back_to_the_carrier_text(self):
        queue = get_arrival_queue(self.team, parse_arrival_queue_filters({"search": "400004"}))
        row = queue.objects[0]
        self.assertEqual(row.destination_label, "Gothenburg")
        self.assertFalse(row.has_canonical_destination)


class ArrivalsPageTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("arcp@example.com", "arcp-team")
        today = timezone.localdate()
        cls.port = create_location(
            cls.team, {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT"}
        )
        cls.terminal = create_location(
            cls.team, {"name": "Oceanterminalen", "location_type": LocationType.DEPOT, "parent_location": cls.port}
        )
        cls.shipment = Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-PAGE",
            carrier="Maersk",
            status=Shipment.Status.IN_TRANSIT,
            destination_port="Gothenburg",
            destination_location=cls.terminal,
            eta=today + timedelta(days=2),
            original_eta=today + timedelta(days=2),
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_page_offers_the_canonical_place_filter(self):
        response = self.client.get(reverse("visibility:arrivals"))
        self.assertContains(response, 'name="destination_location"')
        self.assertContains(response, "Oceanterminalen")

    def test_filtering_by_place_over_http_narrows_the_queue(self):
        response = self.client.get(reverse("visibility:arrivals"), {"destination_location": str(self.terminal.pk)})
        self.assertContains(response, "SHP-PAGE")

    def test_a_place_with_nothing_routed_to_it_says_the_filter_is_why(self):
        empty = create_location(self.team, {"name": "Empty Depot"})
        response = self.client.get(reverse("visibility:arrivals"), {"destination_location": str(empty.pk)})
        self.assertContains(response, "No arrivals match these filters")


class TenantIsolationTest(TestCase):
    """A location id from another team must reach nothing, not that team's subtree."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("arcmine@example.com", "arcmine-team")
        cls.other_user, cls.other_team = make_user_and_team("arctheirs@example.com", "arctheirs-team")
        today = timezone.localdate()

        cls.their_port = create_location(
            cls.other_team, {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT"}
        )
        cls.their_terminal = create_location(
            cls.other_team,
            {"name": "Oceanterminalen", "location_type": LocationType.DEPOT, "parent_location": cls.their_port},
        )
        Shipment.objects.create(
            team=cls.other_team,
            shipment_number="SHP-THEIRS",
            status=Shipment.Status.IN_TRANSIT,
            destination_port="Gothenburg",
            destination_location=cls.their_terminal,
            eta=today + timedelta(days=2),
            original_eta=today + timedelta(days=2),
        )
        cls.my_terminal = create_location(cls.team, {"name": "My Depot"})
        Shipment.objects.create(
            team=cls.team,
            shipment_number="SHP-MINE",
            status=Shipment.Status.IN_TRANSIT,
            destination_location=cls.my_terminal,
            eta=today + timedelta(days=2),
            original_eta=today + timedelta(days=2),
        )

    def test_another_teams_location_id_matches_nothing_for_me(self):
        queue = get_arrival_queue(
            self.team, parse_arrival_queue_filters({"destination_location": str(self.their_terminal.pk)})
        )
        self.assertEqual(queue.objects, [])

    def test_another_teams_port_id_does_not_expand_to_their_subtree(self):
        queue = get_arrival_queue(
            self.team, parse_arrival_queue_filters({"destination_location": str(self.their_port.pk)})
        )
        self.assertEqual([obj.label for obj in queue.objects], [])

    def test_the_place_dropdown_does_not_leak_another_teams_locations(self):
        queue = get_arrival_queue(self.team, parse_arrival_queue_filters({}))
        self.assertEqual([location.name for location in queue.destination_location_choices], ["My Depot"])

    def test_my_own_filter_still_works(self):
        queue = get_arrival_queue(
            self.team, parse_arrival_queue_filters({"destination_location": str(self.my_terminal.pk)})
        )
        self.assertEqual([obj.label for obj in queue.objects], ["SHP-MINE"])
