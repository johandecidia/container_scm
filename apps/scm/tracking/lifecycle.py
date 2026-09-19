"""Starting and stopping the tracking of one container.

Four decisions already existed before this module, and none of them is repeated here:

    carrier resolution   who is carrying the box       integrations/carriers/carrier_resolution.py
    provider routing     who to ask about it           tracking/provider_routing.py
    activation           the asking and the storing    tracking/activation.py
    refresh              both of the above, from a container, on demand
                                                       tracking/manual_refresh.py

What was missing was a *lifecycle*: one application-level answer to "start watching this
box" and "stop watching this box" that the container list, the Container Workspace and
the import pipeline can all call, so the three cannot drift into three behaviours. That
is the whole of this module. It adds no transport, no parser, no second activation path
and no tracking state of its own.

**There is no ``Container.tracking_enabled``.** Whether a container is tracked is whether
it has a subscription the sync engine would still run —
:data:`~apps.scm.tracking.selectors.LIVE_SUBSCRIPTION_STATUSES`, which the Control Tower
already reads. A boolean beside it could disagree with it, and the disagreement would be
invisible: the flag would say tracking and the scheduler would poll nothing.

Start and Stop are therefore *status transitions on subscriptions*, and the scheduler
needs to know nothing about why one happened. It asks
:func:`~apps.scm.tracking.selectors.get_due_tracking_subscriptions` for the watches that
are due; this module decides which watches are live.

The three subscription statuses this module moves between, and what each means:

``ACTIVE`` / ``FAILED`` / ``SYNCING``
    Live. The container is tracked and the scheduler runs it. ``FAILED`` is live on
    purpose — a watch we believe in that is not answering is precisely what a control
    tower exists to surface.
``PAUSED``
    **Locally stopped, not yet released at the provider.** Not polled, not counted as
    tracking, and kept with its ``provider_reference`` intact so the release can be
    retried by pressing Stop again. Also what
    :mod:`apps.scm.tracking.source_switch` uses for a superseded watch, which is the
    same fact: kept, not polled.
``CANCELLED``
    Fully stopped — locally and, where the provider has one, externally. Its events,
    payloads, positions and carrier evidence all remain; only the watch is over.

Stop never deletes anything. A stopped container still shows its whole journey in the
workspace, and can be started again later.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

from .models import TrackingSubscription
from .selectors import LIVE_SUBSCRIPTION_STATUSES

if TYPE_CHECKING:
    from apps.scm.containers.models import Container
    from apps.teams.models import Team

    from .manual_refresh import RefreshResult

logger = logging.getLogger(__name__)

# What a lifecycle call achieved, as a value rather than a sentence, so callers branch
# on it and the UI decides what to say. Carried on ``RefreshResult.state``, which is
# already the outcome type the tracking panel renders.
STARTED = "tracking_started"
ALREADY_ACTIVE = "tracking_already_active"
STOPPED = "tracking_stopped"
ALREADY_STOPPED = "tracking_already_stopped"
# Local tracking stopped, and a provider that bills for an external subscription could
# not be told. Not a success: somebody is still paying for it.
STOP_INCOMPLETE = "tracking_stop_incomplete"

# The statuses Stop acts on. ``COMPLETED`` is deliberately absent: that leg is over,
# nothing polls it, and cancelling history would claim somebody switched it off.
STOPPABLE_STATUSES: tuple[str, ...] = (
    *LIVE_SUBSCRIPTION_STATUSES,
    TrackingSubscription.Status.PAUSED,
)


# ---------------------------------------------------------------------------
# Reading the state
# ---------------------------------------------------------------------------


def get_live_container_subscriptions(*, team: Team, container: Container) -> list[TrackingSubscription]:
    """Every watch on this container the sync engine would still run, newest first."""
    return list(
        TrackingSubscription.objects.filter(
            team=team,
            container=container,
            status__in=LIVE_SUBSCRIPTION_STATUSES,
        )
        .select_related("provider")
        .order_by("-created_at")
    )


def is_container_tracked(*, team: Team, container: Container) -> bool:
    """Whether anything is currently watching this container.

    The single question the Start/Stop controls are rendered from, and the same one
    :attr:`apps.scm.containers.workspace.ContainerWorkspace.has_live_tracking` answers
    for a workspace that has already loaded its subscriptions.
    """
    return TrackingSubscription.objects.filter(
        team=team,
        container=container,
        status__in=LIVE_SUBSCRIPTION_STATUSES,
    ).exists()


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


def start_container_tracking(*, team: Team, container: Container, actor=None) -> RefreshResult:
    """Start tracking ``container``, through whichever provider should answer for it.

    The chain is the one that already exists — carrier resolution, provider routing,
    activation — reached through :func:`refresh_container_tracking`, because that
    function already asks the two questions a start has to ask in the right order:
    does this container have a proved source to refresh, or does one have to be found?

    Three things happen here and nowhere else.

    **Already tracked is a success, not a second subscription.** A container with a live
    watch returns immediately with :data:`ALREADY_ACTIVE`. No provider is called, so a
    double click, a stale page or an import that lists the same box twice costs nothing
    and creates nothing.

    **A stopped watch is resumed before anything is fetched.** Stop leaves the watch
    ``CANCELLED``, and a cancelled watch is excluded from
    :func:`~apps.scm.tracking.selectors.get_verified_container_subscriptions` — which is
    what carrier resolution reads as trusted knowledge. Starting without resuming would
    therefore re-run discovery on a container whose carrier we already proved, spending
    a lookup, up to five Traqo probes and possibly a billable Vizion identification to
    re-learn what is written on the row in front of us. Resuming first makes a restart
    cost one ordinary sync.

    That is the one write here that precedes a fetch, and it is not the thing activation's
    fetch-before-write rule protects: it creates no source and asserts nothing new. It
    restores a watch that already returned this container's own events.

    **Never raises.** Every provider failure is classified by the sync engine or the
    probe, so the result is always something the UI can show and an import can log.
    """
    from .manual_refresh import refresh_container_tracking

    live = get_live_container_subscriptions(team=team, container=container)
    if live:
        return _already_active(live)

    resumed = _resume_stopped_watches(team=team, container=container)
    result = refresh_container_tracking(team=team, container=container)

    _log_start(team=team, container=container, actor=actor, result=result, resumed=resumed)
    if result.tracked:
        return _replace_state(result, STARTED)
    return result


def _already_active(live: list[TrackingSubscription]) -> RefreshResult:
    """Report a container that is already being watched, without asking anybody."""
    from .manual_refresh import INFO, RefreshResult, describe_subscription_carrier

    carrier_code, carrier_name = describe_subscription_carrier(live[0])
    return RefreshResult(
        level=INFO,
        state=ALREADY_ACTIVE,
        message=_("This container is already being tracked."),
        carrier_code=carrier_code,
        carrier_name=carrier_name,
        tracked=True,
    )


def _resume_stopped_watches(*, team: Team, container: Container) -> list[TrackingSubscription]:
    """Return every stopped watch on this container to ACTIVE. Returns the ones changed.

    ``next_sync_at`` is cleared rather than kept, for the reason
    :func:`~apps.scm.tracking.services.complete_tracking_subscription` clears it: the
    dispatcher reads a null as "due now", and a stale timestamp from before the stop
    would otherwise decide when a restarted watch is first polled.

    Paused watches are resumed too. A paused watch is one nothing is fetching — whether
    it was superseded by another provider or left behind by a stop whose external
    release failed — and "start tracking this container" means all of them.
    """
    stopped = list(
        TrackingSubscription.objects.filter(
            team=team,
            container=container,
            status__in=[TrackingSubscription.Status.CANCELLED, TrackingSubscription.Status.PAUSED],
        ).select_related("provider")
    )
    for subscription in stopped:
        subscription.status = TrackingSubscription.Status.ACTIVE
        subscription.next_sync_at = None
        subscription.save(update_fields=["status", "next_sync_at", "updated_at"])
        logger.info(
            "Resumed tracking subscription %s (%s) for container %s.",
            subscription.pk,
            subscription.provider.code,
            container.container_id,
        )
    return stopped


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


def stop_container_tracking(*, team: Team, container: Container, actor=None) -> RefreshResult:
    """Stop tracking ``container``, at the provider as well as here where that is possible.

    The symmetric operation, in the order that keeps the money right:

        the container's watches → external release, where the provider bills for one
                                → local deactivation
                                → no further scheduled work

    **External first.** A provider that charges for an active subscription has to be told
    before we forget its handle, and ``provider_reference`` is that handle — Vizion's
    reference id, Traqo's sealine. Which providers need telling is
    :func:`~apps.scm.tracking.sources.get_provider_stop`, per source, for the same reason
    ``scheduled_sync`` is per source: a blanket rule in either direction would either
    keep paying for Vizion references or invent an endpoint Traqo does not publish.

    **A refused release is not a refused stop.** Where the provider was asked and failed,
    the watch is left ``PAUSED`` rather than ``CANCELLED``: nothing polls it, the
    container reads as not tracked, and the reference is still on the row so pressing
    Stop again retries the release. Declining to stop locally would be strictly worse —
    we would go on spending requests *and* go on paying for the reference.

    **Nothing is deleted.** Events, raw payloads, positions, ETA history and carrier
    evidence are untouched, so a stopped container still shows its whole journey and can
    be started again.

    Idempotent: a container nothing is watching returns :data:`ALREADY_STOPPED`, and no
    provider is called for it.
    """
    from .manual_refresh import INFO, RefreshResult, describe_subscription_carrier

    stoppable = list(
        TrackingSubscription.objects.filter(
            team=team,
            container=container,
            status__in=STOPPABLE_STATUSES,
        ).select_related("provider")
    )
    if not stoppable:
        return RefreshResult(
            level=INFO,
            state=ALREADY_STOPPED,
            message=_("This container is not being tracked."),
            tracked=False,
        )

    carrier_code, carrier_name = describe_subscription_carrier(stoppable[0])
    unreleased = [
        subscription for subscription in stoppable if not _stop_one_subscription(subscription, container=container)
    ]

    _log_stop(team=team, container=container, actor=actor, stopped=stoppable, unreleased=unreleased)
    return _describe_stop(
        stopped=stoppable,
        unreleased=unreleased,
        carrier_code=carrier_code,
        carrier_name=carrier_name,
    )


def _stop_one_subscription(subscription: TrackingSubscription, *, container: Container) -> bool:
    """Stop one watch. Returns True when it was fully stopped, provider included.

    False means the provider was asked to release its subscription and could not, so the
    watch is parked in ``PAUSED`` for a retry instead of being closed.
    """
    from .services import cancel_tracking_subscription, pause_tracking_subscription
    from .sources import STOP_FAILED, release_provider_subscription

    outcome = release_provider_subscription(subscription)
    if outcome.state == STOP_FAILED:
        pause_tracking_subscription(subscription)
        # The provider's own words, which the release path keeps free of credentials.
        # Recorded on the watch so the next Stop — and anybody reading the row — can
        # see why it is parked rather than closed.
        subscription.last_error_message = outcome.detail
        subscription.save(update_fields=["last_error_message", "updated_at"])
        logger.warning(
            "Tracking for %s: %s could not release its subscription (%s). Watch %s paused for retry.",
            container.container_id,
            subscription.provider.code,
            outcome.detail,
            subscription.pk,
        )
        return False

    cancel_tracking_subscription(subscription)
    logger.info(
        "Tracking for %s stopped: watch %s (%s) cancelled, provider release %s.",
        container.container_id,
        subscription.pk,
        subscription.provider.code,
        outcome.state,
    )
    return True


def _describe_stop(
    *,
    stopped: list[TrackingSubscription],
    unreleased: list[TrackingSubscription],
    carrier_code: str,
    carrier_name: str,
) -> RefreshResult:
    """Say what the stop achieved, and say plainly when a provider still has to be told."""
    from .manual_refresh import SUCCESS, WARNING, RefreshResult

    if unreleased:
        providers = ", ".join(
            sorted({subscription.provider.name or subscription.provider.code for subscription in unreleased})
        )
        return RefreshResult(
            level=WARNING,
            state=STOP_INCOMPLETE,
            message=_(
                "Tracking stopped here, but %(providers)s could not be told to stop. "
                "Its subscription may still be charged — press Stop again to retry."
            )
            % {"providers": providers},
            carrier_code=carrier_code,
            carrier_name=carrier_name,
            tracked=False,
        )

    return RefreshResult(
        level=SUCCESS,
        state=STOPPED,
        message=_("Tracking stopped. The tracking history for this container is kept.")
        if len(stopped) == 1
        else _("Tracking stopped for %(count)s sources. The tracking history for this container is kept.")
        % {"count": len(stopped)},
        carrier_code=carrier_code,
        carrier_name=carrier_name,
        tracked=False,
    )


# ---------------------------------------------------------------------------
# Audit
#
# Who started or stopped tracking, and when, through the audit trail that already
# exists. Deliberately not two columns on TrackingSubscription: "stopped_by" would be
# overwritten by the next stop, while the log keeps every one of them — and the whole
# question is a history rather than a current value. ``updated_at`` on the watch
# already says when it last changed.
# ---------------------------------------------------------------------------


def _log_start(*, team: Team, container: Container, actor, result: RefreshResult, resumed: list) -> None:
    from apps.scm.audit_log.models import SCMAuditLog
    from apps.scm.audit_log.services import log_scm_action

    log_scm_action(
        team=team,
        action=SCMAuditLog.Action.TRACKING_STARTED,
        object_type="Container",
        object_id=container.pk,
        object_repr=container.container_id,
        actor=actor,
        metadata={
            "tracked": result.tracked,
            "state": result.state,
            "carrier_code": result.carrier_code,
            "resumed_subscriptions": [subscription.pk for subscription in resumed],
        },
    )


def _log_stop(*, team: Team, container: Container, actor, stopped: list, unreleased: list) -> None:
    from apps.scm.audit_log.models import SCMAuditLog
    from apps.scm.audit_log.services import log_scm_action

    log_scm_action(
        team=team,
        action=SCMAuditLog.Action.TRACKING_STOPPED,
        object_type="Container",
        object_id=container.pk,
        object_repr=container.container_id,
        actor=actor,
        metadata={
            "subscriptions": [subscription.pk for subscription in stopped],
            "providers": sorted({subscription.provider.code for subscription in stopped}),
            "unreleased_subscriptions": [subscription.pk for subscription in unreleased],
        },
    )


def _replace_state(result: RefreshResult, state: str) -> RefreshResult:
    """Return ``result`` with a lifecycle state, keeping everything it reported.

    ``RefreshResult`` is frozen, and its message, level and counts are exactly what the
    panel should show — a start that fetched events should say so. Only the machine-
    readable state changes, so a caller can tell a start from a refresh.
    """
    from dataclasses import replace

    return replace(result, state=state)
