"""Visibility views — request handling and rendering only.

Every view is team-scoped through ``request.default_team``, the SCM convention for
URLs that carry no team slug. The GeoJSON endpoints take an id from the URL and
must therefore filter on both the id *and* the team: changing a number in the URL
has to return 404, not another team's map.
"""

from __future__ import annotations

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from apps.scm.containers.models import Container
from apps.scm.decorators import scm_login_required
from apps.scm.shipments.models import Shipment

from .geojson import journey_feature_collection, object_detail_urls, overview_feature_collection
from .mapbox import get_mapbox_config
from .read_models import ObjectKind
from .selectors import (
    get_container_journey_events,
    get_container_visibility,
    get_shipment_journey_events,
    get_shipment_visibility,
    get_visibility_overview,
    parse_visibility_filters,
)

BOARD_TEMPLATE = "scm/visibility/partials/visibility_board.html"


@scm_login_required
def visibility_overview(request):
    """The Supply Chain Visibility page, and its HTMX filter refreshes."""
    team = request.default_team
    overview = get_visibility_overview(team=team, filters=parse_visibility_filters(request.GET))
    context = {
        "overview": overview,
        "filters": overview.filters,
        "map_data_url": _map_data_url(request),
        "team_slug": team.slug,
    }
    if request.htmx:
        return render(request, BOARD_TEMPLATE, context)
    return render(
        request,
        "scm/visibility/pages/visibility_overview.html",
        {**context, "mapbox": get_mapbox_config()},
    )


@scm_login_required
def visibility_map_data(request):
    """Current positions for everything matching the current filters, as GeoJSON."""
    team = request.default_team
    overview = get_visibility_overview(team=team, filters=parse_visibility_filters(request.GET))
    return JsonResponse(overview_feature_collection(overview.objects))


@scm_login_required
def visibility_object_panel(request, kind: str, pk: int):
    """The SCM-styled card shown when a map object is selected."""
    team = request.default_team
    if kind == ObjectKind.SHIPMENT:
        shipment = get_object_or_404(Shipment, pk=pk, team=team)
        obj = get_shipment_visibility(team=team, shipment=shipment)
    elif kind == ObjectKind.CONTAINER:
        container = get_object_or_404(Container, pk=pk, team=team)
        obj = get_container_visibility(team=team, container=container)
    else:
        raise Http404

    return render(
        request,
        "scm/visibility/partials/visibility_object_panel.html",
        {"object": obj, **object_detail_urls(obj), "team_slug": team.slug},
    )


@scm_login_required
def shipment_map_data(request, pk: int):
    """One shipment's journey — its located events and how they connect."""
    team = request.default_team
    shipment = get_object_or_404(Shipment, pk=pk, team=team)
    events = get_shipment_journey_events(team=team, shipment=shipment)
    return JsonResponse(journey_feature_collection(events))


@scm_login_required
def container_map_data(request, pk: int):
    """One container's journey. Works with or without a shipment."""
    team = request.default_team
    container = get_object_or_404(Container, pk=pk, team=team)
    events = get_container_journey_events(team=team, container=container)
    return JsonResponse(journey_feature_collection(events, container_number=container.container_id))


def _map_data_url(request) -> str:
    """The overview GeoJSON URL carrying the current filters.

    The map is never rebuilt when a filter changes — its source is pointed at this
    URL again — so the URL has to describe the same selection the list shows.
    """
    base = reverse("visibility:map_data")
    query = request.GET.urlencode()
    return f"{base}?{query}" if query else base
