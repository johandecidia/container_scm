from decimal import Decimal
from typing import cast

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.scm.procurement.models import PurchaseOrder

from .models import SupplierDelivery, SupplierDeliveryStatus
from .selectors import get_remaining_qty_by_po_line
from .services import ContainerAssignment, build_delivery_reference, split_qty_evenly


class SupplierDeliveryForm(forms.Form):
    """Form for creating or editing a supplier delivery."""

    purchase_order = forms.ModelChoiceField(
        label=_("Purchase Order"),
        queryset=PurchaseOrder.objects.none(),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    delivery_reference = forms.CharField(
        label=_("Delivery Reference"),
        max_length=100,
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full"}),
    )
    supplier = forms.CharField(
        label=_("Supplier"),
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full"}),
    )
    status = forms.ChoiceField(
        label=_("Status"),
        choices=SupplierDeliveryStatus.choices,
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    planned_ship_date = forms.DateField(
        label=_("Planned Ship Date"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "input input-bordered w-full"}),
    )
    planned_arrival_date = forms.DateField(
        label=_("Planned Arrival Date"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "input input-bordered w-full"}),
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"class": "textarea textarea-bordered w-full", "rows": 3}),
    )

    def __init__(self, *args, team=None, instance: SupplierDelivery | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if team is not None:
            po_field = cast(forms.ModelChoiceField, self.fields["purchase_order"])
            po_field.queryset = PurchaseOrder.objects.filter(team=team)
        if instance is not None:
            self.fields["purchase_order"].initial = instance.purchase_order_id
            self.fields["delivery_reference"].initial = instance.delivery_reference
            self.fields["supplier"].initial = instance.supplier
            self.fields["status"].initial = instance.status
            self.fields["planned_ship_date"].initial = instance.planned_ship_date
            self.fields["planned_arrival_date"].initial = instance.planned_arrival_date
            self.fields["notes"].initial = instance.notes

    def get_delivery_data(self) -> dict:
        """Return data suitable for passing to create/update services."""
        return {
            "purchase_order": self.cleaned_data["purchase_order"],
            "delivery_reference": self.cleaned_data["delivery_reference"],
            "supplier": self.cleaned_data.get("supplier", ""),
            "status": self.cleaned_data["status"],
            "planned_ship_date": self.cleaned_data.get("planned_ship_date"),
            "planned_arrival_date": self.cleaned_data.get("planned_arrival_date"),
            "notes": self.cleaned_data.get("notes", ""),
        }


NEW_DELIVERY = "new"


class LinkContainersForm(forms.Form):
    """Book a batch of just-added containers onto one purchase order.

    A container reaches a PO only through a delivery line, so this form asks for
    the two things a delivery line cannot be created without — which delivery, and
    which PO line and quantity per container — and prefills both so the common case
    (one order line, one delivery) is a single click.

    The per-container fields are built in ``__init__`` because the batch size is
    only known at request time; they are named ``line_<pk>`` and ``qty_<pk>`` and
    read back through :meth:`get_assignments`.
    """

    delivery = forms.ChoiceField(
        label=_("Delivery"),
        # x-model drives the "new delivery reference" field's visibility in the template.
        widget=forms.Select(attrs={"class": "select select-bordered w-full", "x-model": "delivery"}),
    )
    delivery_reference = forms.CharField(
        label=_("New delivery reference"),
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full"}),
    )

    def __init__(self, *args, team, purchase_order, containers, **kwargs):
        super().__init__(*args, **kwargs)
        self.team = team
        self.purchase_order = purchase_order
        self.containers = list(containers)
        self.po_lines = list(purchase_order.lines.all().order_by("line_no"))
        self.existing_deliveries = list(
            SupplierDelivery.objects.filter(team=team, purchase_order=purchase_order).order_by("-created_at")
        )

        # Existing deliveries first, so "add to the delivery already on this PO" is
        # the default and a second, empty delivery is a deliberate choice.
        delivery_field = cast(forms.ChoiceField, self.fields["delivery"])
        delivery_field.choices = [
            (str(d.pk), f"{d.delivery_reference} ({d.get_status_display()})") for d in self.existing_deliveries
        ] + [(NEW_DELIVERY, _("➕ Create a new delivery"))]
        delivery_field.initial = str(self.existing_deliveries[0].pk) if self.existing_deliveries else NEW_DELIVERY
        self.fields["delivery_reference"].initial = build_delivery_reference(team, purchase_order)

        remaining = get_remaining_qty_by_po_line(purchase_order)
        default_line = self._default_line(remaining)
        prefill = split_qty_evenly(
            remaining.get(default_line.pk, Decimal("0")) if default_line else Decimal("0"),
            len(self.containers),
        )
        line_choices = [
            (str(line.pk), self._line_label(line, remaining.get(line.pk, Decimal("0")))) for line in self.po_lines
        ]

        for index, container in enumerate(self.containers):
            self.fields[f"line_{container.pk}"] = forms.ChoiceField(
                label=_("Order line"),
                choices=line_choices,
                initial=str(default_line.pk) if default_line else None,
                widget=forms.Select(attrs={"class": "select select-bordered select-sm w-full"}),
            )
            self.fields[f"qty_{container.pk}"] = forms.DecimalField(
                label=_("Quantity"),
                min_value=Decimal("0"),
                max_digits=12,
                decimal_places=3,
                initial=prefill[index],
                widget=forms.NumberInput(attrs={"class": "input input-bordered input-sm w-full", "step": "0.001"}),
            )

    def _default_line(self, remaining: dict):
        """The line to preselect: the first with room left, else the first line."""
        if not self.po_lines:
            return None
        with_room = [line for line in self.po_lines if remaining.get(line.pk, Decimal("0")) > 0]
        return with_room[0] if with_room else self.po_lines[0]

    @staticmethod
    def _line_label(line, remaining: Decimal) -> str:
        description = line.description or line.item_no
        return f"{line.line_no} — {description} ({_('remaining')}: {remaining})"

    def container_rows(self):
        """Yield (container, line field, qty field) so the template can loop once."""
        for container in self.containers:
            yield container, self[f"line_{container.pk}"], self[f"qty_{container.pk}"]

    def clean_delivery_reference(self) -> str:
        reference = (self.cleaned_data.get("delivery_reference") or "").strip()
        if self.data.get("delivery") != NEW_DELIVERY:
            return reference
        if not reference:
            raise forms.ValidationError(_("Enter a reference for the new delivery."))
        if SupplierDelivery.objects.filter(team=self.team, delivery_reference=reference).exists():
            raise forms.ValidationError(_("A delivery with this reference already exists."))
        return reference

    @property
    def creates_delivery(self) -> bool:
        return self.cleaned_data.get("delivery") == NEW_DELIVERY

    def get_delivery(self) -> SupplierDelivery | None:
        """The chosen existing delivery, or None when a new one is to be created."""
        if self.creates_delivery:
            return None
        return next(d for d in self.existing_deliveries if str(d.pk) == self.cleaned_data["delivery"])

    def get_assignments(self) -> list[ContainerAssignment]:
        lines_by_pk = {str(line.pk): line for line in self.po_lines}
        return [
            ContainerAssignment(
                container=container,
                purchase_order_line=lines_by_pk[self.cleaned_data[f"line_{container.pk}"]],
                delivery_qty=self.cleaned_data[f"qty_{container.pk}"],
            )
            for container in self.containers
        ]
