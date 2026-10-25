# Business Central location mappings — Settings → Integrations → Business Central → Locations.
#
# Team Administrators only (`scm_team_admin_required`, 404 for members), and every
# lookup goes through the active team's own Business Central integration, so a
# manipulated id never reaches another team's integration or mappings.
from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_team_admin_required

from .item_views import _team_integration
from .location_forms import LocationMappingForm
from .models import BusinessCentralItemLedgerEntry, BusinessCentralLocationMapping

LOCATIONS_TEMPLATE = "scm/integrations/pages/location_mappings.html"
FORM_TEMPLATE = "scm/integrations/pages/location_mapping_form.html"


def _unmapped_codes(integration, mapped: set[str]) -> list[dict]:
    """Location codes seen in synced item ledger entries that have no mapping yet."""
    rows = (
        BusinessCentralItemLedgerEntry.objects.filter(integration=integration)
        .exclude(location_code="")
        .values("location_code")
        .annotate(entries=Count("id"))
        .order_by("location_code")
    )
    return [row for row in rows if row["location_code"].upper() not in mapped]


@scm_team_admin_required
def location_mappings(request, pk: int):
    """List the integration's location mappings, add one, and show codes still unmapped."""
    integration = _team_integration(request, pk)
    if request.method == "POST":
        form = LocationMappingForm(request.POST, integration=integration)
        if form.is_valid():
            mapping = form.save()
            messages.success(request, _("Location {code} saved.").format(code=mapping.bc_location_code))
            return redirect("integrations:location_mappings", pk=integration.pk)
    else:
        form = LocationMappingForm(integration=integration, initial={"bc_location_code": request.GET.get("code", "")})
    mappings = BusinessCentralLocationMapping.objects.filter(integration=integration).select_related("pegasus_location")
    context = {
        "integration": integration,
        "mappings": mappings,
        "form": form,
        "unmapped": _unmapped_codes(integration, {m.bc_location_code for m in mappings}),
    }
    return render(request, LOCATIONS_TEMPLATE, context)


@scm_team_admin_required
def location_mapping_update(request, pk: int, mapping_pk: int):
    integration = _team_integration(request, pk)
    mapping = get_object_or_404(BusinessCentralLocationMapping, integration=integration, pk=mapping_pk)
    form = LocationMappingForm(request.POST or None, instance=mapping, integration=integration)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Location {code} updated.").format(code=mapping.bc_location_code))
        return redirect("integrations:location_mappings", pk=integration.pk)
    return render(request, FORM_TEMPLATE, {"integration": integration, "mapping": mapping, "form": form})


@scm_team_admin_required
@require_POST
def location_mapping_delete(request, pk: int, mapping_pk: int):
    integration = _team_integration(request, pk)
    mapping = get_object_or_404(BusinessCentralLocationMapping, integration=integration, pk=mapping_pk)
    mapping.delete()
    messages.info(request, _("Location {code} removed.").format(code=mapping.bc_location_code))
    return redirect("integrations:location_mappings", pk=integration.pk)
