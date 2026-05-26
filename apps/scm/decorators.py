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
