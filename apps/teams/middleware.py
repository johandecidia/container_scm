import contextlib

from django.utils.functional import SimpleLazyObject

from apps.teams.context import set_current_team, unset_current_team
from apps.teams.helpers import get_default_team_from_request, get_team_for_request
from apps.teams.models import Membership


def _get_team(request, view_kwargs):
    if not hasattr(request, "_cached_team"):
        team = get_team_for_request(request, view_kwargs)
        if team:
            request.session["team"] = team.id
        request._cached_team = team
    return request._cached_team


def _get_default_team(request, view_kwargs):
    if not hasattr(request, "_cached_default_team"):
        team = _get_team(request, view_kwargs)
        if not team:
            team = get_default_team_from_request(request)
        request._cached_default_team = team
    return request._cached_default_team


def _get_team_membership(request):
    if not hasattr(request, "_cached_team_membership"):
        team_membership = None
        if request.user.is_authenticated and request.team:
            with contextlib.suppress(Membership.DoesNotExist):
                team_membership = Membership.objects.get(team=request.team, user=request.user)
        request._cached_team_membership = team_membership
    return request._cached_team_membership


class TeamsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        finally:
            unset_current_team(getattr(request, "__team_context_token", None))

    def process_view(self, request, view_func, view_args, view_kwargs):
        # The team for the current request based solely on the `team_slug` view kwarg.
        # This will be None for non-team views or if the team does not exist.
        request.team = SimpleLazyObject(lambda: _get_team(request, view_kwargs))

        # This will be the same as `request.team` except when `request.team=None` in which case
        # it will be the default team for the current user, if they belong to any teams.
        request.default_team = SimpleLazyObject(lambda: _get_default_team(request, view_kwargs))

        # This is the team membership for the current user and current team. It will be None unless the user
        # is authenticated and is a member of `request.team`.
        request.team_membership = SimpleLazyObject(lambda: _get_team_membership(request))

        request.__team_context_token = set_current_team(request.team)
