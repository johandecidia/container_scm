"""Supplier delivery views — request handling and response rendering only.

Business logic belongs in services.py; queries belong in selectors.py.
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_login_required

from .forms import SupplierDeliveryForm
from .models import SupplierDelivery, SupplierDeliveryStatus
from .selectors import (
    get_delivery_lines_for_delivery,
    get_delivery_total_qty,
    get_linked_shipments_for_delivery,
    get_po_delivery_summary,
    get_supplier_deliveries_for_team,
    get_supplier_delivery_dashboard,
)
from .services import create_supplier_delivery, mark_supplier_delivery_received, update_supplier_delivery

DELIVERIES_PER_PAGE = 50


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


@scm_login_required
def supplier_delivery_dashboard(request):
    team = request.default_team
    dashboard = get_supplier_delivery_dashboard(team=team)
    context = {
        "dashboard": dashboard,
        "team_slug": team.slug,
    }
    return render(request, "scm/supplier_deliveries/pages/supplier_delivery_dashboard.html", context)
