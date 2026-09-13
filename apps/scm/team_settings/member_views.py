"""Settings → Members: who is on the team, and who may manage it.

Request handling and rendering only. Everything this page writes goes through the
team app's own models and services — `Membership`, `Invitation`,
`InvitationForm`, `MembershipForm` and `send_invitation` — so there is one
definition of what a team member is and one invitation email.

**One rule is enforced here rather than in a form**: a team must always keep at
least one administrator. `apps.teams.roles.is_final_admin` answers it, and both
the role change and the removal ask the same question, so they cannot disagree
about what the last admin is. Without it a team can lock itself out of its own
settings, which nothing in the product can repair.

Every write re-renders the whole members panel, so the member table, the pending
invitations and any notice are one response and cannot drift apart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_team_admin_required
from apps.teams.forms import MembershipForm
from apps.teams.invitations import send_invitation
from apps.teams.models import Invitation, Membership
from apps.teams.roles import ROLE_CHOICES, is_final_admin

from .forms import SettingsInvitationForm
from .selectors import get_admin_count, get_member_rows, get_pending_invitations

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

MEMBERS_PAGE_TEMPLATE = "scm/team_settings/pages/members.html"
MEMBERS_PANEL_TEMPLATE = "scm/team_settings/partials/member_panel.html"

FINAL_ADMIN_MESSAGE = _("This is the team's only administrator. Make another member an administrator first.")


def _panel_context(request, *, invitation_form=None, notice: StrOrPromise = "") -> dict:
    """Everything the members panel renders, for a page load or an HTMX swap."""
    team = request.default_team
    return {
        "team": team,
        "team_slug": team.slug,
        "member_rows": get_member_rows(team, request.user),
        "admin_count": get_admin_count(team),
        "role_choices": ROLE_CHOICES,
        "pending_invitations": get_pending_invitations(team),
        "invitation_form": invitation_form or SettingsInvitationForm(team),
        "notice": notice,
    }


def _panel(request, **kwargs):
    return render(request, MEMBERS_PANEL_TEMPLATE, _panel_context(request, **kwargs))


def _respond(request, *, notice: StrOrPromise = "", level=messages.success, message=None, **kwargs):
    """Swap the panel back for HTMX; fall back to a message and a redirect otherwise."""
    if request.htmx:
        return _panel(request, notice=notice, **kwargs)
    if message is not None:
        level(request, message)
    return redirect("team_settings:members")


@scm_team_admin_required
def members(request):
    """The team's members and pending invitations."""
    return render(request, MEMBERS_PAGE_TEMPLATE, _panel_context(request))


@scm_team_admin_required
@require_POST
def member_role_update(request, membership_id: int):
    """Change one member's role between Member and Admin.

    Demoting the final administrator is refused — including demoting yourself, which
    is the way it actually happens. A team with no admin cannot invite, promote or
    reach this page again.
    """
    team = request.default_team
    membership = get_object_or_404(Membership, team=team, pk=membership_id)

    # Asked before the form is bound, deliberately. A bound ModelForm writes the
    # posted values onto its instance during validation, so a guard that read
    # `membership.role` afterwards would be comparing the change to itself.
    if request.POST.get("role") != membership.role and is_final_admin(membership):
        return _respond(request, notice=FINAL_ADMIN_MESSAGE, level=messages.error, message=FINAL_ADMIN_MESSAGE)

    form = MembershipForm(request.POST, instance=membership)
    if not form.is_valid():
        invalid = _("That is not a valid role.")
        return _respond(request, notice=invalid, level=messages.error, message=invalid)

    membership = form.save()
    message = _("Role for {member} updated.").format(member=membership.user.get_display_name())
    return _respond(request, notice=message, message=message)


@scm_team_admin_required
@require_POST
def member_remove(request, membership_id: int):
    """Remove a member from the team. The final administrator cannot be removed."""
    team = request.default_team
    membership = get_object_or_404(Membership, team=team, pk=membership_id)
    if is_final_admin(membership):
        return _respond(request, notice=FINAL_ADMIN_MESSAGE, level=messages.error, message=FINAL_ADMIN_MESSAGE)

    display_name = membership.user.get_display_name()
    membership.delete()
    message = _("{member} was removed from {team}.").format(member=display_name, team=team.name)
    return _respond(request, notice=message, message=message)


@scm_team_admin_required
@require_POST
def invitation_send(request):
    """Invite somebody to the team, through the team app's own invitation email."""
    team = request.default_team
    form = SettingsInvitationForm(team, request.POST)
    if not form.is_valid():
        return _respond(
            request, invitation_form=form, level=messages.error, message=_("That invitation could not be sent.")
        )

    invitation = form.save(commit=False)
    invitation.team = team
    invitation.invited_by = request.user
    invitation.save()
    send_invitation(invitation)
    message = _("Invitation sent to {email}.").format(email=invitation.email)
    return _respond(request, notice=message, message=message)


@scm_team_admin_required
@require_POST
def invitation_resend(request, invitation_id):
    team = request.default_team
    invitation = get_object_or_404(Invitation, team=team, id=invitation_id, is_accepted=False)
    send_invitation(invitation)
    message = _("Invitation resent to {email}.").format(email=invitation.email)
    return _respond(request, notice=message, message=message)


@scm_team_admin_required
@require_POST
def invitation_cancel(request, invitation_id):
    team = request.default_team
    invitation = get_object_or_404(Invitation, team=team, id=invitation_id, is_accepted=False)
    email = invitation.email
    invitation.delete()
    message = _("Invitation to {email} cancelled.").format(email=email)
    return _respond(request, notice=message, message=message)
