"""Views for adding containers: one at a time, pasted in bulk, or from a CSV.

All three render the same modal shell with the same tabs and all three write
through :mod:`apps.scm.containers.intake`, so the only difference between them is
where the numbers come from.
"""

import json

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_login_required

from .forms import ContainerCsvImportForm, ContainerPasteForm, QuickContainerForm
from .intake import (
    bulk_create_containers,
    create_or_get_container,
    entries_from_csv,
    entries_from_text,
    parse_and_validate_container_number,
    preview_containers,
)
from .selectors import filter_containers

CONTAINERS_PER_PAGE = 25

SINGLE_TEMPLATE = "scm/containers/partials/container_intake_single.html"
PASTE_TEMPLATE = "scm/containers/partials/container_intake_paste.html"
CSV_TEMPLATE = "scm/containers/partials/container_intake_csv.html"
PREVIEW_TEMPLATE = "scm/containers/partials/container_intake_preview.html"
RESULT_TEMPLATE = "scm/containers/partials/container_intake_result.html"
MODAL_TEMPLATE = "scm/containers/partials/container_intake_modal.html"


def _modal(request, team, *, tab: str, body_template: str, **extra):
    """Render the intake modal with one tab active.

    Opening the modal and switching tabs are GETs and get the whole dialog, so the
    tab strip always matches what is below it. Submitting is a POST and gets the
    body alone, which is what the forms target — an invalid submit therefore
    replaces the form, never the page behind the modal.
    """
    context = {"tab": tab, "body_template": body_template, "team_slug": team.slug, **extra}
    if request.method == "POST":
        return render(request, body_template, context)
    return render(request, MODAL_TEMPLATE, context)


def _refreshed_table_context(team) -> dict:
    """Context for re-rendering the container table out of band after a write."""
    from .selectors import get_active_equipment_types

    paginator = Paginator(filter_containers(team=team), CONTAINERS_PER_PAGE)
    page_obj = paginator.get_page(1)
    return {
        "containers": page_obj,
        "page_obj": page_obj,
        "equipment_types": get_active_equipment_types(),
        "team_slug": team.slug,
        "table_oob": True,
    }


@scm_login_required
def container_create(request):
    """Add one container from its number alone."""
    team = request.default_team
    if request.method == "POST":
        form = QuickContainerForm(request.POST)
        if form.is_valid():
            try:
                container, created = create_or_get_container(
                    team=team,
                    user=request.user,
                    number=form.cleaned_data["container_number"],
                    carrier=form.cleaned_data.get("carrier", ""),
                )
            except ValidationError as exc:
                form.add_error("container_number", exc)
            else:
                context = {
                    "container": container,
                    "created": created,
                    "tab": "single",
                    **_refreshed_table_context(team),
                }
                return render(request, "scm/containers/partials/container_intake_created.html", context)
        return _modal(request, team, tab="single", body_template=SINGLE_TEMPLATE, form=form)

    return _modal(request, team, tab="single", body_template=SINGLE_TEMPLATE, form=QuickContainerForm())


@scm_login_required
@require_POST
def container_number_check(request):
    """Answer "is this a container number?" while it is being typed.

    Uses the same parse and check-digit validation as the create itself, so the
    feedback can never disagree with what happens on submit.
    """
    raw = request.POST.get("container_number", "")
    context: dict = {"raw": raw.strip()}
    if context["raw"]:
        try:
            context["parts"] = parse_and_validate_container_number(raw)
        except ValidationError as exc:
            context["error"] = " ".join(exc.messages)
    return render(request, "scm/containers/partials/container_number_feedback.html", context)


@scm_login_required
def container_import_paste(request):
    """Paste a list of container numbers and preview what would be imported."""
    team = request.default_team
    if request.method == "POST":
        form = ContainerPasteForm(request.POST)
        if form.is_valid():
            entries = entries_from_text(form.cleaned_data["numbers"], form.cleaned_data.get("carrier", ""))
            return _preview_response(request, team, entries=entries, tab="paste")
        return _modal(request, team, tab="paste", body_template=PASTE_TEMPLATE, form=form)

    return _modal(request, team, tab="paste", body_template=PASTE_TEMPLATE, form=ContainerPasteForm())


@scm_login_required
def container_import_csv(request):
    """Upload a small CSV of container numbers and preview what would be imported."""
    team = request.default_team
    if request.method == "POST":
        form = ContainerCsvImportForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                entries = entries_from_csv(form.cleaned_data["file"])
            except ValidationError as exc:
                form.add_error("file", exc)
            else:
                if not entries:
                    form.add_error("file", _("No container numbers were found in the file."))
                else:
                    return _preview_response(request, team, entries=entries, tab="csv")
        return _modal(request, team, tab="csv", body_template=CSV_TEMPLATE, form=form)

    return _modal(request, team, tab="csv", body_template=CSV_TEMPLATE, form=ContainerCsvImportForm())


@scm_login_required
@require_POST
def container_import_confirm(request):
    """Create the valid, new containers from a previewed list."""
    team = request.default_team
    entries = _entries_from_payload(request.POST.get("entries", ""))
    tab = request.POST.get("tab") or "paste"
    if not entries:
        return _modal(
            request,
            team,
            tab=tab,
            body_template=PASTE_TEMPLATE if tab != "csv" else CSV_TEMPLATE,
            form=ContainerPasteForm() if tab != "csv" else ContainerCsvImportForm(),
            intake_error=_("That import could not be read. Paste the numbers again."),
        )

    result = bulk_create_containers(team=team, user=request.user, entries=entries)
    context = {"result": result, "tab": tab, **_refreshed_table_context(team)}
    return render(request, RESULT_TEMPLATE, context)


def _preview_response(request, team, *, entries: list[tuple[str, str]], tab: str):
    preview = preview_containers(team=team, entries=entries)
    return _modal(
        request,
        team,
        tab=tab,
        body_template=PREVIEW_TEMPLATE,
        preview=preview,
        payload=json.dumps([[row.number, row.carrier] for row in preview.rows]),
    )


def _entries_from_payload(payload: str) -> list[tuple[str, str]]:
    """Read back the previewed list. Re-validated downstream, so shape is all that matters."""
    try:
        raw = json.loads(payload or "[]")
    except TypeError, ValueError:
        return []
    if not isinstance(raw, list):
        return []
    entries = []
    for item in raw:
        if isinstance(item, list | tuple) and len(item) == 2 and all(isinstance(part, str) for part in item):
            entries.append((item[0], item[1]))
    return entries
