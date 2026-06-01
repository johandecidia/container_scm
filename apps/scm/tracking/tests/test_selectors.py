"""Tests for tracking selectors — team isolation is critical."""

from django.test import TestCase
from django.utils import timezone

from apps.scm.shipments.models import Shipment
from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingSubscription, TrackingSyncRun
from apps.scm.tracking.selectors import (
    get_due_tracking_subscriptions,
    get_team_tracking_subscriptions,
    get_tracking_events_for_shipment,
    get_tracking_events_for_team,
    get_tracking_subscription_for_team,
    get_tracking_sync_runs_for_subscription,
)
from apps.teams.models import Team


def _team(slug):
    return Team.objects.create(name=slug, slug=slug)


def _provider(code="SEL_PROV"):
    return TrackingProvider.objects.create(
        code=code,
        name=f"Provider {code}",
        provider_type=TrackingProvider.ProviderType.MANUAL,
    )


class GetTeamTrackingSubscriptionsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sel-sub-team")
        cls.other_team = _team("sel-sub-other-team")
        cls.provider = _provider("SEL_SUB_PROV")
        cls.own = TrackingSubscription.objects.create(
            team=cls.team, provider=cls.provider, tracking_reference="OWN-REF"
        )
        cls.other = TrackingSubscription.objects.create(
            team=cls.other_team, provider=cls.provider, tracking_reference="OTHER-REF"
        )

    def test_returns_own_team_subscriptions(self):
        qs = get_team_tracking_subscriptions(self.team)
        self.assertIn(self.own, qs)

    def test_does_not_return_other_team_subscriptions(self):
        qs = get_team_tracking_subscriptions(self.team)
        self.assertNotIn(self.other, qs)


class GetTrackingSubscriptionForTeamTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sel-get-sub-team")
        cls.other_team = _team("sel-get-sub-other-team")
        cls.provider = _provider("SEL_GET_PROV")
        cls.own = TrackingSubscription.objects.create(
            team=cls.team, provider=cls.provider, tracking_reference="GET-OWN"
        )
        cls.other = TrackingSubscription.objects.create(
            team=cls.other_team, provider=cls.provider, tracking_reference="GET-OTHER"
        )

    def test_returns_own_subscription(self):
        sub = get_tracking_subscription_for_team(self.team, self.own.pk)
        self.assertEqual(sub, self.own)

    def test_raises_for_other_team(self):
        with self.assertRaises(TrackingSubscription.DoesNotExist):
            get_tracking_subscription_for_team(self.team, self.other.pk)


class GetTrackingEventsForTeamTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sel-ev-team")
        cls.other_team = _team("sel-ev-other-team")
        cls.provider = _provider("SEL_EV_PROV")
        cls.sub = TrackingSubscription.objects.create(team=cls.team, provider=cls.provider, tracking_reference="EV-REF")
        cls.event = TrackingEvent.objects.create(team=cls.team, provider=cls.provider, subscription=cls.sub)
        cls.other_sub = TrackingSubscription.objects.create(
            team=cls.other_team, provider=cls.provider, tracking_reference="EV-OTHER"
        )
        cls.other_event = TrackingEvent.objects.create(
            team=cls.other_team, provider=cls.provider, subscription=cls.other_sub
        )

    def test_returns_own_team_events(self):
        qs = get_tracking_events_for_team(self.team)
        self.assertIn(self.event, qs)

    def test_does_not_return_other_team_events(self):
        qs = get_tracking_events_for_team(self.team)
        self.assertNotIn(self.other_event, qs)


class GetTrackingEventsForShipmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sel-ev-shp-team")
        cls.other_team = _team("sel-ev-shp-other-team")
        cls.provider = _provider("SEL_SHP_PROV")
        cls.shipment = Shipment.objects.create(team=cls.team, shipment_number="SHP-SEL-EV")
        cls.other_shipment = Shipment.objects.create(team=cls.other_team, shipment_number="SHP-SEL-EV-OTHER")
        cls.event = TrackingEvent.objects.create(team=cls.team, provider=cls.provider, shipment=cls.shipment)

    def test_returns_events_for_shipment(self):
        qs = get_tracking_events_for_shipment(self.team, self.shipment)
        self.assertIn(self.event, qs)

    def test_other_team_shipment_returns_empty(self):
        qs = get_tracking_events_for_shipment(self.team, self.other_shipment)
        self.assertEqual(list(qs), [])


class GetDueTrackingSubscriptionsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sel-due-team")
        cls.provider = _provider("SEL_DUE_PROV")
        # Due: active, no next_sync_at
        cls.due = TrackingSubscription.objects.create(
            team=cls.team,
            provider=cls.provider,
            tracking_reference="DUE-REF",
            status=TrackingSubscription.Status.ACTIVE,
            next_sync_at=None,
        )
        # Not due: paused
        cls.paused = TrackingSubscription.objects.create(
            team=cls.team,
            provider=cls.provider,
            tracking_reference="PAUSED-REF",
            status=TrackingSubscription.Status.PAUSED,
            next_sync_at=None,
        )
        # Not due: next_sync_at in the future
        cls.future = TrackingSubscription.objects.create(
            team=cls.team,
            provider=cls.provider,
            tracking_reference="FUTURE-REF",
            status=TrackingSubscription.Status.ACTIVE,
            next_sync_at=timezone.now() + timezone.timedelta(hours=1),
        )

    def test_due_subscription_returned(self):
        qs = get_due_tracking_subscriptions(team=self.team)
        self.assertIn(self.due, qs)

    def test_paused_subscription_not_returned(self):
        qs = get_due_tracking_subscriptions(team=self.team)
        self.assertNotIn(self.paused, qs)

    def test_future_subscription_not_returned(self):
        qs = get_due_tracking_subscriptions(team=self.team)
        self.assertNotIn(self.future, qs)


class GetTrackingSyncRunsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sel-sync-team")
        cls.provider = _provider("SEL_SYNC_PROV")
        cls.sub = TrackingSubscription.objects.create(
            team=cls.team, provider=cls.provider, tracking_reference="SYNC-SEL-REF"
        )
        cls.sync_run = TrackingSyncRun.objects.create(team=cls.team, provider=cls.provider, subscription=cls.sub)

    def test_returns_runs_for_subscription(self):
        qs = get_tracking_sync_runs_for_subscription(self.team, self.sub)
        self.assertIn(self.sync_run, qs)
