"""Procurement views — request handling and response rendering only.

Business logic belongs in services.py; queries belong in selectors.py.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.scm.decorators import scm_login_required

from .activity import get_purchase_order_activity
from .forms import PurchaseOrderForm
from .selectors import (
    get_purchase_order_line_summaries,
    get_purchase_order_workspace,
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
    """Permanently delete a purchase order and its related records.

    Business Central orders are refused by the service, which owns the rule. This
    view only chooses how to say so: 403 for HTMX, because htmx does not swap on a
    4xx and the row stays where it is, and the message-plus-redirect the rest of
    SCM uses for a refused operation otherwise.
    """
    team = request.default_team
    purchase_order = get_object_or_404(get_team_purchase_orders(team=team), pk=purchase_order_id)
    try:
        delete_purchase_order(purchase_order=purchase_order)
    except PermissionDenied:
        denial = _("This purchase order is managed by Business Central and cannot be deleted here.")
        if request.htmx:
            return HttpResponseForbidden(denial)
        messages.error(request, denial)
        return redirect("procurement:purchase_order_list")
    if request.htmx:
        # The row targets itself with hx-swap="outerHTML", so an empty body removes it.
        return HttpResponse(status=200)
    messages.success(request, _("Purchase order deleted."))
    return redirect("procurement:purchase_order_list")


@scm_login_required
def purchase_order_detail(request, purchase_order_id: int):
    """The Purchase Order Workspace: overview, containers, deliveries and activity.

    Kept on the `procurement:purchase_order_detail` route and template name it has
    always had, so every existing link, redirect and bookmark still resolves. All
    four sections are rendered in one response and switched client-side — see the
    template.
    """
    team = request.default_team
    purchase_order = get_object_or_404(
        get_team_purchase_orders(team=team),
        pk=purchase_order_id,
    )
    workspace = get_purchase_order_workspace(team=team, purchase_order=purchase_order)
    context = {
        "purchase_order": purchase_order,
        "workspace": workspace,
        "line_summaries": get_purchase_order_line_summaries(workspace),
        # Derived entirely from what the workspace already loaded — no extra query.
        "activity": get_purchase_order_activity(workspace),
        # The names the previous document-style page used, kept so existing
        # templates, tests and any partial that reads them keep working.
        "lines": workspace.lines,
        "events": workspace.events,
        "fulfillment": workspace.fulfillment,
        "total_order_amount": workspace.total_order_amount,
        "linked_containers": workspace.containers,
        "team_slug": team.slug,
    }
    return render(request, "scm/procurement/pages/purchase_order_detail.html", context)
