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
