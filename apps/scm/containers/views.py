# Container views — request handling, response rendering, form handling only.
# Business logic belongs in services.py; queries belong in selectors.py.
#
# The canonical location views — the Locations list, the Location Workspace, the
# location and alias forms, and LOC-5's evidence action — live in location_views.py.
# They were split out unchanged when this file passed 750 lines; the routes and
# templates are the ones they always had.
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.analytics.models import SavedFilter
from apps.scm.analytics.selectors import get_saved_filters
from apps.scm.decorators import scm_login_required
from apps.scm.tracking.manual_refresh import refresh_container_tracking
from apps.scm.visibility.context import get_container_map_context

from .activity import get_container_activity
from .choices import MovementType
from .discovery import (
    add_planned_container,
    cancel_planned_container,
    get_planned_containers,
    run_discovery_for_team,
)
from .forms import ContainerForm, ContainerMovementForm, PlannedContainerForm
from .models import Container, PlannedContainer, PlannedContainerStatus
from .movements import record_container_movement
from .selectors import (
    filter_containers,
    get_active_equipment_types,
    get_container_workspace,
)
from .services import delete_container, update_container

CONTAINERS_PER_PAGE = 25

# The tracking panel is its own HTMX component: the detail page includes it, and
# a refresh re-renders exactly this and nothing else.
TRACKING_PANEL_TEMPLATE = "scm/containers/partials/container_tracking_panel.html"

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
                # The whole page, for the same reason a movement reloads it: this
                # modal is opened from both the list and the workspace, and an edit
                # changes the header, the Overview panel and the row at once.
                response = HttpResponse(status=204)
                response["HX-Refresh"] = "true"
                return response
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
