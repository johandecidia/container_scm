from typing import cast

from django import forms
from django.utils.translation import gettext_lazy as _

from .intake import carrier_choices, parse_and_validate_container_number
from .models import Container, ContainerLocation, EquipmentType
from .utils import parse_container_id, validate_container_id

MAX_PASTED_CONTAINERS = 500


class QuickContainerForm(forms.Form):
    """The primary "Add Container" form: a container number, and nothing else required.

    Everything technical — the four ID components, equipment type, status and
    condition — is derived or defaulted, and can be changed afterwards through the
    normal edit form.
    """

    container_number = forms.CharField(
        label=_("Container number"),
        max_length=20,
        help_text=_("Full ISO 6346 number, e.g. MSCU1234567"),
        widget=forms.TextInput(
            attrs={
                "class": "input input-bordered w-full font-mono uppercase",
                "placeholder": "MSCU1234567",
                "autocomplete": "off",
                "autofocus": "autofocus",
            }
        ),
    )
    carrier = forms.ChoiceField(
        label=_("Carrier"),
        choices=carrier_choices,
        required=False,
        help_text=_("Optional. Never guessed from the container number."),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )

    def clean_container_number(self) -> str:
        """Normalise and validate through the shared intake rules."""
        parts = parse_and_validate_container_number(self.cleaned_data["container_number"])
        self.parts = parts
        return f"{parts['owner_code']}{parts['category_id']}{parts['serial_number']}{parts['check_digit']}"


class ContainerPasteForm(forms.Form):
    """Bulk intake by pasting a list of container numbers."""

    numbers = forms.CharField(
        label=_("Container numbers"),
        help_text=_("One per line, or separated by comma, semicolon or tab — paste straight from Excel."),
        widget=forms.Textarea(
            attrs={
                "class": "textarea textarea-bordered w-full font-mono",
                "rows": 8,
                "placeholder": "TRDU9258963\nMSCU1234567\nCMAU7654321",
            }
        ),
    )
    carrier = forms.ChoiceField(
        label=_("Carrier"),
        choices=carrier_choices,
        required=False,
        help_text=_("Optional. Applied to every container in this list."),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )

    def clean_numbers(self) -> str:
        from .intake import split_container_numbers

        numbers = split_container_numbers(self.cleaned_data["numbers"])
        if not numbers:
            raise forms.ValidationError(_("Enter at least one container number."))
        if len(numbers) > MAX_PASTED_CONTAINERS:
            raise forms.ValidationError(
                _("Too many container numbers at once — the maximum is %(max)s.") % {"max": MAX_PASTED_CONTAINERS}
            )
        return self.cleaned_data["numbers"]


class ContainerCsvImportForm(forms.Form):
    """Bulk intake from a small CSV: a ``container_number`` column, optional ``carrier``."""

    file = forms.FileField(
        label=_("CSV file"),
        help_text=_("A container_number column is required; a carrier column is optional."),
        widget=forms.FileInput(attrs={"accept": ".csv,text/csv", "class": "file-input file-input-bordered w-full"}),
    )

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if not uploaded.name.lower().endswith(".csv"):
            raise forms.ValidationError(_("Upload a .csv file."))
        if uploaded.size > 2 * 1024 * 1024:
            raise forms.ValidationError(_("File too large. Maximum size is 2 MB."))
        return uploaded


class ContainerForm(forms.Form):
    """Form for creating or editing a container.

    The container's four ID components are entered via a single ``container_id_input``
    field (e.g. ``MSCU1234567``) which is parsed and validated on clean.
    """

    container_id_input = forms.CharField(
        label=_("Container ID"),
        max_length=11,
        help_text=_("Enter the full container ID, e.g. MSCU1234567"),
        widget=forms.TextInput(attrs={"placeholder": "MSCU1234567", "class": "input input-bordered w-full"}),
    )
    equipment_type = forms.ModelChoiceField(
        label=_("Equipment type"),
        queryset=EquipmentType.objects.filter(is_active=True),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    status = forms.ChoiceField(
        label=_("Status"),
        choices=cast(list[tuple[str, str]], Container._meta.get_field("status").choices),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    condition = forms.ChoiceField(
        label=_("Condition"),
        choices=cast(list[tuple[str, str]], Container._meta.get_field("condition").choices),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    color_code = forms.CharField(
        label=_("Color code"),
        max_length=50,
        required=False,
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full"}),
    )
    color_system = forms.ChoiceField(
        label=_("Color system"),
        choices=cast(list[tuple[str, str]], Container._meta.get_field("color_system").choices),
        required=False,
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    manufacture_date = forms.DateField(
        label=_("Manufacture date"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "input input-bordered w-full"}),
    )
    manufacturer = forms.CharField(
        label=_("Manufacturer"),
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full"}),
    )
    manufacturer_id = forms.CharField(
        label=_("Manufacturer ID"),
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full"}),
    )
    current_location = forms.ModelChoiceField(
        label=_("Current location"),
        queryset=ContainerLocation.objects.none(),
        required=False,
        empty_label=_("— No location —"),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"class": "textarea textarea-bordered w-full", "rows": 3}),
    )

    def __init__(self, *args, instance: Container | None = None, team=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._instance = instance
        if team is not None:
            self.fields["current_location"].queryset = ContainerLocation.objects.filter(
                team=team, is_active=True
            ).order_by("name")
        elif instance is not None and instance.team_id:
            self.fields["current_location"].queryset = ContainerLocation.objects.filter(
                team_id=instance.team_id, is_active=True
            ).order_by("name")
        if instance is not None:
            self.fields["container_id_input"].initial = instance.container_id
            self.fields["equipment_type"].initial = instance.equipment_type_id
            self.fields["status"].initial = instance.status
            self.fields["condition"].initial = instance.condition
            self.fields["color_code"].initial = instance.color_code
            self.fields["color_system"].initial = instance.color_system
            self.fields["manufacture_date"].initial = instance.manufacture_date
            self.fields["manufacturer"].initial = instance.manufacturer
            self.fields["manufacturer_id"].initial = instance.manufacturer_id
            self.fields["current_location"].initial = instance.current_location_id
            self.fields["notes"].initial = instance.notes

    def clean_container_id_input(self) -> dict:
        raw = self.cleaned_data["container_id_input"]
        parts = parse_container_id(raw)
        try:
            validate_container_id(
                parts["owner_code"],
                parts["category_id"],
                parts["serial_number"],
                parts["check_digit"],
            )
        except forms.ValidationError:
            raise
        return parts

    def get_container_data(self) -> dict:
        """Return a dict suitable for passing to create_container / update_container."""
        parts = self.cleaned_data["container_id_input"]
        return {
            "owner_code": parts["owner_code"],
            "category_id": parts["category_id"],
            "serial_number": parts["serial_number"],
            "check_digit": parts["check_digit"],
            "equipment_type": self.cleaned_data["equipment_type"],
            "status": self.cleaned_data["status"],
            "condition": self.cleaned_data["condition"],
            "color_code": self.cleaned_data.get("color_code", ""),
            "color_system": self.cleaned_data.get("color_system", ""),
            "manufacture_date": self.cleaned_data.get("manufacture_date"),
            "manufacturer": self.cleaned_data.get("manufacturer", ""),
            "manufacturer_id": self.cleaned_data.get("manufacturer_id", ""),
            "current_location": self.cleaned_data.get("current_location"),
            "notes": self.cleaned_data.get("notes", ""),
        }


class PlannedContainerForm(forms.Form):
    """Simple form for adding a container number to the planned pool."""

    container_number = forms.CharField(
        label=_("Container Number"),
        max_length=11,
        help_text=_("Full container number, e.g. MCUU1000001"),
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full font-mono", "placeholder": "MCUU1000001"}),
    )
    carrier = forms.CharField(
        label=_("Carrier"),
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full"}),
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"class": "textarea textarea-bordered w-full", "rows": 2}),
    )

    def clean_container_number(self) -> str:
        return self.cleaned_data["container_number"].upper().strip()


class ContainerLocationForm(forms.ModelForm):
    """Form for creating or editing a ContainerLocation."""

    class Meta:
        model = ContainerLocation
        fields = [
            "name",
            "location_type",
            "country",
            "city",
            "address",
            "external_reference",
            "owner_name",
            "notes",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "location_type": forms.Select(attrs={"class": "select select-bordered w-full"}),
            "country": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "city": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "address": forms.Textarea(attrs={"class": "textarea textarea-bordered w-full", "rows": 2}),
            "external_reference": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "owner_name": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "notes": forms.Textarea(attrs={"class": "textarea textarea-bordered w-full", "rows": 2}),
        }
