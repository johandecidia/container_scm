# Container condition views — the Settings page for the team's grading vocabulary.
#
# Request handling and rendering only, the same arrangement as location_views.py:
# queries come from selectors.py, and the four views here are add, rename, retire and
# the list they all swap back into.
#
# There is no delete. A condition containers are graded with is protected by the FK,
# and `is_active = False` is the honest way to stop offering one — the boxes already
# carrying it keep saying what they were graded as. See
# `apps.scm.containers.models.ContainerCondition`.
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_login_required

from .forms import ContainerConditionForm
from .models import ContainerCondition
from .selectors import get_conditions_with_usage

CONDITION_TABLE_TEMPLATE = "scm/containers/partials/container_condition_table.html"
CONDITION_FORM_TEMPLATE = "scm/containers/partials/container_condition_form.html"


def _table(request, team):
    """Re-render the conditions table. Every write here returns this."""
    return render(
        request,
        CONDITION_TABLE_TEMPLATE,
        {"conditions": get_conditions_with_usage(team), "team_slug": team.slug},
    )


@scm_login_required
def container_condition_list(request):
    """The team's container conditions: what they are called, and what uses them."""
    team = request.default_team
    return render(
        request,
        "scm/containers/pages/container_condition_list.html",
        {"conditions": get_conditions_with_usage(team), "team_slug": team.slug},
    )


@scm_login_required
def container_condition_create(request):
    """Add a condition to this team's list."""
    team = request.default_team
    if request.method == "POST":
        form = ContainerConditionForm(request.POST, team=team)
        if form.is_valid():
            condition = form.save(commit=False)
            condition.team = team
            condition.save()
            if request.htmx:
                return _table(request, team)
            messages.success(request, _("Condition added."))
            return redirect("containers:condition_list")
    else:
        form = ContainerConditionForm(team=team)
    return render(
        request,
        CONDITION_FORM_TEMPLATE,
        {"form": form, "modal_title": _("New Condition"), "form_action": request.path, "team_slug": team.slug},
    )


@scm_login_required
def container_condition_update(request, condition_id):
    """Rename, reorder or reactivate one condition.

    Renaming is the ordinary case and changes nothing structural: the containers
    pointing at this row keep pointing at it, and every one of them starts reading
    the new wording. That is the whole reason conditions moved out of code.
    """
    team = request.default_team
    # Fetched with the usage count, which the form uses to say what retiring this
    # would actually affect.
    condition = get_object_or_404(get_conditions_with_usage(team), pk=condition_id)
    if request.method == "POST":
        form = ContainerConditionForm(request.POST, instance=condition, team=team)
        if form.is_valid():
            form.save()
            if request.htmx:
                return _table(request, team)
            messages.success(request, _("Condition updated."))
            return redirect("containers:condition_list")
    else:
        form = ContainerConditionForm(instance=condition, team=team)
    return render(
        request,
        CONDITION_FORM_TEMPLATE,
        {
            "form": form,
            "condition": condition,
            "modal_title": _("Edit Condition"),
            "form_action": request.path,
            "team_slug": team.slug,
        },
    )


@scm_login_required
@require_POST
def container_condition_deactivate(request, condition_id):
    """Retire a condition, or bring a retired one back.

    A toggle rather than two routes, the same as locations: the button is in the row
    and its meaning is whatever the row currently is.
    """
    team = request.default_team
    condition = get_object_or_404(ContainerCondition, pk=condition_id, team=team)
    condition.is_active = not condition.is_active
    condition.save(update_fields=["is_active", "updated_at"])
    if request.htmx:
        return _table(request, team)
    messages.success(request, _("Condition updated."))
    return redirect("containers:condition_list")
