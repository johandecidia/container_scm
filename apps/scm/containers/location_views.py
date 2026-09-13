# Container location views — request handling, response rendering, form handling only.
#
# Split out of views.py, which had grown past 750 lines covering containers, planned
# containers and locations at once. Nothing here changed in the move: same views, same
# routes, same templates. What it buys is that the location master data — the
# workspace, the form, the aliases and LOC-5's evidence action — reads as one file,
# beside the read models and services it calls.
#
# Business logic belongs in services.py; queries belong in selectors.py.
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_login_required
from apps.scm.visibility.context import get_location_map_context

from .choices import ContainerStatus
from .forms import ContainerLocationForm, LocationAliasForm, LocationEvidenceAliasForm
from .models import ContainerLocation, LocationAlias
from .selectors import (
    get_active_equipment_types,
    get_alias_source_suggestions,
    get_location_aliases,
    get_location_inventory,
    get_location_overview_movements,
    get_location_workspace,
    get_team_locations_with_counts,
)
from .services import create_location, create_location_alias, delete_location_alias, update_location
from .views import CONTAINERS_PER_PAGE

# The location workspace's inventory table is its own HTMX component: filtering and
# paging it replaces the table without re-rendering the workspace around it.
LOCATION_INVENTORY_TEMPLATE = "scm/containers/partials/location_inventory.html"


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
    location = get_object_or_404(
        # The parent is rendered in the header and the hierarchy panel; joining it
        # here costs nothing and saves the workspace a query.
        ContainerLocation.objects.select_related("parent_location"),
        pk=location_id,
        team=team,
    )
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


def _location_form_context(request, form, *, team, title: str, location=None) -> dict:
    """Context for the location modal, carrying the page it was opened from.

    ``return_to`` arrives as a query parameter on the way in and travels back as a
    hidden field, so the round trip through a validation error does not lose it. It
    is filtered against the known flags here rather than trusted, and it decides the
    modal's HTMX target too: a form opened from the location list swaps that list's
    table, and one opened from anywhere else has no table to swap and stays in the
    modal until the view redirects it.

    ``hierarchy_impact`` is what recording a parent here would probably settle, and
    only for an existing location — a place being created has no evidence behind it
    yet. Read-only, and read at most once per form: it is an estimate to inform the
    choice, not a step in making it, and nothing is re-resolved by opening the modal.
    """
    from apps.scm.visibility.location_quality import get_hierarchy_impact

    requested = request.POST.get("return_to") or request.GET.get("return_to", "")
    return_to = requested if requested in _LOCATION_FORM_RETURNS else ""
    return {
        "form": form,
        "modal_title": title,
        "form_action": request.path,
        "return_to": return_to,
        "form_target": "#modal-container" if return_to else "#location-table",
        "form_swap": "innerHTML" if return_to else "outerHTML",
        "hierarchy_impact": None if location is None else get_hierarchy_impact(team, location),
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
    Location Data Quality queue — where the edit is usually the coordinates or, since
    LOC-6, the parent. It is the same form and the same ``update_location`` in every
    case, so validation stays on ``ContainerLocation.clean``, which is what makes a
    cycle impossible however the parent was set.
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
        _location_form_context(request, form, team=team, title=_("Edit Location"), location=location),
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
