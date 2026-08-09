"""The automatic polling chain, end to end and unattended.

Manual refresh has always worked; what this covers is the path nobody watches:

    beat → dispatch_due_tracking_subscriptions
         → sync_single_tracking_subscription
         → carrier call → parse → upsert
         → next_sync_at moved forward

Both halves matter. A dispatcher that queues the wrong subscriptions wastes a
carrier's rate limit on watches that are finished; a sync that forgets to move
next_sync_at forward re-polls the same reference on every tick forever.

The socket layer is the only thing mocked — no test here makes a network call.
"""

from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.integrations.carriers.maersk.client import MaerskClient
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.scm.shipments.models import Shipment
from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingSubscription
from apps.scm.tracking.polling import INTERVAL_AFTER_ARRIVAL, INTERVAL_IN_TRANSIT, base_interval_minutes
from apps.scm.tracking.selectors import get_due_tracking_subscriptions
from apps.scm.tracking.tasks import dispatch_due_tracking_subscriptions, sync_single_tracking_subscription
from apps.teams.models import Team

from .test_maersk_live_payload import CONTAINER_NUMBER, live_payload

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "auto-polling"}}
_DISPATCH_SYNC = "apps.scm.tracking.tasks.sync_single_tracking_subscription.delay"

API_KEY = "polling-secret-key"
MAERSK_CONFIG = {
    "base_url": "https://example.invalid/maersk",
    "tracking_path": "/track-and-trace/public-events",
    "auth_style": "api_key_header",
    "api_key_header_name": "Consumer-Key",
    "reference_params": {"container_number": "equipmentReference"},
    "max_retries": 0,
    "retry_backoff_seconds": 0,
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    """A carrier that always answers with the recorded live response."""

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else live_payload()
        self.calls = 0

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls += 1
        return FakeResponse(200, self.payload)


def make_container(team: Team, serial: str = "925896", owner: str = "TRD", check_digit: int = 3) -> Container:
    equipment_type = EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=check_digit,
        equipment_type=equipment_type,
    )


@override_settings(CACHES=_LOCMEM)
class AutomaticPollingChainTest(TestCase):
    """A due, verified subscription is polled unattended and rescheduled."""

    def setUp(self):
        self.team = Team.objects.create(name="auto-poll", slug="auto-poll")
        self.integration = Integration.objects.create(
            team=self.team,
            name="Maersk",
            provider_code="maersk",
            provider_family=Integration.ProviderFamily.CARRIER,
            api_style=Integration.ApiStyle.DCSA,
            config=MAERSK_CONFIG,
            is_active=True,
        )
        set_integration_credentials(self.integration, IntegrationCredential.AuthType.API_KEY, {"api_key": API_KEY})
        self.provider = TrackingProvider.objects.create(code="maersk", name="Maersk")
        self.container = make_container(self.team)
        self.subscription = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.provider,
            container=self.container,
            tracking_reference=CONTAINER_NUMBER,
            next_sync_at=timezone.now() - timedelta(minutes=1),
        )
        self.session = FakeSession()

    def _run_scheduled_sync(self, subscription_id=None) -> dict:
        """Drive the real dispatcher and the real sync task, with a fake socket."""
        client = MaerskClient(self.integration, session=self.session)
        with mock.patch("apps.scm.integrations.carriers.factory.build_carrier_client", return_value=client):
            if subscription_id is not None:
                return sync_single_tracking_subscription.run(subscription_id)
            queued = []
            with mock.patch(_DISPATCH_SYNC, side_effect=lambda pk: queued.append(pk)):
                dispatch_due_tracking_subscriptions.run()
            results = [sync_single_tracking_subscription.run(pk) for pk in queued]
            return {"queued": queued, "results": results}

    # -- the chain ----------------------------------------------------------

    def test_a_due_subscription_is_dispatched(self):
        with mock.patch(_DISPATCH_SYNC) as delay:
            dispatch_due_tracking_subscriptions.run()
        delay.assert_called_once_with(self.subscription.pk)

    def test_the_dispatched_sync_calls_the_carrier_and_stores_events(self):
        outcome = self._run_scheduled_sync()
        self.assertEqual(outcome["queued"], [self.subscription.pk])
        self.assertEqual(self.session.calls, 1)
        self.assertEqual(outcome["results"][0]["status"], "success")
        self.assertEqual(outcome["results"][0]["events_created"], 10)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 10)

    def test_events_from_an_unattended_sync_carry_location_and_vessel(self):
        self._run_scheduled_sync()
        arrival = TrackingEvent.objects.get(team=self.team, event_code="ARRI")
        self.assertEqual(arrival.location_unlocode, "SEGOT")
        self.assertEqual(arrival.vessel_name, "JEBEL ALI")
        self.assertEqual(arrival.voyage_number, "623W")

    def test_events_are_linked_to_the_subscriptions_container(self):
        self._run_scheduled_sync()
        for event in TrackingEvent.objects.filter(team=self.team):
            self.assertEqual(event.container_id, self.container.pk)

    def test_next_sync_at_is_moved_into_the_future(self):
        self._run_scheduled_sync()
        self.subscription.refresh_from_db()
        self.assertGreater(self.subscription.next_sync_at, timezone.now())

    def test_last_synced_at_is_recorded(self):
        before = timezone.now()
        self._run_scheduled_sync()
        self.subscription.refresh_from_db()
        self.assertGreaterEqual(self.subscription.last_synced_at, before)
        self.assertEqual(self.subscription.tracking_status, TrackingSubscription.TrackingStatus.TRACKING)

    def test_a_synced_subscription_is_no_longer_due(self):
        """Without this the dispatcher would re-queue it on the very next tick."""
        self._run_scheduled_sync()
        self.assertNotIn(self.subscription, list(get_due_tracking_subscriptions()))

    def test_a_second_scheduled_sync_creates_no_duplicates(self):
        self._run_scheduled_sync()
        TrackingSubscription.objects.filter(pk=self.subscription.pk).update(
            next_sync_at=timezone.now() - timedelta(minutes=1)
        )
        outcome = self._run_scheduled_sync()
        self.assertEqual(outcome["results"][0]["events_created"], 0)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 10)

    def test_a_missing_subscription_is_reported_not_raised(self):
        self.assertEqual(sync_single_tracking_subscription.run(9999999)["status"], "not_found")


@override_settings(CACHES=_LOCMEM)
class ShipmentEtaFromAutomaticSyncTest(TestCase):
    """An unattended sync moves the shipment's ETA and records the change.

    The derivation itself is covered in apps.scm.shipments; what is proved here is
    that the scheduled path reaches it at all — an ETA that only updates when someone
    presses Refresh is not carrier tracking.
    """

    def setUp(self):
        self.team = Team.objects.create(name="auto-eta", slug="auto-eta")
        self.integration = Integration.objects.create(
            team=self.team,
            name="Maersk",
            provider_code="maersk",
            provider_family=Integration.ProviderFamily.CARRIER,
            api_style=Integration.ApiStyle.DCSA,
            config=MAERSK_CONFIG,
            is_active=True,
        )
        set_integration_credentials(self.integration, IntegrationCredential.AuthType.API_KEY, {"api_key": API_KEY})
        self.provider = TrackingProvider.objects.create(code="maersk", name="Maersk")
        self.container = make_container(self.team)
        self.shipment = Shipment.objects.create(team=self.team, shipment_number="SHP-AUTO-ETA", carrier="Maersk")
        self.subscription = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.provider,
            container=self.container,
            shipment=self.shipment,
            tracking_reference=CONTAINER_NUMBER,
            next_sync_at=timezone.now() - timedelta(minutes=1),
        )

    def _in_transit_payload(self) -> dict:
        """The live response as it looked while the vessel was still at sea."""
        payload = live_payload()
        for event in payload["events"]:
            if event.get("transportEventTypeCode") == "ARRI":
                event["eventClassifierCode"] = "EST"
        return payload

    def _sync(self, payload):
        client = MaerskClient(self.integration, session=FakeSession(payload))
        with mock.patch("apps.scm.integrations.carriers.factory.build_carrier_client", return_value=client):
            return sync_single_tracking_subscription.run(self.subscription.pk)

    def test_an_estimated_arrival_sets_the_shipment_eta(self):
        self._sync(self._in_transit_payload())
        self.shipment.refresh_from_db()
        self.assertIsNotNone(self.shipment.eta)
        self.assertEqual(self.shipment.eta.isoformat(), "2026-07-16")

    def test_the_change_is_recorded_in_eta_history_with_full_precision(self):
        """The carrier said 20:27 local (18:27Z); rounding that to a date loses a day."""
        from datetime import UTC, datetime

        from apps.scm.tracking.models import ETAHistory

        self._sync(self._in_transit_payload())
        history = ETAHistory.objects.get(shipment=self.shipment)
        self.assertEqual(history.new_eta_at, datetime(2026, 7, 16, 18, 27, tzinfo=UTC))
        self.assertEqual(history.location_unlocode, "SEGOT")
        self.assertEqual(history.source, "maersk")
        self.assertIsNotNone(history.tracking_event_id)

    def test_an_actual_arrival_is_never_replaced_by_a_forecast(self):
        """The recorded response reports arrival as ACT; no ETA may be derived from it."""
        from apps.scm.tracking.models import ETAHistory

        self._sync(live_payload())
        self.shipment.refresh_from_db()
        self.assertIsNotNone(self.shipment.actual_arrival_at)
        self.assertIsNone(self.shipment.eta)
        self.assertEqual(ETAHistory.objects.filter(shipment=self.shipment).count(), 0)

    def test_actual_events_move_the_shipment_status(self):
        self._sync(live_payload())
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.ARRIVED)
        self.assertEqual(self.shipment.tracking_status, "Gate In")


class DispatchEligibilityTest(TestCase):
    """Which subscriptions the scheduler is allowed to poll."""

    def setUp(self):
        self.team = Team.objects.create(name="eligibility", slug="eligibility")
        self.provider = TrackingProvider.objects.create(code="maersk", name="Maersk")

    def _subscription(self, reference="TRDU9258963", **kwargs):
        kwargs.setdefault("next_sync_at", timezone.now() - timedelta(minutes=1))
        return TrackingSubscription.objects.create(
            team=self.team, provider=self.provider, tracking_reference=reference, **kwargs
        )

    def _due(self) -> list:
        return list(get_due_tracking_subscriptions())

    def test_an_active_due_subscription_is_eligible(self):
        self.assertIn(self._subscription(), self._due())

    def test_a_subscription_that_is_not_due_yet_is_not_dispatched(self):
        self._subscription(next_sync_at=timezone.now() + timedelta(hours=2))
        self.assertEqual(self._due(), [])

    def test_a_completed_subscription_is_not_dispatched(self):
        self._subscription(status=TrackingSubscription.Status.COMPLETED)
        self.assertEqual(self._due(), [])

    def test_a_cancelled_subscription_is_not_dispatched(self):
        self._subscription(status=TrackingSubscription.Status.CANCELLED)
        self.assertEqual(self._due(), [])

    def test_a_paused_subscription_is_not_dispatched(self):
        self._subscription(status=TrackingSubscription.Status.PAUSED)
        self.assertEqual(self._due(), [])

    def test_a_failed_subscription_is_eligible_once_its_backoff_has_passed(self):
        """Backoff is expressed in next_sync_at, so a failure defers, never disables."""
        failed = self._subscription(status=TrackingSubscription.Status.FAILED, consecutive_failures=3)
        self.assertIn(failed, self._due())

    def test_a_failed_subscription_still_inside_its_backoff_is_not_dispatched(self):
        self._subscription(
            status=TrackingSubscription.Status.FAILED,
            consecutive_failures=3,
            next_sync_at=timezone.now() + timedelta(hours=1),
        )
        self.assertEqual(self._due(), [])

    def test_teams_are_isolated(self):
        other_team = Team.objects.create(name="eligibility-other", slug="eligibility-other")
        mine = self._subscription()
        theirs = TrackingSubscription.objects.create(
            team=other_team,
            provider=self.provider,
            tracking_reference="MSKU0000000",
            next_sync_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertEqual(list(get_due_tracking_subscriptions(team=self.team)), [mine])
        self.assertEqual(list(get_due_tracking_subscriptions(team=other_team)), [theirs])

    def test_the_dispatcher_covers_every_team(self):
        other_team = Team.objects.create(name="eligibility-other-2", slug="eligibility-other-2")
        self._subscription()
        TrackingSubscription.objects.create(
            team=other_team,
            provider=self.provider,
            tracking_reference="MSKU0000000",
            next_sync_at=timezone.now() - timedelta(minutes=1),
        )
        with mock.patch(_DISPATCH_SYNC) as delay:
            result = dispatch_due_tracking_subscriptions.run()
        self.assertEqual(result["queued"], 2)
        self.assertEqual(delay.call_count, 2)


class PollingIntervalTest(TestCase):
    """The interval follows the state of the journey, for shipments and bare containers."""

    def setUp(self):
        self.team = Team.objects.create(name="intervals", slug="intervals")
        self.provider = TrackingProvider.objects.create(code="maersk", name="Maersk")
        self.container = make_container(self.team)
        self.subscription = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.provider,
            container=self.container,
            tracking_reference=CONTAINER_NUMBER,
            last_event_at=timezone.now(),
        )

    def _event(self, event_type, time_type=TrackingEvent.EventTimeType.ACTUAL):
        return TrackingEvent.objects.create(
            team=self.team,
            provider=self.provider,
            container=self.container,
            subscription=self.subscription,
            event_type=event_type,
            event_time_type=time_type,
            event_datetime=timezone.now(),
            event_fingerprint=f"fp-{event_type}-{time_type}",
        )

    def test_a_container_in_transit_is_polled_hourly(self):
        self.assertEqual(base_interval_minutes(self.subscription), INTERVAL_IN_TRANSIT)

    def test_a_container_that_has_arrived_is_polled_more_slowly(self):
        self._event(TrackingEvent.EventType.VESSEL_ARRIVED)
        self.assertEqual(base_interval_minutes(self.subscription), INTERVAL_AFTER_ARRIVAL)

    def test_an_estimated_arrival_does_not_slow_polling_down(self):
        """A forecast arrival is exactly when the ETA is still moving."""
        self._event(TrackingEvent.EventType.VESSEL_ARRIVED, TrackingEvent.EventTimeType.ESTIMATED)
        self.assertEqual(base_interval_minutes(self.subscription), INTERVAL_IN_TRANSIT)

    def test_a_shipments_own_arrival_milestone_still_governs(self):
        shipment = Shipment.objects.create(
            team=self.team,
            shipment_number="SHP-INTERVAL-1",
            carrier="Maersk",
            actual_arrival_at=timezone.now(),
        )
        self.subscription.shipment = shipment
        self.assertEqual(base_interval_minutes(self.subscription), INTERVAL_AFTER_ARRIVAL)

    def test_an_explicit_override_wins(self):
        self.subscription.sync_interval_minutes = 5
        self.assertEqual(base_interval_minutes(self.subscription), 5)


@override_settings(CACHES=_LOCMEM)
class PollingCompletionTest(TestCase):
    """Polling stops when there is nothing left to learn."""

    def setUp(self):
        self.team = Team.objects.create(name="completion", slug="completion")
        self.integration = Integration.objects.create(
            team=self.team,
            name="Maersk",
            provider_code="maersk",
            provider_family=Integration.ProviderFamily.CARRIER,
            api_style=Integration.ApiStyle.DCSA,
            config=MAERSK_CONFIG,
            is_active=True,
        )
        set_integration_credentials(self.integration, IntegrationCredential.AuthType.API_KEY, {"api_key": API_KEY})
        self.provider = TrackingProvider.objects.create(code="maersk", name="Maersk")
        self.container = make_container(self.team)
        self.subscription = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.provider,
            container=self.container,
            tracking_reference=CONTAINER_NUMBER,
            next_sync_at=timezone.now() - timedelta(minutes=1),
        )

    def _sync_with(self, payload):
        client = MaerskClient(self.integration, session=FakeSession(payload))
        with mock.patch("apps.scm.integrations.carriers.factory.build_carrier_client", return_value=client):
            return sync_single_tracking_subscription.run(self.subscription.pk)

    def _delivered_payload(self) -> dict:
        payload = live_payload()
        delivered = dict(payload["events"][-1])
        delivered["eventID"] = "delivered-event"
        delivered["equipmentEventTypeCode"] = "DELIVERED"
        delivered["description"] = "Delivered"
        payload["events"].append(delivered)
        return payload

    def test_an_in_progress_journey_keeps_polling(self):
        self._sync_with(live_payload())
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)
        self.assertIsNotNone(self.subscription.next_sync_at)

    def test_a_delivered_container_completes_and_stops(self):
        self._sync_with(self._delivered_payload())
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.COMPLETED)
        self.assertIsNone(self.subscription.next_sync_at)

    def test_a_completed_subscription_is_not_picked_up_again(self):
        self._sync_with(self._delivered_payload())
        with mock.patch(_DISPATCH_SYNC) as delay:
            dispatch_due_tracking_subscriptions.run()
        delay.assert_not_called()

    def test_an_arrived_shipment_keeps_polling_more_slowly(self):
        """Arrival is not the end: discharge, gate-out and delivery are still to come."""
        self._with_shipment()
        self._sync_with(live_payload())
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.shipment.status, Shipment.Status.ARRIVED)
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)
        self.assertGreater(
            self.subscription.next_sync_at,
            timezone.now() + timedelta(minutes=INTERVAL_IN_TRANSIT),
        )

    def test_a_cancelled_shipment_completes_its_subscription(self):
        """Where there is a shipment, its status is the terminal signal."""
        shipment = self._with_shipment(status=Shipment.Status.CANCELLED)
        self._sync_with(live_payload())
        self.subscription.refresh_from_db()
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, Shipment.Status.CANCELLED)
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.COMPLETED)
        self.assertIsNone(self.subscription.next_sync_at)

    def test_a_container_delivery_event_does_not_complete_a_shipment_subscription(self):
        """A shipment is delivered when its goods are received, not when a box moves.

        Guards the boundary the container fallback must not cross: with a shipment
        present, only the shipment's own status may end the watch.
        """
        self._with_shipment()
        self._sync_with(self._delivered_payload())
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, TrackingSubscription.Status.ACTIVE)

    def _with_shipment(self, **kwargs) -> Shipment:
        defaults = {"shipment_number": "SHP-COMPLETE-1", "carrier": "Maersk"}
        defaults.update(kwargs)
        shipment = Shipment.objects.create(team=self.team, **defaults)
        self.subscription.shipment = shipment
        self.subscription.save(update_fields=["shipment"])
        return shipment
