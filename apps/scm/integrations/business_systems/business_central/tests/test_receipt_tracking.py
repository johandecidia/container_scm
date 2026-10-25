"""Business Central receive through the real tracking lifecycle (stage 2g).

Every test runs the whole chain — receipt and ledger entries → resolution → receive →
the destination's stop-on-receive → ``stop_container_tracking`` — against real
``TrackingSubscription`` rows. The subscriptions are direct-carrier watches whose stop
needs no remote call, so nothing internal is mocked; where a test counts stop calls it
wraps the real function rather than replacing it.
"""

from datetime import date, timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.scm.containers.choices import MovementType
from apps.scm.containers.intake import create_or_get_container
from apps.scm.containers.models import ContainerMovement
from apps.scm.containers.movements import record_container_movement
from apps.scm.containers.receive import TRACKING_NOT_APPLIED, TRACKING_STOPPED, receive_container
from apps.scm.containers.tests.test_receive import _track as track
from apps.scm.integrations.business_systems.business_central.receipt_receive import process_purchase_receipts
from apps.scm.integrations.business_systems.business_central.tests.test_receipt_lifecycle import at
from apps.scm.integrations.business_systems.business_central.tests.test_receipt_receive import (
    BOUNDARY,
    map_location,
    terminal,
)
from apps.scm.integrations.business_systems.business_central.tests.test_receipt_resolution import (
    RECENT,
    bc_receipt,
    ile,
    load,
    receipt_setup,
)
from apps.scm.integrations.models import BusinessCentralReceiptContainer, ReceiveReason, ReceiveStatus
from apps.scm.tracking import lifecycle
from apps.scm.tracking.lifecycle import is_container_tracked
from apps.scm.tracking.models import TrackingSubscription

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-rcpt-tracking"}}
_REAL_STOP = lifecycle.stop_container_tracking
Line = BusinessCentralReceiptContainer
S, W = ReceiveStatus, ReceiveReason
Status = TrackingSubscription.Status


@override_settings(CACHES=_LOCMEM)
class ReceiptTrackingLifecycleTest(TestCase):
    """Oceanterminalen (L005) stops tracking on receive; Conspect (L006) does not."""

    def setUp(self):
        self.integration, self.user, self.t20 = receipt_setup("rcpt-tracking")
        self.team = self.integration.team
        self.terminal = terminal(self.team, "Oceanterminalen")
        self.terminal.auto_stop_tracking_on_receive = True
        self.terminal.save()
        self.conspect = terminal(self.team, "Conspect")
        self.elsewhere = terminal(self.team, "Customer yard")
        map_location(self.integration, "L005", self.terminal)
        map_location(self.integration, "L006", self.conspect)

    def tearDown(self):
        cache.clear()

    def _container(self, number="MCUU2901785", team=None):
        team = team or self.team
        return create_or_get_container(team=team, user=None, number=number, attributes={"equipment_type": self.t20})[0]

    def _receipt(self, number="IN1", serial="MCUU2901785", *, entry_no=BOUNDARY + 1, loc="L005", posted=None):
        load(
            self.integration,
            [bc_receipt(number, [("CON22G1", 1)], posted=posted)],
            [ile(entry_no, serial, doc=number, loc=loc, posted=posted)],
        )

    def _process(self):
        with mock.patch("apps.scm.tracking.lifecycle.stop_container_tracking", wraps=_REAL_STOP) as stop:
            process_purchase_receipts(self.integration)
        return stop

    def _line(self, number="IN1"):
        return Line.objects.get(receipt__integration=self.integration, receipt__receipt_no=number)

    def test_new_receipt_at_an_auto_stop_destination_stops_tracking(self):
        container = self._container()
        subscription = track(self.team, container)
        self._receipt()
        stop = self._process()

        line = self._line()
        movement = ContainerMovement.objects.get(container=container)
        self.assertEqual((movement.movement_type, movement.to_location), (MovementType.GATE_IN, self.terminal))
        self.assertEqual(
            (line.receive_status, line.movement, line.tracking_result), (S.RECEIVED, movement, TRACKING_STOPPED)
        )
        stop.assert_called_once()
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.CANCELLED)  # the lifecycle's own end state for Stop
        self.assertFalse(is_container_tracked(team=self.team, container=container))

    def test_new_receipt_at_a_destination_without_auto_stop_keeps_tracking(self):
        container = self._container()
        subscription = track(self.team, container)
        self._receipt(loc="L006")
        stop = self._process()

        self.assertEqual(
            (self._line().receive_status, self._line().tracking_result), (S.RECEIVED, TRACKING_NOT_APPLIED)
        )
        stop.assert_not_called()
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.ACTIVE)
        self.assertTrue(is_container_tracked(team=self.team, container=container))

    def test_two_destinations_in_one_pass_follow_their_own_setting(self):
        at_terminal, at_conspect = self._container("MCUU2901785"), self._container("TCLU1234568")
        terminal_watch, conspect_watch = track(self.team, at_terminal), track(self.team, at_conspect)
        self._receipt("IN1", "MCUU2901785", entry_no=BOUNDARY + 1, loc="L005")
        self._receipt("IN2", "TCLU1234568", entry_no=BOUNDARY + 2, loc="L006")
        self._process()
        terminal_watch.refresh_from_db()
        conspect_watch.refresh_from_db()
        self.assertEqual((terminal_watch.status, conspect_watch.status), (Status.CANCELLED, Status.ACTIVE))

    def test_a_paused_watch_is_stopped_the_way_stop_always_does(self):
        container = self._container()
        subscription = track(self.team, container)
        subscription.status = Status.PAUSED
        subscription.save(update_fields=["status"])
        self._receipt()
        self._process()
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.CANCELLED)

    def test_old_posting_date_on_a_new_entry_is_received_and_stopped(self):
        container = self._container()
        subscription = track(self.team, container)
        self._receipt(posted=date.today() - timedelta(days=120))
        self._process()
        subscription.refresh_from_db()
        self.assertEqual((self._line().receive_status, subscription.status), (S.RECEIVED, Status.CANCELLED))

    def test_historical_receipt_receives_nothing_and_stops_nothing(self):
        container = self._container()
        subscription = track(self.team, container)
        self._receipt(entry_no=BOUNDARY)
        stop = self._process()
        self.assertEqual((self._line().receive_status, self._line().receive_reason), (S.NOT_RECEIVED, W.HISTORICAL))
        stop.assert_not_called()
        self.assertFalse(ContainerMovement.objects.exists())
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.ACTIVE)

    def test_already_received_container_is_not_received_or_stopped_again(self):
        container = self._container()
        receive_container(team=self.team, container=container, location=self.terminal, occurred_at=at(RECENT, 10))
        subscription = track(self.team, container)
        self._receipt()
        stop = self._process()
        self.assertEqual(self._line().receive_status, S.ALREADY_RECEIVED)
        stop.assert_not_called()
        self.assertEqual(ContainerMovement.objects.count(), 1)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.ACTIVE)

    def test_later_physical_movement_holds_the_receipt_and_keeps_tracking(self):
        container = self._container()
        record_container_movement(
            team=self.team, container=container, movement_type=MovementType.GATE_IN, to_location=self.elsewhere,
            occurred_at=at(RECENT, 15),
        )  # fmt: skip
        subscription = track(self.team, container)
        self._receipt()
        stop = self._process()
        self.assertEqual((self._line().receive_status, self._line().receive_reason), (S.NOT_RECEIVED, W.NEWER_MOVEMENT))
        stop.assert_not_called()
        container.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual((container.current_location, subscription.status), (self.elsewhere, Status.ACTIVE))

    def test_backdated_receipt_with_a_later_movement_never_stops_tracking(self):
        container = self._container()
        record_container_movement(
            team=self.team, container=container, movement_type=MovementType.GATE_OUT, to_location=self.elsewhere,
            from_location=self.terminal, occurred_at=at(date.today() - timedelta(days=5), 8),
        )  # fmt: skip
        subscription = track(self.team, container)
        self._receipt(posted=date.today() - timedelta(days=30))
        self._process()
        self.assertEqual(self._line().receive_reason, W.NEWER_MOVEMENT)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.ACTIVE)

    def test_same_receipt_twice_receives_and_stops_once(self):
        container = self._container()
        track(self.team, container)
        self._receipt()
        first = self._process()
        self._receipt()  # the same receipt and entry read again
        second = self._process()
        self.assertEqual((first.call_count, second.call_count), (1, 0))
        self.assertEqual(ContainerMovement.objects.filter(container=container).count(), 1)
        self.assertEqual(Line.objects.filter(receipt__integration=self.integration).count(), 1)

    def test_other_teams_container_and_watch_are_untouched(self):
        other = receipt_setup("rcpt-tracking-other")[0]
        theirs = self._container(team=other.team)
        their_watch = track(other.team, theirs)
        mine = self._container()
        my_watch = track(self.team, mine)
        self._receipt()
        self._process()
        my_watch.refresh_from_db()
        their_watch.refresh_from_db()
        self.assertEqual((my_watch.status, their_watch.status), (Status.CANCELLED, Status.ACTIVE))
        self.assertFalse(ContainerMovement.objects.filter(team=other.team).exists())
