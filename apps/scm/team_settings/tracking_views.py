"""Settings → Tracking: the team's direct carrier integrations, and its tracking default.

Request handling and rendering only. The reads are in
:mod:`.tracking_selectors`, the writes are the integration app's existing services
— `connect_carrier_integration`, `activate_integration`, `deactivate_integration`,
`test_integration_connection` — and the credential encryption is the credential
service's. Nothing about credentials is re-implemented here and there is no second
credential model.

**The connection test runs in the request.** The person who pressed the button is
waiting to find out whether the key they just pasted works, and a queued task would
answer them with "queued". It is one small carrier call, already rate-limited and
already logged to `IntegrationRequestLog` like every other.
"""

import logging

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_team_admin_required
from apps.scm.integrations.carriers.registry import UnknownCarrierError
from apps.scm.integrations.services import (
    activate_integration,
    connect_carrier_integration,
    deactivate_integration,
    test_integration_connection,
)
from apps.scm.tracking.preferences import get_team_default_provider_name

from .forms import CarrierCredentialForm
from .tracking_selectors import get_carrier_settings_row, get_carrier_settings_rows

logger = logging.getLogger(__name__)

TRACKING_PAGE_TEMPLATE = "scm/team_settings/pages/tracking.html"
TRACKING_PANEL_TEMPLATE = "scm/team_settings/partials/tracking_panel.html"
CREDENTIAL_FORM_TEMPLATE = "scm/team_settings/partials/carrier_credential_form.html"


def _row_or_404(team, provider_code: str):
    row = get_carrier_settings_row(team, provider_code)
    if row is None:
        raise Http404(f"No connectable carrier '{provider_code}'.")
    return row


def _panel_context(request, *, notice: str = "", notice_level: str = "info") -> dict:
    team = request.default_team
    return {
        "team": team,
        "team_slug": team.slug,
        "carrier_rows": get_carrier_settings_rows(team),
        "default_provider_name": get_team_default_provider_name(team),
        "notice": notice,
        "notice_level": notice_level,
    }


def _leave_credential_modal(request):
    """Send the browser back to the tracking page after a credential form succeeded.

    204 with ``HX-Redirect`` rather than a panel swap, the same idiom the location
    forms use: the form lives in a modal, so a swap would have to replace an element
    the modal is not inside, leaving the modal itself open over the result. The
    reload rebuilds the panel and the modal goes with the old document.
    """
    url = reverse("team_settings:tracking")
    if request.htmx:
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


def _respond(request, *, notice: str = "", notice_level: str = "info", message=None, level=messages.success):
    """Swap the tracking panel back for HTMX; message and redirect otherwise."""
    if request.htmx:
        return render(
            request, TRACKING_PANEL_TEMPLATE, _panel_context(request, notice=notice, notice_level=notice_level)
        )
    if message is not None:
        level(request, message)
    return redirect("team_settings:tracking")


@scm_team_admin_required
def tracking(request):
    """The team's direct carrier integrations and its default tracking provider."""
    return render(request, TRACKING_PAGE_TEMPLATE, _panel_context(request))


@scm_team_admin_required
def carrier_credentials(request, provider_code: str):
    """Enter or replace one carrier's credentials.

    GET renders the form — with empty fields and a masked placeholder, because the
    stored value is never sent back to the browser. POST stores what was supplied,
    merged over what is already there by the credential service.
    """
    team = request.default_team
    row = _row_or_404(team, provider_code)

    if request.method == "POST":
        form = CarrierCredentialForm(row, request.POST)
        if form.is_valid():
            try:
                connect_carrier_integration(
                    team,
                    provider_code,
                    form.changed_credentials(),
                    test_connection_reference=form.test_reference(),
                )
            except (UnknownCarrierError, ValueError) as exc:
                # The message names the configuration problem, never a credential.
                logger.warning("Carrier %s could not be connected for team %s: %s", provider_code, team.pk, exc)
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    _("{carrier} credentials saved. Test the connection to confirm they work.").format(
                        carrier=row.name
                    ),
                )
                return _leave_credential_modal(request)
    else:
        form = CarrierCredentialForm(row)

    return render(
        request,
        CREDENTIAL_FORM_TEMPLATE,
        {
            "form": form,
            "row": row,
            "form_action": request.path,
            "team_slug": team.slug,
        },
    )


@scm_team_admin_required
@require_POST
def carrier_test_connection(request, provider_code: str):
    """Call the carrier now and report what it said.

    The result is recorded on the integration by the service, so the row's
    last-success and last-error lines are the same facts the scheduled sync writes.
    """
    team = request.default_team
    row = _row_or_404(team, provider_code)
    if row.integration is None:
        return _respond(
            request,
            notice=_("{carrier} is not connected yet.").format(carrier=row.name),
            notice_level="warning",
            message=_("{carrier} is not connected yet.").format(carrier=row.name),
            level=messages.warning,
        )

    result = test_integration_connection(row.integration)
    if result.get("success"):
        return _respond(
            request,
            notice=_("{carrier} responded successfully.").format(carrier=row.name),
            notice_level="success",
            message=_("{carrier} responded successfully.").format(carrier=row.name),
        )
    # The provider's own message. It is the reason the test failed and is what makes
    # the failure actionable; `test_integration_connection` already keeps secrets out
    # of it, and the credential service never puts one there.
    failed = _("{carrier} connection test failed: {reason}").format(
        carrier=row.name, reason=result.get("message") or _("no response")
    )
    return _respond(request, notice=failed, notice_level="error", message=failed, level=messages.error)


@scm_team_admin_required
@require_POST
def carrier_toggle(request, provider_code: str):
    """Switch a connected carrier integration on or off for this team.

    Deactivating is the honest way to stop routing to a carrier: the credentials and
    the history stay, containers already tracked through it keep their events, and
    nothing needs deleting to take it out of service.
    """
    team = request.default_team
    row = _row_or_404(team, provider_code)
    if row.integration is None:
        raise Http404(f"'{provider_code}' is not connected for this team.")

    if row.is_active:
        deactivate_integration(row.integration)
        notice = _("{carrier} deactivated. Containers already tracked through it keep their events.").format(
            carrier=row.name
        )
    else:
        activate_integration(row.integration)
        notice = _("{carrier} activated.").format(carrier=row.name)
    return _respond(request, notice=notice, notice_level="info", message=notice)
