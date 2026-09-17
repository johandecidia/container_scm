"""Making a changed tracking-source preference take effect, now.

Storing a preference and acting on it are separate things, and both have to happen
when somebody changes a container's tracking source: the preference is recorded by
:mod:`.preferences`, and this module is what turns it into a watch through the write
path that already exists.

Without it the setting would be inert for exactly the containers it matters most
for. ``refresh_container_tracking`` refreshes the sources a container has already
proved and only *discovers* a new one when it has none — which is correct for a
refresh, and means a container tracked through Traqo would keep being tracked
through Traqo however many times its provider was changed to Maersk.

So the change itself does the activation:

    preference stored → carrier from evidence already held → routing → activation

No provider is called to work out the carrier. ``use_trusted_knowledge`` alone, with
every paid and free lookup switched off, so changing a setting never spends a
request on discovery; a container whose carrier nobody has established yet simply
keeps the preference and picks it up at the next refresh.

**Superseded watches are paused, not deleted.** A container can legitimately have
several verified sources, but not two providers polling it for the same leg — that
is the duplicate the scheduled sync would otherwise run forever. ``PAUSED`` is
excluded from the due query and kept by
:func:`apps.scm.tracking.selectors.get_verified_container_subscriptions`, so the
old provider stops being fetched and every event it ever produced stays in the
journey.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

from .models import TrackingSubscription

if TYPE_CHECKING:
    from apps.scm.containers.models import Container
    from apps.teams.models import Team

    from .manual_refresh import RefreshResult

logger = logging.getLogger(__name__)


def apply_container_tracking_source(*, team: Team, container: Container) -> RefreshResult:
    """Start tracking ``container`` through its currently preferred provider.

    Returns the same :class:`~apps.scm.tracking.manual_refresh.RefreshResult` the
    refresh button produces, so the workspace renders one kind of outcome banner
    however tracking was started.

    Never raises: an unreachable provider becomes a result to show, and nothing about
    the container changes unless a provider actually answered.
    """
    from apps.scm.integrations.carriers.carrier_resolution import resolve_carrier_for_container

    from .activation import activate_tracking_route
    from .manual_refresh import INFO, NOT_CONFIGURED, RefreshResult, describe_activation
    from .preferences import TEAM_DEFAULT, get_container_provider_override

    chosen = get_container_provider_override(container)

    resolution = resolve_carrier_for_container(
        team=team,
        container=container,
        # Evidence already held, and nothing else. Changing a setting must not spend a
        # provider request, let alone a billable identification.
        use_trusted_knowledge=True,
        use_traqo_lookup=False,
        use_traqo_probe=False,
        use_direct_discovery=False,
        use_vizion_aci=False,
    )
    if not resolution.resolved:
        return RefreshResult(
            level=INFO,
            state=NOT_CONFIGURED,
            message=_(
                "Tracking source saved. Nobody has established which carrier is moving this container yet — "
                "refresh its tracking to find out."
            ),
        )

    activation = activate_tracking_route(team=team, container=container, resolution=resolution)
    if activation.subscription is not None and chosen != TEAM_DEFAULT:
        _pause_superseded_watches(
            team=team,
            container=container,
            keep=activation.subscription,
        )
    return describe_activation(resolution, activation, reference=container.container_id)


def _pause_superseded_watches(*, team: Team, container: Container, keep: TrackingSubscription) -> int:
    """Pause this container's other running watches, leaving ``keep`` alone.

    Only the watches that would still be *polled* — an already paused or cancelled one
    needs nothing done to it, and a completed leg is history rather than a duplicate.
    Returns how many were paused.
    """
    superseded = list(
        TrackingSubscription.objects.filter(
            team=team,
            container=container,
            status__in=[
                TrackingSubscription.Status.ACTIVE,
                TrackingSubscription.Status.FAILED,
                TrackingSubscription.Status.SYNCING,
            ],
        ).exclude(pk=keep.pk)
    )
    for subscription in superseded:
        subscription.status = TrackingSubscription.Status.PAUSED
        subscription.save(update_fields=["status", "updated_at"])
        logger.info(
            "Paused tracking subscription %s (%s) for container %s: superseded by %s.",
            subscription.pk,
            subscription.provider_id,
            container.container_id,
            keep.provider_id,
        )
    return len(superseded)
