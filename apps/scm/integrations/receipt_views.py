# Business Central posted purchase receipts — Settings → Integrations → Business Central → Receipts.
#
# Monitoring and review only: counts, why a receipt was or was not received, and a
# button to queue a processing pass. Nothing here edits Business Central data.
# Team Administrators only; every lookup goes through the team's own integration.
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_team_admin_required

from .item_views import _team_integration
from .models import BusinessCentralReceiptContainer, ReceiptResolution, ReceiveReason, ReceiveStatus
from .selectors import get_receipt_summary
from .tasks import process_business_central_purchase_receipts_task

RECEIPTS_TEMPLATE = "scm/integrations/pages/receipts.html"
_LIST_LIMIT = 200


@scm_team_admin_required
def receipts(request, pk: int):
    """Receipt and receive counts, and the serial rows for one receive status, reason or resolution."""
    integration = _team_integration(request, pk)
    rows = BusinessCentralReceiptContainer.objects.filter(receipt__integration=integration)
    status = request.GET.get("status", "")
    reason = request.GET.get("reason", "")
    resolution = request.GET.get("resolution", "")
    filtered = True
    if status in ReceiveStatus.values:
        rows = rows.filter(receive_status=status)
    elif reason in ReceiveReason.values:
        rows = rows.filter(receive_status=ReceiveStatus.NOT_RECEIVED, receive_reason=reason)
    elif resolution in ReceiptResolution.values:
        rows = rows.filter(resolution_status=resolution)
    else:
        filtered = False
    context = {
        "integration": integration,
        "summary": get_receipt_summary(integration),
        "filtered": filtered,
        "rows": rows.select_related("receipt", "container", "movement").order_by("-entry_no")[:_LIST_LIMIT],
    }
    return render(request, RECEIPTS_TEMPLATE, context)


@scm_team_admin_required
@require_POST
def receipts_process(request, pk: int):
    """Queue a processing pass: re-resolve receipts and receive only what the location settings allow."""
    integration = _team_integration(request, pk)
    process_business_central_purchase_receipts_task.delay(integration.pk)
    messages.success(request, _("Processing queued. Refresh the page in a moment to see the result."))
    return redirect("integrations:receipts", pk=integration.pk)
