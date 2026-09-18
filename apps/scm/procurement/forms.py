from typing import cast

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.scm.containers.models import Container

from .models import ContainerAcquisition, PurchaseOrder, PurchaseOrderLine


class PurchaseOrderForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = [
            "po_number",
            "supplier_no",
            "supplier_name",
            "status",
            "order_date",
            "expected_receipt_date",
            "currency",
        ]
        widgets = {
            "order_date": forms.DateInput(attrs={"type": "date"}),
            "expected_receipt_date": forms.DateInput(attrs={"type": "date"}),
        }
        labels = {
            "po_number": _("PO Number"),
            "supplier_no": _("Supplier No"),
            "supplier_name": _("Supplier Name"),
            "status": _("Status"),
            "order_date": _("Order Date"),
            "expected_receipt_date": _("Expected Receipt Date"),
            "currency": _("Currency"),
        }


class PurchaseOrderLineForm(forms.ModelForm):
    """Add or edit a hand-entered order line.

    Only the fields a person enters. ``external_id`` is the source system's key and
    is generated for manual lines; ``shipped_qty`` and ``received_qty`` are
    fulfillment figures the delivery and receipt paths own, and typing them here
    would put a second, unreconciled opinion next to them.
    """

    class Meta:
        model = PurchaseOrderLine
        fields = [
            "line_no",
            "item_no",
            "description",
            "ordered_qty",
            "unit_price",
            "expected_receipt_date",
        ]
        widgets = {
            "line_no": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "item_no": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "description": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "ordered_qty": forms.NumberInput(attrs={"class": "input input-bordered w-full", "step": "0.001"}),
            "unit_price": forms.NumberInput(attrs={"class": "input input-bordered w-full", "step": "0.0001"}),
            "expected_receipt_date": forms.DateInput(
                attrs={"type": "date", "class": "input input-bordered w-full"}, format="%Y-%m-%d"
            ),
        }
        labels = {
            "line_no": _("Line No"),
            "item_no": _("Item No"),
            "description": _("Description"),
            "ordered_qty": _("Ordered Qty"),
            "unit_price": _("Unit Price"),
            "expected_receipt_date": _("Expected Receipt Date"),
        }


class AcquiredContainerForm(forms.Form):
    """Pick the physical container this order line acquired.

    The choices exclude every container that already has a procurement origin,
    including one acquired by this same line. A box came from one place, so offering
    an already-acquired one would be offering a choice the service will refuse.
    """

    container = forms.ModelChoiceField(
        label=_("Container"),
        queryset=Container.objects.none(),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )

    def __init__(self, *args, team=None, **kwargs):
        super().__init__(*args, **kwargs)
        if team is not None:
            acquired_ids = ContainerAcquisition.objects.values_list("container_id", flat=True)
            field = cast(forms.ModelChoiceField, self.fields["container"])
            field.queryset = (
                Container.objects.filter(team=team)
                .exclude(pk__in=acquired_ids)
                .select_related("equipment_type")
                .order_by("owner_code", "category_id", "serial_number")
            )


class ContainerLoadForm(forms.Form):
    """Say which container carries this order line's goods, and how much of it.

    Unlike the acquisition form this offers every container in the team, including
    ones already loaded with this line: the write is an upsert on the pair, so
    picking the same box again is how a recorded quantity is corrected.

    The quantity is optional, because knowing the goods are in a box is useful before
    anybody has counted them. Blank means "not recorded" and is stored as such.
    """

    container = forms.ModelChoiceField(
        label=_("Container"),
        queryset=Container.objects.none(),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    quantity = forms.DecimalField(
        label=_("Quantity"),
        required=False,
        min_value=0,
        max_digits=12,
        decimal_places=3,
        widget=forms.NumberInput(attrs={"class": "input input-bordered w-full", "step": "0.001"}),
        help_text=_("Leave blank when it is not known how much of the line went into this container."),
    )

    def __init__(self, *args, team=None, **kwargs):
        super().__init__(*args, **kwargs)
        if team is not None:
            field = cast(forms.ModelChoiceField, self.fields["container"])
            field.queryset = (
                Container.objects.filter(team=team)
                .select_related("equipment_type")
                .order_by("owner_code", "category_id", "serial_number")
            )
