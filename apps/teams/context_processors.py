from apps.teams.helpers import get_open_invitations_for_user


def team(request):
    team = getattr(request, "team", None) or getattr(request, "default_team", None)
    return {
        "team": team,
    }


def user_teams(request):
    if not hasattr(request, "user") or not request.user.is_authenticated:
        return {}

    current_team = getattr(request, "team", None) or getattr(request, "default_team", None)
    if not current_team:
        return {}

    other_membership = request.user.membership_set
    if current_team and current_team.pk:
        other_membership = other_membership.exclude(team=current_team)

    pending_invitations = get_open_invitations_for_user(request.user)
    return {
        "other_teams": {
            membership.team.name: membership.team.dashboard_url
            for membership in other_membership.select_related("team")
        },
        "user_pending_invitations": pending_invitations,
    }
