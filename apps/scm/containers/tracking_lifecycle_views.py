"""Starting and stopping one container's tracking, from the two places it is offered.

The container list and the Container Workspace both need the action, and there is one
of it: :mod:`apps.scm.tracking.lifecycle` owns what start and stop *mean*, and these
views do nothing but authorise the request, scope it to the team, and re-render whatever
the caller was looking at. A second Start button with its own idea of the rules is
exactly what this file exists to prevent.

**Administrator-only, enforced by the decorator.** ``scm_team_admin_required`` is the
same gate Settings and the tracking-source selector use, so a member without admin
rights gets a 404 whether or not the button was ever rendered for them — a hand-made
POST reaches the same check as a click.

**POST only.** Starting tracking spends provider requests and stopping it can release a
billable subscription; neither is something a link, a crawler or a prefetch may do.
``require_POST`` and the CSRF token that the shared ``hx-headers`` on ``<body>`` supplies
are both load-bearing.

Two response shapes, because there are two callers and each has to see its own state
change reflected:

    the list       ``origin=row`` → the container's own table row, re-annotated
    the workspace  anything else  → the tracking panel, with the result banner

The origin is a presentation hint and nothing more: both values run identical business
logic, and a request that lies about where it came from gets the wrong HTML rather than
different behaviour.
"""

from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.scm.decorators import scm_team_admin_required
from apps.scm.tracking.lifecycle import start_container_tracking, stop_container_tracking

from .models import Container
from .views import MESSAGE_LEVELS, TRACKING_PANEL_TEMPLATE, tracking_panel_context

ROW_TEMPLATE = "scm/containers/partials/container_row.html"

# The POST value the container list sends so it gets a table row back.
ROW_ORIGIN = "row"


@scm_team_admin_required
@require_POST
def container_start_tracking(request, container_id):
    """Start tracking this container, through whichever provider should answer for it.

    Runs in the request rather than on a queue, for the same reason "Refresh tracking"
    does: the person who pressed the button is waiting to find out whether a carrier
    answered, and "queued" is not that answer.
    """
    team = request.default_team
    container = get_object_or_404(Container, pk=container_id, team=team)
    result = start_container_tracking(team=team, container=container, actor=request.user)
    return _respond(request, team=team, container=container, result=result)


@scm_team_admin_required
@require_POST
def container_stop_tracking(request, container_id):
    """Stop tracking this container, at the provider as well as here where it can be.

    Also in the request, and for a stronger reason: a provider that bills for a
    subscription has to be told, and the result says whether it was. Deferring that
    would report success for a release that had not happened yet.
    """
    team = request.default_team
    container = get_object_or_404(Container, pk=container_id, team=team)
    result = stop_container_tracking(team=team, container=container, actor=request.user)
    return _respond(request, team=team, container=container, result=result)


def _respond(request, *, team, container, result):
    """Re-render whatever the caller was looking at, with the outcome on it."""
    if request.htmx and request.POST.get("origin") == ROW_ORIGIN:
        return render(
            request,
            ROW_TEMPLATE,
            {
                # Re-read through the list's own selector so the row's tracking
                # annotations are rebuilt rather than guessed at — the cell the user is
                # watching is rendered from them.
                "container": _annotated(team, container),
                "can_manage_tracking": True,
                "team_slug": team.slug,
            },
        )

    if request.htmx:
        return render(
            request,
            TRACKING_PANEL_TEMPLATE,
            tracking_panel_context(request, team=team, container=container, refresh=result),
        )

    MESSAGE_LEVELS[result.level](request, result.message)
    return redirect("containers:detail", container_id=container.pk)


def _annotated(team, container: Container) -> Container:
    """The container as the list sees it, tracking annotations included."""
    from .selectors import get_team_containers

    return get_team_containers(team).get(pk=container.pk)
