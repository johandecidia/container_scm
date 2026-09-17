from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from health_check.views import HealthCheckView

from apps.teams.decorators import login_and_team_required
from apps.teams.helpers import get_open_invitations_for_user


def home(request):
    if request.user.is_authenticated:
        team = request.default_team
        if team:
            return HttpResponseRedirect(reverse("web_team:home", args=[team.slug]))
        else:
            if (open_invitations := get_open_invitations_for_user(request.user)) and len(open_invitations) > 1:
                invitation = open_invitations[0]
                return HttpResponseRedirect(reverse("teams:accept_invitation", args=[invitation["id"]]))

            messages.info(
                request,
                _("Teams are enabled but you have no teams. Create a team below to access the rest of the dashboard."),
            )
            return HttpResponseRedirect(reverse("teams:manage_teams"))
    else:
        return render(request, "web/landing_page.html")


@login_and_team_required
def team_home(request, team_slug):
    """Send an authenticated SCM user to their Control Tower.

    A redirect rather than a second rendering of the Control Tower: that page has
    one source of truth, the visibility overview. The URL stays valid so team
    switching, invitations and ``Team.get_absolute_url`` keep working — and because
    reaching it with a slug is what puts the team in the session, the team-less
    ``/scm/`` URLs resolve to the team the user just picked.
    """
    return HttpResponseRedirect(reverse("visibility:overview"))


def simulate_error(request):
    raise Exception("This is a simulated error.")


class HealthCheck(HealthCheckView):
    def get(self, request, *args, **kwargs):
        tokens = settings.HEALTH_CHECK_TOKENS
        if tokens and request.GET.get("token") not in tokens:
            raise Http404
        return super().get(request, *args, **kwargs)
