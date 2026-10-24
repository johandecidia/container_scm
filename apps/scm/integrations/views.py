# Integration views — request handling, response rendering, form handling only.
#
# Settings → Integrations: Team Administrators create, edit, test and (de)activate
# the team's Business Central integration. Every lookup is scoped to
# ``request.default_team``. Credentials are written by the configuration service
# (encrypted by the credential service) and never rendered back to the browser.
#
# The connection test runs in the request, as the carrier test in Settings → Tracking
# does: the administrator pressing the button is waiting for the answer, and it is a
# handful of read-only calls. It never imports or changes Business Central data.
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_team_admin_required

from .business_systems.business_central.configuration import (
    save_business_central_integration,
    test_business_central_connection,
)
from .business_systems.business_central.diagnostics import format_diagnostic
from .forms import BusinessCentralIntegrationForm
from .selectors import (
    get_integration_monitoring_context,
    get_team_business_central_integrations,
    is_sync_in_progress,
)
from .services import activate_integration, deactivate_integration
from .tasks import sync_business_central_purchase_orders_task

LIST_PAGE_TEMPLATE = "scm/integrations/pages/integration_list.html"
PANEL_TEMPLATE = "scm/integrations/partials/integrations_panel.html"
FORM_TEMPLATE = "scm/integrations/partials/integration_form.html"


def _panel_context(request, *, notice: str = "", notice_level: str = "info", close_modal: bool = False) -> dict:
    team = request.default_team
    rows = [get_integration_monitoring_context(i) for i in get_team_business_central_integrations(team)]
    return {
        "rows": rows,
        # One Business Central integration per team (unique on team + provider_code).
        "can_add": not rows,
        "team_slug": team.slug,
        "notice": notice,
        "notice_level": notice_level,
        "close_modal": close_modal,
    }


def _respond(request, notice: str, notice_level: str, *, redirect_to: str = "integrations:list", **redirect_kwargs):
    """Swap the integrations panel back for HTMX; message and redirect otherwise."""
    if request.htmx:
        return render(request, PANEL_TEMPLATE, _panel_context(request, notice=notice, notice_level=notice_level))
    getattr(messages, notice_level)(request, notice)
    return redirect(redirect_to, **redirect_kwargs)


@scm_team_admin_required
def integration_list(request):
    """List the team's Business Central integrations with health/sync summaries."""
    return render(request, LIST_PAGE_TEMPLATE, _panel_context(request))


@scm_team_admin_required
def integration_detail(request, pk: int):
    """Monitoring detail for one Business Central integration (team-scoped)."""
    team = request.default_team
    integration = get_object_or_404(get_team_business_central_integrations(team), pk=pk)
    context = get_integration_monitoring_context(integration)
    context["team_slug"] = team.slug
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
def integration_create(request):
    """Modal form to add the team's Business Central integration."""
    return _integration_form(request, integration=None)


@scm_team_admin_required
def integration_update(request, pk: int):
    """Modal form to edit the team's Business Central integration (blank secret keeps the stored one)."""
    integration = get_object_or_404(get_team_business_central_integrations(request.default_team), pk=pk)
    return _integration_form(request, integration=integration)


def _integration_form(request, *, integration):
    team = request.default_team
    if request.method == "POST":
        form = BusinessCentralIntegrationForm(team, request.POST, integration=integration)
        if form.is_valid():
            saved = save_business_central_integration(
                team,
                name=form.cleaned_data["name"],
                config=form.connection_config(),
                is_active=form.cleaned_data["is_active"],
                credentials=form.changed_credentials(),
                integration=integration,
            )
            notice = _("{name} saved. Test the connection to confirm the credentials work.").format(name=saved.name)
            if not request.htmx:
                messages.success(request, notice)
                return redirect("integrations:list")
            # Replace the panel and empty the modal container in one response — no page reload.
            response = render(
                request,
                PANEL_TEMPLATE,
                _panel_context(request, notice=notice, notice_level="success", close_modal=True),
            )
            response["HX-Retarget"] = "#integrations-panel"
            response["HX-Reswap"] = "outerHTML"
            return response
    else:
        form = BusinessCentralIntegrationForm(team, integration=integration)

    return render(request, FORM_TEMPLATE, {"form": form, "integration": integration, "form_action": request.path})


@scm_team_admin_required
@require_POST
def integration_test_connection(request, pk: int):
    """Test the connection now (read-only) and report the result."""
    integration = get_object_or_404(get_team_business_central_integrations(request.default_team), pk=pk)
    result = test_business_central_connection(integration)
    if result.get("success"):
        notice, level = _("Connected to Business Central: {name}.").format(name=integration.name), "success"
    else:
        # Sanitised at the source: typed client errors never carry tokens, secrets or response bodies.
        notice, level = _("Connection test failed — {reason}").format(reason=result.get("message")), "error"
        # Only Team Administrators reach this view; the diagnostic is built from allowlisted fields.
        if details := format_diagnostic(result.get("diagnostic") or {}):
            notice = f"{notice} {details}"
    if request.POST.get("next") == "detail":
        return _respond(request, notice, level, redirect_to="integrations:detail", pk=integration.pk)
    return _respond(request, notice, level)


@scm_team_admin_required
@require_POST
def integration_toggle(request, pk: int):
    """Activate or deactivate the integration. Credentials, config and sync history are kept."""
    integration = get_object_or_404(get_team_business_central_integrations(request.default_team), pk=pk)
    if integration.is_active:
        deactivate_integration(integration)
        notice = _("{name} deactivated.").format(name=integration.name)
    else:
        activate_integration(integration)
        notice = _("{name} activated.").format(name=integration.name)
    return _respond(request, notice, "info")


# Deletion is not supported: deactivate instead, which keeps the sync history and watermarks.


@scm_team_admin_required
def integration_delete(request, *args, **kwargs):
    return HttpResponse(status=501)
