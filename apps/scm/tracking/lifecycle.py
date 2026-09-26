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
    payloads, positions and carrier evidence all remain; only the watch is over. On a
    provider that holds a subscription of its own, that subscription is gone, so Start
    re-establishes it through activation rather than by flipping the status back.

Stop never deletes anything. A stopped container still shows its whole journey in the
workspace, and can be started again later.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
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


def stoppable_subscriptions(*, team: Team, container_ids: Iterable[int]):
    """The watches :func:`stop_container_tracking` acts on for these containers.

    :data:`STOPPABLE_STATUSES` — live watches, and paused ones whose provider release
    may still be owed. The one definition of "Stop has something to do here": the stop
    itself reads it, and so does anything that previews a stop, so the two cannot say
    different things about the same container.
    """
    return TrackingSubscription.objects.filter(
        team=team,
        container_id__in=list(container_ids),
        status__in=STOPPABLE_STATUSES,
    )


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


def start_container_tracking(*, team: Team, container: Container, actor=None) -> RefreshResult:
    """Start tracking ``container``, through whichever provider should answer for it.

    The chain is the one that already exists — carrier resolution, provider routing,
    activation — reached through :func:`refresh_container_tracking`, because that
    function already asks the two questions a start has to ask in the right order:
    does this container have a proved source to refresh, or does one have to be found?

    Four things happen here and nowhere else.

    **Already tracked is a success, not a second subscription.** A container with a live
    watch returns immediately with :data:`ALREADY_ACTIVE`. No provider is called, so a
    double click, a stale page or an import that lists the same box twice costs nothing
    and creates nothing.

    **A stopped watch that holds nothing at a provider is resumed before anything is
    fetched.** Stop leaves the watch ``CANCELLED``, and a cancelled watch is excluded from
    :func:`~apps.scm.tracking.selectors.get_verified_container_subscriptions` — which is
    what carrier resolution reads as trusted knowledge. Starting without resuming would
    therefore re-run discovery on a container whose carrier we already proved, spending
    a lookup, up to five Traqo probes and possibly a billable Vizion identification to
    re-learn what is written on the row in front of us. Resuming first makes a restart
    cost one ordinary sync.

    That is the one write here that precedes a fetch, and it is not the thing activation's
    fetch-before-write rule protects: it creates no source and asserts nothing new. It
    restores a watch that already returned this container's own events.

    **A watch whose provider resource Stop released is not resumed.** For those rows the
    status is the only thing a resume could restore — the Vizion reference is
    unsubscribed, the Traqo shipment untracked — and a refresh does not necessarily
    re-create them: Vizion is never polled at all, so a resumed Vizion watch would read
    ACTIVE with nothing subscribed. Such a restart goes through activation instead, from
    the carrier the released watch recorded, so no discovery is spent either. Activation
    fetches before it writes, so the row becomes live only once a provider has actually
    taken the container on again, and stays ``CANCELLED`` when none did.

    **Never raises.** Every provider failure is classified by the sync engine or the
    probe, so the result is always something the UI can show and an import can log.
    """
    from .manual_refresh import refresh_container_tracking

    live = get_live_container_subscriptions(team=team, container=container)
    if live:
        return _already_active(live)

    released = _released_watches(team=team, container=container)
    resumed = _resume_stopped_watches(team=team, container=container, exclude=released)
    reactivated = None if resumed else _reactivate_released_watches(team=team, container=container, released=released)
    result = reactivated or refresh_container_tracking(team=team, container=container)

    _log_start(team=team, container=container, actor=actor, result=result, resumed=resumed, released=released)
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


def _released_watches(*, team: Team, container: Container) -> list[TrackingSubscription]:
    """The cancelled watches on this container whose provider resource Stop has given up.

    ``CANCELLED`` on a provider that holds a subscription of its own — see
    :func:`~apps.scm.tracking.sources.holds_provider_subscription`. ``PAUSED`` is never
    one of them: a pause is exactly what Stop leaves when the release failed, and what a
    superseded watch is left in, so its provider resource is still there to resume.
    Newest first, the order trusted knowledge reads verified sources in.
    """
    from .sources import holds_provider_subscription

    cancelled = (
        TrackingSubscription.objects.filter(
            team=team,
            container=container,
            status=TrackingSubscription.Status.CANCELLED,
        )
        .select_related("provider")
        .order_by("-created_at")
    )
    return [subscription for subscription in cancelled if holds_provider_subscription(subscription.provider.code)]


def _reactivate_released_watches(
    *, team: Team, container: Container, released: list[TrackingSubscription]
) -> RefreshResult | None:
    """Start tracking again through activation, from the carrier a released watch recorded.

    The source_switch pattern: carrier from evidence already held → routing → activation,
    with no provider asked to work out the carrier. The evidence is the released watch
    itself, which trusted knowledge cannot see because it is cancelled.

    Activation decides who is asked, so the restarted source is whichever provider routing
    picks today. For a Traqo watch that is normally Traqo again, and the same row is made
    live through ``get_or_create`` only after Traqo answered. A Vizion watch stays
    cancelled: Vizion is not a provider routing activates, so the container is tracked
    again through the one that is.

    None when no released watch recorded a carrier, which leaves the ordinary refresh —
    and so first-time discovery — to start the container.
    """
    from apps.scm.integrations.carriers.carrier_resolution import (
        FOUND,
        STEP_TRUSTED,
        CarrierResolution,
        ResolutionStep,
    )
    from apps.scm.integrations.carriers.http import interactive_carrier_requests

    from .activation import activate_tracking_route
    from .manual_refresh import describe_activation
    from .models import CarrierSource

    watch = next((subscription for subscription in released if subscription.carrier_code), None)
    if watch is None:
        return None

    source = watch.carrier_source or CarrierSource.EXISTING_VERIFIED_SOURCE
    resolution = CarrierResolution(
        container_number=container.container_id,
        carrier_code=watch.carrier_code,
        carrier_name=watch.carrier_name,
        source=source,
        verified=True,
        steps=(ResolutionStep(step=STEP_TRUSTED, outcome=FOUND, carrier_code=watch.carrier_code, detail=source),),
    )
    with interactive_carrier_requests():
        activation = activate_tracking_route(team=team, container=container, resolution=resolution)

    logger.info(
        "Restart of %s after an external release: watch %s (%s) re-routed for carrier %s, activation %s.",
        container.container_id,
        watch.pk,
        watch.provider.code,
        watch.carrier_code,
        activation.state,
    )
    return describe_activation(resolution, activation, reference=container.container_id)


def _resume_stopped_watches(
    *, team: Team, container: Container, exclude: Iterable[TrackingSubscription] = ()
) -> list[TrackingSubscription]:
    """Return every stopped watch on this container to ACTIVE. Returns the ones changed.

    ``next_sync_at`` is cleared rather than kept, for the reason
    :func:`~apps.scm.tracking.services.complete_tracking_subscription` clears it: the
    dispatcher reads a null as "due now", and a stale timestamp from before the stop
    would otherwise decide when a restarted watch is first polled.

    Paused watches are resumed too. A paused watch is one nothing is fetching — whether
    it was superseded by another provider or left behind by a stop whose external
    release failed — and "start tracking this container" means all of them. Its
    provider resource was never released, so resuming it creates no duplicate.

    ``exclude`` is the watches whose provider resource was released; see
    :func:`_released_watches` for why resuming them would be wrong.
    """
    stopped = list(
        TrackingSubscription.objects.filter(
            team=team,
            container=container,
            status__in=[TrackingSubscription.Status.CANCELLED, TrackingSubscription.Status.PAUSED],
        )
        .exclude(pk__in=[subscription.pk for subscription in exclude])
        .select_related("provider")
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
# Automatic start, for containers an import has just created
# ---------------------------------------------------------------------------


@dataclass
class AutoStartSummary:
    """What automatically starting tracking achieved for one batch of new containers.

    Returned rather than raised, and returned *whole* — including the failures — because
    the caller is an import that has already succeeded. The import's own result is not
    in question here; what this says is whether the tracking it asked for happened, so
    somebody can see later that it did not.
    """

    # Whether auto-start applied at all. False means the team has it switched off, or
    # this import overrode it off — and then nothing was attempted, which is different
    # from attempting and failing.
    enabled: bool = False
    considered: int = 0
    started: int = 0
    already_tracked: int = 0
    # Containers a provider could not be found or reached for. Each entry is
    # ``(container_id, reason)`` — the reason is the lifecycle's own message, which is
    # built from outcome values rather than from provider error text.
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def attempted(self) -> bool:
        return self.enabled and self.considered > 0

    @property
    def failure_count(self) -> int:
        return len(self.failed)


def auto_start_tracking_for_containers(
    *,
    team: Team,
    containers: Iterable[Container],
    actor=None,
    enabled: bool | None = None,
) -> AutoStartSummary:
    """Start tracking containers an import has just created, if that is the policy.

    ``enabled`` is the override, and ``None`` means "no override": the team's
    :func:`~apps.scm.tracking.preferences.get_team_auto_start_tracking` setting decides.
    An import that asked one way or the other passes ``True`` or ``False`` and the team
    default is not consulted, which is the whole of the override semantics.

    **Only the containers passed in.** Deciding *which* containers an import created is
    the import's job and it already knows — the intake result lists them, and the job
    importer reports each row as created or skipped. A container that merely appeared in
    the file again is not new, and starting tracking for it would mean a re-uploaded
    spreadsheet silently began tracking a fleet somebody had deliberately stopped.

    **Tracking is downstream of persistence.** The containers are saved before this
    runs, and nothing here can undo that. Each one is started on its own, so a carrier
    outage on the third container does not cost the fourth its tracking, and a failure
    is counted and logged rather than raised — an import that stored its containers
    correctly is a successful import even when tracking could not be started for them.

    Programming errors are not swallowed quietly either. ``start_container_tracking``
    classifies every provider failure itself, so an exception escaping it is a bug; it
    is logged with a traceback and recorded as a failure for that container rather than
    being allowed to discard the rest of the batch.
    """
    from .preferences import get_team_auto_start_tracking

    is_enabled = get_team_auto_start_tracking(team) if enabled is None else bool(enabled)
    summary = AutoStartSummary(enabled=is_enabled)
    if not is_enabled:
        return summary

    for container in containers:
        summary.considered += 1
        try:
            result = start_container_tracking(team=team, container=container, actor=actor)
        except Exception:  # noqa: BLE001 — see the docstring: a bug here must not lose the import
            logger.exception(
                "Automatic tracking for %s raised unexpectedly after it was imported.",
                container.container_id,
            )
            summary.failed.append((container.container_id, "An unexpected error prevented tracking from starting."))
            continue

        if result.state == ALREADY_ACTIVE:
            summary.already_tracked += 1
        elif result.tracked:
            summary.started += 1
        else:
            summary.failed.append((container.container_id, str(result.message)))

    logger.info(
        "Automatic tracking for %s newly imported container(s): %s started, %s already tracked, %s could not start.",
        summary.considered,
        summary.started,
        summary.already_tracked,
        summary.failure_count,
    )
    return summary


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
    :func:`~apps.scm.tracking.sources.release_provider_subscription`, per source, for the
    same reason ``scheduled_sync`` is per source: a blanket rule in either direction would
    either keep paying for Vizion references or invent an endpoint Traqo does not publish.

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

    stoppable = list(stoppable_subscriptions(team=team, container_ids=[container.pk]).select_related("provider"))
    if not stoppable:
        return RefreshResult(
            level=INFO,
            state=ALREADY_STOPPED,
            message=_("This container is not being tracked."),
            tracked=False,
        )

    carrier_code, carrier_name = describe_subscription_carrier(stoppable[0])
    unreleased = [
        subscription
        for subscription in stoppable
        if not _stop_one_subscription(subscription, label=container.container_id)
    ]

    _log_stop(
        team=team,
        object_type="Container",
        object_id=container.pk,
        object_repr=container.container_id,
        actor=actor,
        stopped=stoppable,
        unreleased=unreleased,
    )
    return _describe_stop(
        stopped=stoppable,
        unreleased=unreleased,
        carrier_code=carrier_code,
        carrier_name=carrier_name,
    )


def stop_subscription_tracking(*, team: Team, subscription: TrackingSubscription, actor=None) -> RefreshResult:
    """Stop one watch, with exactly the semantics of :func:`stop_container_tracking`.

    For the tracking detail page, which is about a single subscription — one that may
    track a booking or bill of lading and have no container at all, and one of possibly
    several legs when it does. Stopping the whole container from there would stop legs the
    reader was not looking at, so the scope is the watch; everything else is shared: the
    same :data:`STOPPABLE_STATUSES`, the same external-first release, ``CANCELLED`` only
    once the provider let go, ``PAUSED`` when it refused, the same audit action and the
    same result wording.

    There is deliberately no way left to mark a watch ``CANCELLED`` without asking its
    provider first. That was what the page's old Cancel did, and it left a Vizion
    reference or a Traqo shipment subscribed behind a row claiming it was over.
    """
    from .manual_refresh import INFO, RefreshResult, describe_subscription_carrier

    if subscription.team_id != team.pk or subscription.status not in STOPPABLE_STATUSES:
        return RefreshResult(
            level=INFO,
            state=ALREADY_STOPPED,
            message=_("Tracking for this reference is already stopped."),
            tracked=False,
        )

    carrier_code, carrier_name = describe_subscription_carrier(subscription)
    released = _stop_one_subscription(subscription, label=subscription.tracking_reference)
    unreleased = [] if released else [subscription]

    _log_stop(
        team=team,
        object_type="TrackingSubscription",
        object_id=subscription.pk,
        object_repr=subscription.tracking_reference,
        actor=actor,
        stopped=[subscription],
        unreleased=unreleased,
    )
    return _describe_stop(
        stopped=[subscription],
        unreleased=unreleased,
        carrier_code=carrier_code,
        carrier_name=carrier_name,
    )


def _stop_one_subscription(subscription: TrackingSubscription, *, label: str) -> bool:
    """Stop one watch. Returns True when it was fully stopped, provider included.

    False means the provider was asked to release its subscription and could not, so the
    watch is parked in ``PAUSED`` for a retry instead of being closed.
    """
    from .services import cancel_tracking_subscription, pause_tracking_subscription
    from .sources import STOP_FAILED, release_provider_subscription

    outcome = release_provider_subscription(subscription)
    if outcome.state == STOP_FAILED:
        pause_tracking_subscription(subscription)
        # The sanitised sentence, never ``outcome.detail``: ``last_error_message`` is
        # rendered on team-facing pages, and a provider's own text can carry our account's
        # plan, allowance or billing. The detail goes to the log below and nowhere else —
        # the same split ``sync.outcome_for_carrier_error`` makes for a failed fetch.
        subscription.last_error_message = outcome.safe_message
        subscription.save(update_fields=["last_error_message", "updated_at"])
        logger.warning(
            "Tracking for %s: %s could not release its subscription (%s). Watch %s paused for retry.",
            label,
            subscription.provider.code,
            outcome.detail,
            subscription.pk,
        )
        return False

    cancel_tracking_subscription(subscription)
    logger.info(
        "Tracking for %s stopped: watch %s (%s) cancelled, provider release %s.",
        label,
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


def _log_start(
    *, team: Team, container: Container, actor, result: RefreshResult, resumed: list, released: list
) -> None:
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
            # Not resumed: their provider resource was released, so the restart went
            # through activation instead.
            "released_subscriptions": [subscription.pk for subscription in released],
        },
    )


def _log_stop(
    *, team: Team, object_type: str, object_id, object_repr: str, actor, stopped: list, unreleased: list
) -> None:
    from apps.scm.audit_log.models import SCMAuditLog
    from apps.scm.audit_log.services import log_scm_action

    log_scm_action(
        team=team,
        action=SCMAuditLog.Action.TRACKING_STOPPED,
        object_type=object_type,
        object_id=object_id,
        object_repr=object_repr,
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
