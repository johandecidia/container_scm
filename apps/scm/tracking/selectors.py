# Tracking selectors — all read/query operations.
# Every function that returns team-owned data must accept `team` as first argument.
from django.db import models
from django.utils import timezone

from apps.teams.models import Team

from .models import TrackingEvent, TrackingProvider, TrackingSubscription, TrackingSyncRun


def get_team_tracking_providers(team: Team):  # noqa: ARG001 — providers are global, team arg kept for API consistency
    """Return all active tracking providers (global, not team-scoped)."""
    return TrackingProvider.objects.filter(is_active=True).order_by("name")


def get_team_tracking_subscriptions(team: Team):
    """Return all tracking subscriptions for a team."""
    return (
        TrackingSubscription.objects.filter(team=team)
        .select_related("provider", "shipment", "container")
        .order_by("-created_at")
    )


def get_tracking_subscription_for_team(team: Team, subscription_id: int) -> TrackingSubscription:
    """Return a single tracking subscription, scoped to the team."""
    return TrackingSubscription.objects.select_related("provider", "shipment", "container").get(
        team=team, pk=subscription_id
    )


def get_tracking_events_for_team(team: Team):
    """Return all tracking events for a team."""
    return (
        TrackingEvent.objects.filter(team=team)
        .select_related("provider", "subscription", "shipment", "container")
        .order_by("-event_datetime", "-created_at")
    )


def get_tracking_events_for_shipment(team: Team, shipment):
    """Return all tracking events for a specific shipment, scoped to the team."""
    return (
        TrackingEvent.objects.filter(team=team, shipment=shipment)
        .select_related("provider", "subscription", "container")
        .order_by("-event_datetime", "-created_at")
    )


def get_tracking_events_for_container(team: Team, container):
    """Return all tracking events for a specific container, scoped to the team."""
    return (
        TrackingEvent.objects.filter(team=team, container=container)
        .select_related("provider", "subscription", "shipment")
        .order_by("-event_datetime", "-created_at")
    )


def get_latest_tracking_event_for_shipment(team: Team, shipment) -> TrackingEvent | None:
    """Return the most recent tracking event for a shipment."""
    return (
        TrackingEvent.objects.filter(team=team, shipment=shipment)
        .select_related("provider")
        .order_by("-event_datetime", "-created_at")
        .first()
    )


# ---------------------------------------------------------------------------
# Container-level derivation
#
# A container that is tracked without being on a shipment still has a status and an
# arrival forecast — they are just not on any row of any table. Both are derived
# here, from the container's own events, so nothing has to be stored twice and a
# standalone tracked container is not a second-class citizen in the UI.
# ---------------------------------------------------------------------------

# Estimated events that forecast an arrival. A forecast departure is not an ETA.
# Public because the bulk workspace builder applies the same rule to many
# containers at once, and two copies of this tuple would eventually disagree.
ARRIVAL_FORECAST_EVENT_TYPES = (TrackingEvent.EventType.VESSEL_ARRIVED, TrackingEvent.EventType.ETA_UPDATED)


def get_latest_meaningful_actual_event(team: Team, container) -> TrackingEvent | None:
    """Return the container's most recent classified, observed event.

    Three filters, each load-bearing:

    *actual* — a forecast says where the carrier expects the box to be, which is not
    where it is. A status derived from an estimate would report arrival before it
    happened.

    *classified* — an event we could not map has no status to offer; skipping it lets
    the last event we do understand stand, instead of blanking the status.

    *most recent* — not the furthest point in a nominal progression. Carriers reuse
    codes across a journey (a box is gated in on export and again on empty return),
    so ranking codes would report an earlier movement as the current state.
    """
    return (
        TrackingEvent.objects.filter(
            team=team,
            container=container,
            event_time_type=TrackingEvent.EventTimeType.ACTUAL,
        )
        .exclude(event_type=TrackingEvent.EventType.UNKNOWN)
        .exclude(event_datetime__isnull=True)
        .select_related("provider")
        .order_by("-event_datetime", "-created_at")
        .first()
    )


def get_container_tracking_eta_event(team: Team, container) -> TrackingEvent | None:
    """Return the carrier's current arrival forecast for a container, or None.

    The latest ESTIMATED or PLANNED arrival event — but only while it is still a
    forecast. Once the carrier reports an *actual* arrival at or after it, the
    forecast has been answered and showing it as an ETA would contradict what
    happened, which is the same rule the shipment ETA already follows.
    """
    events = TrackingEvent.objects.filter(team=team, container=container).exclude(event_datetime__isnull=True)

    forecast = (
        events.filter(
            event_time_type__in=[TrackingEvent.EventTimeType.ESTIMATED, TrackingEvent.EventTimeType.PLANNED],
            event_type__in=ARRIVAL_FORECAST_EVENT_TYPES,
        )
        .select_related("provider")
        .order_by("-event_datetime", "-created_at")
        .first()
    )
    if forecast is None:
        return None

    has_arrived = events.filter(
        event_time_type=TrackingEvent.EventTimeType.ACTUAL,
        event_type__in=(TrackingEvent.EventType.VESSEL_ARRIVED, TrackingEvent.EventType.DISCHARGED),
    ).exists()
    return None if has_arrived else forecast


# A subscription left in SYNCING for longer than this is assumed to belong to a
# worker that died; the sync lock — not this status — prevents double runs.
STALE_SYNCING_MINUTES = 60


def get_due_tracking_subscriptions(team: Team | None = None):
    """Return subscriptions that are due for syncing.

    A subscription is due when:
    - status is ACTIVE or FAILED, or it has been stuck in SYNCING long enough that
      the worker holding it is presumed dead (otherwise a crashed sync would
      starve the subscription forever), and
    - next_sync_at is in the past or null.

    Concurrency is prevented by the sync lock, not by the SYNCING status.
    """
    now = timezone.now()
    stale_cutoff = now - timezone.timedelta(minutes=STALE_SYNCING_MINUTES)
    runnable = models.Q(status__in=[TrackingSubscription.Status.ACTIVE, TrackingSubscription.Status.FAILED]) | models.Q(
        status=TrackingSubscription.Status.SYNCING, updated_at__lte=stale_cutoff
    )

    qs = TrackingSubscription.objects.filter(runnable).filter(
        models.Q(next_sync_at__isnull=True) | models.Q(next_sync_at__lte=now)
    )
    if team is not None:
        qs = qs.filter(team=team)
    return qs.select_related("provider", "team", "shipment", "container")


def get_tracking_sync_runs_for_subscription(team: Team, subscription: TrackingSubscription):
    """Return sync run history for a subscription, scoped to the team."""
    return (
        TrackingSyncRun.objects.filter(team=team, subscription=subscription)
        .select_related("provider")
        .order_by("-started_at", "-created_at")
    )
