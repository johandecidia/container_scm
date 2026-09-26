"""Receiving containers from a pasted report: preview, the write, idempotency, tracking.

The receive is a GATE_IN through ``record_container_movement``, so what is asserted
about it is that the existing machinery sees it — the projection, the arrival
lifecycle — rather than any receive-specific state. The tracking assertions are about
what Receive does *not* own: it asks the lifecycle's own Stop, or nothing.
"""

import pathlib
import threading
from datetime import UTC, datetime
from unittest import mock

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.scm.containers.choices import LocationSource, LocationType, MovementType
from apps.scm.containers.models import Container, ContainerLocation, ContainerMovement, EquipmentType
from apps.scm.containers.receive import (
    ALREADY_RECEIVED,
    DUPLICATE,
    INVALID,
    NOT_FOUND,
    READY,
    RECEIVED,
    TRACKING_NOT_ACTIVE,
    TRACKING_NOT_APPLIED,
    TRACKING_STOP_FAILED,
    TRACKING_STOPPED,
    bulk_receive,
    preview_bulk_receive,
    receive_container,
)
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.shipments.models import Shipment
from apps.scm.tracking import lifecycle
from apps.scm.tracking.lifecycle import is_container_tracked
from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
from apps.scm.tracking.models import TrackingSubscription
from apps.scm.tracking.preferences import set_team_stop_tracking_on_receive
from apps.scm.visibility.arrival_lifecycle import ArrivalState, get_container_arrival_lifecycle
from apps.teams.models import Team

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "receive"}}
SITE = "MCR AB - Oceanterminalen"


def _equipment(iso_code="22G1") -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code=iso_code, defaults={"category": "GP", "length_ft": 20, "description": "20' GP"}
    )[0]


def _container(team, serial: str, iso_code="22G1") -> Container:
    return Container.objects.create(
        team=team,
        owner_code="PSL",
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit("PSL", "U", serial),
        equipment_type=_equipment(iso_code),
    )


def _line(container: Container, when: str, iso_type="G1", site=SITE) -> str:
    return f"PSLU\t{container.serial_number}{container.check_digit}\t22\t{iso_type}\t{when}\t{site}"


def _track(team, container) -> TrackingSubscription:
    """A live watch through a direct carrier, whose stop needs no remote call."""
    return get_or_create_container_subscription(
        team=team, container=container, provider_code="maersk", provider_name="Maersk", carrier_code="maersk"
    )


def _aware(*args) -> datetime:
    return timezone.make_aware(datetime(*args))


@override_settings(CACHES=_LOCMEM)
class ReceiveTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Receive", slug="receive")
        cls.other_team = Team.objects.create(name="Other", slug="receive-other")
        cls.terminal = ContainerLocation.objects.create(
            team=cls.team, name="Oceanterminalen", location_type=LocationType.TERMINAL
        )
        cls.depot = ContainerLocation.objects.create(team=cls.team, name="MCR Depot")
        cls.foreign_terminal = ContainerLocation.objects.create(team=cls.other_team, name="Oceanterminalen")

    def setUp(self):
        self.box = _container(self.team, "291303")
        self.second = _container(self.team, "291379")


class PreviewTest(ReceiveTestCase):
    def preview(self, text):
        return preview_bulk_receive(team=self.team, location=self.terminal, text=text)

    def test_an_existing_container_is_ready_and_nothing_is_written(self):
        preview = self.preview(_line(self.box, "2026-09-17 16:00:51"))

        (row,) = preview.rows
        self.assertEqual(row.state, READY)
        self.assertEqual(row.container, self.box)
        self.assertEqual(row.occurred_at, _aware(2026, 9, 17, 16, 0, 51))
        self.assertFalse(ContainerMovement.objects.exists())

    def test_a_missing_container_is_not_found_and_not_created(self):
        preview = self.preview("PSLU\t2913529\t22\tG1\t2026-09-11 13:31:04\tx")

        self.assertEqual(preview.rows[0].state, NOT_FOUND)
        self.assertEqual(Container.objects.filter(serial_number="291352").count(), 0)

    def test_another_teams_container_is_not_a_match(self):
        foreign = _container(self.other_team, "291299")

        preview = self.preview(_line(foreign, "2026-09-11 13:29:31"))

        self.assertEqual(preview.rows[0].state, NOT_FOUND)
        self.assertIsNone(preview.rows[0].container)

    def test_an_iso_mismatch_is_a_warning_and_the_row_stays_ready(self):
        (row,) = self.preview(_line(self.box, "2026-09-17 16:00:51", iso_type="10")).rows

        self.assertEqual(row.state, READY)
        self.assertTrue(any("2210" in warning and "22G1" in warning for warning in row.warnings))
        self.box.refresh_from_db()
        self.assertEqual(self.box.equipment_type_id, "22G1")

    def test_a_site_that_does_not_name_the_destination_is_a_warning(self):
        (matching,) = self.preview(_line(self.box, "2026-09-17 16:00:51")).rows
        (other,) = self.preview(_line(self.box, "2026-09-17 16:00:51", site="Arken Depot")).rows

        self.assertEqual(matching.warnings, [])
        self.assertEqual(len(other.warnings), 1)
        self.assertEqual(other.state, READY)
        self.assertFalse(ContainerLocation.objects.filter(name__icontains="Arken").exists())

    def test_an_already_received_row_is_marked(self):
        receive_container(
            team=self.team, container=self.box, location=self.terminal, occurred_at=_aware(2026, 9, 17, 16, 0, 51)
        )

        (row,) = self.preview(_line(self.box, "2026-09-17 16:00:51")).rows

        self.assertEqual(row.state, ALREADY_RECEIVED)
        self.assertFalse(row.will_stop_tracking)

    def test_a_receive_at_another_time_or_place_is_not_the_same_receive(self):
        receive_container(
            team=self.team, container=self.box, location=self.depot, occurred_at=_aware(2026, 9, 17, 16, 0, 51)
        )
        receive_container(team=self.team, container=self.box, location=self.terminal, occurred_at=_aware(2026, 9, 1, 8))

        self.assertEqual(self.preview(_line(self.box, "2026-09-17 16:00:51")).rows[0].state, READY)

    def test_tracking_on_and_tracked_previews_a_stop(self):
        set_team_stop_tracking_on_receive(self.team, True)
        _track(self.team, self.box)

        rows = self.preview(
            "\n".join([_line(self.box, "2026-09-17 16:00:51"), _line(self.second, "2026-09-14 10:11:23")])
        ).rows

        self.assertTrue(rows[0].will_stop_tracking)
        self.assertFalse(rows[1].will_stop_tracking)  # Not tracked: Receive only.
        self.assertTrue(is_container_tracked(team=self.team, container=self.box))

    def test_tracking_off_previews_receive_only(self):
        _track(self.team, self.box)

        (row,) = self.preview(_line(self.box, "2026-09-17 16:00:51")).rows

        self.assertTrue(row.is_tracked)
        self.assertFalse(row.will_stop_tracking)

    def test_invalid_and_duplicate_rows_are_reported_in_line_order(self):
        line = _line(self.box, "2026-09-17 16:00:51")
        rows = self.preview("\n".join([line, "PSLU\t2913031\t22\t10\t2026-09-17 16:00:51\tx", line])).rows

        self.assertEqual([row.state for row in rows], [READY, INVALID, DUPLICATE])

    def test_another_teams_location_is_refused(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            preview_bulk_receive(
                team=self.team, location=self.foreign_terminal, text=_line(self.box, "2026-09-17 16:00:51")
            )


class ReceiveWriteTest(ReceiveTestCase):
    def test_a_receive_is_a_gate_in_movement_at_the_chosen_location(self):
        result = bulk_receive(team=self.team, location=self.terminal, text=_line(self.box, "2026-09-17 16:00:51"))

        self.assertEqual(result.rows[0].state, RECEIVED)
        movement = ContainerMovement.objects.get(container=self.box)
        self.assertEqual(movement.movement_type, MovementType.GATE_IN)
        self.assertEqual(movement.to_location, self.terminal)
        self.assertEqual(movement.occurred_at, _aware(2026, 9, 17, 16, 0, 51))
        self.assertEqual(movement.source, LocationSource.DEPOT)
        self.assertIn(SITE, movement.notes)
        self.assertIn("22G1", movement.notes)
        self.assertNotEqual(movement.created_at, movement.occurred_at)

    def test_the_current_location_is_projected_from_the_movement(self):
        bulk_receive(team=self.team, location=self.terminal, text=_line(self.box, "2026-09-17 16:00:51"))

        self.box.refresh_from_db()
        self.assertEqual(self.box.current_location, self.terminal)
        self.assertEqual(self.box.last_location_update, _aware(2026, 9, 17, 16, 0, 51))
        self.assertEqual(self.box.location_source, LocationSource.DEPOT)

    def test_an_older_receive_does_not_override_a_newer_position(self):
        receive_container(team=self.team, container=self.box, location=self.depot, occurred_at=_aware(2026, 9, 20, 9))

        bulk_receive(team=self.team, location=self.terminal, text=_line(self.box, "2026-09-17 16:00:51"))

        self.box.refresh_from_db()
        self.assertEqual(self.box.current_location, self.depot)

    def test_the_arrival_lifecycle_sees_the_receive(self):
        shipment = Shipment.objects.create(
            team=self.team,
            shipment_number="SHP-RECV",
            status=Shipment.Status.IN_TRANSIT,
            destination_location=self.terminal,
            actual_departure_at=_aware(2026, 9, 1),
        )
        bulk_receive(team=self.team, location=self.terminal, text=_line(self.box, "2026-09-17 16:00:51"))

        lifecycle = get_container_arrival_lifecycle(self.team, self.box, shipment)

        self.assertEqual(lifecycle.state, ArrivalState.ARRIVED)
        self.assertEqual(lifecycle.arrived_at, _aware(2026, 9, 17, 16, 0, 51))

    def test_container_masterdata_is_not_changed(self):
        bulk_receive(team=self.team, location=self.terminal, text=_line(self.box, "2026-09-17 16:00:51", iso_type="10"))

        self.box.refresh_from_db()
        self.assertEqual(self.box.equipment_type_id, "22G1")

    def test_not_found_rows_do_not_stop_the_others(self):
        text = "\n".join([_line(self.box, "2026-09-17 16:00:51"), "PSLU\t2913529\t22\tG1\t2026-09-11 13:31:04\tx"])

        result = bulk_receive(team=self.team, location=self.terminal, text=text)

        self.assertEqual([row.state for row in result.rows], [RECEIVED, NOT_FOUND])
        self.assertEqual(ContainerMovement.objects.count(), 1)


class IdempotencyTest(ReceiveTestCase):
    def test_the_same_receive_twice_records_one_movement(self):
        when = _aware(2026, 9, 17, 16, 0, 51)

        first = receive_container(team=self.team, container=self.box, location=self.terminal, occurred_at=when)
        second = receive_container(team=self.team, container=self.box, location=self.terminal, occurred_at=when)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.movement.pk, second.movement.pk)
        self.assertEqual(ContainerMovement.objects.count(), 1)

    def test_the_same_paste_twice_records_each_receive_once(self):
        text = "\n".join([_line(self.box, "2026-09-17 16:00:51"), _line(self.second, "2026-09-14 10:11:23")])

        bulk_receive(team=self.team, location=self.terminal, text=text)
        again = bulk_receive(team=self.team, location=self.terminal, text=text)

        self.assertEqual([row.state for row in again.rows], [ALREADY_RECEIVED, ALREADY_RECEIVED])
        self.assertEqual(ContainerMovement.objects.count(), 2)

    def test_a_row_repeated_inside_one_paste_is_received_once(self):
        line = _line(self.box, "2026-09-17 16:00:51")

        result = bulk_receive(team=self.team, location=self.terminal, text=f"{line}\n{line}")

        self.assertEqual([row.state for row in result.rows], [RECEIVED, DUPLICATE])
        self.assertEqual(ContainerMovement.objects.count(), 1)

    def test_a_receive_recorded_after_the_preview_is_not_repeated(self):
        """The service decides, not the preview: a second tab that confirmed first wins."""
        text = _line(self.box, "2026-09-17 16:00:51")
        stale = preview_bulk_receive(team=self.team, location=self.terminal, text=text)
        receive_container(
            team=self.team, container=self.box, location=self.terminal, occurred_at=_aware(2026, 9, 17, 16, 0, 51)
        )

        with mock.patch("apps.scm.containers.receive.preview_bulk_receive", return_value=stale):
            result = bulk_receive(team=self.team, location=self.terminal, text=text)

        self.assertEqual(result.rows[0].state, ALREADY_RECEIVED)
        self.assertEqual(ContainerMovement.objects.count(), 1)


@override_settings(CACHES=_LOCMEM)
class ConcurrentReceiveTest(TransactionTestCase):
    """Two confirms of the same receive at once, on two connections."""

    def test_concurrent_receives_record_one_movement(self):
        team = Team.objects.create(name="Race", slug="receive-race")
        terminal = ContainerLocation.objects.create(team=team, name="Oceanterminalen")
        box = _container(team, "291303")
        when = _aware(2026, 9, 17, 16, 0, 51)
        barrier = threading.Barrier(2)
        outcomes = []

        def confirm():
            try:
                barrier.wait()
                outcomes.append(receive_container(team=team, container=box, location=terminal, occurred_at=when))
            finally:
                connection.close()

        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(outcome.created for outcome in outcomes), [False, True])
        self.assertEqual(ContainerMovement.objects.filter(container=box).count(), 1)


class TrackingPolicyTest(ReceiveTestCase):
    def receive(self, *lines):
        return bulk_receive(team=self.team, location=self.terminal, text="\n".join(lines))

    def test_policy_off_leaves_tracking_untouched(self):
        _track(self.team, self.box)

        with mock.patch("apps.scm.tracking.lifecycle.stop_container_tracking") as stop:
            result = self.receive(_line(self.box, "2026-09-17 16:00:51"))

        stop.assert_not_called()
        self.assertEqual(result.rows[0].tracking, TRACKING_NOT_APPLIED)
        self.assertTrue(is_container_tracked(team=self.team, container=self.box))

    def test_policy_on_stops_a_tracked_container_through_the_lifecycle(self):
        set_team_stop_tracking_on_receive(self.team, True)
        subscription = _track(self.team, self.box)

        result = self.receive(_line(self.box, "2026-09-17 16:00:51"))

        self.assertEqual((result.rows[0].state, result.rows[0].tracking), (RECEIVED, TRACKING_STOPPED))
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, TrackingSubscription.Status.CANCELLED)
        self.assertEqual(result.counts["tracking_stopped"], 1)

    def test_policy_on_uses_the_existing_stop_service(self):
        set_team_stop_tracking_on_receive(self.team, True)
        _track(self.team, self.box)

        real_stop = lifecycle.stop_container_tracking
        with mock.patch("apps.scm.tracking.lifecycle.stop_container_tracking", wraps=real_stop) as stop:
            self.receive(_line(self.box, "2026-09-17 16:00:51"))

        stop.assert_called_once()
        self.assertEqual(stop.call_args.kwargs["container"], self.box)

    def test_policy_on_and_untracked_still_receives(self):
        set_team_stop_tracking_on_receive(self.team, True)

        result = self.receive(_line(self.box, "2026-09-17 16:00:51"))

        self.assertEqual((result.rows[0].state, result.rows[0].tracking), (RECEIVED, TRACKING_NOT_ACTIVE))

    def test_a_tracking_failure_keeps_the_receive(self):
        set_team_stop_tracking_on_receive(self.team, True)
        _track(self.team, self.box)

        with mock.patch("apps.scm.tracking.lifecycle.stop_container_tracking", side_effect=RuntimeError("boom")):
            result = self.receive(_line(self.box, "2026-09-17 16:00:51"), _line(self.second, "2026-09-14 10:11:23"))

        self.assertEqual([row.state for row in result.rows], [RECEIVED, RECEIVED])
        self.assertEqual(result.rows[0].tracking, TRACKING_STOP_FAILED)
        self.assertEqual(ContainerMovement.objects.count(), 2)
        self.box.refresh_from_db()
        self.assertEqual(self.box.current_location, self.terminal)

    def test_an_incomplete_provider_release_is_a_failure_beside_a_receive(self):
        from apps.scm.tracking.lifecycle import STOP_INCOMPLETE
        from apps.scm.tracking.manual_refresh import WARNING, RefreshResult

        set_team_stop_tracking_on_receive(self.team, True)
        incomplete = RefreshResult(level=WARNING, state=STOP_INCOMPLETE, message="provider refused", tracked=False)

        with mock.patch("apps.scm.tracking.lifecycle.stop_container_tracking", return_value=incomplete):
            result = self.receive(_line(self.box, "2026-09-17 16:00:51"))

        self.assertEqual((result.rows[0].state, result.rows[0].tracking), (RECEIVED, TRACKING_STOP_FAILED))
        self.assertEqual(result.rows[0].tracking_message, "provider refused")

    def test_an_already_received_row_does_not_stop_tracking_again(self):
        receive_container(
            team=self.team, container=self.box, location=self.terminal, occurred_at=_aware(2026, 9, 17, 16, 0, 51)
        )
        set_team_stop_tracking_on_receive(self.team, True)
        _track(self.team, self.box)

        with mock.patch("apps.scm.tracking.lifecycle.stop_container_tracking") as stop:
            result = self.receive(_line(self.box, "2026-09-17 16:00:51"))

        stop.assert_not_called()
        self.assertEqual(result.rows[0].state, ALREADY_RECEIVED)
        self.assertTrue(result.rows[0].is_tracked)

    def test_receive_contains_no_provider_specific_logic(self):
        source = (pathlib.Path(__file__).parents[1] / "receive.py").read_text().lower()

        for provider in ("traqo", "vizion", "maersk", "hapag", "cma_cgm", "provider_code", "provider =="):
            self.assertNotIn(provider, source)


class TeamIsolationTest(ReceiveTestCase):
    def test_another_teams_container_cannot_be_received(self):
        from django.core.exceptions import ValidationError

        foreign = _container(self.other_team, "291299")

        with self.assertRaises(ValidationError):
            receive_container(team=self.team, container=foreign, location=self.terminal, occurred_at=timezone.now())
        self.assertFalse(ContainerMovement.objects.exists())

    def test_another_teams_location_cannot_be_received_at(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            receive_container(
                team=self.team, container=self.box, location=self.foreign_terminal, occurred_at=timezone.now()
            )
        self.assertFalse(ContainerMovement.objects.exists())


class GateInSemanticsTest(ReceiveTestCase):
    def test_gate_date_time_in_is_a_gate_in_and_loc3_reads_arrived_not_received(self):
        shipment = Shipment.objects.create(
            team=self.team,
            shipment_number="SHP-GATE",
            status=Shipment.Status.IN_TRANSIT,
            destination_location=self.terminal,
            actual_departure_at=_aware(2026, 9, 1),
        )
        bulk_receive(team=self.team, location=self.terminal, text=_line(self.box, "2026-09-17 16:00:51"))

        movement = ContainerMovement.objects.get(container=self.box)
        self.assertEqual(movement.movement_type, MovementType.GATE_IN)
        self.assertNotEqual(movement.movement_type, MovementType.RECEIVED)
        lifecycle = get_container_arrival_lifecycle(self.team, self.box, shipment)
        self.assertEqual(lifecycle.state, ArrivalState.ARRIVED)
        self.assertIsNone(lifecycle.received_at)


class DestinationTimezoneTest(ReceiveTestCase):
    """The report's wall-clock time is the destination's, whoever pastes it."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.port = ContainerLocation.objects.create(team=cls.team, name="Göteborg", timezone="Europe/Stockholm")
        cls.terminal.parent_location = cls.port
        cls.terminal.save()

    def receive(self, when):
        return bulk_receive(team=self.team, location=self.terminal, text=_line(self.box, when))

    def test_a_summer_gate_in_is_read_as_cest_through_the_parent_port(self):
        with timezone.override("America/New_York"):  # The operator's own profile.
            result = self.receive("2026-09-17 16:00:51")

        self.assertEqual(result.timezone.name, "Europe/Stockholm")
        movement = ContainerMovement.objects.get(container=self.box)
        self.assertEqual(movement.occurred_at, datetime(2026, 9, 17, 14, 0, 51, tzinfo=UTC))
        self.assertIn("Europe/Stockholm", movement.notes)

    def test_a_winter_gate_in_is_read_as_cet(self):
        self.receive("2026-01-15 16:00:51")

        movement = ContainerMovement.objects.get(container=self.box)
        self.assertEqual(movement.occurred_at, datetime(2026, 1, 15, 15, 0, 51, tzinfo=UTC))

    def test_the_locations_own_timezone_beats_its_parents(self):
        self.terminal.timezone = "Europe/London"
        self.terminal.save()

        self.receive("2026-09-17 16:00:51")

        self.assertEqual(
            ContainerMovement.objects.get(container=self.box).occurred_at,
            datetime(2026, 9, 17, 15, 0, 51, tzinfo=UTC),
        )

    def test_idempotency_holds_across_operators_in_different_timezones(self):
        with timezone.override("America/New_York"):
            self.receive("2026-09-17 16:00:51")
        with timezone.override("Asia/Shanghai"):
            again = self.receive("2026-09-17 16:00:51")

        self.assertEqual(again.rows[0].state, ALREADY_RECEIVED)
        self.assertEqual(ContainerMovement.objects.count(), 1)

    def test_an_ambiguous_dst_time_is_a_warning(self):
        preview = preview_bulk_receive(
            team=self.team, location=self.terminal, text=_line(self.box, "2026-10-25 02:30:00")
        )

        self.assertEqual(preview.rows[0].state, READY)
        self.assertTrue(any("daylight-saving" in warning for warning in preview.rows[0].warnings))

    def test_without_any_location_timezone_the_active_one_is_used(self):
        with timezone.override("Asia/Shanghai"):
            preview = preview_bulk_receive(
                team=self.team, location=self.depot, text=_line(self.box, "2026-09-17 16:00:51")
            )

        self.assertFalse(preview.timezone.is_from_location)
        self.assertEqual(preview.rows[0].occurred_at, datetime(2026, 9, 17, 8, 0, 51, tzinfo=UTC))


class TrackingStatePreviewTest(ReceiveTestCase):
    """Preview and Confirm read the lifecycle's own definition of what Stop acts on."""

    def watch(self, status):
        subscription = _track(self.team, self.box)
        subscription.status = status
        subscription.save(update_fields=["status"])
        return subscription

    def preview_and_confirm(self):
        text = _line(self.box, "2026-09-17 16:00:51")
        preview = preview_bulk_receive(team=self.team, location=self.terminal, text=text)
        result = bulk_receive(team=self.team, location=self.terminal, text=text)
        return preview.rows[0], result.rows[0]

    def test_policy_on(self):
        cases = [
            (TrackingSubscription.Status.ACTIVE, True, TRACKING_STOPPED, TrackingSubscription.Status.CANCELLED),
            (TrackingSubscription.Status.PAUSED, True, TRACKING_STOPPED, TrackingSubscription.Status.CANCELLED),
            (TrackingSubscription.Status.CANCELLED, False, TRACKING_NOT_ACTIVE, TrackingSubscription.Status.CANCELLED),
            (None, False, TRACKING_NOT_ACTIVE, None),
        ]
        set_team_stop_tracking_on_receive(self.team, True)
        for status, previews_stop, outcome, final in cases:
            with self.subTest(status=status):
                ContainerMovement.objects.all().delete()
                TrackingSubscription.objects.all().delete()
                subscription = self.watch(status) if status else None

                preview_row, result_row = self.preview_and_confirm()

                self.assertEqual(preview_row.will_stop_tracking, previews_stop)
                self.assertEqual((result_row.state, result_row.tracking), (RECEIVED, outcome))
                if subscription is not None:
                    subscription.refresh_from_db()
                    self.assertEqual(subscription.status, final)

    def test_a_paused_watch_is_not_active_tracking_but_stop_still_acts_on_it(self):
        self.watch(TrackingSubscription.Status.PAUSED)

        preview_row, _result = self.preview_and_confirm()

        self.assertFalse(preview_row.is_tracked)
        self.assertTrue(preview_row.has_stoppable_tracking)

    def test_policy_off_touches_no_subscription_in_any_state(self):
        for status in (TrackingSubscription.Status.ACTIVE, TrackingSubscription.Status.PAUSED):
            with self.subTest(status=status):
                ContainerMovement.objects.all().delete()
                TrackingSubscription.objects.all().delete()
                subscription = self.watch(status)

                with mock.patch("apps.scm.tracking.lifecycle.stop_container_tracking") as stop:
                    preview_row, result_row = self.preview_and_confirm()

                stop.assert_not_called()
                self.assertFalse(preview_row.will_stop_tracking)
                self.assertEqual(result_row.tracking, TRACKING_NOT_APPLIED)
                subscription.refresh_from_db()
                self.assertEqual(subscription.status, status)

    def test_preview_and_stop_share_one_definition(self):
        from apps.scm.tracking.lifecycle import STOPPABLE_STATUSES, stoppable_subscriptions

        self.watch(TrackingSubscription.Status.PAUSED)

        self.assertIn(TrackingSubscription.Status.PAUSED, STOPPABLE_STATUSES)
        with mock.patch("apps.scm.tracking.lifecycle.stoppable_subscriptions", wraps=stoppable_subscriptions) as spy:
            set_team_stop_tracking_on_receive(self.team, True)
            self.preview_and_confirm()

        # Twice by the previews (one inside bulk_receive) and once by the stop itself.
        self.assertEqual(spy.call_count, 3)
