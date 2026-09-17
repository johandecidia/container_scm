"""Hand-entered purchase order lines, and the Business Central wall around them.

The write API and the views are both covered here, because the read-only rule is
only worth anything if it holds on every path into the data: a service call, a form
post, an HTMX post. A rule enforced in one of the three is a rule with two ways
around it.
"""

from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse

from apps.scm.procurement.manual import (
    create_purchase_order_line,
    delete_purchase_order_line,
    update_purchase_order,
    update_purchase_order_line,
)
from apps.scm.procurement.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderSource,
)
from apps.teams.models import Membership, Team
from apps.users.models import CustomUser


def _order(team, source, po_number="PO-1") -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=f"{team.slug}-{source}-{po_number}",
        po_number=po_number,
        supplier_name="Supplier",
        source_system=source,
    )


def _line(order, line_no="10000") -> PurchaseOrderLine:
    return PurchaseOrderLine.objects.create(
        team=order.team,
        purchase_order=order,
        external_id=f"{order.external_id}-{line_no}",
        line_no=line_no,
        item_no="DOORS",
        ordered_qty=Decimal("100"),
    )


class ManualLineServiceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Manual", slug="manual-lines")
        cls.manual = _order(cls.team, PurchaseOrderSource.MANUAL, "MAN-1")
        cls.bc = _order(cls.team, PurchaseOrderSource.BUSINESS_CENTRAL, "BC-1")

    def test_creates_a_line_with_a_generated_external_id(self):
        """A manual line has no source key, but the column is part of the order's uniqueness."""
        line = create_purchase_order_line(
            team=self.team,
            purchase_order=self.manual,
            line_no="10000",
            item_no="DOORS",
            description="Steel doors",
            ordered_qty=Decimal("100"),
        )

        self.assertEqual(line.purchase_order, self.manual)
        self.assertEqual(line.ordered_qty, Decimal("100"))
        self.assertTrue(line.external_id.startswith("manual-"))

    def test_two_manual_lines_on_one_order_do_not_collide(self):
        create_purchase_order_line(team=self.team, purchase_order=self.manual, line_no="10000", item_no="A")
        create_purchase_order_line(team=self.team, purchase_order=self.manual, line_no="20000", item_no="B")

        self.assertEqual(self.manual.lines.count(), 2)

    def test_updates_a_manual_line(self):
        line = _line(self.manual)

        update_purchase_order_line(line=line, ordered_qty=Decimal("250"), description="Revised")

        line.refresh_from_db()
        self.assertEqual(line.ordered_qty, Decimal("250"))
        self.assertEqual(line.description, "Revised")

    def test_deletes_a_manual_line(self):
        line = _line(self.manual)

        delete_purchase_order_line(line=line)

        self.assertFalse(PurchaseOrderLine.objects.filter(pk=line.pk).exists())

    def test_updates_a_manual_order_header(self):
        update_purchase_order(purchase_order=self.manual, supplier_name="New Supplier")

        self.manual.refresh_from_db()
        self.assertEqual(self.manual.supplier_name, "New Supplier")

    # -- the Business Central wall ------------------------------------------

    def test_refuses_to_create_a_line_on_a_bc_order(self):
        with self.assertRaises(PermissionDenied):
            create_purchase_order_line(team=self.team, purchase_order=self.bc, line_no="10000", item_no="X")
        self.assertFalse(self.bc.lines.exists())

    def test_refuses_to_update_a_bc_line(self):
        line = _line(self.bc)

        with self.assertRaises(PermissionDenied):
            update_purchase_order_line(line=line, ordered_qty=Decimal("999"))

        line.refresh_from_db()
        self.assertEqual(line.ordered_qty, Decimal("100"))

    def test_refuses_to_delete_a_bc_line(self):
        line = _line(self.bc)

        with self.assertRaises(PermissionDenied):
            delete_purchase_order_line(line=line)

        self.assertTrue(PurchaseOrderLine.objects.filter(pk=line.pk).exists())

    def test_refuses_to_update_a_bc_order_header(self):
        with self.assertRaises(PermissionDenied):
            update_purchase_order(purchase_order=self.bc, supplier_name="Hijacked")

        self.bc.refresh_from_db()
        self.assertEqual(self.bc.supplier_name, "Supplier")


class ManualLineViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Manual views", slug="manual-line-views")
        cls.user = CustomUser.objects.create_user(username="lines@example.com", password="pw")
        Membership.objects.create(team=cls.team, user=cls.user, role="admin")
        cls.manual = _order(cls.team, PurchaseOrderSource.MANUAL, "MAN-V")
        cls.bc = _order(cls.team, PurchaseOrderSource.BUSINESS_CENTRAL, "BC-V")

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["team"] = self.team.pk
        session.save()

    def test_create_line_form_renders(self):
        response = self.client.get(
            reverse("procurement:purchase_order_line_create", kwargs={"purchase_order_id": self.manual.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add order line")

    def test_posting_the_form_creates_a_line(self):
        response = self.client.post(
            reverse("procurement:purchase_order_line_create", kwargs={"purchase_order_id": self.manual.pk}),
            {"line_no": "10000", "item_no": "DOORS", "description": "Steel doors", "ordered_qty": "100"},
        )

        self.assertEqual(response.status_code, 302)
        line = self.manual.lines.get()
        self.assertEqual(line.item_no, "DOORS")
        self.assertEqual(line.team, self.team)

    def test_editing_a_line_through_the_view(self):
        line = _line(self.manual)

        response = self.client.post(
            reverse("procurement:purchase_order_line_update", kwargs={"line_id": line.pk}),
            {"line_no": "10000", "item_no": "DOORS", "description": "", "ordered_qty": "175"},
        )

        self.assertEqual(response.status_code, 302)
        line.refresh_from_db()
        self.assertEqual(line.ordered_qty, Decimal("175"))

    def test_deleting_a_line_through_the_view(self):
        line = _line(self.manual)

        response = self.client.delete(reverse("procurement:purchase_order_line_delete", kwargs={"line_id": line.pk}))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(PurchaseOrderLine.objects.filter(pk=line.pk).exists())

    def test_editing_a_manual_order_header_through_the_view(self):
        response = self.client.post(
            reverse("procurement:purchase_order_update", kwargs={"purchase_order_id": self.manual.pk}),
            {
                "po_number": "MAN-V",
                "supplier_no": "S9",
                "supplier_name": "Renamed Supplier",
                "status": "open",
                "currency": "SEK",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.supplier_name, "Renamed Supplier")

    # -- BC read-only, through the views ------------------------------------

    def test_bc_order_offers_no_line_actions_on_the_workspace(self):
        _line(self.bc)
        response = self.client.get(
            reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": self.bc.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Add line")
        self.assertNotContains(response, "Link acquired container")

    def test_manual_order_offers_the_line_actions(self):
        _line(self.manual)
        response = self.client.get(
            reverse("procurement:purchase_order_detail", kwargs={"purchase_order_id": self.manual.pk})
        )

        self.assertContains(response, "Add line")
        self.assertContains(response, "Link acquired container")
        self.assertContains(response, "Add to container / load")

    def test_creating_a_line_on_a_bc_order_is_refused(self):
        response = self.client.post(
            reverse("procurement:purchase_order_line_create", kwargs={"purchase_order_id": self.bc.pk}),
            {"line_no": "10000", "item_no": "X", "ordered_qty": "1"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.bc.lines.exists())

    def test_deleting_a_bc_line_is_refused_with_403_for_htmx(self):
        """htmx does not swap on a 4xx, so the row stays where it is and says why."""
        line = _line(self.bc)

        response = self.client.delete(
            reverse("procurement:purchase_order_line_delete", kwargs={"line_id": line.pk}),
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(PurchaseOrderLine.objects.filter(pk=line.pk).exists())

    def test_editing_a_bc_order_header_is_refused(self):
        response = self.client.post(
            reverse("procurement:purchase_order_update", kwargs={"purchase_order_id": self.bc.pk}),
            {"po_number": "BC-V", "supplier_name": "Hijacked", "status": "open", "currency": "EUR"},
        )

        self.assertEqual(response.status_code, 302)
        self.bc.refresh_from_db()
        self.assertEqual(self.bc.supplier_name, "Supplier")

    def test_another_teams_line_is_not_reachable(self):
        other_team = Team.objects.create(name="Other", slug="manual-line-other")
        other_line = _line(_order(other_team, PurchaseOrderSource.MANUAL, "OTHER-1"))

        response = self.client.get(reverse("procurement:purchase_order_line_update", kwargs={"line_id": other_line.pk}))

        self.assertEqual(response.status_code, 404)
