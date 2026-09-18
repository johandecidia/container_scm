"""Views that link purchase order lines to physical containers.

Request handling only: every write goes to
:mod:`apps.scm.procurement.container_links`, which owns the rules and will be
called by the Business Central import next. A view that decided for itself whether
a box already had a procurement origin would be a second opinion about where
containers came from.

All four respond to a successful write with ``HX-Refresh``, as the container
workspace's own modals do. A link changes the line's containers, the Containers tab
and the Completeness panel at once, and swapping one of them would leave the other
two describing the order as it was.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.scm.decorators import scm_login_required

from .container_links import (
    link_acquired_container,
    remove_container_load,
    set_container_load,
    unlink_acquired_container,
)
from .forms import AcquiredContainerForm, ContainerLoadForm
from .models import ContainerAcquisition, ContainerLoad, PurchaseOrderLine

LINK_FORM_TEMPLATE = "scm/procurement/partials/purchase_order_link_form.html"


def _refresh_or_redirect(request, purchase_order_id: int, message):
    """Reload the purchase order workspace, however the request arrived."""
    if request.htmx:
        response = HttpResponse(status=204)
        response["HX-Refresh"] = "true"
        return response
    messages.success(request, message)
    return redirect("procurement:purchase_order_detail", purchase_order_id=purchase_order_id)


def _get_line(team, line_id: int) -> PurchaseOrderLine:
    return get_object_or_404(
        PurchaseOrderLine.objects.select_related("purchase_order"),
        pk=line_id,
        team=team,
    )


def _render_form(request, *, line, form, modal_title, submit_label):
    return render(
        request,
        LINK_FORM_TEMPLATE,
        {
            "form": form,
            "line": line,
            "purchase_order": line.purchase_order,
            "modal_title": modal_title,
            "submit_label": submit_label,
            "form_action": request.path,
            "team_slug": request.default_team.slug,
        },
    )


@scm_login_required
def line_link_acquired_container(request, line_id: int):
    """Record that a container was acquired through this order line.

    Domain refusals — the box already came from somewhere else — surface as a form
    error rather than a 500: that is something the person filling the form can act
    on, by unlinking it where it is.
    """
    team = request.default_team
    line = _get_line(team, line_id)
    title = _("Link acquired container")
    submit = _("Link container")

    if request.method == "POST":
        form = AcquiredContainerForm(request.POST, team=team)
        if form.is_valid():
            try:
                link_acquired_container(
                    team=team,
                    purchase_order_line=line,
                    container=form.cleaned_data["container"],
                )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                return _refresh_or_redirect(request, line.purchase_order_id, _("Container linked."))
    else:
        form = AcquiredContainerForm(team=team)

    return _render_form(request, line=line, form=form, modal_title=title, submit_label=submit)


@scm_login_required
@require_http_methods(["POST", "DELETE"])
def acquisition_unlink(request, acquisition_id: int):
    """Drop an acquisition link. The container itself is untouched."""
    team = request.default_team
    acquisition = get_object_or_404(
        ContainerAcquisition.objects.select_related("purchase_order_line"),
        pk=acquisition_id,
        team=team,
    )
    purchase_order_id = acquisition.purchase_order_line.purchase_order_id
    unlink_acquired_container(team=team, acquisition=acquisition)
    return _refresh_or_redirect(request, purchase_order_id, _("Container unlinked."))


@scm_login_required
def line_add_container_load(request, line_id: int):
    """Record that this order line's goods travel in a container, with an optional quantity.

    The service upserts on the (line, container) pair, so submitting the same
    container again corrects its quantity instead of adding a second row.
    """
    team = request.default_team
    line = _get_line(team, line_id)
    title = _("Add to container / load")
    submit = _("Save load")

    if request.method == "POST":
        form = ContainerLoadForm(request.POST, team=team)
        if form.is_valid():
            try:
                set_container_load(
                    team=team,
                    purchase_order_line=line,
                    container=form.cleaned_data["container"],
                    quantity=form.cleaned_data.get("quantity"),
                )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                return _refresh_or_redirect(request, line.purchase_order_id, _("Container load saved."))
    else:
        form = ContainerLoadForm(team=team)

    return _render_form(request, line=line, form=form, modal_title=title, submit_label=submit)


@scm_login_required
@require_http_methods(["POST", "DELETE"])
def container_load_remove(request, load_id: int):
    """Drop a load link. The container itself is untouched."""
    team = request.default_team
    load = get_object_or_404(
        ContainerLoad.objects.select_related("purchase_order_line"),
        pk=load_id,
        team=team,
    )
    purchase_order_id = load.purchase_order_line.purchase_order_id
    remove_container_load(team=team, load=load)
    return _refresh_or_redirect(request, purchase_order_id, _("Container load removed."))
