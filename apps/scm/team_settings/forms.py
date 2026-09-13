"""Forms for the Settings area.

Nothing here re-implements validation that already exists. `SettingsInvitationForm`
is `apps.teams.forms.InvitationForm` with DaisyUI widgets on it — the duplicate-
invitation and already-a-member checks stay where they are, in the team app.

`CarrierCredentialForm` builds its fields from the carrier's auth style rather
than from a list of its own, so the form can only ever ask for credentials the
client will actually read.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.teams.forms import InvitationForm

from .tracking_selectors import MASKED_PLACEHOLDER, CarrierSettingsRow


class SettingsInvitationForm(InvitationForm):
    """The team app's invitation form, styled for this page."""

    def __init__(self, team, *args, **kwargs):
        super().__init__(team, *args, **kwargs)
        self.fields["email"].widget.attrs.update(
            {"class": "input input-bordered w-full", "placeholder": _("name@company.com")}
        )
        self.fields["role"].widget = forms.Select(
            choices=self.fields["role"].choices,
            attrs={"class": "select select-bordered w-full sm:w-auto"},
        )


# What each credential key is called in the UI, and the hint that stops somebody
# pasting the wrong half of a portal credential pair into it.
CREDENTIAL_LABELS: dict[str, str] = {
    "api_key": _("API key"),
    "client_id": _("Client ID"),
    "client_secret": _("Client secret"),
}


class CarrierCredentialForm(forms.Form):
    """Collect or replace the credentials for one direct carrier integration.

    The fields come from
    :func:`apps.scm.integrations.carriers.dcsa.client.credential_fields_for_auth_style`
    by way of the settings row, so an OAuth carrier asks for a client id and secret
    and an API-key carrier asks for a key, with no per-carrier form.

    **A stored credential is never rendered back.** The field is empty with a masked
    placeholder, and leaving it empty means "keep what is stored" — which is also
    what makes rotating one of two values possible without re-entering the other.
    A carrier with nothing stored yet must be given every field, because a partial
    credential set produces a call that fails with no explanation.
    """

    # Not a credential and not secret: a container number the account can see, used
    # only to verify connectivity. It is on this form because it is the one thing a
    # connection test cannot run without, and two of the three carriers ship without
    # one — a reference known to an account belongs to that account, not to this
    # repository.
    TEST_REFERENCE_FIELD = "test_connection_reference"

    def __init__(self, row: CarrierSettingsRow, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.row = row
        self.credential_field_names = tuple(row.credential_fields)
        for name in self.credential_field_names:
            self.fields[name] = forms.CharField(
                label=CREDENTIAL_LABELS.get(name, name.replace("_", " ").title()),
                required=not row.has_credentials,
                strip=True,
                widget=forms.PasswordInput(
                    render_value=False,
                    attrs={
                        "class": "input input-bordered w-full font-mono",
                        "autocomplete": "new-password",
                        "placeholder": MASKED_PLACEHOLDER if row.has_credentials else "",
                    },
                ),
            )
        if self.credential_field_names:
            self.fields[self.TEST_REFERENCE_FIELD] = forms.CharField(
                label=_("Test reference (optional)"),
                required=False,
                strip=True,
                initial=row.test_connection_reference,
                help_text=_("A container number this account can see. Used only by Test connection."),
                widget=forms.TextInput(
                    attrs={"class": "input input-bordered w-full font-mono uppercase", "placeholder": "MRKU1234567"}
                ),
            )

    def clean(self):
        cleaned = super().clean()
        if not self.credential_field_names:
            raise forms.ValidationError(_("This carrier's authentication style is not configurable here."))
        supplied = any((cleaned.get(name) or "").strip() for name in self.credential_field_names)
        reference = (cleaned.get(self.TEST_REFERENCE_FIELD) or "").strip()
        if self.row.has_credentials and not supplied and not reference:
            raise forms.ValidationError(_("Enter a new credential value or a test reference, or cancel."))
        return cleaned

    def changed_credentials(self) -> dict:
        """Only the credential values supplied — a blank field keeps the stored one."""
        return {
            name: self.cleaned_data[name].strip()
            for name in self.credential_field_names
            if (self.cleaned_data.get(name) or "").strip()
        }

    def test_reference(self) -> str:
        return (self.cleaned_data.get(self.TEST_REFERENCE_FIELD) or "").strip().upper()
