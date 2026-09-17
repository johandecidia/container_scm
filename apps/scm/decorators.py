"""
SCM view decorators.

SCM URLs live at /scm/... without a team_slug path segment.
`request.team` is therefore None for these views, so we use
`request.default_team` (set by TeamsMiddleware from session or
the user's first team).
"""

from functools import wraps

from django.http import Http404, HttpResponseRedirect
from django.urls import reverse

from apps.teams.roles import is_admin


def scm_login_required(view_func):
    """Require login and at least one team membership.

    Uses `request.default_team` so it works for /scm/ URLs that have no
    team_slug in the path.
    """

    @wraps(view_func)
    def _inner(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return HttpResponseRedirect("{}?next={}".format(reverse("account_login"), request.path))
        if not request.default_team:
            raise Http404
        return view_func(request, *args, **kwargs)

    return _inner


def scm_team_admin_required(view_func):
    """Require login and *administrator* rights on the active team.

    The same shape as `scm_login_required` — `request.default_team`, because /scm/
    URLs carry no team_slug — with `apps.teams.roles.is_admin` as the test. No new
    permission concept: admin is the Membership role the team app already has, and
    this decorator is the only thing Settings needs in order to be closed.

    A member without admin rights gets a 404 rather than a 403, which is what
    `apps.teams.decorators.team_admin_required` does for the team-scoped URLs: it
    does not confirm that the page exists to somebody who may not see it.
    """

    @wraps(view_func)
    def _inner(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return HttpResponseRedirect("{}?next={}".format(reverse("account_login"), request.path))
        team = request.default_team
        if not team or not is_admin(request.user, team):
            raise Http404
        return view_func(request, *args, **kwargs)

    return _inner
