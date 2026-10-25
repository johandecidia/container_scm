"""Form for Business Central location mappings (Settings → Integrations → Business Central → Locations).

Validated server-side: the code is an identifier, unique per integration; only a
physical location maps to a Pegasus location (one of the team's own); automatic
receive needs a physical location, a Pegasus location, the posted-receipt trigger
and an active mapping; creating containers needs automatic receive.
"""

from __future__ import annotations

import re
from typing import cast

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.scm.containers.models import ContainerLocation

from .business_systems.business_central.exceptions import BusinessCentralConfigurationError
from .business_systems.business_central.location_mappings import (
    apply_auto_receive_switch,
    normalise_location_code,
)
from .models import BusinessCentralLocationMapping, Integration

Mapping = BusinessCentralLocationMapping
_CODE = re.compile(r"^[A-Z0-9_.\-]{1,20}$")


class LocationMappingForm(forms.ModelForm):
    class Meta:
        model = Mapping
        fields = [
            "bc_location_code",
            "name",
            "location_type",
            "pegasus_location",
            "receive_trigger",
            "is_active",
            "auto_receive_enabled",
            "auto_create_containers",
        ]
        help_texts = {
            "location_type": _("Commercial and customer locations are never received physically."),
            "auto_receive_enabled": _(
                "Receive containers in Pegasus when Business Central posts a purchase receipt here. "
                "Only receipts posted after switching on are received."
            ),
            "auto_create_containers": _("Create a missing container when its item has a mapped equipment type."),
        }

    def __init__(self, *args, integration: Integration, **kwargs):
        self.integration = integration
        super().__init__(*args, **kwargs)
        self.instance.integration = integration
        self.instance.team = integration.team
        self._was_enabled = bool(self.instance.pk and self.instance.auto_receive_enabled)
        location = cast(forms.ModelChoiceField, self.fields["pegasus_location"])
        location.queryset = ContainerLocation.objects.filter(team=integration.team, is_active=True)
        location.required = False

    def clean_bc_location_code(self) -> str:
        value = normalise_location_code(self.cleaned_data["bc_location_code"])
        if not _CODE.match(value):
            raise forms.ValidationError(_("Use up to 20 letters, digits or the characters _ . -"))
        duplicates = Mapping.objects.filter(integration=self.integration, bc_location_code=value)
        if self.instance.pk:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise forms.ValidationError(_("This location code is already configured. Edit it instead."))
        return value

    def clean(self):
        cleaned = super().clean()
        physical = cleaned.get("location_type") == Mapping.LocationType.PHYSICAL
        if cleaned.get("pegasus_location") and not physical:
            self.add_error("pegasus_location", _("Only a physical location maps to a Pegasus location."))
        if cleaned.get("auto_receive_enabled"):
            if not physical:
                self.add_error("auto_receive_enabled", _("Only a physical location can receive automatically."))
            if not cleaned.get("pegasus_location"):
                self.add_error("auto_receive_enabled", _("Choose the Pegasus location to receive into."))
            if cleaned.get("receive_trigger") != Mapping.ReceiveTrigger.POSTED_PURCHASE_RECEIPT:
                self.add_error(
                    "auto_receive_enabled", _("Automatic receive needs the posted purchase receipt trigger.")
                )
            if not cleaned.get("is_active"):
                self.add_error("auto_receive_enabled", _("An inactive location cannot receive automatically."))
        if cleaned.get("auto_create_containers") and not cleaned.get("auto_receive_enabled"):
            self.add_error("auto_create_containers", _("Containers can only be created with automatic receive."))
        return cleaned

    def _post_clean(self):
        # The boundary is not a form field; set it before the model's constraints are validated.
        if not self.errors:
            self.instance.auto_receive_enabled = bool(self.cleaned_data.get("auto_receive_enabled"))
            try:
                apply_auto_receive_switch(self.instance, was_enabled=self._was_enabled)
            except BusinessCentralConfigurationError:
                self.add_error(
                    "auto_receive_enabled",
                    _("Sync the item ledger entries completely first, so earlier receipts are not received."),
                )
        super()._post_clean()
