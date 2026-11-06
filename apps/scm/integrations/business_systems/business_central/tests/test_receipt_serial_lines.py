"""Posted-receipt serials on purchase order lines (stage 3b.4). No Business Central calls.

Modelled on PO 100021 in the sandbox (verified 2026-10-27): line 10000 CON22G1, 49
ordered, 3 received on IN28149 (BBCU3272941, BBCU3273059) and IN28166 (BBCU3273090).
"""

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.intake import create_or_get_container
from apps.scm.containers.models import Container, ContainerMovement
from apps.scm.integrations.business_systems.business_central.receipt_resolution import resolve_purchase_receipts
from apps.scm.integrations.business_systems.business_central.receipt_serial_lines import (
    PostedSerialState,
    UnattributedReason,
    get_order_posted_serials,
    link_posted_receipt_serials,
)
from apps.scm.integrations.business_systems.business_central.tests.test_receipt_resolution import (
    COMPANY,
    bc_receipt,
    ile,
    load,
    receipt_setup,
)
from apps.scm.procurement.container_links import link_acquired_container, link_business_central_acquisition
from apps.scm.procurement.models import ContainerAcquisition, PurchaseOrder, PurchaseOrderLine, PurchaseOrderSource
from apps.scm.tracking.models import TrackingSubscription
from apps.teams.roles import ROLE_MEMBER

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "bc-rcpt-serials"}}
_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
State = PostedSerialState
SERIALS = ("BBCU3272941", "BBCU3273059", "BBCU3273090")


def receipt(number, lines, *, order="100021"):
    """A posted receipt for ``order`` with ``lines`` = [(sequence, item, quantity), ...]."""
    raw = bc_receipt(number, [(item, qty) for _, item, qty in lines])
    raw["orderNumber"] = order
    for raw_line, (sequence, _, _) in zip(raw["purchaseReceiptLines"], lines, strict=True):
        raw_line["sequence"] = sequence
    return raw


def reference_receipts():
    receipts = [receipt("IN28149", [(10000, "CON22G1", 2)]), receipt("IN28166", [(10000, "CON22G1", 1)])]
    entries = [
        ile(94829, SERIALS[0], doc="IN28149"),
        ile(94830, SERIALS[1], doc="IN28149"),
        ile(95055, SERIALS[2], doc="IN28166"),
    ]
    return receipts, entries


@override_settings(CACHES=_LOCMEM)
class _Case(TestCase):
    def setUp(self):
        self.integration, self.user, self.t20 = receipt_setup("rcpt-serials")
        self.team = self.integration.team
        self.po = self.order(self.team)
        self.line = self.po_line(self.po, "10000")

    def tearDown(self):
        cache.clear()

    @staticmethod
    def order(team, number="100021", source=PurchaseOrderSource.BUSINESS_CENTRAL) -> PurchaseOrder:
        return PurchaseOrder.objects.create(
            team=team, external_id=f"bc-{number}", po_number=number, source_system=source, source_company_id=COMPANY
        )

    @staticmethod
    def po_line(po, line_no, item="CON22G1", ordered=49, received=3) -> PurchaseOrderLine:
        return PurchaseOrderLine.objects.create(
            team=po.team,
            purchase_order=po,
            external_id=f"{po.external_id}-{line_no}",
            line_no=line_no,
            item_no=item,
            ordered_qty=ordered,
            received_qty=received,
        )

    def container(self, number, team=None) -> Container:
        return create_or_get_container(
            team=team or self.team, user=self.user, number=number, attributes={"equipment_type": self.t20}
        )[0]

    def load(self, receipts, entries):
        load(self.integration, receipts, entries)
        resolve_purchase_receipts(self.integration)

    def posted(self, po=None):
        return get_order_posted_serials(self.team, po or self.po)


class AttributionTest(_Case):
    def test_reference_order_serials_land_on_line_10000(self):
        self.load(*reference_receipts())
        posted = self.posted()
        serials = [(p.serial_no, p.receipt_no) for p in posted.for_line(self.line.pk)]
        self.assertEqual(serials, [("BBCU3272941", "IN28149"), ("BBCU3273059", "IN28149"), ("BBCU3273090", "IN28166")])
        self.assertEqual(posted.unattributed, [])
        self.assertTrue(all(p.state == State.NOT_IN_PEGASUS for p in posted.for_line(self.line.pk)))

    def test_receipt_of_another_order_is_not_used(self):
        self.load([receipt("IN1", [(10000, "CON22G1", 1)], order="100022")], [ile(1, SERIALS[0])])
        self.assertEqual(self.posted().by_line, {})

    def test_receipt_without_order_number_is_not_used(self):
        self.load([receipt("IN1", [(10000, "CON22G1", 1)], order="")], [ile(1, SERIALS[0])])
        self.assertEqual(self.posted().by_line, {})

    def test_same_item_on_two_lines_resolves_by_the_receipt_line(self):
        line_20000 = self.po_line(self.po, "20000")
        self.load(
            [receipt("IN1", [(10000, "CON22G1", 0), (20000, "CON22G1", 1)])],
            [ile(1, SERIALS[0])],
        )
        posted = self.posted()
        self.assertEqual(posted.for_line(self.line.pk), [])
        self.assertEqual([p.serial_no for p in posted.for_line(line_20000.pk)], [SERIALS[0]])

    def test_same_item_received_on_two_lines_of_one_receipt_is_not_guessed(self):
        self.po_line(self.po, "20000")
        self.load(
            [receipt("IN1", [(10000, "CON22G1", 1), (20000, "CON22G1", 1)])],
            [ile(1, SERIALS[0]), ile(2, SERIALS[1])],
        )
        posted = self.posted()
        self.assertEqual(posted.by_line, {})
        self.assertEqual({u.reason for u in posted.unattributed}, {UnattributedReason.SAME_ITEM_ON_SEVERAL_LINES})
        self.assertEqual(len(posted.unattributed), 2)

    def test_order_line_missing_in_pegasus_is_reported(self):
        self.load([receipt("IN1", [(30000, "CON22G1", 1)])], [ile(1, SERIALS[0])])
        posted = self.posted()
        self.assertEqual(posted.by_line, {})
        self.assertEqual(posted.unattributed[0].reason, UnattributedReason.ORDER_LINE_NOT_FOUND)

    def test_entry_without_serial_is_ignored(self):
        self.load([receipt("IN1", [(10000, "CON22G1", 1)])], [ile(1, "")])
        posted = self.posted()
        self.assertEqual(posted.by_line, {})
        self.assertEqual(posted.unattributed, [])

    def test_undone_receipt_is_not_a_received_container(self):
        self.load(
            [receipt("IN1", [(10000, "CON22G1", 1), (10000, "CON22G1", -1)])],
            [ile(1, SERIALS[0]), ile(2, SERIALS[0], qty=-1)],
        )
        self.assertEqual(self.posted().by_line, {})

    def test_same_serial_on_two_lines_of_the_order_is_not_guessed(self):
        line_20000 = self.po_line(self.po, "20000")
        self.load(
            [receipt("IN1", [(10000, "CON22G1", 1)]), receipt("IN2", [(20000, "CON22G1", 1)])],
            [ile(1, SERIALS[0], doc="IN1"), ile(2, SERIALS[0], doc="IN2")],
        )
        posted = self.posted()
        self.assertEqual(posted.for_line(self.line.pk), [])
        self.assertEqual(posted.for_line(line_20000.pk), [])
        self.assertEqual({u.reason for u in posted.unattributed}, {UnattributedReason.SEVERAL_LINES})

    def test_serial_posted_on_another_order_is_flagged(self):
        other = self.order(self.team, "100022")
        self.po_line(other, "10000")
        self.load(
            [receipt("IN1", [(10000, "CON22G1", 1)]), receipt("IN2", [(10000, "CON22G1", 1)], order="100022")],
            [ile(1, SERIALS[0], doc="IN1"), ile(2, SERIALS[0], doc="IN2")],
        )
        (posted,) = self.posted().for_line(self.line.pk)
        self.assertEqual(posted.state, State.SEVERAL_ORDERS)
        self.assertEqual(posted.other_orders, ("100022",))

    def test_manual_order_with_the_same_number_gets_nothing(self):
        manual = self.order(self.team, "100023", source=PurchaseOrderSource.MANUAL)
        self.load([receipt("IN1", [(10000, "CON22G1", 1)], order="100023")], [ile(1, SERIALS[0])])
        self.assertEqual(self.posted(manual).by_line, {})


class LinkingTest(_Case):
    def test_existing_container_is_linked_once(self):
        containers = [self.container(s) for s in SERIALS]
        self.load(*reference_receipts())
        first = link_posted_receipt_serials(self.integration)
        self.assertEqual((first.linked, first.already_linked), (3, 0))
        second = link_posted_receipt_serials(self.integration)
        self.assertEqual((second.linked, second.already_linked), (0, 3))
        self.assertEqual(
            set(ContainerAcquisition.objects.filter(purchase_order_line=self.line).values_list("container", flat=True)),
            {c.pk for c in containers},
        )
        self.assertTrue(all(p.state == State.LINKED for p in self.posted().for_line(self.line.pk)))

    def test_missing_container_is_not_created(self):
        self.load(*reference_receipts())
        result = link_posted_receipt_serials(self.integration)
        self.assertEqual((result.linked, result.not_in_pegasus), (0, 3))
        self.assertFalse(Container.objects.filter(team=self.team).exists())
        self.assertFalse(ContainerAcquisition.objects.exists())

    def test_container_acquired_elsewhere_is_a_conflict_and_is_not_moved(self):
        container = self.container(SERIALS[0])
        manual = self.order(self.team, "M-1", source=PurchaseOrderSource.MANUAL)
        manual_line = self.po_line(manual, "1")
        link_acquired_container(team=self.team, purchase_order_line=manual_line, container=container)
        self.load(*reference_receipts())
        result = link_posted_receipt_serials(self.integration)
        self.assertEqual(result.conflicts, 1)
        self.assertEqual(ContainerAcquisition.objects.get(container=container).purchase_order_line, manual_line)
        posted = next(p for p in self.posted().for_line(self.line.pk) if p.serial_no == SERIALS[0])
        self.assertEqual(posted.state, State.CONFLICT)
        self.assertEqual(posted.other_line, manual_line)

    def test_serial_on_two_orders_is_not_linked(self):
        self.container(SERIALS[0])
        other = self.order(self.team, "100022")
        self.po_line(other, "10000")
        self.load(
            [receipt("IN1", [(10000, "CON22G1", 1)]), receipt("IN2", [(10000, "CON22G1", 1)], order="100022")],
            [ile(1, SERIALS[0], doc="IN1"), ile(2, SERIALS[0], doc="IN2")],
        )
        result = link_posted_receipt_serials(self.integration)
        self.assertEqual(result.linked, 0)
        self.assertEqual(result.several_orders, 2)
        self.assertFalse(ContainerAcquisition.objects.exists())

    def test_other_team_container_is_never_used(self):
        from apps.teams.models import Team

        other = Team.objects.create(name="other-serials", slug="other-serials")
        self.container(SERIALS[0], team=other)
        self.load(*reference_receipts())
        link_posted_receipt_serials(self.integration)
        self.assertFalse(ContainerAcquisition.objects.exists())
        self.assertEqual(Container.objects.filter(team=self.team).count(), 0)

    def test_linking_has_no_physical_effect(self):
        containers = [self.container(s) for s in SERIALS]
        before = [(c.current_location_id, c.status) for c in containers]
        self.load(*reference_receipts())
        link_posted_receipt_serials(self.integration)
        self.assertFalse(ContainerMovement.objects.exists())
        self.assertFalse(TrackingSubscription.objects.exists())
        after = [(c.current_location_id, c.status) for c in Container.objects.filter(pk__in=[c.pk for c in containers])]
        self.assertEqual(sorted(before), sorted(after))
        self.assertFalse(self.integration.purchase_receipts.filter(containers__receive_status="received").exists())

    def test_business_central_writer_refuses_a_manual_order(self):
        manual = self.order(self.team, "M-2", source=PurchaseOrderSource.MANUAL)
        with self.assertRaises(PermissionDenied):
            link_business_central_acquisition(
                team=self.team, purchase_order_line=self.po_line(manual, "1"), container=self.container(SERIALS[0])
            )

    def test_manual_writer_still_refuses_a_business_central_order(self):
        with self.assertRaises(PermissionDenied):
            link_acquired_container(team=self.team, purchase_order_line=self.line, container=self.container(SERIALS[0]))


@override_settings(STORAGES=_TEST_STORAGES)
class OrderPageTest(_Case):
    def setUp(self):
        super().setUp()
        self.team.members.add(self.user, through_defaults={"role": ROLE_MEMBER})

    def _page(self, user=None) -> str:
        client = Client()
        client.force_login(user or self.user)
        response = client.get(reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": self.po.pk}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_posted_serials_and_receipts_are_shown(self):
        self.load(*reference_receipts())
        page = self._page()
        for serial in SERIALS:
            self.assertIn(serial, page)
        self.assertIn("IN28149", page)
        self.assertIn("IN28166", page)
        self.assertIn("not in Pegasus", page)

    def test_existing_container_is_linked(self):
        container = self.container(SERIALS[0])
        self.load(*reference_receipts())
        page = self._page()
        self.assertIn(reverse("containers:detail", kwargs={"container_id": container.pk}), page)

    def test_conflict_is_shown(self):
        container = self.container(SERIALS[0])
        manual = self.order(self.team, "M-3", source=PurchaseOrderSource.MANUAL)
        link_acquired_container(team=self.team, purchase_order_line=self.po_line(manual, "1"), container=container)
        self.load(*reference_receipts())
        self.assertIn("acquired through M-3 line 1", self._page())

    def test_unattributed_serials_are_listed(self):
        self.po_line(self.po, "20000")
        self.load(
            [receipt("IN1", [(10000, "CON22G1", 1), (20000, "CON22G1", 1)])], [ile(1, SERIALS[0]), ile(2, SERIALS[1])]
        )
        page = self._page()
        self.assertIn("Posted serials not placed on a line", page)
        # One group per receipt and reason, listing every serial — not one row per serial.
        self.assertEqual(page.count("Same item on several receipt lines"), 1)
        self.assertIn(SERIALS[0], page)
        self.assertIn(SERIALS[1], page)

    def test_long_source_location_name_is_truncated_with_full_name_in_title(self):
        name = "Lager MCR05 - Lager Oceantermialen i JohnEvans"
        PurchaseOrderLine.objects.filter(pk=self.line.pk).update(location_code="L005", location_name=name)
        page = self._page()
        self.assertIn(f'class="block max-w-[10rem] truncate text-base-content/60" title="{name}"', page)

    def test_other_team_sees_nothing(self):
        from apps.teams.models import Team
        from apps.users.models import CustomUser

        self.load(*reference_receipts())
        outsider = CustomUser.objects.create_user(username="serials-outsider@example.com", password="pw")
        other = Team.objects.create(name="Outsider", slug="serials-outsider")
        other.members.add(outsider, through_defaults={"role": ROLE_MEMBER})
        client = Client()
        client.force_login(outsider)
        response = client.get(reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": self.po.pk}))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(get_order_posted_serials(other, self.po).by_line, {})


class TaskTest(_Case):
    def test_purchase_order_sync_queues_linking_and_receipts_do_not(self):
        from unittest import mock

        from apps.scm.integrations.models import IntegrationSyncRun
        from apps.scm.integrations.tasks import _queue_receipt_serial_linking_after

        R = IntegrationSyncRun.ResourceType
        with mock.patch("apps.scm.integrations.tasks.link_business_central_receipt_serials_task.delay") as delay:
            _queue_receipt_serial_linking_after(self.integration, R.PURCHASE_RECEIPTS)
            delay.assert_not_called()
            _queue_receipt_serial_linking_after(self.integration, R.PURCHASE_ORDERS)
            delay.assert_called_once_with(self.integration.pk)

    def test_receipt_processing_links_existing_containers(self):
        from apps.scm.integrations.tasks import process_business_central_purchase_receipts_task

        self.container(SERIALS[0])
        load(self.integration, *reference_receipts())
        result = process_business_central_purchase_receipts_task(self.integration.pk)
        self.assertEqual(result["serial_links"]["linked"], 1)
        self.assertEqual(result["serial_links"]["not_in_pegasus"], 2)
        self.assertFalse(ContainerMovement.objects.exists())
