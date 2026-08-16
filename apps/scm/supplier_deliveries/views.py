"""Supplier delivery views — request handling and response rendering only.

Business logic belongs in services.py; queries belong in selectors.py.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.containers.models import Container
from apps.scm.decorators import scm_login_required
from apps.scm.procurement.models import PurchaseOrder

from .forms import LinkContainersForm, SupplierDeliveryForm
from .models import SupplierDelivery, SupplierDeliveryStatus
from .selectors import (
    get_delivery_lines_for_delivery,
    get_delivery_total_qty,
    get_linked_shipments_for_delivery,
    get_po_delivery_summary,
    get_supplier_deliveries_for_team,
    get_supplier_delivery_dashboard,
)
from .services import (
    create_supplier_delivery,
    link_containers_to_delivery,
    mark_supplier_delivery_received,
    update_supplier_delivery,
)

DELIVERIES_PER_PAGE = 50

LINK_FORM_TEMPLATE = "scm/supplier_deliveries/partials/link_containers_form.html"
LINK_RESULT_TEMPLATE = "scm/supplier_deliveries/partials/link_containers_result.html"


@scm_login_required
def supplier_delivery_list(request):
    team = request.default_team
    deliveries_qs = get_supplier_deliveries_for_team(team=team)
    paginator = Paginator(deliveries_qs, DELIVERIES_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))
    context = {
        "deliveries": page_obj,
        "page_obj": page_obj,
        "team_slug": team.slug,
    }
    return render(request, "scm/supplier_deliveries/pages/supplier_delivery_list.html", context)


@scm_login_required
def supplier_delivery_detail(request, delivery_id: int):
    team = request.default_team
    delivery = get_object_or_404(SupplierDelivery, team=team, pk=delivery_id)
    lines = get_delivery_lines_for_delivery(team=team, delivery=delivery)
    summary = get_po_delivery_summary(team=team, purchase_order=delivery.purchase_order)
    linked_shipments = get_linked_shipments_for_delivery(team=team, delivery=delivery)
    delivery_qty = get_delivery_total_qty(delivery=delivery)
    context = {
        "delivery": delivery,
        "lines": lines,
        "summary": summary,
        "linked_shipments": linked_shipments,
        "delivery_qty": delivery_qty,
        "team_slug": team.slug,
    }
    return render(request, "scm/supplier_deliveries/pages/supplier_delivery_detail.html", context)


@scm_login_required
def supplier_delivery_create(request):
    team = request.default_team
    if request.method == "POST":
        form = SupplierDeliveryForm(request.POST, team=team)
        if form.is_valid():
            data = form.get_delivery_data()
            create_supplier_delivery(
                team=team,
                purchase_order=data["purchase_order"],
                delivery_reference=data["delivery_reference"],
                supplier=data.get("supplier", ""),
                status=data["status"],
                planned_ship_date=data.get("planned_ship_date"),
                planned_arrival_date=data.get("planned_arrival_date"),
                notes=data.get("notes", ""),
            )
            messages.success(request, _("Supplier delivery created."))
            return redirect("supplier_deliveries:list")
    else:
        form = SupplierDeliveryForm(team=team)

    context = {
        "form": form,
        "form_title": _("New Supplier Delivery"),
        "team_slug": team.slug,
    }
    return render(request, "scm/supplier_deliveries/pages/supplier_delivery_form.html", context)


@scm_login_required
def supplier_delivery_update(request, delivery_id: int):
    team = request.default_team
    delivery = get_object_or_404(SupplierDelivery, team=team, pk=delivery_id)
    if request.method == "POST":
        form = SupplierDeliveryForm(request.POST, team=team, instance=delivery)
        if form.is_valid():
            update_supplier_delivery(delivery=delivery, data=form.get_delivery_data())
            messages.success(request, _("Supplier delivery updated."))
            return redirect("supplier_deliveries:detail", delivery_id=delivery_id)
    else:
        form = SupplierDeliveryForm(team=team, instance=delivery)

    context = {
        "form": form,
        "delivery": delivery,
        "form_title": _("Edit Supplier Delivery"),
        "team_slug": team.slug,
    }
    return render(request, "scm/supplier_deliveries/pages/supplier_delivery_form.html", context)


@require_POST
@scm_login_required
def supplier_delivery_mark_received(request, delivery_id: int):
    """HTMX inline action — mark a supplier delivery as received.

    Returns a badge partial reflecting the new status.
    """
    team = request.default_team
    delivery = get_object_or_404(SupplierDelivery, pk=delivery_id, team=team)
    if delivery.status not in (SupplierDeliveryStatus.RECEIVED, SupplierDeliveryStatus.CANCELLED):
        mark_supplier_delivery_received(delivery)
    return render(
        request,
        "scm/supplier_deliveries/partials/delivery_status_badge.html",
        {"delivery": delivery},
    )


# ---------------------------------------------------------------------------
# Linking containers to a purchase order
# ---------------------------------------------------------------------------


def render_link_containers_step(request, *, team, purchase_order, containers, form=None):
    """Render the step that books a batch of containers onto a purchase order.

    Called from the container intake modal once the containers exist, and again by
    :func:`supplier_delivery_link_containers` when the submitted form is invalid, so
    both paths show the same thing.

    A PO with no order lines cannot take a delivery line at all — that is reported
    here rather than offering a form that can only fail.
    """
    context = {
        "purchase_order": purchase_order,
        "containers": containers,
        "team_slug": team.slug,
    }
    if not purchase_order.lines.exists():
        context["blocked_reason"] = _(
            "This purchase order has no order lines, so containers cannot be booked against it yet."
        )
        return render(request, LINK_FORM_TEMPLATE, context)

    context["form"] = form or LinkContainersForm(team=team, purchase_order=purchase_order, containers=containers)
    return render(request, LINK_FORM_TEMPLATE, context)


@require_POST
@scm_login_required
def supplier_delivery_link_containers(request):
    """Book the submitted containers onto the purchase order, creating a delivery if asked.

    HTMX only: replaces the intake modal body with the result, or with the form
    again when something did not validate.
    """
    team = request.default_team
    purchase_order = get_object_or_404(PurchaseOrder, team=team, pk=request.POST.get("purchase_order"))
    containers = list(
        Container.objects.filter(team=team, pk__in=request.POST.getlist("containers")).order_by(
            "owner_code", "serial_number"
        )
    )
    if not containers:
        return render_link_containers_step(request, team=team, purchase_order=purchase_order, containers=[])

    form = LinkContainersForm(request.POST, team=team, purchase_order=purchase_order, containers=containers)
    if not form.is_valid():
        return render_link_containers_step(
            request, team=team, purchase_order=purchase_order, containers=containers, form=form
        )

    created_delivery = form.creates_delivery
    try:
        # One transaction over both writes: a quantity that overflows its PO line
        # must not leave a new, empty delivery behind on the purchase order.
        with transaction.atomic():
            delivery = form.get_delivery() or create_supplier_delivery(
                team=team,
                purchase_order=purchase_order,
                delivery_reference=form.cleaned_data["delivery_reference"],
                supplier=purchase_order.supplier_name,
            )
            lines = link_containers_to_delivery(team=team, delivery=delivery, assignments=form.get_assignments())
    except ValidationError as exc:
        form.add_error(None, exc)
        return render_link_containers_step(
            request, team=team, purchase_order=purchase_order, containers=containers, form=form
        )

    context = {
        "purchase_order": purchase_order,
        "delivery": delivery,
        "created_delivery": created_delivery,
        "lines": lines,
        "skipped_count": len(containers) - len(lines),
        "team_slug": team.slug,
    }
    return render(request, LINK_RESULT_TEMPLATE, context)


@scm_login_required
def supplier_delivery_dashboard(request):
    team = request.default_team
    dashboard = get_supplier_delivery_dashboard(team=team)
    context = {
        "dashboard": dashboard,
        "team_slug": team.slug,
    }
    return render(request, "scm/supplier_deliveries/pages/supplier_delivery_dashboard.html", context)
