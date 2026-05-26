# Container views — request handling, response rendering, form handling only.
# Business logic belongs in services.py; queries belong in selectors.py.
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _

from apps.teams.decorators import login_and_team_required

from .forms import ContainerForm
from .models import Container
from .selectors import filter_team_containers, get_container_by_id
from .services import create_container, delete_container, update_container


@login_and_team_required
def container_list(request, team_slug):
    containers = filter_team_containers(team=request.team, query_params=request.GET)
    context = {"containers": containers, "team_slug": team_slug}
    if request.htmx:
        return render(request, "scm/containers/partials/container_table.html", context)
    return render(request, "scm/containers/pages/container_list.html", context)


@login_and_team_required
def container_detail(request, team_slug, container_id):
    container = get_object_or_404(Container, pk=container_id, team=request.team)
    return render(request, "scm/containers/pages/container_detail.html", {
        "container": container,
        "team_slug": team_slug,
    })


@login_and_team_required
def container_create(request, team_slug):
    if request.method == "POST":
        form = ContainerForm(request.POST)
        if form.is_valid():
            container = create_container(team=request.team, **form.cleaned_data)
            if request.htmx:
                containers = filter_team_containers(team=request.team, query_params=request.GET)
                return render(request, "scm/containers/partials/container_table.html", {
                    "containers": containers,
                    "team_slug": team_slug,
                })
            messages.success(request, _("Container created."))
            return redirect("containers:list", team_slug=team_slug)
        if request.htmx:
            return render(request, "scm/containers/partials/container_form.html", {
                "form": form,
                "modal_title": _("New Container"),
                "form_action": request.path,
                "team_slug": team_slug,
            })
    else:
        form = ContainerForm()

    context = {
        "form": form,
        "modal_title": _("New Container"),
        "form_action": request.path,
        "team_slug": team_slug,
    }
    return render(request, "scm/containers/partials/container_form.html", context)


@login_and_team_required
def container_update(request, team_slug, container_id):
    container = get_object_or_404(Container, pk=container_id, team=request.team)
    if request.method == "POST":
        form = ContainerForm(request.POST, instance=container)
        if form.is_valid():
            container = update_container(container, **form.cleaned_data)
            if request.htmx:
                return render(request, "scm/containers/partials/container_row.html", {
                    "container": container,
                    "team_slug": team_slug,
                })
            messages.success(request, _("Container updated."))
            return redirect("containers:detail", team_slug=team_slug, container_id=container_id)
        if request.htmx:
            return render(request, "scm/containers/partials/container_form.html", {
                "form": form,
                "modal_title": _("Edit Container"),
                "form_action": request.path,
                "team_slug": team_slug,
            })
    else:
        form = ContainerForm(instance=container)

    context = {
        "form": form,
        "modal_title": _("Edit Container"),
        "form_action": request.path,
        "team_slug": team_slug,
    }
    return render(request, "scm/containers/partials/container_form.html", context)


@login_and_team_required
def container_delete(request, team_slug, container_id):
    container = get_object_or_404(Container, pk=container_id, team=request.team)
    if request.method in ("POST", "DELETE"):
        delete_container(container)
        if request.htmx:
            return HttpResponse(status=200)
        messages.success(request, _("Container deleted."))
        return redirect("containers:list", team_slug=team_slug)
    return render(request, "scm/containers/pages/container_detail.html", {
        "container": container,
        "team_slug": team_slug,
    })
