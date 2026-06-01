# Analytics views — request handling, response rendering, form handling only.

from django.shortcuts import render

from apps.scm.decorators import scm_login_required
from apps.scm.search import search_scm

from .selectors import get_latest_snapshot, get_live_dashboard_stats


@scm_login_required
def analytics_dashboard(request):
    team = request.default_team
    snapshot = get_latest_snapshot(team)
    live_stats = get_live_dashboard_stats(team)
    context = {
        "snapshot": snapshot,
        "live_stats": live_stats,
        "team": team,
    }
    return render(request, "scm/analytics/pages/analytics_dashboard.html", context)


@scm_login_required
def scm_search(request):
    """HTMX-friendly global SCM search endpoint."""
    team = request.default_team
    query = request.GET.get("q", "").strip()
    results = search_scm(team=team, query=query) if query else []
    return render(
        request,
        "scm/partials/search_results.html",
        {"results": results, "query": query},
    )
