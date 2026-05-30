from django import forms
from django.utils.translation import gettext_lazy as _

from apps.scm.containers.models import Container

from .models import Shipment


class ShipmentForm(forms.ModelForm):
    """Form for creating or editing a shipment.

    Does not expose: team, created_by, tracking_status, last_tracking_sync_at.
    Status changes must go through ShipmentStatusForm / the status-change view.
    """

    class Meta:
        model = Shipment
        fields = [
            "shipment_number",
            "reference",
            "customer_name",
            "carrier",
            "carrier_booking_reference",
            "bill_of_lading_number",
            "origin_port",
            "destination_port",
            "etd",
            "eta",
            "notes",
        ]
        widgets = {
            "shipment_number": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "reference": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "customer_name": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "carrier": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "carrier_booking_reference": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "bill_of_lading_number": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "origin_port": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "destination_port": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "etd": forms.DateInput(attrs={"type": "date", "class": "input input-bordered w-full"}),
            "eta": forms.DateInput(attrs={"type": "date", "class": "input input-bordered w-full"}),
            "notes": forms.Textarea(attrs={"class": "textarea textarea-bordered w-full", "rows": 3}),
        }


class ShipmentStatusForm(forms.Form):
    """Form for changing the status of a shipment."""

    status = forms.ChoiceField(
        label=_("New status"),
        choices=Shipment.Status.choices,
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )


class ShipmentContainerForm(forms.Form):
    """Form for adding a container to a shipment.

    The container queryset is scoped to the team and excludes already-linked containers.
    """

    container = forms.ModelChoiceField(
        label=_("Container"),
        queryset=Container.objects.none(),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    sequence = forms.IntegerField(
        label=_("Sequence"),
        required=False,
        initial=0,
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "input input-bordered w-full"}),
    )
    seal_number = forms.CharField(
        label=_("Seal number"),
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full"}),
    )
    gross_weight_kg = forms.DecimalField(
        label=_("Gross weight (kg)"),
        max_digits=10,
        decimal_places=2,
        required=False,
        widget=forms.NumberInput(attrs={"class": "input input-bordered w-full", "step": "0.01"}),
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"class": "textarea textarea-bordered w-full", "rows": 2}),
    )

    def __init__(self, *args, team=None, shipment=None, **kwargs):
        super().__init__(*args, **kwargs)
        if team is not None:
            qs = Container.objects.filter(team=team).select_related("equipment_type")
            if shipment is not None:
                linked_ids = shipment.shipment_containers.values_list("container_id", flat=True)
                qs = qs.exclude(pk__in=linked_ids)
            self.fields["container"].queryset = qs

    def get_container_data(self) -> dict:
        """Return a dict suitable for passing to add_container_to_shipment."""
        return {
            "sequence": self.cleaned_data.get("sequence") or 0,
            "seal_number": self.cleaned_data.get("seal_number", ""),
            "gross_weight_kg": self.cleaned_data.get("gross_weight_kg"),
            "notes": self.cleaned_data.get("notes", ""),
        }
