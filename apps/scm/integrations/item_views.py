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

from .item_forms import ItemRuleForm
from .models import BusinessCentralItemRule
from .selectors import get_item_summary, get_team_business_central_integrations

ITEMS_TEMPLATE = "scm/integrations/pages/item_rules.html"
RULE_FORM_TEMPLATE = "scm/integrations/pages/item_rule_form.html"


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
