# Container views — request handling, response rendering, form handling only.
# Business logic belongs in services.py; queries belong in selectors.py.
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.analytics.models import SavedFilter
from apps.scm.analytics.selectors import get_saved_filters
from apps.scm.decorators import scm_login_required
from apps.scm.tracking.manual_refresh import refresh_container_tracking
from apps.scm.visibility.context import get_container_map_context, get_location_map_context

from .activity import get_container_activity
from .choices import ContainerStatus, MovementType
from .discovery import (
    add_planned_container,
    cancel_planned_container,
    get_planned_containers,
    run_discovery_for_team,
)
from .forms import (
    ContainerForm,
    ContainerLocationForm,
    ContainerMovementForm,
    LocationAliasForm,
    LocationEvidenceAliasForm,
    PlannedContainerForm,
)
from .models import Container, ContainerLocation, LocationAlias, PlannedContainer, PlannedContainerStatus
from .movements import record_container_movement
from .selectors import (
    filter_containers,
    get_active_equipment_types,
    get_alias_source_suggestions,
    get_container_workspace,
    get_location_aliases,
    get_location_inventory,
    get_location_overview_movements,
    get_location_workspace,
    get_team_locations_with_counts,
)
from .services import (
    create_location,
    create_location_alias,
    delete_container,
    delete_location_alias,
    update_container,
    update_location,
)

CONTAINERS_PER_PAGE = 25

# The tracking panel is its own HTMX component: the detail page includes it, and
# a refresh re-renders exactly this and nothing else.
TRACKING_PANEL_TEMPLATE = "scm/containers/partials/container_tracking_panel.html"

# The location workspace's inventory table is its own HTMX component: filtering and
# paging it replaces the table without re-rendering the workspace around it.
LOCATION_INVENTORY_TEMPLATE = "scm/containers/partials/location_inventory.html"

# Maps a RefreshResult level onto the messages framework, so the tracking service
# stays independent of it.
_MESSAGE_LEVELS = {
    "success": messages.success,
    "info": messages.info,
    "warning": messages.warning,
    "error": messages.error,
}


@scm_login_required
def container_list(request):
    team = request.default_team
    containers_qs = filter_containers(
        team=team,
        status=request.GET.get("status"),
        condition=request.GET.get("condition"),
        equipment_type=request.GET.get("equipment_type"),
        location_type=request.GET.get("location_type"),
        location_id=request.GET.get("location_id"),
        missing_location=request.GET.get("missing_location") == "1",
        search=request.GET.get("search"),
        sort=request.GET.get("sort", "newest"),
    )
    paginator = Paginator(containers_qs, CONTAINERS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))
    saved_filters = get_saved_filters(team, request.user, SavedFilter.ViewKey.CONTAINERS)
    from .choices import LocationType
    from .selectors import get_team_locations

    context = {
        "containers": page_obj,
        "page_obj": page_obj,
        "equipment_types": get_active_equipment_types(),
        "locations": get_team_locations(team),
        "location_types": LocationType.choices,
        "saved_filters": saved_filters,
        "view_key": SavedFilter.ViewKey.CONTAINERS,
        "team_slug": team.slug,
    }
    if request.htmx:
        return render(request, "scm/containers/partials/container_table.html", context)
    return render(request, "scm/containers/pages/container_list.html", context)


@scm_login_required
def container_detail(request, container_id):
    """The Container Workspace: overview, journey, activity and related objects.

    Kept on the `containers:detail` route and template name it has always had, so
    every existing link and redirect still resolves. All four sections are rendered
    in one response and switched client-side — see the template.
    """
    team = request.default_team
    container = get_object_or_404(Container, pk=container_id, team=team)
    workspace = get_container_workspace(team=team, container=container)
    return render(
        request,
        "scm/containers/pages/container_detail.html",
        {
            "container": container,
            "workspace": workspace,
            # Derived from what the workspace already loaded, plus one query for the
            # ETA history. Team-scoped throughout.
            "activity": get_container_activity(team=team, container=container, workspace=workspace),
            # The map and the journey summary read the same workspace, so the page
            # loads this container's tracking once.
            **get_container_map_context(team=team, container=container, workspace=workspace),
            "team_slug": team.slug,
        },
    )


# Creating a container lives in intake_views.py, next to paste and CSV import:
# all three share one parse/validate/create path.


@scm_login_required
def container_update(request, container_id):
    team = request.default_team
    container = get_object_or_404(Container, pk=container_id, team=team)
    if request.method == "POST":
        form = ContainerForm(request.POST, instance=container, team=team)
        if form.is_valid():
            container = update_container(container=container, user=request.user, data=form.get_container_data())
            if request.htmx:
                return render(
                    request,
                    "scm/containers/partials/container_row.html",
                    {"container": container, "team_slug": team.slug},
                )
            messages.success(request, _("Container updated."))
            return redirect("containers:detail", container_id=container_id)
        if request.htmx:
            return render(
                request,
                "scm/containers/partials/container_form.html",
                {
                    "form": form,
                    "modal_title": _("Edit Container"),
                    "form_action": request.path,
                    "team_slug": team.slug,
                },
            )
    else:
        form = ContainerForm(instance=container, team=team)

    context = {
        "form": form,
        "modal_title": _("Edit Container"),
        "form_action": request.path,
        "team_slug": team.slug,
    }
    return render(request, "scm/containers/partials/container_form.html", context)


@scm_login_required
def container_delete(request, container_id):
    team = request.default_team
    container = get_object_or_404(Container, pk=container_id, team=team)
    if request.method in ("POST", "DELETE"):
        delete_container(container=container, user=request.user)
        if request.htmx:
            return HttpResponse(status=200)
        messages.success(request, _("Container deleted."))
        return redirect("containers:list")
    return render(
        request,
        "scm/containers/pages/container_detail.html",
        {"container": container, "team_slug": team.slug},
    )


@scm_login_required
@require_POST
def container_refresh_tracking(request, container_id):
    """Fetch this container's tracking from its carrier now and report the result.

    The carrier call runs in the request so the person who pressed the button sees
    the real outcome. An HTMX request gets the tracking panel back with the result
    rendered inside it; anything else falls back to a message and a redirect.
    """
    team = request.default_team
    container = get_object_or_404(Container, pk=container_id, team=team)
    result = refresh_container_tracking(team=team, container=container)

    if request.htmx:
        workspace = get_container_workspace(team=team, container=container)
        return render(
            request,
            TRACKING_PANEL_TEMPLATE,
            {
                "container": container,
                "workspace": workspace,
                # The panel shows position, ETA and freshness through the shared
                # visibility components, so a refresh has to rebuild them too.
                **get_container_map_context(team=team, container=container, workspace=workspace),
                "refresh": result,
                "team_slug": team.slug,
            },
        )

    _MESSAGE_LEVELS[result.level](request, result.message)
    return redirect("containers:detail", container_id=container.pk)


@scm_login_required
def container_record_movement(request, container_id):
    """Record a gate in, gate out, receipt or transfer for this container.

    The view does no state logic at all: it validates the form, hands the values to
    ``record_container_movement`` and re-renders. Which movement wins, whether the
    current location changes, and what a gate-out leaves behind are decided in
    ``movements.py`` — a view that reimplemented any of that would be a second
    opinion about where containers are.

    Domain validation surfaces as a form error rather than a 500: "a gate-out needs
    an origin" is something the person filling the form can fix.
    """
    team = request.default_team
    container = get_object_or_404(Container, pk=container_id, team=team)
    requested_type = request.GET.get("type") or MovementType.GATE_IN

    if request.method == "POST":
        form = ContainerMovementForm(request.POST, team=team, container=container)
        if form.is_valid():
            try:
                record_container_movement(team=team, container=container, **form.movement_data())
            except ValidationError as error:
                form.add_error(None, error)
            else:
                if request.htmx:
                    # The whole page: a movement changes the header's physical state,
                    # the Overview panel and the Activity tab at once, and swapping
                    # one of them would leave the other two contradicting it.
                    response = HttpResponse(status=204)
                    response["HX-Refresh"] = "true"
                    return response
                messages.success(request, _("Movement recorded."))
                return redirect("containers:detail", container_id=container.pk)
    else:
        initial = {"occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M")}
        if (destination := _inbound_destination_id(team, container, requested_type)) is not None:
            initial["to_location"] = destination
        form = ContainerMovementForm(
            team=team,
            container=container,
            movement_type=requested_type,
            initial=initial,
        )

    return render(
        request,
        "scm/containers/partials/container_movement_form.html",
        {
            "form": form,
            "container": container,
            "modal_title": _("Record movement"),
            "form_action": request.path,
            "team_slug": team.slug,
        },
    )


def _inbound_destination_id(team, container, movement_type: str) -> int | None:
    """The canonical place this box is inbound to, for prefilling a receipt.

    Only for a receipt, and only a *default*: receiving is the movement whose
    destination is knowable in advance, because the shipment already says where the
    box was booked to. A gate-in can happen anywhere on the way, and offering the
    booked destination for one would put a guess in the field.

    Read through the arrival lifecycle rather than from ``destination_location``
    directly, so the field is prefilled with the same place the lifecycle will judge
    the resulting movement against. Falls back to nothing rather than to the
    container's current location — a box standing at the wrong depot should not have
    that depot suggested as where it is being received.
    """
    if movement_type != MovementType.RECEIVED:
        return None

    from apps.scm.shipments.models import ShipmentContainer
    from apps.scm.visibility.arrival_lifecycle import get_container_arrival_lifecycle

    workspace_shipment = (
        ShipmentContainer.objects.filter(container=container, shipment__team=team)
        .select_related("shipment", "shipment__destination_location")
        .order_by("-created_at")
        .first()
    )
    if workspace_shipment is None:
        return None
    lifecycle = get_container_arrival_lifecycle(team, container, workspace_shipment.shipment)
    return lifecycle.destination.pk if lifecycle.destination is not None else None


# ---------------------------------------------------------------------------
# Container discovery views
# ---------------------------------------------------------------------------


@scm_login_required
def planned_container_dashboard(request):
    """Dashboard showing planned containers by status."""
    team = request.default_team
    status_filter = request.GET.get("status")
    planned_containers = get_planned_containers(team=team, status=status_filter or None)
    counts = {
        "planned": PlannedContainer.objects.filter(team=team, status=PlannedContainerStatus.PLANNED).count(),
        "detected": PlannedContainer.objects.filter(team=team, status=PlannedContainerStatus.DETECTED).count(),
        "in_transit": PlannedContainer.objects.filter(team=team, status=PlannedContainerStatus.IN_TRANSIT).count(),
        "arrived": PlannedContainer.objects.filter(team=team, status=PlannedContainerStatus.ARRIVED).count(),
        "cancelled": PlannedContainer.objects.filter(team=team, status=PlannedContainerStatus.CANCELLED).count(),
    }
    context = {
        "planned_containers": planned_containers,
        "counts": counts,
        "status_filter": status_filter,
        "status_choices": PlannedContainerStatus.choices,
        "team_slug": team.slug,
    }
    return render(request, "scm/containers/pages/planned_container_dashboard.html", context)


@scm_login_required
def planned_container_add(request):
    """Add a container number to the planned pool."""
    team = request.default_team
    if request.method == "POST":
        form = PlannedContainerForm(request.POST)
        if form.is_valid():
            add_planned_container(
                team=team,
                container_number=form.cleaned_data["container_number"],
                carrier=form.cleaned_data.get("carrier", ""),
                notes=form.cleaned_data.get("notes", ""),
            )
            messages.success(request, _("Planned container added."))
            return redirect("containers:discovery_dashboard")
    else:
        form = PlannedContainerForm()
    context = {"form": form, "team_slug": team.slug}
    return render(request, "scm/containers/partials/planned_container_form.html", context)


@scm_login_required
def planned_container_cancel(request, pk):
    """Cancel a planned container."""
    team = request.default_team
    planned = get_object_or_404(PlannedContainer, pk=pk, team=team)
    if request.method == "POST":
        cancel_planned_container(planned=planned)
        messages.success(request, _("Planned container cancelled."))
    return redirect("containers:discovery_dashboard")


@scm_login_required
def planned_container_run_discovery(request):
    """Manually trigger a discovery run for all planned containers."""
    team = request.default_team
    if request.method == "POST":
        summary = run_discovery_for_team(team=team)
        messages.success(
            request,
            _(f"Discovery complete: checked {summary['checked']}, detected {summary['detected']}."),
        )
    return redirect("containers:discovery_dashboard")


# ---------------------------------------------------------------------------
# Container location views
# ---------------------------------------------------------------------------

# Where a location form sends the operator when it was opened from somewhere other
# than the location list. A flag mapped to a named route rather than a URL taken
# from the request, so this can only ever mean one of the pages listed here and
# there is nothing to redirect openly to.
_LOCATION_FORM_RETURNS = {"location_quality": "visibility:location_quality"}


def _location_form_return(request):
    """An HTMX redirect back to the page a location form was opened from, or None.

    204 with ``HX-Redirect``: the form lives in a modal, so there is no element to
    swap on success — the operator came from a queue and belongs back on a freshly
    built one, with the row they just fixed gone from it.
    """
    route = _LOCATION_FORM_RETURNS.get(request.POST.get("return_to", ""))
    if route is None:
        return None
    response = HttpResponse(status=204)
    response["HX-Redirect"] = reverse(route)
    return response


@scm_login_required
def container_location_list(request):
    """The canonical locations, and how healthy the master data behind them is.

    The list used to carry its own table of unmatched carrier place names. LOC-5
    replaced that with the Location Data Quality queue, which aggregates the same
    evidence and can act on it, so what is left here is a pointer carrying the
    counts — two queries, and no second table of the same rows to keep in step.
    """
    from apps.scm.visibility.location_quality import get_location_quality_summary

    team = request.default_team
    return render(
        request,
        "scm/containers/pages/container_location_list.html",
        {
            "locations": get_team_locations_with_counts(team),
            "quality": get_location_quality_summary(team),
            "team_slug": team.slug,
        },
    )


@scm_login_required
def container_location_detail(request, location_id):
    """The Location Workspace: what is here, what is expected, what has moved.

    Four sections in one response, switched client-side, in the same shell as the
    Container and Purchase Order workspaces. The Inventory tab paginates and filters
    server-side over the shared container list, so an HTMX request returns just that
    table — a depot with six hundred boxes must not put all of them in the DOM.

    Inactive locations still open. Deactivating a location does not move the
    containers standing on it, so refusing to show them would hide real inventory.
    """
    team = request.default_team
    location = get_object_or_404(ContainerLocation, pk=location_id, team=team)
    workspace = get_location_workspace(team=team, location=location)

    inventory = get_location_inventory(
        team=team,
        location=location,
        status=request.GET.get("status"),
        equipment_type=request.GET.get("equipment_type"),
        search=request.GET.get("search"),
        sort=request.GET.get("sort"),
    )
    paginator = Paginator(inventory, CONTAINERS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "location": location,
        "workspace": workspace,
        "inventory": page_obj,
        "page_obj": page_obj,
        "overview_movements": get_location_overview_movements(workspace),
        # The map card, when this location has coordinates. Built here rather than
        # in the template so the page does not learn a Mapbox detail — the same
        # arrangement the container and shipment workspaces use.
        **get_location_map_context(location),
        # The workspace already loaded the aliases; the panel is included with them
        # rather than the page asking a second time.
        "alias_source_suggestions": get_alias_source_suggestions(team),
        "equipment_types": get_active_equipment_types(),
        "status_choices": ContainerStatus.choices,
        "inventory_filters": {
            "status": request.GET.get("status", ""),
            "equipment_type": request.GET.get("equipment_type", ""),
            "search": request.GET.get("search", ""),
        },
        "team_slug": team.slug,
    }
    if request.htmx:
        return render(request, LOCATION_INVENTORY_TEMPLATE, context)
    return render(request, "scm/containers/pages/container_location_detail.html", context)


def _location_form_context(request, form, *, team, title: str) -> dict:
    """Context for the location modal, carrying the page it was opened from.

    ``return_to`` arrives as a query parameter on the way in and travels back as a
    hidden field, so the round trip through a validation error does not lose it. It
    is filtered against the known flags here rather than trusted, and it decides the
    modal's HTMX target too: a form opened from the location list swaps that list's
    table, and one opened from anywhere else has no table to swap and stays in the
    modal until the view redirects it.
    """
    requested = request.POST.get("return_to") or request.GET.get("return_to", "")
    return_to = requested if requested in _LOCATION_FORM_RETURNS else ""
    return {
        "form": form,
        "modal_title": title,
        "form_action": request.path,
        "return_to": return_to,
        "form_target": "#modal-container" if return_to else "#location-table",
        "form_swap": "innerHTML" if return_to else "outerHTML",
        "team_slug": team.slug,
    }


@scm_login_required
def container_location_create(request):
    """Create a new container location.

    Also the escape hatch from the Location Data Quality queue, for a place a
    provider keeps naming that genuinely is not in the network yet. It is the
    ordinary form, filled in by a person: nothing creates a location from carrier
    text, which is how a location list ends up with four spellings of Göteborg.
    """
    team = request.default_team
    if request.method == "POST":
        form = ContainerLocationForm(request.POST, team=team)
        if form.is_valid():
            create_location(team=team, data=form.cleaned_data)
            if (back := _location_form_return(request)) is not None:
                messages.success(request, _("Location created."))
                return back
            if request.htmx:
                locations = get_team_locations_with_counts(team)
                return render(
                    request,
                    "scm/containers/partials/container_location_table.html",
                    {"locations": locations, "team_slug": team.slug},
                )
            messages.success(request, _("Location created."))
            return redirect("containers:location_list")
    else:
        form = ContainerLocationForm(team=team)
    return render(
        request,
        "scm/containers/partials/container_location_form.html",
        _location_form_context(request, form, team=team, title=_("New Location")),
    )


@scm_login_required
def container_location_update(request, location_id):
    """Edit an existing container location.

    Reached from the location list, from the Location Workspace, and from the
    Location Data Quality queue — where the edit being made is almost always the
    coordinates. It is the same form and the same ``update_location`` in every case,
    so validation stays where LOC-4 put it, on ``ContainerLocation.clean``.
    """
    team = request.default_team
    location = get_object_or_404(ContainerLocation, pk=location_id, team=team)
    if request.method == "POST":
        form = ContainerLocationForm(request.POST, instance=location, team=team)
        if form.is_valid():
            update_location(location=location, data=form.cleaned_data)
            if (back := _location_form_return(request)) is not None:
                messages.success(request, _("Location updated."))
                return back
            if request.htmx:
                locations = get_team_locations_with_counts(team)
                return render(
                    request,
                    "scm/containers/partials/container_location_table.html",
                    {"locations": locations, "team_slug": team.slug},
                )
            messages.success(request, _("Location updated."))
            return redirect("containers:location_list")
    else:
        form = ContainerLocationForm(instance=location, team=team)
    return render(
        request,
        "scm/containers/partials/container_location_form.html",
        _location_form_context(request, form, team=team, title=_("Edit Location")),
    )


def _alias_panel(request, team, location):
    """Re-render the aliases panel for one location.

    Every alias write returns this, so add and remove both leave the page showing
    the current set without a reload — and there is one template deciding what an
    alias list looks like.
    """
    return render(
        request,
        "scm/containers/partials/location_aliases.html",
        {
            "location": location,
            "aliases": get_location_aliases(team=team, location=location),
            "alias_source_suggestions": get_alias_source_suggestions(team),
            "team_slug": team.slug,
        },
    )


@scm_login_required
def container_location_alias_create(request, location_id):
    """Record an external name for a location.

    A duplicate is a validation error, not a second row: the unique constraints on
    (team, source, code) and (team, source, name) are what let the resolver treat an
    alias hit as certain, so the form has to surface a collision rather than the
    database raising one.
    """
    team = request.default_team
    location = get_object_or_404(ContainerLocation, pk=location_id, team=team)

    if request.method == "POST":
        form = LocationAliasForm(request.POST)
        if form.is_valid():
            try:
                create_location_alias(team=team, location=location, data=form.alias_data())
            except ValidationError as error:
                form.add_error(None, error)
            else:
                if request.htmx:
                    return _alias_panel(request, team, location)
                messages.success(request, _("Alias added."))
                return redirect("containers:location_detail", location_id=location.pk)
    else:
        form = LocationAliasForm()

    context = {
        "form": form,
        "location": location,
        "modal_title": _("Add alias"),
        "form_action": request.path,
        "alias_source_suggestions": get_alias_source_suggestions(team),
        "team_slug": team.slug,
    }
    return render(request, "scm/containers/partials/location_alias_form.html", context)


@scm_login_required
@require_POST
def container_location_alias_delete(request, location_id, alias_id):
    """Remove an external name from a location."""
    team = request.default_team
    location = get_object_or_404(ContainerLocation, pk=location_id, team=team)
    alias = get_object_or_404(LocationAlias, pk=alias_id, team=team, location=location)
    delete_location_alias(team=team, alias=alias)

    if request.htmx:
        return _alias_panel(request, team, location)
    messages.success(request, _("Alias removed."))
    return redirect("containers:location_detail", location_id=location.pk)


@scm_login_required
def location_evidence_alias(request):
    """Record a row of the Location Data Quality queue as an alias. LOC-5's action.

    The queue is a read model in the visibility app, which writes nothing; this is
    where its one action lands, beside the rest of the location master data it
    changes. What it does is exactly what an operator does by hand on a location's
    own page — ``create_location_alias``, the same service, the same constraints —
    with the evidence carried in rather than retyped.

    Three refusals, all of them the point of the feature:

    *No location is created.* The operator chooses one that exists.

    *No evidence is rewritten.* The historic ``TrackingEvent`` rows keep saying
    exactly what the carrier said and what the resolver concluded at the time. The
    alias changes what the *next* resolution will decide — see the module docstring
    of :mod:`apps.scm.containers.location_resolver` — and the queue says so.

    *No string is accepted on trust.* ``has_unmatched_evidence`` establishes that
    this team really has unresolved or ambiguous evidence under this provider and
    name. Without it the endpoint would be a general-purpose "record an alias for
    anything" URL wearing a queue's clothes, and one team could file an alias
    against evidence it cannot see.
    """
    from apps.scm.visibility.location_quality import has_unmatched_evidence

    team = request.default_team
    queue_url = reverse("visibility:location_quality")

    if request.method == "POST":
        form = LocationEvidenceAliasForm(request.POST, team=team)
        if form.is_valid():
            source = form.cleaned_data["source"]
            external_name = form.cleaned_data["external_name"]
            if not has_unmatched_evidence(team, source=source, raw_name=external_name):
                form.add_error(None, _("No unmatched evidence from that source names this place."))
            else:
                try:
                    create_location_alias(
                        team=team,
                        location=form.cleaned_data["location"],
                        data=form.alias_data(),
                    )
                except ValidationError as error:
                    # A duplicate is the common one: the unique constraints on
                    # (team, source, code) and (team, source, name) are what let the
                    # resolver treat an alias hit as certain, so a collision has to
                    # be shown here rather than surface as a database fault.
                    form.add_error(None, error)
                else:
                    messages.success(
                        request,
                        _("“%(name)s” from %(source)s now resolves to %(location)s.")
                        % {
                            "name": external_name,
                            "source": source,
                            "location": form.cleaned_data["location"].full_name,
                        },
                    )
                    if request.htmx:
                        response = HttpResponse(status=204)
                        response["HX-Redirect"] = queue_url
                        return response
                    return redirect(queue_url)
    else:
        form = LocationEvidenceAliasForm(
            team=team,
            initial={
                "source": request.GET.get("source", ""),
                "external_name": request.GET.get("name", ""),
            },
        )

    return render(
        request,
        "scm/containers/partials/location_evidence_alias_form.html",
        {
            "form": form,
            "form_action": request.path,
            "raw_name": form.data.get("external_name") or request.GET.get("name", ""),
            "raw_unlocode": request.POST.get("unlocode") or request.GET.get("unlocode", ""),
            "provider_code": form.data.get("source") or request.GET.get("source", ""),
            "team_slug": team.slug,
        },
    )


@scm_login_required
def container_location_deactivate(request, location_id):
    """Toggle active state of a container location.

    Shared by the list, which swaps its own table back in over HTMX, and by the
    Location Workspace, which posts a plain form. ``return_to=detail`` is a flag
    rather than a URL so it can only ever mean this location's own page.
    """
    team = request.default_team
    location = get_object_or_404(ContainerLocation, pk=location_id, team=team)
    if request.method == "POST":
        location.is_active = not location.is_active
        location.save(update_fields=["is_active"])
        if request.htmx:
            locations = get_team_locations_with_counts(team)
            return render(
                request,
                "scm/containers/partials/container_location_table.html",
                {"locations": locations, "team_slug": team.slug},
            )
        messages.success(request, _("Location updated."))
        if request.POST.get("return_to") == "detail":
            return redirect("containers:location_detail", location_id=location.pk)
    return redirect("containers:location_list")
