"""Choosing which provider one container is tracked through, from its workspace.

The control is on the Container Workspace because that is where somebody notices
the problem it solves — this box's carrier is connected directly and its tracking
is coming through an aggregator, or the reverse. It is administrator-only, enforced
by the decorator rather than by whether the selector was rendered.

Three lines of work and no logic of its own: store the preference
(:mod:`apps.scm.tracking.preferences`, which owns what may be chosen), make it real
(:mod:`apps.scm.tracking.source_switch`, which activates through the existing write
path), and re-render the tracking panel with the outcome — the same panel and the
same result banner the Refresh button produces.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_team_admin_required
from apps.scm.tracking.preferences import InvalidTrackingProvider, set_container_provider_override
from apps.scm.tracking.source_switch import apply_container_tracking_source

from .models import Container
from .views import _MESSAGE_LEVELS, TRACKING_PANEL_TEMPLATE, tracking_panel_context


@scm_team_admin_required
@require_POST
def container_set_tracking_source(request, container_id):
    """Set (or clear) this container's tracking provider, and act on the change.

    An invalid choice is refused by the preference layer and reported without
    touching the container: the selector is built from the same allow-list, so this
    only fires on a stale page or a hand-made request.
    """
    team = request.default_team
    container = get_object_or_404(Container, pk=container_id, team=team)

    try:
        set_container_provider_override(
            team=team,
            container=container,
            provider_code=request.POST.get("provider", ""),
        )
    except InvalidTrackingProvider as exc:
        if request.htmx:
            return render(
                request,
                TRACKING_PANEL_TEMPLATE,
                tracking_panel_context(request, team=team, container=container, error=str(exc)),
            )
        messages.error(request, str(exc))
        return redirect("containers:detail", container_id=container.pk)

    result = apply_container_tracking_source(team=team, container=container)

    if request.htmx:
        return render(
            request,
            TRACKING_PANEL_TEMPLATE,
            tracking_panel_context(request, team=team, container=container, refresh=result),
        )
    _MESSAGE_LEVELS[result.level](request, result.message)
    return redirect("containers:detail", container_id=container.pk)
