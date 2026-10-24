"""Forms for configuring integrations from Settings → Integrations.

`BusinessCentralIntegrationForm` collects the non-secret connection settings and
the Entra app credentials. **The client secret is never rendered back**: the field
is always empty, and leaving it empty on edit keeps the stored secret. Persisting
is the configuration service's job, which encrypts via the credential service.
"""

from __future__ import annotations

import re

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.teams.models import Team

from .business_systems.business_central.client import _require_guid
from .business_systems.business_central.configuration import (
    DEFAULT_API_VERSION,
    PROVIDER_CODE,
    get_stored_client_id,
    has_stored_client_secret,
)
from .business_systems.business_central.exceptions import BusinessCentralConfigurationError
from .models import Integration

MASKED_PLACEHOLDER = "••••••••"

# A tenant is addressed by GUID or by a verified domain (contoso.onmicrosoft.com).
_TENANT_DOMAIN = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")
# Business Central environment names: letters, digits, '-' and '_', at most 30 chars.
_ENVIRONMENT = re.compile(r"^[A-Za-z0-9_-]{1,30}$")
_API_VERSION = re.compile(r"^v\d+\.\d+$")

# The project's widget templates already apply DaisyUI's input/select/checkbox classes.
_MONO_INPUT = "font-mono"


class BusinessCentralIntegrationForm(forms.Form):
    name = forms.CharField(label=_("Integration name"), max_length=100, widget=forms.TextInput())
    provider = forms.ChoiceField(
        label=_("Provider"),
        choices=[(PROVIDER_CODE, _("Microsoft Dynamics 365 Business Central"))],
        initial=PROVIDER_CODE,
    )
    tenant_id = forms.CharField(
        label=_("Tenant ID"),
        help_text=_("Microsoft Entra tenant GUID or domain."),
        widget=forms.TextInput(attrs={"class": _MONO_INPUT, "placeholder": "00000000-0000-0000-0000-000000000000"}),
    )
    environment = forms.CharField(
        label=_("Environment"),
        help_text=_("Business Central environment name, e.g. Production or Sandbox."),
    )
    company_id = forms.CharField(
        label=_("Company ID"),
        widget=forms.TextInput(attrs={"class": _MONO_INPUT, "placeholder": "00000000-0000-0000-0000-000000000000"}),
    )
    client_id = forms.CharField(
        label=_("Client ID"),
        help_text=_("Application (client) ID of the Entra app registration."),
        widget=forms.TextInput(attrs={"class": _MONO_INPUT, "autocomplete": "off"}),
    )
    client_secret = forms.CharField(
        label=_("Client secret"),
        strip=True,
        widget=forms.PasswordInput(render_value=False, attrs={"class": _MONO_INPUT, "autocomplete": "new-password"}),
    )
    api_version = forms.CharField(
        label=_("API version"), initial=DEFAULT_API_VERSION, widget=forms.TextInput(attrs={"class": _MONO_INPUT})
    )
    is_active = forms.BooleanField(
        label=_("Active"), required=False, initial=True, widget=forms.CheckboxInput(attrs={"class": "toggle"})
    )

    def __init__(self, team: Team, *args, integration: Integration | None = None, **kwargs):
        self.team = team
        self.integration = integration
        if integration is not None and not args and "data" not in kwargs:
            config = integration.config or {}
            kwargs["initial"] = {
                "name": integration.name,
                "provider": PROVIDER_CODE,
                "tenant_id": config.get("tenant_id", ""),
                "environment": config.get("environment", ""),
                "company_id": config.get("company_id", ""),
                "client_id": get_stored_client_id(integration),
                "api_version": config.get("api_version") or DEFAULT_API_VERSION,
                "is_active": integration.is_active,
            }
        super().__init__(*args, **kwargs)
        if integration is not None and has_stored_client_secret(integration):
            secret = self.fields["client_secret"]
            secret.required = False
            secret.help_text = _("Stored encrypted. Leave blank to keep the current secret.")
            secret.widget.attrs["placeholder"] = MASKED_PLACEHOLDER

    def clean_tenant_id(self) -> str:
        value = self.cleaned_data["tenant_id"].strip()
        try:
            return _require_guid(value, "tenant_id")
        except BusinessCentralConfigurationError:
            if _TENANT_DOMAIN.match(value):
                return value.lower()
        raise forms.ValidationError(_("Enter a tenant GUID or a domain such as contoso.onmicrosoft.com."))

    def clean_environment(self) -> str:
        value = self.cleaned_data["environment"].strip()
        if not _ENVIRONMENT.match(value):
            raise forms.ValidationError(_("Use up to 30 letters, digits, hyphens or underscores."))
        return value

    def clean_company_id(self) -> str:
        try:
            return _require_guid(self.cleaned_data["company_id"].strip(), "company_id")
        except BusinessCentralConfigurationError:
            raise forms.ValidationError(_("Enter the company ID as a GUID.")) from None

    def clean_client_id(self) -> str:
        try:
            return _require_guid(self.cleaned_data["client_id"].strip(), "client_id")
        except BusinessCentralConfigurationError:
            raise forms.ValidationError(_("Enter the application (client) ID as a GUID.")) from None

    def clean_api_version(self) -> str:
        value = self.cleaned_data["api_version"].strip()
        if not _API_VERSION.match(value):
            raise forms.ValidationError(_("Use the form v2.0."))
        return value

    def clean(self):
        cleaned = super().clean()
        # One Business Central integration per team (Integration is unique on team + provider_code).
        existing = Integration.objects.filter(team=self.team, provider_code=PROVIDER_CODE)
        if self.integration is not None:
            existing = existing.exclude(pk=self.integration.pk)
        if existing.exists():
            raise forms.ValidationError(
                _("This team already has a Business Central integration. Edit it, or use a separate team.")
            )
        return cleaned

    def connection_config(self) -> dict:
        return {key: self.cleaned_data[key] for key in ("tenant_id", "environment", "company_id", "api_version")}

    def changed_credentials(self) -> dict:
        """The credential values to store — a blank secret keeps the stored one."""
        credentials = {"client_id": self.cleaned_data["client_id"]}
        if self.cleaned_data.get("client_secret"):
            credentials["client_secret"] = self.cleaned_data["client_secret"]
        return credentials
