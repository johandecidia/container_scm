"""Automatic receive from posted purchase receipts (stage 2d). No Business Central calls."""

from datetime import datetime, time
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.scm.containers.choices import LocationSource, MovementType
from apps.scm.containers.intake import create_or_get_container
from apps.scm.containers.models import Container, ContainerLocation, ContainerMovement
from apps.scm.containers.receive import TRACKING_NOT_APPLIED, TRACKING_STOPPED
from apps.scm.containers.tests.test_receive import _track as track
from apps.scm.integrations.business_systems.business_central.receipt_receive import process_purchase_receipts
from apps.scm.integrations.business_systems.business_central.tests.test_receipt_resolution import (
    COMPANY,
    bc_receipt,
    ile,
    load,
    receipt_setup,
)
from apps.scm.integrations.models import (
    BusinessCentralLocationMapping,
    BusinessCentralReceiptContainer,
    ReceiveReason,
    ReceiveStatus,
)
from apps.scm.tracking import lifecycle
from apps.scm.tracking.models import TrackingSubscription

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-rcpt-receive"}}
Mapping = BusinessCentralLocationMapping
Line = BusinessCentralReceiptContainer
S, W = ReceiveStatus, ReceiveReason
Status = TrackingSubscription.Status
BOUNDARY = 1000


def terminal(team, name="Oceanterminalen") -> ContainerLocation:
    return ContainerLocation.objects.create(team=team, name=name, location_type="terminal", timezone="Europe/Stockholm")


def map_location(integration, code, location=None, *, auto=True, create=False, active=True, after=BOUNDARY, **extra):
    """A mapping saved directly (the form's checks are tested in test_location_mappings)."""
    physical = location is not None
    return Mapping.objects.create(
        team=integration.team,
        integration=integration,
        bc_location_code=code,
        location_type=Mapping.LocationType.PHYSICAL if physical else Mapping.LocationType.CUSTOMER,
        pegasus_location=location,
        receive_trigger=Mapping.ReceiveTrigger.POSTED_PURCHASE_RECEIPT if physical else Mapping.ReceiveTrigger.NONE,
        auto_receive_enabled=auto and physical,
        auto_receive_after_entry_no=after if auto and physical else None,
        auto_create_containers=create,
        is_active=active,
        **extra,
    )


@override_settings(CACHES=_LOCMEM)
class AutomaticReceiveTest(TestCase):
    def setUp(self):
        self.integration, self.user, self.t20 = receipt_setup("rcpt-receive")
        self.team = self.integration.team
        self.location = terminal(self.team)
        self.mapping = map_location(self.integration, "L005", self.location)
        map_location(self.integration, "L005UTHYRT")

    def tearDown(self):
        cache.clear()

    def _container(self, number="MCUU2901785"):
        return create_or_get_container(
            team=self.team, user=self.user, number=number, attributes={"equipment_type": self.t20}
        )[0]

    def _process(self):
        return process_purchase_receipts(self.integration)

    def _line(self, serial="MCUU2901785"):
        return Line.objects.get(receipt__integration=self.integration, serial_no=serial)

    def _status(self, serial="MCUU2901785"):
        line = self._line(serial)
        return line.receive_status, line.receive_reason

    def test_new_receipt_receives_an_existing_container(self):
        container = self._container()
        load(self.integration, [bc_receipt("IN1", [("CON22G1", 1)])], [ile(BOUNDARY + 1, "MCUU2901785")])
        result = self._process()

        self.assertEqual(result.received, ["MCUU2901785"])
        line = self._line()
        self.assertEqual((line.receive_status, line.receive_reason, line.container_created), (S.RECEIVED, "", False))
        movement = line.movement
        self.assertEqual(
            (movement.container, movement.movement_type, movement.to_location, movement.source),
            (container, MovementType.GATE_IN, self.location, LocationSource.API),
        )
        expected = datetime.combine(line.posting_date, time.min, tzinfo=ZoneInfo("Europe/Stockholm"))
        self.assertEqual(movement.occurred_at, expected)
        self.assertIn("IN1", movement.notes)
        container.refresh_from_db()
        self.assertEqual((container.current_location, container.equipment_type_id), (self.location, "22G1"))

    def test_several_containers_on_several_lines_of_the_same_item(self):
        serials = ["MCUU2901785", "TCLU1234568", "CRTU4101684"]
        for serial in serials:
            self._container(serial)
        load(
            self.integration,
            [bc_receipt("IN2", [("CON22G1", 2), ("CON22G1", 1)])],
            [ile(BOUNDARY + i, s, doc="IN2") for i, s in enumerate(serials, start=1)],
        )
        self._process()
        self.assertEqual(ContainerMovement.objects.filter(movement_type=MovementType.GATE_IN).count(), 3)
        self.assertTrue(all(self._status(s) == (S.RECEIVED, "") for s in serials))

    def test_missing_container_is_not_created_without_permission(self):
        load(self.integration, [bc_receipt("IN3", [("CON22G1", 1)])], [ile(BOUNDARY + 1, "MCUU2901785", doc="IN3")])
        self._process()
        self.assertEqual(self._status(), (S.NOT_RECEIVED, W.CONTAINER_NOT_FOUND))
        self.assertFalse(Container.objects.exists())

    def test_missing_container_is_created_and_received_when_allowed(self):
        self.mapping.auto_create_containers = True
        self.mapping.save()
        load(self.integration, [bc_receipt("IN4", [("CON22G1", 1)])], [ile(BOUNDARY + 1, "MCUU2901785", doc="IN4")])
        self._process()
        line = self._line()
        self.assertEqual((line.receive_status, line.container_created), (S.RECEIVED, True))
        self.assertEqual((line.container.equipment_type_id, line.container.current_location), ("22G1", self.location))
        self.assertIsNone(line.container.created_by)

    def test_missing_equipment_type_creates_nothing(self):
        self.mapping.auto_create_containers = True
        self.mapping.save()
        load(
            self.integration,
            [bc_receipt("IN5", [("CONNOTYPE", 1)])],
            [ile(BOUNDARY + 1, "MCUU2901785", item="CONNOTYPE", doc="IN5")],
        )
        self._process()
        self.assertEqual(self._status(), (S.NOT_RECEIVED, W.MISSING_EQUIPMENT_TYPE))
        self.assertFalse(Container.objects.exists())

    def test_invalid_serial(self):
        load(self.integration, [bc_receipt("IN6", [("CON22G1", 1)])], [ile(BOUNDARY + 1, "MCUU2901786", doc="IN6")])
        self._process()
        self.assertEqual(self._status("MCUU2901786"), (S.NOT_RECEIVED, W.INVALID_SERIAL))

    def test_unknown_location(self):
        self._container()
        load(
            self.integration,
            [bc_receipt("IN7", [("CON22G1", 1)])],
            [ile(BOUNDARY + 1, "MCUU2901785", doc="IN7", loc="L001")],
        )
        self._process()
        self.assertEqual(self._status(), (S.NOT_RECEIVED, W.UNKNOWN_LOCATION))
        self.assertFalse(ContainerMovement.objects.exists())

    def test_location_without_auto_receive(self):
        map_location(self.integration, "L005PROD", terminal(self.team, "Produktion"), auto=False)
        self._container()
        load(
            self.integration,
            [bc_receipt("IN8", [("CON22G1", 1)])],
            [ile(BOUNDARY + 1, "MCUU2901785", doc="IN8", loc="L005PROD")],
        )
        self._process()
        self.assertEqual(self._status(), (S.NOT_RECEIVED, W.AUTO_RECEIVE_DISABLED))

    def test_commercial_rented_location_is_never_received(self):
        self._container()
        load(
            self.integration,
            [bc_receipt("IN9", [("CON22G1", 1)])],
            [ile(BOUNDARY + 1, "MCUU2901785", doc="IN9", loc="L005UTHYRT")],
        )
        self._process()
        self.assertEqual(self._status(), (S.NOT_RECEIVED, W.NOT_PHYSICAL))
        self.assertFalse(ContainerMovement.objects.exists())

    def test_inactive_location(self):
        self.mapping.is_active = False
        self.mapping.save()
        self._container()
        load(self.integration, [bc_receipt("IN10", [("CON22G1", 1)])], [ile(BOUNDARY + 1, "MCUU2901785", doc="IN10")])
        self._process()
        self.assertEqual(self._status(), (S.NOT_RECEIVED, W.LOCATION_INACTIVE))

    def test_unresolved_receipt_is_not_received(self):
        self._container()
        load(self.integration, [bc_receipt("IN11", [("CON22G1", 2)])], [ile(BOUNDARY + 1, "MCUU2901785", doc="IN11")])
        self._process()
        self.assertEqual(self._status(), (S.NOT_RECEIVED, W.RECEIPT_UNRESOLVED))

    def test_processing_twice_and_reimporting_receive_once(self):
        self._container()
        receipt, entry = bc_receipt("IN12", [("CON22G1", 1)]), ile(BOUNDARY + 1, "MCUU2901785", doc="IN12")
        load(self.integration, [receipt], [entry])
        self._process()
        load(self.integration, [receipt], [entry])
        result = self._process()
        self.assertEqual((result.received, result.checked), ([], 0))  # the row is final
        self.assertEqual(ContainerMovement.objects.count(), 1)
        self.assertEqual(Line.objects.count(), 1)

    def _auto_stop(self, on=True):
        self.location.auto_stop_tracking_on_receive = on
        self.location.save(update_fields=["auto_stop_tracking_on_receive"])

    def test_destination_with_auto_stop_on_stops_tracking(self):
        self._auto_stop()
        subscription = track(self.team, self._container())
        load(self.integration, [bc_receipt("IN13", [("CON22G1", 1)])], [ile(BOUNDARY + 1, "MCUU2901785", doc="IN13")])
        self._process()
        subscription.refresh_from_db()
        self.assertEqual((subscription.status, self._line().tracking_result), (Status.CANCELLED, TRACKING_STOPPED))

    def test_destination_with_auto_stop_off_keeps_tracking(self):
        subscription = track(self.team, self._container())
        load(self.integration, [bc_receipt("IN14", [("CON22G1", 1)])], [ile(BOUNDARY + 1, "MCUU2901785", doc="IN14")])
        self._process()
        subscription.refresh_from_db()
        line = self._line()
        self.assertEqual((line.receive_status, line.tracking_result), (S.RECEIVED, TRACKING_NOT_APPLIED))
        self.assertEqual(subscription.status, Status.ACTIVE)

    def test_already_received_is_not_stopped_again(self):
        self._auto_stop()
        track(self.team, self._container())
        load(self.integration, [bc_receipt("IN15", [("CON22G1", 1)])], [ile(BOUNDARY + 1, "MCUU2901785", doc="IN15")])
        real_stop = lifecycle.stop_container_tracking
        with mock.patch("apps.scm.tracking.lifecycle.stop_container_tracking", wraps=real_stop) as stop:
            self._process()
            self._process()
        stop.assert_called_once()

    def test_teams_are_isolated(self):
        other, other_user, t20 = receipt_setup("rcpt-receive-other")
        other_location = terminal(other.team)
        map_location(other, "L005", other_location)
        create_or_get_container(
            team=other.team, user=other_user, number="MCUU2901785", attributes={"equipment_type": t20}
        )
        load(other, [bc_receipt("IN16", [("CON22G1", 1)])], [ile(BOUNDARY + 1, "MCUU2901785", doc="IN16")])
        load(self.integration, [bc_receipt("IN16", [("CON22G1", 1)])], [ile(BOUNDARY + 1, "MCUU2901785", doc="IN16")])

        self._process()  # this team has no such container and may not create it
        self.assertEqual(self._status(), (S.NOT_RECEIVED, W.CONTAINER_NOT_FOUND))
        self.assertFalse(ContainerMovement.objects.exists())
        self.assertFalse(Line.objects.filter(receipt__integration=other).exists())  # untouched until its own pass

        process_purchase_receipts(other)
        movement = ContainerMovement.objects.get()
        self.assertEqual((movement.team, movement.to_location), (other.team, other_location))

    def test_entries_of_another_company_are_not_received(self):
        self._container()
        load(self.integration, [bc_receipt("IN17", [("CON22G1", 1)])], [])
        load(self.integration, [], [ile(BOUNDARY + 1, "MCUU2901785", doc="IN17")], company="other-company")
        self._process()
        self.assertFalse(Line.objects.exists())
        self.assertFalse(ContainerMovement.objects.exists())


@override_settings(CACHES=_LOCMEM)
class ProcessingTaskTest(TestCase):
    def tearDown(self):
        cache.clear()

    def test_receipt_and_ledger_syncs_queue_processing_only_when_receipts_are_enabled(self):
        from apps.scm.integrations.models import IntegrationSyncRun
        from apps.scm.integrations.tasks import _queue_receipt_processing_after

        integration, _user, _t20 = receipt_setup("rcpt-task")
        R = IntegrationSyncRun.ResourceType
        task = "apps.scm.integrations.tasks.process_business_central_purchase_receipts_task.delay"
        with mock.patch(task) as delay:
            _queue_receipt_processing_after(integration, R.PURCHASE_RECEIPTS)
            delay.assert_not_called()  # resource off
            integration.config = {"company_id": COMPANY, "resources": {R.PURCHASE_RECEIPTS: {"enabled": True}}}
            integration.save()
            _queue_receipt_processing_after(integration, R.PURCHASE_ORDERS)
            delay.assert_not_called()
            _queue_receipt_processing_after(integration, R.ITEM_LEDGER_ENTRIES)
            _queue_receipt_processing_after(integration, R.PURCHASE_RECEIPTS)
            self.assertEqual(delay.call_count, 2)
