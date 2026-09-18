"""Procurement views — request handling and response rendering only.

Business logic belongs in services.py and manual.py; queries belong in selectors.py.
The views that link order lines to physical containers live in link_views.py.
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
from .forms import PurchaseOrderForm, PurchaseOrderLineForm
from .manual import (
    create_purchase_order_line,
    delete_purchase_order_line,
    update_purchase_order,
    update_purchase_order_line,
)
from .models import PurchaseOrderLine
from .selectors import (
    get_purchase_order_line_summaries,
    get_purchase_order_workspace,
    get_team_purchase_orders,
)
from .services import calculate_purchase_order_fulfillment, create_purchase_order, delete_purchase_order

PURCHASE_ORDERS_PER_PAGE = 50

LINE_FORM_TEMPLATE = "scm/procurement/partials/purchase_order_line_form.html"

# What SCM says when a writer reaches a record Business Central owns. One string, so
# the header, the line actions and the delete route cannot disagree about the reason.
BC_DENIAL = _("This purchase order is managed by Business Central and cannot be edited here.")


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
    return render(
        request,
        "scm/procurement/pages/purchase_order_create.html",
        {"form": form, "form_title": _("New purchase order"), "team_slug": team.slug},
    )


@scm_login_required
def purchase_order_update(request, purchase_order_id: int):
    """Edit an SCM-owned purchase order header.

    A Business Central order is turned away before the form is even built, not only
    on save: rendering an editable form for a record SCM may not write invites
    somebody to type into it and be refused at the end. ``manual.update_purchase_order``
    owns the rule and refuses it again on the way through, which is what covers the
    admin and the shell.

    The same full-page form as create, because the fields are the same ones and a
    second layout for them would be a second thing to keep in step.
    """
    team = request.default_team
    purchase_order = get_object_or_404(get_team_purchase_orders(team=team), pk=purchase_order_id)
    if purchase_order.is_business_central:
        return deny_business_central(request, purchase_order_id)

    form = PurchaseOrderForm(request.POST or None, instance=purchase_order)
    if request.method == "POST" and form.is_valid():
        update_purchase_order(purchase_order=purchase_order, **form.cleaned_data)
        messages.success(request, _("Purchase order updated."))
        return redirect("procurement:purchase_order_detail", purchase_order_id=purchase_order_id)
    return render(
        request,
        "scm/procurement/pages/purchase_order_create.html",
        {
            "form": form,
            "form_title": _("Edit purchase order"),
            "purchase_order": purchase_order,
            "team_slug": team.slug,
        },
    )


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


# ---------------------------------------------------------------------------
# Hand-entered order lines
#
# Business Central owns its own lines, so all three refuse a BC order. The rule is
# enforced in manual.py — these views only choose how to say so: 403 for HTMX,
# because htmx does not swap on a 4xx and the modal stays put with the reason in it,
# and the message-plus-redirect the rest of SCM uses otherwise.
# ---------------------------------------------------------------------------


def _line_form_response(request, *, form, purchase_order, line=None):
    return render(
        request,
        LINE_FORM_TEMPLATE,
        {
            "form": form,
            "purchase_order": purchase_order,
            "line": line,
            "modal_title": _("Edit order line") if line is not None else _("Add order line"),
            "form_action": request.path,
            "team_slug": request.default_team.slug,
        },
    )


def deny_business_central(request, purchase_order_id: int):
    """How SCM says no to a writer who reached a record Business Central owns.

    Public because ``link_views`` says it the same way. The refusal itself belongs to
    the service layer — ``manual.refuse_business_central`` — and this is only its
    presentation: one denial sentence and one response shape, so the header, the line
    actions, the container links and the delete route cannot phrase it differently.
    """
    if request.htmx:
        return HttpResponseForbidden(BC_DENIAL)
    messages.error(request, BC_DENIAL)
    return redirect("procurement:purchase_order_detail", purchase_order_id=purchase_order_id)


def _line_saved(request, purchase_order_id: int, message):
    """Reload the workspace: a line changes the order's quantities and its gaps at once."""
    if request.htmx:
        response = HttpResponse(status=204)
        response["HX-Refresh"] = "true"
        return response
    messages.success(request, message)
    return redirect("procurement:purchase_order_detail", purchase_order_id=purchase_order_id)


@scm_login_required
def purchase_order_line_create(request, purchase_order_id: int):
    team = request.default_team
    purchase_order = get_object_or_404(get_team_purchase_orders(team=team), pk=purchase_order_id)
    if purchase_order.is_business_central:
        return deny_business_central(request, purchase_order_id)

    if request.method == "POST":
        form = PurchaseOrderLineForm(request.POST)
        if form.is_valid():
            create_purchase_order_line(team=team, purchase_order=purchase_order, **form.cleaned_data)
            return _line_saved(request, purchase_order_id, _("Order line added."))
    else:
        form = PurchaseOrderLineForm()
    return _line_form_response(request, form=form, purchase_order=purchase_order)


@scm_login_required
def purchase_order_line_update(request, line_id: int):
    team = request.default_team
    line = get_object_or_404(PurchaseOrderLine.objects.select_related("purchase_order"), pk=line_id, team=team)
    if line.purchase_order.is_business_central:
        return deny_business_central(request, line.purchase_order_id)

    if request.method == "POST":
        form = PurchaseOrderLineForm(request.POST, instance=line)
        if form.is_valid():
            update_purchase_order_line(line=line, **form.cleaned_data)
            return _line_saved(request, line.purchase_order_id, _("Order line updated."))
    else:
        form = PurchaseOrderLineForm(instance=line)
    return _line_form_response(request, form=form, purchase_order=line.purchase_order, line=line)


@scm_login_required
@require_http_methods(["POST", "DELETE"])
def purchase_order_line_delete(request, line_id: int):
    team = request.default_team
    line = get_object_or_404(PurchaseOrderLine.objects.select_related("purchase_order"), pk=line_id, team=team)
    purchase_order_id = line.purchase_order_id
    try:
        delete_purchase_order_line(line=line)
    except PermissionDenied:
        return deny_business_central(request, purchase_order_id)
    return _line_saved(request, purchase_order_id, _("Order line deleted."))
