# Container views — request handling, response rendering, form handling only.
# Business logic belongs in services.py; queries belong in selectors.py.
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.analytics.models import SavedFilter
from apps.scm.analytics.selectors import get_saved_filters
from apps.scm.decorators import scm_login_required
from apps.scm.tracking.manual_refresh import refresh_container_tracking
from apps.scm.visibility.context import get_container_map_context

from .activity import get_container_activity
from .choices import ContainerStatus
from .discovery import (
    add_planned_container,
    cancel_planned_container,
    get_planned_containers,
    run_discovery_for_team,
)
from .forms import ContainerForm, ContainerLocationForm, PlannedContainerForm
from .models import Container, ContainerLocation, PlannedContainer, PlannedContainerStatus
from .selectors import (
    filter_containers,
    get_active_equipment_types,
    get_container_workspace,
    get_location_inventory,
    get_location_overview_movements,
    get_location_workspace,
    get_team_locations_with_counts,
)
from .services import create_location, delete_container, update_container, update_location

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


@scm_login_required
def container_location_list(request):
    """List all container locations with container counts."""
    team = request.default_team
    locations = get_team_locations_with_counts(team)
    return render(
        request,
        "scm/containers/pages/container_location_list.html",
        {"locations": locations, "team_slug": team.slug},
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


@scm_login_required
def container_location_create(request):
    """Create a new container location."""
    team = request.default_team
    if request.method == "POST":
        form = ContainerLocationForm(request.POST)
        if form.is_valid():
            create_location(team=team, data=form.cleaned_data)
            if request.htmx:
                locations = get_team_locations_with_counts(team)
                return render(
                    request,
                    "scm/containers/partials/container_location_table.html",
                    {"locations": locations, "team_slug": team.slug},
                )
            messages.success(request, _("Location created."))
            return redirect("containers:location_list")
        if request.htmx:
            return render(
                request,
                "scm/containers/partials/container_location_form.html",
                {"form": form, "modal_title": _("New Location"), "form_action": request.path, "team_slug": team.slug},
            )
    else:
        form = ContainerLocationForm()
    return render(
        request,
        "scm/containers/partials/container_location_form.html",
        {"form": form, "modal_title": _("New Location"), "form_action": request.path, "team_slug": team.slug},
    )


@scm_login_required
def container_location_update(request, location_id):
    """Edit an existing container location."""
    team = request.default_team
    location = get_object_or_404(ContainerLocation, pk=location_id, team=team)
    if request.method == "POST":
        form = ContainerLocationForm(request.POST, instance=location)
        if form.is_valid():
            update_location(location=location, data=form.cleaned_data)
            if request.htmx:
                locations = get_team_locations_with_counts(team)
                return render(
                    request,
                    "scm/containers/partials/container_location_table.html",
                    {"locations": locations, "team_slug": team.slug},
                )
            messages.success(request, _("Location updated."))
            return redirect("containers:location_list")
        if request.htmx:
            return render(
                request,
                "scm/containers/partials/container_location_form.html",
                {"form": form, "modal_title": _("Edit Location"), "form_action": request.path, "team_slug": team.slug},
            )
    else:
        form = ContainerLocationForm(instance=location)
    return render(
        request,
        "scm/containers/partials/container_location_form.html",
        {"form": form, "modal_title": _("Edit Location"), "form_action": request.path, "team_slug": team.slug},
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
