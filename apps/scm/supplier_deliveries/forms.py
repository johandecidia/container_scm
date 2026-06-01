from django import forms
from django.utils.translation import gettext_lazy as _

from apps.scm.procurement.models import PurchaseOrder

from .models import SupplierDelivery, SupplierDeliveryStatus


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
            self.fields["purchase_order"].queryset = PurchaseOrder.objects.filter(team=team)
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
