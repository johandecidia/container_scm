from typing import cast

from django import forms
from django.utils.translation import gettext_lazy as _

from .choices import MovementType
from .intake import carrier_choices, parse_and_validate_container_number
from .location_identity import (
    normalize_alias_source,
    normalize_country_code,
    normalize_external_code,
    normalize_location_name,
    normalize_unlocode,
)
from .models import Container, ContainerLocation, EquipmentType, LocationAlias
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
        location_field = cast(forms.ModelChoiceField, self.fields["current_location"])
        if team is not None:
            location_field.queryset = ContainerLocation.objects.filter(team=team, is_active=True).order_by("name")
        elif instance is not None and instance.team_id:
            location_field.queryset = ContainerLocation.objects.filter(
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


class ContainerMovementForm(forms.Form):
    """Record a physical movement of one container: gate in, gate out, receive, transfer.

    One form for all four rather than four near-identical ones, because they differ
    only in which ends of the move are required — and that difference is a domain
    rule, not a presentation one. It is imported from ``movements`` and applied to
    the fields here, so the form and the service cannot come to disagree about
    whether a gate-out needs an origin.

    The form validates shape; it does not decide state. Whether the movement becomes
    ``Container.current_location`` is settled by ``record_container_movement``, and
    a movement that loses to a newer one is still a valid thing to have submitted.
    """

    movement_type = forms.ChoiceField(
        label=_("Movement"),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    from_location = forms.ModelChoiceField(
        label=_("From"),
        queryset=ContainerLocation.objects.none(),
        required=False,
        empty_label=_("— Not recorded —"),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    to_location = forms.ModelChoiceField(
        label=_("To"),
        queryset=ContainerLocation.objects.none(),
        required=False,
        empty_label=_("— Not recorded —"),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    occurred_at = forms.DateTimeField(
        label=_("Occurred at"),
        help_text=_("When the container physically moved — not when it is being entered."),
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local", "class": "input input-bordered w-full"},
            format="%Y-%m-%dT%H:%M",
        ),
    )
    gate_name = forms.CharField(
        label=_("Gate"),
        max_length=100,
        required=False,
        help_text=_("The gate the container passed through, if it is worth recording."),
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full", "placeholder": "John Evans"}),
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"class": "textarea textarea-bordered w-full", "rows": 2}),
    )

    def __init__(self, *args, team=None, container=None, movement_type=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .movements import OPERATIONAL_MOVEMENT_TYPES, requires_destination, requires_origin

        self.container = container
        self.fields["movement_type"].choices = [
            (value, MovementType(value).label) for value in OPERATIONAL_MOVEMENT_TYPES
        ]

        locations = (
            ContainerLocation.objects.none()
            if team is None
            else ContainerLocation.objects.filter(team=team, is_active=True).order_by("name")
        )
        cast(forms.ModelChoiceField, self.fields["from_location"]).queryset = locations
        cast(forms.ModelChoiceField, self.fields["to_location"]).queryset = locations

        # The chosen type decides what the form insists on. Read from the submitted
        # data when there is some, so validation applies the rules for the movement
        # actually being recorded rather than the one the modal opened with.
        chosen = (self.data.get("movement_type") if self.is_bound else None) or movement_type
        if chosen in MovementType.values:
            self.fields["movement_type"].initial = chosen
            self.fields["to_location"].required = requires_destination(chosen)
            # A gate-out's origin may be left blank and taken from where the
            # container currently is; it is only mandatory when there is nothing to
            # take it from, which the service is the one able to judge.
            self.fields["from_location"].required = requires_origin(chosen) and (
                container is None or container.current_location_id is None
            )

        if container is not None and not self.is_bound:
            self.fields["from_location"].initial = container.current_location_id

    def clean_occurred_at(self):
        """Make a naive datetime from the browser aware, in the active timezone.

        ``datetime-local`` has no offset, so what arrives is naive. Storing it
        without a zone would make the movement's position in the ordering depend on
        the server's idea of the time — and the ordering is what decides state.
        """
        from django.utils import timezone as tz

        value = self.cleaned_data["occurred_at"]
        if value is not None and tz.is_naive(value):
            value = tz.make_aware(value)
        return value

    def movement_data(self) -> dict:
        """The cleaned values, for ``record_container_movement``."""
        return {
            "movement_type": self.cleaned_data["movement_type"],
            "from_location": self.cleaned_data.get("from_location"),
            "to_location": self.cleaned_data.get("to_location"),
            "occurred_at": self.cleaned_data["occurred_at"],
            "gate_name": self.cleaned_data.get("gate_name", ""),
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
    """Form for creating or editing a canonical location.

    ``unlocode`` accepts what somebody actually types — ``segot``, ``SE GOT``,
    ``SEGOT`` — and canonicalises it before the model sees it. It is declared here
    rather than taken from the model for exactly that reason: the column holds five
    characters, and a field inheriting that limit would reject "SE GOT" on length
    before ``clean_unlocode`` could turn it into the five it holds.
    """

    unlocode = forms.CharField(
        label=_("UN/LOCODE"),
        required=False,
        # Room for the separators people type, not for a longer code. Anything that
        # does not canonicalise to a real code is rejected by `clean_unlocode`.
        max_length=12,
        widget=forms.TextInput(attrs={"class": "input input-bordered w-full uppercase", "maxlength": 12}),
    )

    class Meta:
        model = ContainerLocation
        fields = [
            "name",
            "location_type",
            "parent_location",
            "unlocode",
            "country_code",
            "country",
            "city",
            "latitude",
            "longitude",
            "timezone",
            "address",
            "external_reference",
            "owner_name",
            "notes",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "location_type": forms.Select(attrs={"class": "select select-bordered w-full"}),
            "parent_location": forms.Select(attrs={"class": "select select-bordered w-full"}),
            "country_code": forms.TextInput(attrs={"class": "input input-bordered w-full uppercase", "maxlength": 2}),
            "country": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "city": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "latitude": forms.NumberInput(attrs={"class": "input input-bordered w-full", "step": "0.000001"}),
            "longitude": forms.NumberInput(attrs={"class": "input input-bordered w-full", "step": "0.000001"}),
            "timezone": forms.TextInput(
                attrs={"class": "input input-bordered w-full", "placeholder": "Europe/Stockholm"}
            ),
            "address": forms.Textarea(attrs={"class": "textarea textarea-bordered w-full", "rows": 2}),
            "external_reference": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "owner_name": forms.TextInput(attrs={"class": "input input-bordered w-full"}),
            "notes": forms.Textarea(attrs={"class": "textarea textarea-bordered w-full", "rows": 2}),
        }

    def __init__(self, *args, team=None, **kwargs):
        super().__init__(*args, **kwargs)
        parent_field = cast(forms.ModelChoiceField, self.fields["parent_location"])
        parent_field.empty_label = _("— No parent —")

        # A parent has to be one of this team's locations, and it cannot be this
        # location itself. Scoped on the queryset rather than only in `clean` so a
        # foreign id is not even offered, and cannot be posted.
        team = team or (self.instance.team if self.instance and self.instance.team_id else None)
        queryset = ContainerLocation.objects.none() if team is None else ContainerLocation.objects.filter(team=team)
        if self.instance and self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        parent_field.queryset = queryset.order_by("name")

        for name in ("country_code", "latitude", "longitude", "timezone", "parent_location"):
            self.fields[name].required = False

    def clean_unlocode(self) -> str:
        """Canonicalise the code, or reject it as not being one.

        A value that cannot be canonicalised is an error rather than silently
        dropped: somebody typing a code into the UN/LOCODE box means to record one,
        and quietly saving nothing would leave them believing they had.
        """
        raw = (self.cleaned_data.get("unlocode") or "").strip()
        if not raw:
            return ""
        normalized = normalize_unlocode(raw)
        if not normalized:
            raise forms.ValidationError(
                _("%(value)s is not a UN/LOCODE. Expected two country letters and three place characters, e.g. SEGOT.")
                % {"value": raw}
            )
        return normalized

    def clean_country_code(self) -> str:
        raw = (self.cleaned_data.get("country_code") or "").strip()
        if not raw:
            return ""
        normalized = normalize_country_code(raw)
        if not normalized:
            raise forms.ValidationError(_("Expected a two-letter ISO country code, e.g. SE."))
        return normalized


class LocationEvidenceAliasForm(forms.Form):
    """Record one row of the Location Data Quality queue as an alias.

    The same decision :class:`LocationAliasForm` records, arrived at from the other
    direction. There, an operator is looking at a place and says what a provider
    calls it; here they are looking at what a provider called something and say
    which place it is. So the evidence is fixed and carried in hidden fields, and
    the only thing being chosen is the canonical location.

    There is deliberately no "create the location too". A canonical location is
    master data somebody should mean to add, and a one-click "create from carrier
    text" is how a location list ends up with four spellings of Göteborg in it.

    Both identifiers travel with the evidence, but only ``external_name`` is stored.
    The alias table's ``external_code`` is for a *provider's own* identifier for a
    place, and the resolver looks it up against a query field the tracking pipeline
    never fills; putting a UN/LOCODE there would file the code where nothing reads
    it. A code that names a place belongs on the location, through the location form.
    """

    location = forms.ModelChoiceField(
        label=_("Canonical location"),
        queryset=ContainerLocation.objects.none(),
        empty_label=_("— Choose a location —"),
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    source = forms.CharField(max_length=50, widget=forms.HiddenInput())
    external_name = forms.CharField(max_length=200, widget=forms.HiddenInput())

    def __init__(self, *args, team=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Scoped on the queryset rather than only in `clean`, so another team's id
        # is not offered and cannot be posted.
        location_field = cast(forms.ModelChoiceField, self.fields["location"])
        location_field.queryset = (
            ContainerLocation.objects.none()
            if team is None
            else ContainerLocation.objects.filter(team=team, is_active=True)
            .select_related("parent_location")
            .order_by("name")
        )

    def clean_source(self) -> str:
        source = normalize_alias_source(self.cleaned_data.get("source"))
        if not source:
            raise forms.ValidationError(_("An alias needs a source."))
        return source

    def clean_external_name(self) -> str:
        name = (self.cleaned_data.get("external_name") or "").strip()
        if not normalize_location_name(name):
            raise forms.ValidationError(_("An alias needs the name the source reported."))
        return name

    def alias_data(self) -> dict:
        """The cleaned values, for ``create_location_alias``."""
        return {
            "source": self.cleaned_data["source"],
            "external_name": self.cleaned_data["external_name"],
        }


class LocationAliasForm(forms.ModelForm):
    """Form for recording what an external source calls a location.

    ``source`` is a free-text field rather than a dropdown because most valid
    values are ``TrackingProvider`` codes — rows in the database, added as providers
    are onboarded — so a fixed list would go stale. Known providers are offered as
    suggestions through a datalist; the two reserved sources are documented in the
    field's help text.
    """

    class Meta:
        model = LocationAlias
        fields = ["source", "external_code", "external_name", "latitude", "longitude"]
        widgets = {
            "source": forms.TextInput(
                attrs={
                    "class": "input input-bordered w-full lowercase",
                    "list": "alias-source-options",
                    "placeholder": "traqo",
                }
            ),
            "external_code": forms.TextInput(attrs={"class": "input input-bordered w-full uppercase"}),
            "external_name": forms.TextInput(
                attrs={"class": "input input-bordered w-full", "placeholder": "GOTHENBURG"}
            ),
            "latitude": forms.NumberInput(attrs={"class": "input input-bordered w-full", "step": "0.000001"}),
            "longitude": forms.NumberInput(attrs={"class": "input input-bordered w-full", "step": "0.000001"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source"].help_text = _(
            "A tracking provider code such as traqo or maersk, or one of: unlocode, internal."
        )
        for name in ("external_code", "external_name", "latitude", "longitude"):
            self.fields[name].required = False

    def clean_source(self) -> str:
        source = normalize_alias_source(self.cleaned_data.get("source"))
        if not source:
            raise forms.ValidationError(_("An alias needs a source."))
        return source

    def clean(self):
        """An alias with neither identifier says nothing and is not stored."""
        cleaned = super().clean()
        code = normalize_external_code(cleaned.get("external_code"))
        name = normalize_location_name(cleaned.get("external_name"))
        if not code and not name:
            raise forms.ValidationError(_("Give the external code, the external name, or both."))
        return cleaned

    def alias_data(self) -> dict:
        """The cleaned values, for ``create_location_alias``."""
        return {
            "source": self.cleaned_data["source"],
            "external_code": self.cleaned_data.get("external_code") or "",
            "external_name": self.cleaned_data.get("external_name") or "",
            "latitude": self.cleaned_data.get("latitude"),
            "longitude": self.cleaned_data.get("longitude"),
        }
