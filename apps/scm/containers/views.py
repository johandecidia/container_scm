# Container views — request handling, response rendering, form handling only.
# Business logic belongs in services.py; queries belong in selectors.py.
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _

from apps.scm.decorators import scm_login_required

from .forms import ContainerForm
from .models import Container
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
