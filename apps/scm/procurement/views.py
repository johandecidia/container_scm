"""Procurement views — request handling and response rendering only.

Business logic belongs in services.py; queries belong in selectors.py.
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.scm.decorators import scm_login_required

from .forms import PurchaseOrderForm
from .selectors import (
    get_purchase_order_events,
    get_purchase_order_lines,
    get_team_purchase_orders,
)
from .services import calculate_purchase_order_fulfillment, create_purchase_order, delete_purchase_order

PURCHASE_ORDERS_PER_PAGE = 50


@scm_login_required
def purchase_order_list(request):
    team = request.default_team
    po_qs = get_team_purchase_orders(team=team)
    paginator = Paginator(po_qs, PURCHASE_ORDERS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))
    po_rows = [(po, calculate_purchase_order_fulfillment(po)) for po in page_obj]
    context = {
        "po_rows": po_rows,
        "page_obj": page_obj,
        "team_slug": team.slug,
    }
    return render(request, "scm/procurement/pages/purchase_order_list.html", context)


@scm_login_required
def purchase_order_create(request):
    team = request.default_team
    form = PurchaseOrderForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        po = create_purchase_order(team=team, **form.cleaned_data)
        return redirect("procurement:purchase_order_detail", purchase_order_id=po.pk)
    return render(request, "scm/procurement/pages/purchase_order_create.html", {"form": form})


@scm_login_required
@require_http_methods(["POST", "DELETE"])
def purchase_order_delete(request, purchase_order_id: int):
    """Permanently delete a purchase order and its related records."""
    team = request.default_team
    purchase_order = get_object_or_404(get_team_purchase_orders(team=team), pk=purchase_order_id)
    delete_purchase_order(purchase_order=purchase_order)
    if request.htmx:
        # The row targets itself with hx-swap="outerHTML", so an empty body removes it.
        return HttpResponse(status=200)
    messages.success(request, _("Purchase order deleted."))
    return redirect("procurement:purchase_order_list")


@scm_login_required
def purchase_order_detail(request, purchase_order_id: int):
    team = request.default_team
    purchase_order = get_object_or_404(
        get_team_purchase_orders(team=team),
        pk=purchase_order_id,
    )
    from apps.scm.supplier_deliveries.selectors import get_containers_for_purchase_order

    lines = list(get_purchase_order_lines(purchase_order=purchase_order))
    events = get_purchase_order_events(purchase_order=purchase_order)
    fulfillment = calculate_purchase_order_fulfillment(purchase_order=purchase_order)
    amounts = [line.line_amount for line in lines if line.line_amount is not None]
    total_order_amount = sum(amounts) if amounts else None
    context = {
        "purchase_order": purchase_order,
        "lines": lines,
        "events": events,
        "fulfillment": fulfillment,
        "total_order_amount": total_order_amount,
        "linked_containers": get_containers_for_purchase_order(team=team, purchase_order=purchase_order),
        "team_slug": team.slug,
    }
    return render(request, "scm/procurement/pages/purchase_order_detail.html", context)
