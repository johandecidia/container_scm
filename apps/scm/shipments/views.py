# Shipment views — request handling, response rendering, form handling only.
# Business logic belongs in services.py; queries belong in selectors.py.
from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _

from apps.scm.decorators import scm_login_required

from .forms import ShipmentContainerForm, ShipmentForm, ShipmentStatusForm
from .models import Shipment, ShipmentContainer
from .selectors import (
    filter_shipments,
    get_shipment_containers,
    get_shipment_detail_context,
    get_shipment_events,
    get_shipment_workspace,
)
from .services import (
    add_container_to_shipment,
    cancel_shipment,
    change_shipment_status,
    create_shipment,
    remove_container_from_shipment,
    update_shipment,
)

SHIPMENTS_PER_PAGE = 25


@scm_login_required
def shipment_list(request):
    team = request.default_team
    shipments_qs = filter_shipments(
        team=team,
        status=request.GET.get("status"),
        search=request.GET.get("search"),
        sort=request.GET.get("sort", "newest"),
    )
    paginator = Paginator(shipments_qs, SHIPMENTS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))
    context = {
        "shipments": page_obj,
        "page_obj": page_obj,
        "status_choices": Shipment.Status.choices,
        "team_slug": team.slug,
    }
    if request.htmx:
        return render(request, "scm/shipments/partials/shipment_table.html", context)
    return render(request, "scm/shipments/pages/shipment_list.html", context)


@scm_login_required
def shipment_detail(request, pk):
    team = request.default_team
    shipment = get_object_or_404(Shipment, pk=pk, team=team)
    detail = get_shipment_detail_context(team=team, shipment_id=pk)
    context = {
        **detail,
        # Keep workspace for partials that use it; also keep events alias for timeline partial
        "workspace": get_shipment_workspace(team=team, shipment=shipment),
        "events": detail["timeline_events"],
        "team_slug": team.slug,
    }
    return render(request, "scm/shipments/pages/shipment_detail.html", context)


@scm_login_required
def shipment_create(request):
    team = request.default_team
    if request.method == "POST":
        form = ShipmentForm(request.POST)
        if form.is_valid():
            shipment = create_shipment(team=team, user=request.user, data=form.cleaned_data)
            if request.htmx:
                shipments_qs = filter_shipments(team=team)
                paginator = Paginator(shipments_qs, SHIPMENTS_PER_PAGE)
                page_obj = paginator.get_page(1)
                return render(
                    request,
                    "scm/shipments/partials/shipment_table.html",
                    {
                        "shipments": page_obj,
                        "page_obj": page_obj,
                        "status_choices": Shipment.Status.choices,
                        "team_slug": team.slug,
                    },
                )
            messages.success(request, _("Shipment created."))
            return redirect("shipments:detail", pk=shipment.pk)
        if request.htmx:
            return render(
                request,
                "scm/shipments/partials/shipment_form.html",
                {"form": form, "modal_title": _("New Shipment"), "form_action": request.path, "team_slug": team.slug},
            )
    else:
        form = ShipmentForm()

    context = {
        "form": form,
        "modal_title": _("New Shipment"),
        "form_action": request.path,
        "team_slug": team.slug,
    }
    return render(request, "scm/shipments/partials/shipment_form.html", context)


@scm_login_required
def shipment_update(request, pk):
    team = request.default_team
    shipment = get_object_or_404(Shipment, pk=pk, team=team)
    if request.method == "POST":
        form = ShipmentForm(request.POST, instance=shipment)
        if form.is_valid():
            shipment = update_shipment(shipment=shipment, user=request.user, data=form.cleaned_data)
            if request.htmx:
                return render(
                    request,
                    "scm/shipments/partials/shipment_row.html",
                    {"shipment": shipment, "team_slug": team.slug},
                )
            messages.success(request, _("Shipment updated."))
            return redirect("shipments:detail", pk=pk)
        if request.htmx:
            return render(
                request,
                "scm/shipments/partials/shipment_form.html",
                {
                    "form": form,
                    "modal_title": _("Edit Shipment"),
                    "form_action": request.path,
                    "team_slug": team.slug,
                },
            )
    else:
        form = ShipmentForm(instance=shipment)

    context = {
        "form": form,
        "modal_title": _("Edit Shipment"),
        "form_action": request.path,
        "team_slug": team.slug,
    }
    return render(request, "scm/shipments/partials/shipment_form.html", context)


@scm_login_required
def shipment_cancel(request, pk):
    team = request.default_team
    shipment = get_object_or_404(Shipment, pk=pk, team=team)
    if request.method == "POST":
        cancel_shipment(shipment=shipment, user=request.user)
        if request.htmx:
            return render(
                request,
                "scm/shipments/partials/shipment_row.html",
                {"shipment": shipment, "team_slug": team.slug},
            )
        messages.success(request, _("Shipment cancelled."))
        return redirect("shipments:list")
    return render(
        request,
        "scm/shipments/pages/shipment_detail.html",
        {
            "shipment": shipment,
            "containers": get_shipment_containers(team=team, shipment=shipment),
            "events": get_shipment_events(team=team, shipment=shipment),
            "team_slug": team.slug,
        },
    )


@scm_login_required
def shipment_status_update(request, pk):
    team = request.default_team
    shipment = get_object_or_404(Shipment, pk=pk, team=team)
    if request.method == "POST":
        form = ShipmentStatusForm(request.POST)
        if form.is_valid():
            shipment = change_shipment_status(
                shipment=shipment, user=request.user, new_status=form.cleaned_data["status"]
            )
            if request.htmx:
                return render(
                    request,
                    "scm/shipments/partials/shipment_row.html",
                    {"shipment": shipment, "team_slug": team.slug},
                )
            messages.success(request, _("Status updated."))
            return redirect("shipments:detail", pk=pk)
        if request.htmx:
            return render(
                request,
                "scm/shipments/partials/shipment_status_form.html",
                {"form": form, "shipment": shipment, "team_slug": team.slug},
            )
    else:
        form = ShipmentStatusForm(initial={"status": shipment.status})

    context = {"form": form, "shipment": shipment, "team_slug": team.slug}
    return render(request, "scm/shipments/partials/shipment_status_form.html", context)


@scm_login_required
def shipment_container_add(request, pk):
    team = request.default_team
    shipment = get_object_or_404(Shipment, pk=pk, team=team)
    if request.method == "POST":
        form = ShipmentContainerForm(request.POST, team=team, shipment=shipment)
        if form.is_valid():
            container = form.cleaned_data["container"]
            add_container_to_shipment(
                team=team,
                shipment=shipment,
                container=container,
                user=request.user,
                data=form.get_container_data(),
            )
            if request.htmx:
                containers = get_shipment_containers(team=team, shipment=shipment)
                return render(
                    request,
                    "scm/shipments/partials/shipment_container_list.html",
                    {"shipment": shipment, "containers": containers, "team_slug": team.slug},
                )
            messages.success(request, _("Container added to shipment."))
            return redirect("shipments:detail", pk=pk)
        if request.htmx:
            return render(
                request,
                "scm/shipments/partials/shipment_container_form.html",
                {"form": form, "shipment": shipment, "team_slug": team.slug},
            )
    else:
        form = ShipmentContainerForm(team=team, shipment=shipment)

    context = {"form": form, "shipment": shipment, "team_slug": team.slug}
    return render(request, "scm/shipments/partials/shipment_container_form.html", context)


@scm_login_required
def shipment_container_remove(request, pk, sc_pk):
    team = request.default_team
    shipment = get_object_or_404(Shipment, pk=pk, team=team)
    sc = get_object_or_404(ShipmentContainer, pk=sc_pk, shipment=shipment)
    if request.method in ("POST", "DELETE"):
        remove_container_from_shipment(team=team, shipment=shipment, shipment_container=sc, user=request.user)
        if request.htmx:
            containers = get_shipment_containers(team=team, shipment=shipment)
            return render(
                request,
                "scm/shipments/partials/shipment_container_list.html",
                {"shipment": shipment, "containers": containers, "team_slug": team.slug},
            )
        messages.success(request, _("Container removed from shipment."))
        return redirect("shipments:detail", pk=pk)
    return render(
        request,
        "scm/shipments/pages/shipment_detail.html",
        {
            "shipment": shipment,
            "containers": get_shipment_containers(team=team, shipment=shipment),
            "events": get_shipment_events(team=team, shipment=shipment),
            "team_slug": team.slug,
        },
    )


@scm_login_required
def shipment_timeline_partial(request, pk):
    team = request.default_team
    shipment = get_object_or_404(Shipment, pk=pk, team=team)
    events = get_shipment_events(team=team, shipment=shipment)
    return render(
        request,
        "scm/shipments/partials/shipment_timeline.html",
        {"shipment": shipment, "events": events, "team_slug": team.slug},
    )
