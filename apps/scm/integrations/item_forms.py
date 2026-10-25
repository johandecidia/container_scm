"""Forms for Business Central item classification (Settings → Integrations → Business Central).

Validated server-side: item numbers and variants are identifiers, a variant only
applies to an exact rule, an equipment type only to a container rule, and a rule
may not duplicate another of the same integration.
"""

from __future__ import annotations

import re
from typing import cast

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.scm.containers.models import EquipmentType

from .models import BusinessCentralItemLedgerEntry, BusinessCentralItemRule, Integration

# Business Central item numbers and variant codes are Code[20] / Code[10].
_ITEM_NO = re.compile(r"^[A-Z0-9_.\-/]{1,20}$")
_VARIANT = re.compile(r"^[A-Z0-9_.\-/]{1,10}$")


class ItemRuleForm(forms.ModelForm):
    class Meta:
        model = BusinessCentralItemRule
        fields = ["match_type", "item_no", "variant_code", "classification", "equipment_type"]
        help_texts = {
            "item_no": _("The Business Central item number, or the start of it for a prefix rule."),
            "variant_code": _("Optional, exact rules only. Item ledger entries currently carry no variant."),
            "equipment_type": _("Required to create containers from this item."),
        }

    def __init__(self, *args, integration: Integration, **kwargs):
        self.integration = integration
        super().__init__(*args, **kwargs)
        equipment_type = cast(forms.ModelChoiceField, self.fields["equipment_type"])
        equipment_type.queryset = EquipmentType.objects.filter(is_active=True)
        equipment_type.required = False

    def clean_item_no(self) -> str:
        value = self.cleaned_data["item_no"].strip().upper()
        if not _ITEM_NO.match(value):
            raise forms.ValidationError(_("Use up to 20 letters, digits or the characters _ . - /"))
        return value

    def clean_variant_code(self) -> str:
        value = (self.cleaned_data.get("variant_code") or "").strip().upper()
        if value and not _VARIANT.match(value):
            raise forms.ValidationError(_("Use up to 10 letters, digits or the characters _ . - /"))
        return value

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("variant_code") and cleaned.get("match_type") == BusinessCentralItemRule.MatchType.PREFIX:
            self.add_error("variant_code", _("A variant can only be set on an exact rule."))
        container = BusinessCentralItemLedgerEntry.ItemClassification.CONTAINER
        if cleaned.get("equipment_type") and cleaned.get("classification") != container:
            self.add_error("equipment_type", _("Only container items map to an equipment type."))
        duplicates = BusinessCentralItemRule.objects.filter(
            integration=self.integration,
            match_type=cleaned.get("match_type"),
            item_no=cleaned.get("item_no"),
            variant_code=cleaned.get("variant_code") or "",
        )
        if self.instance.pk:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise forms.ValidationError(_("A rule for this item already exists. Edit it instead."))
        return cleaned

    def save(self, commit: bool = True) -> BusinessCentralItemRule:
        rule = super().save(commit=False)
        rule.integration = self.integration
        rule.team = self.integration.team
        if commit:
            rule.save()
        return rule
