"""Settings → Locations: whether a container received at each location stops being tracked.

The location itself — name, hierarchy, aliases — is master data edited on the
location pages, and this section links there to add one. What lives here is the
tracking decision that belongs to a destination: ``auto_stop_tracking_on_receive``,
the one stop-on-receive policy every receive path reads. Team Administrators only.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.scm.containers.models import ContainerLocation
from apps.scm.decorators import scm_team_admin_required


@scm_team_admin_required
def locations(request):
    team = request.default_team
    context = {
        "locations": ContainerLocation.objects.filter(team=team)
        .select_related("parent_location")
        .order_by("-is_active", "name"),
    }
    return render(request, "scm/team_settings/pages/locations.html", context)


@scm_team_admin_required
@require_POST
def location_auto_stop(request, location_id: int):
    """Turn stop-on-receive on or off for one of the team's locations.

    Read from the posted value rather than toggled, so a double submit cannot flip it
    back. Applies to receives recorded from now on.
    """
    location = get_object_or_404(ContainerLocation, pk=location_id, team=request.default_team)
    enabled = request.POST.get("auto_stop_tracking_on_receive") in ("1", "true", "on")
    if location.auto_stop_tracking_on_receive != enabled:
        location.auto_stop_tracking_on_receive = enabled
        location.save(update_fields=["auto_stop_tracking_on_receive", "updated_at"])
    if enabled:
        messages.success(request, _("{name}: tracking stops when a container is received.").format(name=location.name))
    else:
        messages.info(request, _("{name}: tracking continues after a receive.").format(name=location.name))
    return redirect("team_settings:locations")
