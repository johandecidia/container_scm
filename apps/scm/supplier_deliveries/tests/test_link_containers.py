"""Tests for booking container numbers onto a purchase order.

A container has no purchase order column: the link is a supplier delivery line.
These tests cover that indirection end to end — the intake modal opened from a PO,
the quantities it prefills, and the delivery it creates when the order has none.
"""

import json
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine, PurchaseOrderStatus
from apps.scm.supplier_deliveries.forms import NEW_DELIVERY, LinkContainersForm
from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryLine, SupplierDeliveryStatus
from apps.scm.supplier_deliveries.selectors import (
    get_containers_for_purchase_order,
    get_remaining_qty_by_po_line,
)
from apps.scm.supplier_deliveries.services import (
    ContainerAssignment,
    build_delivery_reference,
    create_supplier_delivery,
    link_containers_to_delivery,
    split_qty_evenly,
)
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

LINK_FORM_TEMPLATE = "scm/supplier_deliveries/partials/link_containers_form.html"
LINK_RESULT_TEMPLATE = "scm/supplier_deliveries/partials/link_containers_result.html"


def _number(owner: str, serial: str, category: str = "U") -> str:
    return f"{owner}{category}{serial}{calculate_check_digit(owner, category, serial)}"


VALID_A = _number("TRD", "925896")
VALID_B = _number("MSC", "123456")
VALID_C = _number("CMA", "765432")


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team, number: str) -> Container:
    return Container.objects.create(
        team=team,
        owner_code=number[:3],
        category_id=number[3],
        serial_number=number[4:10],
        check_digit=int(number[10]),
        equipment_type=_equipment_type(),
    )


def _po(team, po_number="PO-LINK-1", external_id="bc-link-1") -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=external_id,
        po_number=po_number,
        supplier_no="SUP-1",
        supplier_name="Link Supplier",
        status=PurchaseOrderStatus.OPEN,
    )


def _po_line(team, po, line_no="10000", ordered_qty=100, item_no="ITEM-1") -> PurchaseOrderLine:
    return PurchaseOrderLine.objects.create(
        team=team,
        purchase_order=po,
        external_id=f"bc-link-line-{po.pk}-{line_no}",
        line_no=line_no,
        item_no=item_no,
        description=f"Item {line_no}",
        ordered_qty=ordered_qty,
    )


class SplitQtyEvenlyTest(TestCase):
    def test_even_split_adds_back_up_to_the_total(self):
        parts = split_qty_evenly(Decimal("100"), 3)
        self.assertEqual(sum(parts), Decimal("100"))
        self.assertEqual(parts, [Decimal("33.333"), Decimal("33.333"), Decimal("33.334")])

    def test_single_container_gets_the_whole_remaining_qty(self):
        self.assertEqual(split_qty_evenly(Decimal("42.5"), 1), [Decimal("42.5")])

    def test_nothing_left_splits_into_zeroes(self):
        self.assertEqual(split_qty_evenly(Decimal("0"), 2), [Decimal("0"), Decimal("0")])

    def test_no_containers_splits_into_nothing(self):
        self.assertEqual(split_qty_evenly(Decimal("10"), 0), [])


class BuildDeliveryReferenceTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Ref", slug="ref-team")
        self.po = _po(self.team)

    def test_first_reference_is_d1(self):
        self.assertEqual(build_delivery_reference(self.team, self.po), "PO-LINK-1-D1")

    def test_next_reference_skips_the_taken_one(self):
        create_supplier_delivery(team=self.team, purchase_order=self.po, delivery_reference="PO-LINK-1-D1")
        self.assertEqual(build_delivery_reference(self.team, self.po), "PO-LINK-1-D2")

    def test_another_teams_reference_does_not_push_the_counter(self):
        other = Team.objects.create(name="Ref other", slug="ref-other-team")
        other_po = _po(other, external_id="bc-link-other")
        create_supplier_delivery(team=other, purchase_order=other_po, delivery_reference="PO-LINK-1-D1")
        self.assertEqual(build_delivery_reference(self.team, self.po), "PO-LINK-1-D1")


class RemainingQtyByPoLineTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Remaining", slug="remaining-team")
        self.po = _po(self.team)
        self.line = _po_line(self.team, self.po, ordered_qty=100)

    def test_untouched_line_has_everything_remaining(self):
        self.assertEqual(get_remaining_qty_by_po_line(self.po), {self.line.pk: Decimal("100")})

    def test_booked_qty_is_subtracted(self):
        delivery = create_supplier_delivery(team=self.team, purchase_order=self.po, delivery_reference="D-1")
        SupplierDeliveryLine.objects.create(
            team=self.team, delivery=delivery, purchase_order_line=self.line, delivery_qty=Decimal("30")
        )
        self.assertEqual(get_remaining_qty_by_po_line(self.po), {self.line.pk: Decimal("70")})


class LinkContainersToDeliveryTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Linking", slug="linking-team")
        self.po = _po(self.team)
        self.line = _po_line(self.team, self.po, ordered_qty=100)
        self.delivery = create_supplier_delivery(team=self.team, purchase_order=self.po, delivery_reference="LINK-D1")
        self.container = _container(self.team, VALID_A)

    def _assignment(self, container, qty="50"):
        return ContainerAssignment(container=container, purchase_order_line=self.line, delivery_qty=Decimal(qty))

    def test_creates_one_delivery_line_per_container(self):
        second = _container(self.team, VALID_B)
        lines = link_containers_to_delivery(
            team=self.team,
            delivery=self.delivery,
            assignments=[self._assignment(self.container), self._assignment(second)],
        )
        self.assertEqual(len(lines), 2)
        self.assertEqual(SupplierDeliveryLine.objects.filter(delivery=self.delivery).count(), 2)
        self.assertEqual(lines[0].article, "ITEM-1")

    def test_re_linking_the_same_container_is_a_no_op(self):
        link_containers_to_delivery(
            team=self.team, delivery=self.delivery, assignments=[self._assignment(self.container)]
        )
        lines = link_containers_to_delivery(
            team=self.team, delivery=self.delivery, assignments=[self._assignment(self.container)]
        )
        self.assertEqual(lines, [])
        self.assertEqual(SupplierDeliveryLine.objects.filter(delivery=self.delivery).count(), 1)

    def test_overflowing_the_po_line_links_nothing_at_all(self):
        second = _container(self.team, VALID_B)
        with self.assertRaises(ValidationError):
            link_containers_to_delivery(
                team=self.team,
                delivery=self.delivery,
                assignments=[self._assignment(self.container, "60"), self._assignment(second, "60")],
            )
        self.assertEqual(SupplierDeliveryLine.objects.filter(delivery=self.delivery).count(), 0)

    def test_linked_container_is_reachable_from_the_purchase_order(self):
        link_containers_to_delivery(
            team=self.team, delivery=self.delivery, assignments=[self._assignment(self.container)]
        )
        self.assertEqual(
            list(get_containers_for_purchase_order(team=self.team, purchase_order=self.po)),
            [self.container],
        )

    def test_a_container_appears_once_however_many_lines_it_has(self):
        other_line = _po_line(self.team, self.po, line_no="20000", ordered_qty=100, item_no="ITEM-2")
        link_containers_to_delivery(
            team=self.team, delivery=self.delivery, assignments=[self._assignment(self.container)]
        )
        link_containers_to_delivery(
            team=self.team,
            delivery=self.delivery,
            assignments=[
                ContainerAssignment(
                    container=self.container, purchase_order_line=other_line, delivery_qty=Decimal("10")
                )
            ],
        )
        self.assertEqual(get_containers_for_purchase_order(team=self.team, purchase_order=self.po).count(), 1)


class LinkContainersFormTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Form", slug="form-team")
        self.po = _po(self.team)
        self.line = _po_line(self.team, self.po, ordered_qty=100)
        self.container = _container(self.team, VALID_A)

    def _form(self, data=None, containers=None):
        return LinkContainersForm(
            *([data] if data is not None else []),
            team=self.team,
            purchase_order=self.po,
            containers=containers if containers is not None else [self.container],
        )

    def test_a_po_without_deliveries_only_offers_to_create_one(self):
        form = self._form()
        self.assertEqual([value for value, _label in form.fields["delivery"].choices], [NEW_DELIVERY])
        self.assertEqual(form.fields["delivery"].initial, NEW_DELIVERY)
        self.assertEqual(form.fields["delivery_reference"].initial, "PO-LINK-1-D1")

    def test_an_existing_delivery_is_preselected(self):
        delivery = create_supplier_delivery(team=self.team, purchase_order=self.po, delivery_reference="EXISTING")
        form = self._form()
        self.assertEqual(form.fields["delivery"].initial, str(delivery.pk))

    def test_the_only_order_line_is_preselected_with_the_remaining_qty(self):
        form = self._form()
        self.assertEqual(form.fields[f"line_{self.container.pk}"].initial, str(self.line.pk))
        self.assertEqual(form.fields[f"qty_{self.container.pk}"].initial, Decimal("100"))

    def test_remaining_qty_is_split_across_the_batch(self):
        second = _container(self.team, VALID_B)
        form = self._form(containers=[self.container, second])
        self.assertEqual(form.fields[f"qty_{self.container.pk}"].initial, Decimal("50"))
        self.assertEqual(form.fields[f"qty_{second.pk}"].initial, Decimal("50"))

    def test_a_full_line_falls_back_to_the_first_line(self):
        delivery = create_supplier_delivery(team=self.team, purchase_order=self.po, delivery_reference="FULL")
        SupplierDeliveryLine.objects.create(
            team=self.team, delivery=delivery, purchase_order_line=self.line, delivery_qty=Decimal("100")
        )
        form = self._form()
        self.assertEqual(form.fields[f"line_{self.container.pk}"].initial, str(self.line.pk))
        self.assertEqual(form.fields[f"qty_{self.container.pk}"].initial, Decimal("0"))

    def test_a_new_delivery_needs_a_reference(self):
        form = self._form(
            {
                "delivery": NEW_DELIVERY,
                "delivery_reference": "",
                f"line_{self.container.pk}": str(self.line.pk),
                f"qty_{self.container.pk}": "10",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("delivery_reference", form.errors)

    def test_a_reference_already_in_use_is_rejected(self):
        create_supplier_delivery(team=self.team, purchase_order=self.po, delivery_reference="TAKEN")
        form = self._form(
            {
                "delivery": NEW_DELIVERY,
                "delivery_reference": "TAKEN",
                f"line_{self.container.pk}": str(self.line.pk),
                f"qty_{self.container.pk}": "10",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("delivery_reference", form.errors)

    def test_a_blank_reference_is_fine_when_adding_to_an_existing_delivery(self):
        delivery = create_supplier_delivery(team=self.team, purchase_order=self.po, delivery_reference="EXISTING")
        form = self._form(
            {
                "delivery": str(delivery.pk),
                "delivery_reference": "",
                f"line_{self.container.pk}": str(self.line.pk),
                f"qty_{self.container.pk}": "10",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.get_delivery(), delivery)


class LinkContainersViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Link views", slug="link-views-team")
        cls.user = CustomUser.objects.create_user(username="link-views@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.other_team = Team.objects.create(name="Link other", slug="link-other-team")

    def setUp(self):
        _equipment_type()
        self.client = Client()
        self.client.force_login(self.user)
        self.po = _po(self.team)
        self.line = _po_line(self.team, self.po, ordered_qty=100)

    # -- the intake modal, opened from a purchase order --------------------

    def test_the_modal_says_which_purchase_order_it_is_linking_to(self):
        response = self.client.get(reverse("containers:create"), {"purchase_order": self.po.pk}, HTTP_HX_REQUEST="true")
        self.assertContains(response, "Linking to purchase order")
        self.assertContains(response, self.po.po_number)
        self.assertContains(response, f'name="purchase_order" value="{self.po.pk}"')

    def test_another_teams_purchase_order_is_ignored(self):
        foreign_po = _po(self.other_team, po_number="PO-FOREIGN", external_id="bc-foreign")
        response = self.client.get(
            reverse("containers:create"), {"purchase_order": foreign_po.pk}, HTTP_HX_REQUEST="true"
        )
        self.assertNotContains(response, "Linking to purchase order")

    def test_adding_one_container_goes_on_to_the_link_step(self):
        response = self.client.post(
            reverse("containers:create"),
            data={"container_number": VALID_A, "purchase_order": self.po.pk},
            HTTP_HX_REQUEST="true",
        )
        self.assertTemplateUsed(response, LINK_FORM_TEMPLATE)
        self.assertTrue(Container.objects.filter(team=self.team, serial_number="925896").exists())

    def test_adding_containers_without_a_po_still_ends_at_the_import_summary(self):
        response = self.client.post(
            reverse("containers:create"), data={"container_number": VALID_A}, HTTP_HX_REQUEST="true"
        )
        self.assertTemplateUsed(response, "scm/containers/partials/container_intake_created.html")

    def test_a_pasted_batch_reaches_the_link_step_with_every_container(self):
        payload = json.dumps([[VALID_A, ""], [VALID_B, ""]])
        response = self.client.post(
            reverse("containers:import_confirm"),
            data={"entries": payload, "tab": "paste", "purchase_order": self.po.pk},
            HTTP_HX_REQUEST="true",
        )
        self.assertTemplateUsed(response, LINK_FORM_TEMPLATE)
        self.assertContains(response, VALID_A)
        self.assertContains(response, VALID_B)

    def test_a_container_that_already_existed_is_still_offered_for_linking(self):
        _container(self.team, VALID_A)
        response = self.client.post(
            reverse("containers:import_confirm"),
            data={"entries": json.dumps([[VALID_A, ""]]), "tab": "paste", "purchase_order": self.po.pk},
            HTTP_HX_REQUEST="true",
        )
        self.assertTemplateUsed(response, LINK_FORM_TEMPLATE)
        self.assertContains(response, VALID_A)

    def test_a_purchase_order_without_lines_says_so_instead_of_offering_a_form(self):
        bare_po = _po(self.team, po_number="PO-BARE", external_id="bc-bare")
        response = self.client.post(
            reverse("containers:create"),
            data={"container_number": VALID_A, "purchase_order": bare_po.pk},
            HTTP_HX_REQUEST="true",
        )
        self.assertContains(response, "no order lines")

    # -- the link step itself ----------------------------------------------

    def _link(self, containers, **overrides):
        data = {
            "purchase_order": self.po.pk,
            "containers": [c.pk for c in containers],
            "delivery": NEW_DELIVERY,
            "delivery_reference": "PO-LINK-1-D1",
        }
        for container in containers:
            data[f"line_{container.pk}"] = str(self.line.pk)
            data[f"qty_{container.pk}"] = "10"
        data.update(overrides)
        return self.client.post(reverse("supplier_deliveries:link_containers"), data=data, HTTP_HX_REQUEST="true")

    def test_linking_creates_the_delivery_the_po_did_not_have(self):
        container = _container(self.team, VALID_A)
        response = self._link([container])
        self.assertTemplateUsed(response, LINK_RESULT_TEMPLATE)
        delivery = SupplierDelivery.objects.get(team=self.team, purchase_order=self.po)
        self.assertEqual(delivery.delivery_reference, "PO-LINK-1-D1")
        self.assertEqual(delivery.supplier, self.po.supplier_name)
        self.assertEqual(delivery.status, SupplierDeliveryStatus.PLANNED)
        self.assertEqual(list(get_containers_for_purchase_order(team=self.team, purchase_order=self.po)), [container])

    def test_linking_can_add_to_the_delivery_the_po_already_has(self):
        existing = create_supplier_delivery(team=self.team, purchase_order=self.po, delivery_reference="ALREADY-THERE")
        container = _container(self.team, VALID_A)
        self._link([container], delivery=str(existing.pk), delivery_reference="")
        self.assertEqual(SupplierDelivery.objects.filter(team=self.team, purchase_order=self.po).count(), 1)
        self.assertEqual(SupplierDeliveryLine.objects.filter(delivery=existing).count(), 1)

    def test_a_batch_that_overflows_the_order_line_creates_nothing(self):
        first = _container(self.team, VALID_A)
        second = _container(self.team, VALID_B)
        response = self._link(
            [first, second],
            **{f"qty_{first.pk}": "60", f"qty_{second.pk}": "60"},
        )
        self.assertTemplateUsed(response, LINK_FORM_TEMPLATE)
        self.assertFalse(SupplierDelivery.objects.filter(team=self.team, purchase_order=self.po).exists())
        self.assertFalse(SupplierDeliveryLine.objects.filter(team=self.team).exists())

    def test_an_invalid_reference_re_renders_the_form_without_writing(self):
        container = _container(self.team, VALID_A)
        response = self._link([container], delivery_reference="")
        self.assertTemplateUsed(response, LINK_FORM_TEMPLATE)
        self.assertFalse(SupplierDelivery.objects.filter(team=self.team).exists())

    def test_another_teams_container_cannot_be_linked(self):
        mine = _container(self.team, VALID_A)
        theirs = _container(self.other_team, VALID_C)
        data = {
            "purchase_order": self.po.pk,
            "containers": [mine.pk, theirs.pk],
            "delivery": NEW_DELIVERY,
            "delivery_reference": "PO-LINK-1-D1",
            f"line_{mine.pk}": str(self.line.pk),
            f"qty_{mine.pk}": "10",
            f"line_{theirs.pk}": str(self.line.pk),
            f"qty_{theirs.pk}": "10",
        }
        self.client.post(reverse("supplier_deliveries:link_containers"), data=data, HTTP_HX_REQUEST="true")
        self.assertEqual(list(get_containers_for_purchase_order(team=self.team, purchase_order=self.po)), [mine])

    def test_the_purchase_order_page_lists_what_was_linked(self):
        container = _container(self.team, VALID_A)
        self._link([container])
        response = self.client.get(reverse("procurement:purchase_order_detail", args=[self.po.pk]))
        self.assertContains(response, container.container_id)
