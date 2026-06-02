# Container views — request handling, response rendering, form handling only.
# Business logic belongs in services.py; queries belong in selectors.py.
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _

from apps.scm.decorators import scm_login_required

from .discovery import (
    add_planned_container,
    cancel_planned_container,
    get_planned_containers,
    run_discovery_for_team,
)
from .forms import ContainerForm, PlannedContainerForm
from .models import Container, PlannedContainer, PlannedContainerStatus
from .selectors import filter_containers, get_active_equipment_types, get_container_workspace
from .services import create_container, delete_container, update_container

CONTAINERS_PER_PAGE = 25


@scm_login_required
def container_list(request):
    team = request.default_team
    containers_qs = filter_containers(
        team=team,
        status=request.GET.get("status"),
        condition=request.GET.get("condition"),
        equipment_type=request.GET.get("equipment_type"),
        search=request.GET.get("search"),
        sort=request.GET.get("sort", "newest"),
    )
    paginator = Paginator(containers_qs, CONTAINERS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))
    context = {
        "containers": page_obj,
        "page_obj": page_obj,
        "equipment_types": get_active_equipment_types(),
        "team_slug": team.slug,
    }
    if request.htmx:
        return render(request, "scm/containers/partials/container_table.html", context)
    return render(request, "scm/containers/pages/container_list.html", context)


@scm_login_required
def container_detail(request, container_id):
    team = request.default_team
    container = get_object_or_404(Container, pk=container_id, team=team)
    workspace = get_container_workspace(team=team, container=container)
    return render(
        request,
        "scm/containers/pages/container_detail.html",
        {"container": container, "workspace": workspace, "team_slug": team.slug},
    )


@scm_login_required
def container_create(request):
    team = request.default_team
    if request.method == "POST":
        form = ContainerForm(request.POST)
        if form.is_valid():
            create_container(team=team, user=request.user, data=form.get_container_data())
            if request.htmx:
                containers_qs = filter_containers(team=team)
                paginator = Paginator(containers_qs, CONTAINERS_PER_PAGE)
                page_obj = paginator.get_page(1)
                return render(
                    request,
                    "scm/containers/partials/container_table.html",
                    {
                        "containers": page_obj,
                        "page_obj": page_obj,
                        "equipment_types": get_active_equipment_types(),
                        "team_slug": team.slug,
                    },
                )
            messages.success(request, _("Container created."))
            return redirect("containers:list")
        if request.htmx:
            return render(
                request,
                "scm/containers/partials/container_form.html",
                {
                    "form": form,
                    "modal_title": _("New Container"),
                    "form_action": request.path,
                    "team_slug": team.slug,
                },
            )
    else:
        form = ContainerForm()

    context = {
        "form": form,
        "modal_title": _("New Container"),
        "form_action": request.path,
        "team_slug": team.slug,
    }
    return render(request, "scm/containers/partials/container_form.html", context)


@scm_login_required
def container_update(request, container_id):
    team = request.default_team
    container = get_object_or_404(Container, pk=container_id, team=team)
    if request.method == "POST":
        form = ContainerForm(request.POST, instance=container)
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
        form = ContainerForm(instance=container)

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
