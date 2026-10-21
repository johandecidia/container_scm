# Integration views — request handling, response rendering, form handling only.
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_login_required, scm_team_admin_required
from apps.teams.roles import is_admin

from .selectors import (
    get_integration_monitoring_context,
    get_team_business_central_integrations,
    is_sync_in_progress,
)
from .tasks import (
    sync_business_central_purchase_orders_task,
    test_business_central_connection_task,
)


@scm_login_required
def integration_list(request):
    """List the team's Business Central integrations with health/sync summaries."""
    team = request.default_team
    rows = [get_integration_monitoring_context(i) for i in get_team_business_central_integrations(team)]
    return render(request, "scm/integrations/pages/integration_list.html", {"rows": rows, "team_slug": team.slug})


@scm_login_required
def integration_detail(request, pk: int):
    """Monitoring detail for one Business Central integration (team-scoped)."""
    team = request.default_team
    integration = get_object_or_404(get_team_business_central_integrations(team), pk=pk)
    context = get_integration_monitoring_context(integration)
    context["team_slug"] = team.slug
    # Members may see integration health; only administrators can act on it.
    context["can_manage_integrations"] = is_admin(request.user, team)
    return render(request, "scm/integrations/pages/integration_detail.html", context)


@scm_team_admin_required
@require_POST
def integration_sync_now(request, pk: int):
    """Queue a manual purchase-order sync (team-scoped; no live call in-request)."""
    team = request.default_team
    integration = get_object_or_404(get_team_business_central_integrations(team), pk=pk)
    if is_sync_in_progress(integration):
        messages.warning(request, "A purchase order sync is already running for this integration.")
    else:
        sync_business_central_purchase_orders_task.delay(integration.id, "manual")
        messages.success(request, "Purchase order sync queued.")
    return redirect("integrations:detail", pk=integration.pk)


@scm_team_admin_required
@require_POST
def integration_test_connection(request, pk: int):
    """Queue a connection test (team-scoped; runs off the request cycle)."""
    team = request.default_team
    integration = get_object_or_404(get_team_business_central_integrations(team), pk=pk)
    test_business_central_connection_task.delay(integration.id)
    messages.success(request, "Connection test queued.")
    return redirect("integrations:detail", pk=integration.pk)


# Credential-bearing management (create/update/delete) is intentionally not exposed
# in this UI — credentials are set via the credential service, never through a form.


# Configuration stays administrator-only even while unimplemented, so wiring these
# up later cannot accidentally open them to every member.


@scm_team_admin_required
def integration_create(request, *args, **kwargs):
    return HttpResponse(status=501)


@scm_team_admin_required
def integration_update(request, *args, **kwargs):
    return HttpResponse(status=501)


@scm_team_admin_required
def integration_delete(request, *args, **kwargs):
    return HttpResponse(status=501)
