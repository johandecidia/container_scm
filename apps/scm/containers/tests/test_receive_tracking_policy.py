"""Stop-on-receive is the destination's setting, and every receive path reads it the same way.

Real tracking subscriptions through a direct carrier, whose Stop needs no remote call:
the lifecycle runs for real, nothing internal is mocked. Business Central receive is
covered in ``business_central/tests/test_receipt_receive.py``.
"""

from datetime import timedelta

from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.choices import MovementType
from apps.scm.containers.models import ContainerMovement
from apps.scm.containers.movements import record_container_movement
from apps.scm.containers.receive import (
    RECEIVED,
    TRACKING_NOT_ACTIVE,
    TRACKING_NOT_APPLIED,
    TRACKING_STOPPED,
    apply_receive_tracking_policy,
    bulk_receive,
    receive_container,
)
from apps.scm.containers.tests.test_receive import _LOCMEM, ReceiveTestCase, _auto_stop, _container, _line, _track
from apps.scm.tracking.lifecycle import is_container_tracked
from apps.scm.tracking.models import TrackingSubscription
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
Status = TrackingSubscription.Status


class LocationPolicyTest(ReceiveTestCase):
    """Two locations of one team: Oceanterminalen stops, the depot does not."""

    def setUp(self):
        super().setUp()
        _auto_stop(self.terminal)

    def _receive_at(self, location, container, hours_ago=1):
        return receive_container(
            team=self.team,
            container=container,
            location=location,
            occurred_at=timezone.now() - timedelta(hours=hours_ago),
        ).movement

    def test_receive_at_a_location_with_auto_stop_on_stops_tracking(self):
        subscription = _track(self.team, self.box)
        state, _message = apply_receive_tracking_policy(
            team=self.team, movement=self._receive_at(self.terminal, self.box)
        )
        subscription.refresh_from_db()
        self.assertEqual((state, subscription.status), (TRACKING_STOPPED, Status.CANCELLED))
        self.assertFalse(is_container_tracked(team=self.team, container=self.box))

    def test_receive_at_a_location_with_auto_stop_off_keeps_tracking(self):
        subscription = _track(self.team, self.box)
        state, _message = apply_receive_tracking_policy(team=self.team, movement=self._receive_at(self.depot, self.box))
        subscription.refresh_from_db()
        self.assertEqual((state, subscription.status), (TRACKING_NOT_APPLIED, Status.ACTIVE))

    def test_bulk_receive_at_each_location_follows_that_location(self):
        at_terminal, at_depot = _track(self.team, self.box), _track(self.team, self.second)
        terminal_result = bulk_receive(
            team=self.team, location=self.terminal, text=_line(self.box, "2026-09-17 16:00:51")
        )
        depot_result = bulk_receive(team=self.team, location=self.depot, text=_line(self.second, "2026-09-17 16:00:51"))
        at_terminal.refresh_from_db()
        at_depot.refresh_from_db()
        self.assertEqual(
            (terminal_result.rows[0].state, terminal_result.rows[0].tracking), (RECEIVED, TRACKING_STOPPED)
        )
        self.assertEqual((depot_result.rows[0].state, depot_result.rows[0].tracking), (RECEIVED, TRACKING_NOT_APPLIED))
        self.assertEqual((at_terminal.status, at_depot.status), (Status.CANCELLED, Status.ACTIVE))

    def test_switching_the_setting_applies_to_later_receives_only(self):
        _auto_stop(self.terminal, on=False)
        subscription = _track(self.team, self.box)
        first = self._receive_at(self.terminal, self.box, hours_ago=3)
        self.assertEqual(apply_receive_tracking_policy(team=self.team, movement=first)[0], TRACKING_NOT_APPLIED)

        _auto_stop(self.terminal, on=True)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.ACTIVE)  # nothing retroactive

        second = self._receive_at(self.terminal, self.box, hours_ago=1)
        self.assertEqual(apply_receive_tracking_policy(team=self.team, movement=second)[0], TRACKING_STOPPED)

    def test_a_repeated_receive_takes_no_second_stop_action(self):
        subscription = _track(self.team, self.box)
        apply_receive_tracking_policy(team=self.team, movement=self._receive_at(self.terminal, self.box, hours_ago=2))
        subscription.refresh_from_db()
        stopped_at = subscription.updated_at

        state, _message = apply_receive_tracking_policy(
            team=self.team, movement=self._receive_at(self.terminal, self.box, hours_ago=1)
        )
        subscription.refresh_from_db()
        self.assertEqual((state, subscription.updated_at), (TRACKING_NOT_ACTIVE, stopped_at))
        self.assertEqual(TrackingSubscription.objects.filter(container=self.box).count(), 1)

    def test_a_superseded_receive_stops_nothing(self):
        """A backdated receive that a later movement already overtook is not where the box is."""
        subscription = _track(self.team, self.box)
        record_container_movement(
            team=self.team,
            container=self.box,
            movement_type=MovementType.GATE_IN,
            to_location=self.depot,
            occurred_at=timezone.now() - timedelta(hours=1),
        )
        old = self._receive_at(self.terminal, self.box, hours_ago=5)
        self.assertEqual(apply_receive_tracking_policy(team=self.team, movement=old)[0], TRACKING_NOT_APPLIED)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.ACTIVE)

    def test_teams_are_isolated(self):
        """The other team's container with the same number keeps its watch."""
        _auto_stop(self.foreign_terminal)
        foreign_box = _container(self.other_team, "291303")
        mine, theirs = _track(self.team, self.box), _track(self.other_team, foreign_box)
        apply_receive_tracking_policy(team=self.team, movement=self._receive_at(self.terminal, self.box))
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual((mine.status, theirs.status), (Status.CANCELLED, Status.ACTIVE))


@override_settings(CACHES=_LOCMEM, STORAGES=_TEST_STORAGES)
class ManualReceiveTest(ReceiveTestCase):
    """A gate-in or receipt typed in the movement modal is a receive like any other."""

    def setUp(self):
        super().setUp()
        _auto_stop(self.terminal)
        user = CustomUser.objects.create_user(username="manual-receive@example.com", password="pw")
        self.team.members.add(user, through_defaults={"role": ROLE_MEMBER})
        self.client = Client()
        self.client.force_login(user)
        self.url = reverse("containers:record_movement", kwargs={"container_id": self.box.pk})

    def _post(self, movement_type, location=None):
        data = {"movement_type": movement_type, "occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M")}
        if location is not None:
            data["to_location"] = location.pk
        return self.client.post(self.url, data)

    def test_gate_in_at_an_auto_stop_location_stops_tracking(self):
        subscription = _track(self.team, self.box)
        self._post(MovementType.GATE_IN, self.terminal)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.CANCELLED)
        self.assertEqual(ContainerMovement.objects.get(container=self.box).to_location, self.terminal)

    def test_received_at_an_auto_stop_location_stops_tracking(self):
        subscription = _track(self.team, self.box)
        self._post(MovementType.RECEIVED, self.terminal)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.CANCELLED)

    def test_gate_in_at_a_location_without_auto_stop_keeps_tracking(self):
        subscription = _track(self.team, self.box)
        self._post(MovementType.GATE_IN, self.depot)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.ACTIVE)

    def test_a_departure_never_stops_tracking(self):
        record_container_movement(
            team=self.team,
            container=self.box,
            movement_type=MovementType.GATE_IN,
            to_location=self.depot,
            occurred_at=timezone.now() - timedelta(hours=2),
        )
        subscription = _track(self.team, self.box)
        self._post(MovementType.TRANSFER, self.terminal)  # an internal move, not an arrival
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Status.ACTIVE)
