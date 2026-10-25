"""Lifecycle safety of automatic receive (stages 2e, 2g): activation boundary, history, backdated,
late and future-dated receipts, newer physical movements, concurrency, undo after receive, and the receipts page.
No Business Central calls."""

from datetime import date, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.choices import LocationSource, MovementType
from apps.scm.containers.intake import create_or_get_container
from apps.scm.containers.models import Container, ContainerMovement
from apps.scm.containers.movements import record_container_movement
from apps.scm.containers.receive import receive_container
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralSyncInProgressError
from apps.scm.integrations.business_systems.business_central.item_ledger_sync import upsert_item_ledger_entries
from apps.scm.integrations.business_systems.business_central.locks import cache_lock_key
from apps.scm.integrations.business_systems.business_central.receipt_receive import process_purchase_receipts
from apps.scm.integrations.business_systems.business_central.receipt_resolution import processing_lock_name
from apps.scm.integrations.business_systems.business_central.tests.test_receipt_receive import (
    BOUNDARY,
    map_location,
    terminal,
)
from apps.scm.integrations.business_systems.business_central.tests.test_receipt_resolution import (
    COMPANY,
    RECENT,
    bc_receipt,
    ile,
    load,
    receipt_setup,
)
from apps.scm.integrations.location_forms import LocationMappingForm
from apps.scm.integrations.models import (
    BusinessCentralReceiptContainer,
    ReceiptResolution,
    ReceiveReason,
    ReceiveStatus,
)
from apps.scm.integrations.tests.test_location_mappings import ledger_synced, mapping_data
from apps.teams.models import Membership
from apps.teams.roles import ROLE_ADMIN, ROLE_MEMBER

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-rcpt-life"}}
Line = BusinessCentralReceiptContainer
S, W = ReceiveStatus, ReceiveReason
STOCKHOLM = ZoneInfo("Europe/Stockholm")


def at(day: date, hour: int = 0) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=STOCKHOLM)


@override_settings(CACHES=_LOCMEM)
class LifecycleTestBase(TestCase):
    def setUp(self):
        self.integration, self.user, self.t20 = receipt_setup("rcpt-life")
        self.team = self.integration.team
        self.location = terminal(self.team)
        self.elsewhere = terminal(self.team, "Customer yard")
        self.mapping = map_location(self.integration, "L005", self.location)

    def tearDown(self):
        cache.clear()

    def _container(self, number="MCUU2901785"):
        return create_or_get_container(
            team=self.team, user=self.user, number=number, attributes={"equipment_type": self.t20}
        )[0]

    def _receipt(self, number="IN1", entry_no=BOUNDARY + 1, serial="MCUU2901785", posted=None):
        load(
            self.integration,
            [bc_receipt(number, [("CON22G1", 1)], posted=posted)],
            [ile(entry_no, serial, doc=number, posted=posted)],
        )

    def _process(self):
        return process_purchase_receipts(self.integration)

    def _line(self, receipt="IN1", serial="MCUU2901785"):
        return Line.objects.get(receipt__integration=self.integration, receipt__receipt_no=receipt, serial_no=serial)

    def _status(self, receipt="IN1"):
        line = self._line(receipt)
        return line.receive_status, line.receive_reason


class HistoryAndActivationTest(LifecycleTestBase):
    def test_entry_before_the_boundary_is_historical_even_with_a_recent_posting_date(self):
        self.mapping.auto_create_containers = True
        self.mapping.save()
        self._receipt(entry_no=BOUNDARY, posted=date.today())
        self._process()
        self.assertEqual(self._status(), (S.NOT_RECEIVED, W.HISTORICAL))
        self.assertFalse(Container.objects.exists())  # history never creates containers

    def test_enabling_auto_receive_after_the_historical_import_receives_only_new_receipts(self):
        other = receipt_setup("rcpt-life-enable")[0]
        location = terminal(other.team)
        load(other, [bc_receipt("IN1", [("CON22G1", 1)])], [ile(500, "MCUU2901785")])
        ledger_synced(other, 500)
        create_or_get_container(
            team=other.team, user=None, number="MCUU2901785", attributes={"equipment_type": self.t20}
        )
        create_or_get_container(
            team=other.team, user=None, number="TCLU1234568", attributes={"equipment_type": self.t20}
        )
        form = LocationMappingForm(mapping_data(location), integration=other)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        process_purchase_receipts(other)
        self.assertEqual(Line.objects.get(receipt__integration=other).receive_reason, W.HISTORICAL)

        load(other, [bc_receipt("IN2", [("CON22G1", 1)])], [ile(501, "TCLU1234568", doc="IN2")])
        process_purchase_receipts(other)
        self.assertEqual(
            Line.objects.get(receipt__receipt_no="IN2", receipt__integration=other).receive_status, S.RECEIVED
        )
        self.assertEqual(ContainerMovement.objects.filter(team=other.team).count(), 1)

    def test_old_posting_date_on_a_new_entry_is_received_when_pegasus_registers_it(self):
        """The age of a posting date never blocks on its own; the entryNo boundary keeps history out."""
        self._container()
        posted = date.today() - timedelta(days=400)
        self._receipt(posted=posted)
        before = timezone.now()
        self._process()
        after = timezone.now()
        line = self._line()
        self.assertEqual((line.receive_status, line.posting_date), (S.RECEIVED, posted))
        self.assertTrue(before <= line.movement.occurred_at <= after)  # not 00:00 of the posting date
        self.assertEqual(line.received_at, line.movement.occurred_at)
        self.assertIn(f"posted {posted}", line.movement.notes)

    def test_backdated_receipt_after_a_later_movement_is_held_for_review(self):
        container = self._container()
        posted = date.today() - timedelta(days=60)
        record_container_movement(
            team=self.team, container=container, movement_type=MovementType.GATE_IN, to_location=self.elsewhere,
            occurred_at=at(date.today() - timedelta(days=10), 9),
        )  # fmt: skip
        self._receipt(posted=posted)
        self._process()
        self.assertEqual(self._status(), (S.NOT_RECEIVED, W.NEWER_MOVEMENT))
        self.assertEqual(ContainerMovement.objects.count(), 1)

    def test_future_posting_date_is_held_for_review(self):
        self._container()
        self._receipt(posted=date.today() + timedelta(days=3))
        self._process()
        self.assertEqual(self._status(), (S.NOT_RECEIVED, W.FUTURE_POSTING_DATE))
        self.assertFalse(ContainerMovement.objects.exists())


class PhysicalOrderTest(LifecycleTestBase):
    def test_later_movement_elsewhere_blocks_the_receive(self):
        container = self._container()
        record_container_movement(
            team=self.team, container=container, movement_type=MovementType.GATE_IN, to_location=self.elsewhere,
            occurred_at=at(RECENT, 15),
        )  # fmt: skip
        self._receipt()
        self._process()
        self.assertEqual(self._status(), (S.NOT_RECEIVED, W.NEWER_MOVEMENT))
        container.refresh_from_db()
        self.assertEqual((container.current_location, ContainerMovement.objects.count()), (self.elsewhere, 1))

    def test_delayed_receipt_whose_arrival_was_already_recorded_adds_nothing(self):
        container = self._container()
        posted = RECENT - timedelta(days=5)
        record_container_movement(
            team=self.team, container=container, movement_type=MovementType.GATE_IN, to_location=self.location,
            occurred_at=at(posted, 9),
        )  # fmt: skip
        record_container_movement(
            team=self.team, container=container, movement_type=MovementType.GATE_OUT, to_location=self.elsewhere,
            occurred_at=at(RECENT, 9),
        )  # fmt: skip
        self._receipt(posted=posted)
        self._process()
        # The arrival on the posting day is found first: the receive is already recorded, nothing is added.
        self.assertEqual(self._status(), (S.ALREADY_RECEIVED, ""))
        self.assertEqual(ContainerMovement.objects.count(), 2)

    def test_operator_gate_in_on_the_posting_day_counts_as_already_received(self):
        container = self._container()
        receive_container(team=self.team, container=container, location=self.location, occurred_at=at(RECENT, 10))
        self._receipt()
        self._process()
        self.assertEqual(self._status(), (S.ALREADY_RECEIVED, ""))
        self.assertEqual(ContainerMovement.objects.count(), 1)

    def test_container_already_at_the_location_is_already_received(self):
        container = self._container()
        receive_container(
            team=self.team, container=container, location=self.location, occurred_at=at(RECENT - timedelta(days=3))
        )
        self._receipt()
        self._process()
        self.assertEqual(self._status(), (S.ALREADY_RECEIVED, ""))
        self.assertEqual(ContainerMovement.objects.count(), 1)

    def test_earlier_movement_elsewhere_does_not_block(self):
        container = self._container()
        record_container_movement(
            team=self.team, container=container, movement_type=MovementType.GATE_IN, to_location=self.elsewhere,
            occurred_at=at(RECENT - timedelta(days=3)),
        )  # fmt: skip
        self._receipt()
        self._process()
        self.assertEqual(self._status(), (S.RECEIVED, ""))
        container.refresh_from_db()
        self.assertEqual(container.current_location, self.location)

    def test_receive_by_another_path_after_the_posting_is_not_duplicated(self):
        container = self._container()
        receive_container(
            team=self.team, container=container, location=self.location, occurred_at=at(RECENT),
            source=LocationSource.API,
        )  # fmt: skip
        self._receipt()
        self._process()
        self.assertEqual(self._status(), (S.ALREADY_RECEIVED, ""))
        self.assertEqual(ContainerMovement.objects.count(), 1)

    def test_same_serial_on_a_later_receipt_is_not_received_twice(self):
        self._container()
        self._receipt("IN1", BOUNDARY + 1, posted=RECENT - timedelta(days=1))
        self._process()
        self._receipt("IN2", BOUNDARY + 2)
        self._process()
        self.assertEqual(self._status("IN1"), (S.RECEIVED, ""))
        self.assertEqual(self._status("IN2"), (S.ALREADY_RECEIVED, ""))
        self.assertEqual(ContainerMovement.objects.count(), 1)


class ConcurrencyAndUndoTest(LifecycleTestBase):
    def test_parallel_processing_is_refused_and_changes_nothing(self):
        self._container()
        self._receipt()
        cache.set(cache_lock_key(processing_lock_name(self.integration)), "held", 60)
        with self.assertRaises(BusinessCentralSyncInProgressError):
            self._process()
        self.assertFalse(Line.objects.exists())
        self.assertFalse(ContainerMovement.objects.exists())

    def test_receipt_undone_after_receive_is_flagged_not_reversed(self):
        self._container()
        self._receipt()
        self._process()
        upsert_item_ledger_entries(self.integration, COMPANY, [ile(BOUNDARY + 5, "MCUU2901785", qty=-1)])
        self._process()
        line = self._line()
        self.assertEqual((line.receive_status, line.resolution_status), (S.RECEIVED, ReceiptResolution.REVERSED))
        self.assertTrue(ContainerMovement.objects.filter(pk=line.movement_id).exists())


class ReceiptsPageTest(LifecycleTestBase):
    def _user(self, email, role):
        from apps.users.models import CustomUser

        user = CustomUser.objects.create_user(username=email, email=email, password="pw")
        Membership.objects.create(team=self.team, user=user, role=role)
        self.client.force_login(user)
        session = self.client.session
        session["team"] = self.team.pk
        session.save()

    def test_admin_sees_counts_and_reasons(self):
        self._container()
        self._receipt("IN1")
        load(
            self.integration,
            [bc_receipt("IN2", [("CON22G1", 1)])],
            [ile(BOUNDARY + 2, "TCLU1234568", doc="IN2", loc="L001")],
        )
        self._process()
        self._user("rcpt-admin@example.com", ROLE_ADMIN)
        url = reverse("integrations:receipts", args=[self.integration.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((resp.context["summary"]["received"], resp.context["summary"]["not_received"]), (1, 1))
        self.assertContains(resp, "Unknown BC location")
        resp = self.client.get(url, {"reason": W.UNKNOWN_LOCATION})
        self.assertEqual([row.serial_no for row in resp.context["rows"]], ["TCLU1234568"])

    def test_process_now_queues_a_pass(self):
        self._user("rcpt-admin2@example.com", ROLE_ADMIN)
        with mock.patch(
            "apps.scm.integrations.receipt_views.process_business_central_purchase_receipts_task.delay"
        ) as d:
            self.client.post(reverse("integrations:receipts_process", args=[self.integration.pk]))
        d.assert_called_once_with(self.integration.pk)

    def test_member_gets_404(self):
        self._user("rcpt-member@example.com", ROLE_MEMBER)
        for method, name in (("get", "integrations:receipts"), ("post", "integrations:receipts_process")):
            with self.subTest(name):
                url = reverse(name, args=[self.integration.pk])
                self.assertEqual(getattr(self.client, method)(url).status_code, 404)
