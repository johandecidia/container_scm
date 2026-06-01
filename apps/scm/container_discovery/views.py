"""Container discovery views — request handling and response rendering only.

Business logic belongs in services.py and discovery_service.py;
queries belong in selectors.py.
"""

from django.shortcuts import render

from apps.scm.decorators import scm_login_required

from .selectors import get_container_discovery_dashboard, get_planned_containers


@scm_login_required
def container_discovery_dashboard(request):
    team = request.default_team
    dashboard = get_container_discovery_dashboard(team=team)
    planned = get_planned_containers(team=team)
    context = {
        "dashboard": dashboard,
        "planned": planned,
        "team_slug": team.slug,
    }
    return render(request, "scm/container_discovery/pages/dashboard.html", context)
