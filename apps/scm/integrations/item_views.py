# Business Central item classification — Settings → Integrations → Business Central → Items.
#
# Team Administrators only (`scm_team_admin_required`, 404 for members), and every
# lookup goes through the active team's own Business Central integration, so a
# manipulated id never reaches another team's integration or rules.
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_team_admin_required

from .business_systems.business_central.container_creation import (
    CREATE_APPROVED,
    MATCH_ONLY,
    create_approved_containers,
    get_container_creation_mode,
    get_creation_candidates,
    set_container_creation_mode,
)
from .business_systems.business_central.exceptions import BusinessCentralConfigurationError
from .item_forms import ApproveCandidatesForm, ContainerCreationModeForm, ItemRuleForm
from .models import BusinessCentralItemLedgerEntry, BusinessCentralItemRule
from .selectors import get_item_link_summary, get_item_summary, get_team_business_central_integrations
from .tasks import match_business_central_item_ledger_entries_task

ITEMS_TEMPLATE = "scm/integrations/pages/item_rules.html"
RULE_FORM_TEMPLATE = "scm/integrations/pages/item_rule_form.html"
LINKS_TEMPLATE = "scm/integrations/pages/container_links.html"
# Rows shown per list on the container links page.
_LIST_LIMIT = 200


def _team_integration(request, pk: int):
    return get_object_or_404(get_team_business_central_integrations(request.default_team), pk=pk)


@scm_team_admin_required
def item_rules(request, pk: int):
    """List the integration's item rules, add one, and show which items still need a rule."""
    integration = _team_integration(request, pk)
    if request.method == "POST":
        form = ItemRuleForm(request.POST, integration=integration)
        if form.is_valid():
            rule = form.save()
            messages.success(request, _("Rule for {item} saved.").format(item=rule.item_no))
            return redirect("integrations:item_rules", pk=integration.pk)
    else:
        form = ItemRuleForm(integration=integration, initial={"item_no": request.GET.get("item", "")})
    context = {
        "integration": integration,
        "rules": BusinessCentralItemRule.objects.filter(integration=integration).select_related("equipment_type"),
        "form": form,
        "items": get_item_summary(integration),
    }
    return render(request, ITEMS_TEMPLATE, context)


@scm_team_admin_required
def item_rule_update(request, pk: int, rule_pk: int):
    integration = _team_integration(request, pk)
    rule = get_object_or_404(BusinessCentralItemRule, integration=integration, pk=rule_pk)
    form = ItemRuleForm(request.POST or None, instance=rule, integration=integration)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Rule for {item} updated.").format(item=rule.item_no))
        return redirect("integrations:item_rules", pk=integration.pk)
    return render(request, RULE_FORM_TEMPLATE, {"integration": integration, "rule": rule, "form": form})


@scm_team_admin_required
@require_POST
def item_rule_delete(request, pk: int, rule_pk: int):
    integration = _team_integration(request, pk)
    rule = get_object_or_404(BusinessCentralItemRule, integration=integration, pk=rule_pk)
    rule.delete()
    messages.info(request, _("Rule for {item} removed.").format(item=rule.item_no))
    return redirect("integrations:item_rules", pk=integration.pk)


@scm_team_admin_required
def container_links(request, pk: int):
    """Link status per entry, entries for one status, creation mode and creation candidates."""
    integration = _team_integration(request, pk)
    mode = get_container_creation_mode(integration)
    status = request.GET.get("status", "")
    entries = BusinessCentralItemLedgerEntry.objects.none()
    if status in BusinessCentralItemLedgerEntry.LinkStatus.values:
        entries = (
            BusinessCentralItemLedgerEntry.objects.filter(integration=integration, link_status=status)
            .select_related("container")
            .order_by("-entry_no")[:_LIST_LIMIT]
        )
    candidates = get_creation_candidates(integration) if mode != MATCH_ONLY else []
    context = {
        "integration": integration,
        "summary": get_item_link_summary(integration),
        "status": status,
        "entries": entries,
        "mode": mode,
        "mode_form": ContainerCreationModeForm(initial={"mode": mode}),
        "can_create": mode == CREATE_APPROVED,
        "candidates": candidates[:_LIST_LIMIT],
        "candidate_count": len(candidates),
    }
    return render(request, LINKS_TEMPLATE, context)


@scm_team_admin_required
@require_POST
def container_links_match(request, pk: int):
    """Queue a matching pass (links to existing containers only; creates nothing)."""
    integration = _team_integration(request, pk)
    match_business_central_item_ledger_entries_task.delay(integration.pk)
    messages.success(request, _("Matching queued. Refresh the page in a moment to see the result."))
    return redirect("integrations:container_links", pk=integration.pk)


@scm_team_admin_required
@require_POST
def container_links_mode(request, pk: int):
    integration = _team_integration(request, pk)
    form = ContainerCreationModeForm(request.POST)
    if form.is_valid():
        set_container_creation_mode(integration, form.cleaned_data["mode"])
        messages.success(request, _("Container creation mode saved."))
    else:
        messages.error(request, _("Choose a valid container creation mode."))
    return redirect("integrations:container_links", pk=integration.pk)


@scm_team_admin_required
@require_POST
def container_links_create(request, pk: int):
    """Create containers for the approved serial numbers (each re-checked by the service)."""
    integration = _team_integration(request, pk)
    form = ApproveCandidatesForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(str(error) for error in form.non_field_errors()))
        return redirect("integrations:container_links", pk=integration.pk)
    try:
        result = create_approved_containers(integration, form.serials, request.user)
    except BusinessCentralConfigurationError:
        messages.error(request, _("Container creation is not enabled for this integration."))
        return redirect("integrations:container_links", pk=integration.pk)
    messages.success(
        request,
        _("{created} container(s) created, {existing} already existed.").format(
            created=len(result.created), existing=len(result.existing)
        ),
    )
    for serial_no, reason in result.refused.items():
        messages.warning(request, _("{serial} not created: {reason}").format(serial=serial_no, reason=reason))
    return redirect("integrations:container_links", pk=integration.pk)
