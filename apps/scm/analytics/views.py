# Analytics views — request handling, response rendering, form handling only.

import datetime
import json

from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_login_required
from apps.scm.search import search_scm

from .alerts import get_scm_alerts
from .models import SavedFilter
from .selectors import get_latest_snapshot, get_live_dashboard_stats, get_saved_filters
from .services import (
    get_carrier_analytics,
    get_container_analytics,
    get_supplier_analytics,
    get_transit_time_analytics,
)


def _parse_date(value: str) -> datetime.date | None:
    """Parse an ISO date string, returning None on invalid/empty input."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


@scm_login_required
def analytics_dashboard(request):
    team = request.default_team
    date_from = _parse_date(request.GET.get("date_from", ""))
    date_to = _parse_date(request.GET.get("date_to", ""))

    snapshot = get_latest_snapshot(team)
    live_stats = get_live_dashboard_stats(team)
    transit_stats = get_transit_time_analytics(team, date_from=date_from, date_to=date_to)
    carrier_data = get_carrier_analytics(team, date_from=date_from, date_to=date_to)
    container_stats = get_container_analytics(team)
    supplier_data = get_supplier_analytics(team, date_from=date_from, date_to=date_to)
    alerts = get_scm_alerts(team)

    context = {
        "snapshot": snapshot,
        "live_stats": live_stats,
        "transit_stats": transit_stats,
        "carrier_data": carrier_data,
        "container_stats": container_stats,
        "supplier_data": supplier_data,
        "alerts": alerts,
        "date_from": date_from.isoformat() if date_from else "",
        "date_to": date_to.isoformat() if date_to else "",
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


# ---------------------------------------------------------------------------
# Saved filter views (HTMX endpoints — POST only for state changes)
# ---------------------------------------------------------------------------


@require_POST
@scm_login_required
def saved_filter_create(request):
    """Create a saved filter and return the updated list partial."""
    team = request.default_team
    user = request.user
    name = request.POST.get("name", "").strip()
    view_key = request.POST.get("view_key", "")
    params_raw = request.POST.get("params", "{}")

    try:
        params = json.loads(params_raw)
    except json.JSONDecodeError, TypeError:
        params = {}

    if name and view_key in SavedFilter.ViewKey.values:
        SavedFilter.objects.create(
            team=team,
            user=user,
            name=name,
            view_key=view_key,
            params=params,
        )

    saved_filters = get_saved_filters(team, user, view_key)
    return render(
        request,
        "scm/analytics/partials/saved_filters_list.html",
        {"saved_filters": saved_filters, "view_key": view_key},
    )


@require_POST
@scm_login_required
def saved_filter_delete(request, pk: int):
    """Delete a saved filter and return the updated list partial."""
    team = request.default_team
    saved_filter = get_object_or_404(SavedFilter, pk=pk, team=team, user=request.user)
    view_key = saved_filter.view_key
    saved_filter.delete()
    saved_filters = get_saved_filters(team, request.user, view_key)
    return render(
        request,
        "scm/analytics/partials/saved_filters_list.html",
        {"saved_filters": saved_filters, "view_key": view_key},
    )
