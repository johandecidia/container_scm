"""Receive containers: paste a gate-in report, preview it, confirm it.

Request handling only. Parsing, matching, idempotency and the tracking policy are
:mod:`apps.scm.containers.receive`; the physical movement is
:func:`~apps.scm.containers.movements.record_container_movement`.

**Confirm re-reads everything.** The preview's pasted text and destination travel
back through the browser as plain fields, and Confirm validates and previews them
again before writing. Nothing the preview decided is trusted.

**Any team member may receive**, as any member may record a movement by hand. The
automatic stop that may follow is the team's own policy, which only an
administrator can switch on.
"""

from django.shortcuts import render
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_login_required

from .forms import BulkReceiveForm
from .receive import bulk_receive, preview_bulk_receive

PAGE_TEMPLATE = "scm/containers/pages/container_receive.html"
PREVIEW_TEMPLATE = "scm/containers/partials/container_receive_preview.html"
RESULT_TEMPLATE = "scm/containers/partials/container_receive_result.html"


def _render(request, *, form, partial: str | None = None, **context):
    """The partial for HTMX, the whole page — with the partial inside it — otherwise."""
    context = {"form": form, "partial": partial, "team_slug": request.default_team.slug, **context}
    if request.htmx and partial is not None:
        return render(request, partial, context)
    return render(request, PAGE_TEMPLATE, context)


@scm_login_required
def container_receive(request):
    """The paste form."""
    return _render(request, form=BulkReceiveForm(team=request.default_team))


@scm_login_required
@require_POST
def container_receive_preview(request):
    """What confirming this paste would do. Writes nothing."""
    team = request.default_team
    form = BulkReceiveForm(request.POST, team=team)
    if not form.is_valid():
        return _render(request, form=form, partial=PREVIEW_TEMPLATE, form_invalid=True)
    preview = preview_bulk_receive(team=team, location=form.cleaned_data["location"], text=form.cleaned_data["text"])
    return _render(request, form=form, partial=PREVIEW_TEMPLATE, preview=preview)


@scm_login_required
@require_POST
def container_receive_confirm(request):
    """Receive every ready row, then apply the team's stop-tracking policy."""
    team = request.default_team
    form = BulkReceiveForm(request.POST, team=team)
    if not form.is_valid():
        return _render(request, form=form, partial=PREVIEW_TEMPLATE, form_invalid=True)
    result = bulk_receive(
        team=team,
        location=form.cleaned_data["location"],
        text=form.cleaned_data["text"],
        actor=request.user,
    )
    return _render(request, form=form, partial=RESULT_TEMPLATE, result=result)
