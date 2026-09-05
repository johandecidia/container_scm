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

from apps.scm.containers.location_workspace import count_containers_at_location
from apps.scm.containers.models import Container, ContainerLocation
from apps.scm.decorators import scm_login_required
from apps.scm.shipments.models import Shipment
from apps.scm.tracking.journey import get_container_journey

from .geojson import (
    container_journey_feature_collection,
    journey_feature_collection,
    location_feature_collection,
    map_feature_collection,
)
from .map_positions import (
    MapFilters,
    PositionClass,
    get_container_map_positions,
    get_operational_map,
    parse_map_filters,
)
from .mapbox import get_mapbox_config
from .selectors import (
    get_shipment_journey_events,
    get_visibility_overview,
    parse_visibility_filters,
)
from .work_queues import (
    get_arrival_queue,
    get_exception_queue,
    parse_arrival_queue_filters,
    parse_exception_queue_filters,
)

BOARD_TEMPLATE = "scm/visibility/partials/visibility_board.html"
EXCEPTIONS_QUEUE_TEMPLATE = "scm/visibility/partials/exceptions_queue.html"
ARRIVALS_QUEUE_TEMPLATE = "scm/visibility/partials/arrivals_queue.html"
MAP_LOCATION_PANEL_TEMPLATE = "scm/visibility/partials/map_location_panel.html"

# The query parameters the map card's own controls own. Everything else in the
# query string belongs to the board and narrows which containers are in scope.
MAP_OWN_PARAMS = ("position", "destinations")


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
        {
            **context,
            "mapbox": get_mapbox_config(),
            # Built from the objects the board is already showing, so the map's
            # coverage numbers describe the same selection as the list beside them.
            "operational_map": get_operational_map(team, overview.objects, parse_map_filters(request.GET)),
        },
    )


@scm_login_required
def exceptions_queue(request):
    """The Exceptions work queue, and its HTMX filter refreshes.

    An HTMX request gets the queue partial only, so a filter change replaces the
    rows without re-rendering the page around them.
    """
    team = request.default_team
    queue = get_exception_queue(team=team, filters=parse_exception_queue_filters(request.GET))
    context = {"queue": queue, "filters": queue.filters, "team_slug": team.slug}
    if request.htmx:
        return render(request, EXCEPTIONS_QUEUE_TEMPLATE, context)
    return render(request, "scm/visibility/pages/exceptions.html", context)


@scm_login_required
def arrivals_queue(request):
    """The Arrivals work queue — what is expected, when and where."""
    team = request.default_team
    queue = get_arrival_queue(team=team, filters=parse_arrival_queue_filters(request.GET))
    context = {"queue": queue, "filters": queue.filters, "team_slug": team.slug}
    if request.htmx:
        return render(request, ARRIVALS_QUEUE_TEMPLATE, context)
    return render(request, "scm/visibility/pages/arrivals.html", context)


@scm_login_required
def visibility_map_data(request):
    """The operational map for everything matching the current filters, as GeoJSON.

    Two filter sets reach this endpoint and they do different jobs: the Control
    Tower's own filters decide *which containers* the map is about, and the map's
    decide which classes of position to draw. Both are read from the same query
    string, which is why the map and the board can never describe different fleets.
    """
    team = request.default_team
    overview = get_visibility_overview(team=team, filters=parse_visibility_filters(request.GET))
    operational_map = get_operational_map(team, overview.objects, parse_map_filters(request.GET))
    return JsonResponse(map_feature_collection(operational_map))


@scm_login_required
def visibility_map_location_panel(request, position_class: str, location_id: int):
    """The containers behind one map marker.

    What a marker saying "Oceanterminalen — 84" expands into. Team-scoped twice
    over: the location is looked up by id *and* team, and the containers come from
    the same filtered read the map itself was built from, so an id from another
    tenant is a 404 rather than a list.

    The panel is a list of containers with links into the Container Workspace,
    which is where a container is understood and acted on. It is not a second
    detail page for the map to own.
    """
    team = request.default_team
    if position_class not in PositionClass.values:
        raise Http404
    location = get_object_or_404(ContainerLocation, pk=location_id, team=team)

    overview = get_visibility_overview(team=team, filters=parse_visibility_filters(request.GET))
    # Every class is built, not only the ones the map is currently drawing: the
    # board's filters still narrow which containers are in scope, but a marker's
    # panel has to resolve even if the overlay that produced the link has since
    # been switched off.
    operational_map = get_operational_map(team, overview.objects, MapFilters(show_destinations=True))
    group = next(
        (
            candidate
            for candidate in operational_map.groups
            if candidate.position_class == position_class and candidate.location.pk == location.pk
        ),
        None,
    )
    return render(
        request,
        MAP_LOCATION_PANEL_TEMPLATE,
        {"group": group, "location": location, "position_class": position_class, "team_slug": team.slug},
    )


@scm_login_required
def location_map_data(request, location_id: int):
    """One canonical location as a single marker, for the Location Workspace.

    Two queries, not a whole workspace: the marker needs the place and the number
    of containers standing in it, and building the location's expected-arrivals tab
    to draw one dot would make the map the most expensive thing on the page.
    """
    team = request.default_team
    location = get_object_or_404(ContainerLocation, pk=location_id, team=team)
    if location.latitude is None or location.longitude is None:
        # No coordinates is a state the page states in words. Nothing to count.
        return JsonResponse(location_feature_collection(location))
    return JsonResponse(
        location_feature_collection(location, count_containers_at_location(team=team, location=location))
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
    """One container's journey, plus where MCR says it is and where it is going.

    Drawn from the unified journey, so every source that has reported this box
    contributes. LOC-4's canonical markers are added beside it: the accepted
    physical or tracking-derived position, and the shipment's canonical
    destination. The journey remains the evidence and the markers the conclusion —
    and no line is drawn between them, because a container and its destination are
    not a route.
    """
    team = request.default_team
    container = get_object_or_404(Container, pk=pk, team=team)
    journey = get_container_journey(team=team, container=container)
    return JsonResponse(
        container_journey_feature_collection(
            journey,
            container_number=container.container_id,
            positions=get_container_map_positions(team, container),
        )
    )


def _map_data_url(request) -> str:
    """The map GeoJSON URL carrying the board's filters, and only those.

    The map is never rebuilt when a filter changes — its source is pointed at this
    URL again — so the URL has to describe the same selection the list shows.

    The map's *own* parameters are stripped out on purpose. They are re-applied by
    the map card's controls, which are the authority on their own state; leaving
    them here as well would send each of them twice and make a shared link's query
    string grow every time somebody touched a toggle.
    """
    base = reverse("visibility:map_data")
    params = request.GET.copy()
    for own in MAP_OWN_PARAMS:
        params.pop(own, None)
    query = params.urlencode()
    return f"{base}?{query}" if query else base
