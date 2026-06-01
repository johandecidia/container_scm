"""Tests for tracking timeline helpers."""

from django.test import TestCase
from django.utils import timezone

from apps.scm.shipments.models import Shipment
from apps.scm.tracking.models import TrackingEvent, TrackingProvider
from apps.scm.tracking.timeline import TrackingTimelineItem, get_tracking_timeline_items_for_shipment
from apps.teams.models import Team


def _team(slug):
    return Team.objects.create(name=slug, slug=slug)


def _provider(code="TL_PROV"):
    return TrackingProvider.objects.create(
        code=code,
        name=f"Provider {code}",
        provider_type=TrackingProvider.ProviderType.MANUAL,
    )


class TrackingTimelineTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("tl-test-team")
        cls.provider = _provider("TL_TEST_PROV")
        cls.shipment = Shipment.objects.create(team=cls.team, shipment_number="SHP-TL-1")
        now = timezone.now()
        cls.event_early = TrackingEvent.objects.create(
            team=cls.team,
            provider=cls.provider,
            shipment=cls.shipment,
            event_type=TrackingEvent.EventType.GATE_IN,
            event_datetime=now - timezone.timedelta(hours=2),
            location_name="Hamburg",
            location_unlocode="DEHAM",
            description="Container gated in",
        )
        cls.event_late = TrackingEvent.objects.create(
            team=cls.team,
            provider=cls.provider,
            shipment=cls.shipment,
            event_type=TrackingEvent.EventType.LOADED_ON_VESSEL,
            event_datetime=now - timezone.timedelta(hours=1),
            location_name="Hamburg",
            description="Loaded on vessel",
        )

    def test_returns_timeline_items(self):
        items = get_tracking_timeline_items_for_shipment(self.team, self.shipment)
        self.assertEqual(len(items), 2)

    def test_items_are_tracking_timeline_item(self):
        items = get_tracking_timeline_items_for_shipment(self.team, self.shipment)
        self.assertIsInstance(items[0], TrackingTimelineItem)

    def test_item_has_required_fields(self):
        items = get_tracking_timeline_items_for_shipment(self.team, self.shipment)
        item = items[0]
        self.assertIsNotNone(item.title)
        self.assertIsNotNone(item.datetime)
        self.assertIsNotNone(item.source)
        self.assertEqual(item.type, "tracking")

    def test_items_sorted_newest_first(self):
        items = get_tracking_timeline_items_for_shipment(self.team, self.shipment)
        self.assertEqual(items[0].event_type, TrackingEvent.EventType.LOADED_ON_VESSEL)
        self.assertEqual(items[1].event_type, TrackingEvent.EventType.GATE_IN)

    def test_location_includes_unlocode(self):
        items = get_tracking_timeline_items_for_shipment(self.team, self.shipment)
        # Find the event with location_unlocode
        item = next(i for i in items if i.event_type == TrackingEvent.EventType.GATE_IN)
        self.assertIn("DEHAM", item.location)

    def test_empty_for_unrelated_shipment(self):
        other_shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-TL-EMPTY")
        items = get_tracking_timeline_items_for_shipment(self.team, other_shipment)
        self.assertEqual(items, [])
