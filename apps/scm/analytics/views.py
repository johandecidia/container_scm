# Analytics views — request handling, response rendering, form handling only.

from django.shortcuts import render

from apps.scm.decorators import scm_login_required

from .selectors import get_latest_snapshot


@scm_login_required
def analytics_dashboard(request):
    team = request.default_team
    snapshot = get_latest_snapshot(team)
    context = {
        "snapshot": snapshot,
        "team": team,
    }
    return render(request, "scm/analytics/pages/analytics_dashboard.html", context)
