"""Forms for the Settings area.

Nothing here re-implements validation that already exists. `SettingsInvitationForm`
is `apps.teams.forms.InvitationForm` with DaisyUI widgets on it — the duplicate-
invitation and already-a-member checks stay where they are, in the team app.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.teams.forms import InvitationForm


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
