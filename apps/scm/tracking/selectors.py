# Tracking selectors — all read/query operations.
# Every function that returns team-owned data must accept `team` as first argument.
from datetime import timedelta

from django.db import models
from django.utils import timezone

from apps.scm.integrations.carriers.registry import (
    UnknownCarrierError,
    get_carrier_definition,
    resolve_carrier_code,
)
from apps.teams.models import Team

from .models import TrackingEvent, TrackingProvider, TrackingSubscription, TrackingSyncRun
from .sources import unfetchable_provider_codes


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
    """Return all tracking events for a specific container, scoped to the team.

    Every provider's events, not the current one's. A container can be tracked by
    several carriers over one physical journey — an ocean carrier for the sea leg, a
    second for the onward move — and each of them describes a part of the same trip.
    Filtering to one provider would delete the rest of the journey from the screen.
    """
    return (
        TrackingEvent.objects.filter(team=team, container=container)
        .select_related("provider", "subscription", "shipment")
        .order_by("-event_datetime", "-created_at")
    )


# The watch statuses that mean "we are still tracking this".
#
# Stated once, because "are we tracking this box" is asked by the container panel, by
# the Control Tower's Tracking view and by anything that comes after them, and two
# answers to it would put a container in one list and not the other.
#
# It is the sync engine's own runnable set — see :func:`get_due_tracking_subscriptions`
# — expressed as statuses rather than as due-ness, so a read model can ask whether a
# container is being watched without also asking whether it is due this minute.
#
# Each exclusion is a refusal rather than an oversight:
#
# ``CANCELLED``
#     Somebody stopped this watch on purpose. Counting it would report tracking that
#     was deliberately switched off.
# ``COMPLETED``
#     The leg it covered is over. Its events remain part of the journey — see
#     :func:`get_verified_container_subscriptions` — but nothing is coming from it.
# ``PAUSED``
#     The watch exists and nothing is being fetched for it. "Suspended" is a true
#     answer and "tracking" is not.
#
# FAILED and SYNCING are included, both deliberately. A failing watch is a container
# we believe we are tracking and are not, which is precisely what a control tower
# exists to surface; and a watch mid-sync must not blink out of the list for the
# duration of its own refresh.
LIVE_SUBSCRIPTION_STATUSES: tuple[str, ...] = (
    TrackingSubscription.Status.ACTIVE,
    TrackingSubscription.Status.SYNCING,
    TrackingSubscription.Status.FAILED,
)


def has_live_subscription(subscriptions) -> bool:
    """True when any of *subscriptions* is a watch still being run.

    The in-Python form of :data:`LIVE_SUBSCRIPTION_STATUSES`, for callers that have
    already loaded a container's watches — the workspace builders load all of them in
    bulk, so asking the database again would be a query per container on the one page
    that covers a whole fleet.
    """
    return any(subscription.status in LIVE_SUBSCRIPTION_STATUSES for subscription in subscriptions)


def get_verified_container_subscriptions(team: Team, container) -> list[TrackingSubscription]:
    """Return every tracking source this container has proved, oldest first.

    A subscription is only ever created once a carrier has answered with data, so
    each one is a verified source — and there can be more than one, because a box
    changes hands. Cancelled watches are excluded: someone stopped that source
    deliberately. Everything else is kept, including COMPLETED sources whose leg is
    over, because their events are still part of the journey.

    Oldest first, so the list reads in the order the sources took over the box.
    """
    return list(
        TrackingSubscription.objects.filter(team=team, container=container)
        .exclude(status=TrackingSubscription.Status.CANCELLED)
        .select_related("provider", "shipment")
        .order_by("created_at")
    )


# ---------------------------------------------------------------------------
# Provenance
#
# Three questions the system has to be able to answer separately, because the answers
# can be three different names:
#
#     Who is the carrier?              ONE
#     How do we know?                  Vizion ACI
#     Who supplies the tracking data?  Traqo
#
# Before carrier identity lived on the subscription there was only one name available —
# the provider's — and it was used for all three. Anything reading these must therefore
# go through the read model below rather than the provider row, or it will quietly go
# back to answering the first question with the third one's answer.
# ---------------------------------------------------------------------------


class TrackingProvenance:
    """Who is carrying a container, how that was established, and who supplies the data.

    A thin read model over one subscription. Deliberately not a dataclass built by hand
    at each call site: the fallbacks — an unrecorded carrier, a provider that is also the
    carrier — have to read the same way everywhere, and the one that matters most is that
    an unknown carrier reads as unknown rather than as the aggregator's name.
    """

    __slots__ = ("subscription",)

    def __init__(self, subscription: TrackingSubscription) -> None:
        self.subscription = subscription

    @property
    def carrier_code(self) -> str:
        """The carrier's registry code, falling back only where that is not a guess.

        A watch whose provider *is* a registered carrier needs no recorded carrier to
        answer this: Maersk supplying the data and Maersk carrying the box are the same
        fact, and reading the provider code as a carrier code there is exact rather than
        inferred. This is what keeps every direct watch created before carrier identity
        existed — and every one created by code that has no carrier to pass — reading
        correctly.

        For an aggregator the fallback is refused, because there it *would* be a guess,
        and a specific wrong one: it would name Traqo as the carrier.
        """
        recorded = self.subscription.carrier_code
        if recorded:
            return recorded
        return resolve_carrier_code(self.provider_code) or ""

    @property
    def carrier_name(self) -> str:
        """The carrier, or "" when none has been established for this watch."""
        if self.subscription.carrier_code:
            return self.subscription.carrier_label
        code = self.carrier_code
        if not code:
            return ""
        # The registry's name rather than the provider row's, so the carrier is spelled
        # the same way everywhere however a provider row happened to be labelled.
        try:
            return get_carrier_definition(code).name
        except UnknownCarrierError:  # pragma: no cover — the code came from the registry
            return self.provider_name

    @property
    def carrier_known(self) -> bool:
        return bool(self.carrier_code)

    @property
    def carrier_recorded(self) -> bool:
        """True when the carrier was established and stored, rather than derived here.

        The difference matters for provenance: only a recorded carrier has a
        ``carrier_source`` saying how it was established.
        """
        return bool(self.subscription.carrier_code)

    @property
    def carrier_source(self) -> str:
        return self.subscription.carrier_source

    @property
    def carrier_source_label(self) -> str:
        """How we know, in words — "Vizion Auto Carrier Identification"."""
        return self.subscription.get_carrier_source_display() if self.subscription.carrier_source else ""

    @property
    def provider_code(self) -> str:
        return self.subscription.provider.code if self.subscription.provider_id else ""

    @property
    def provider_name(self) -> str:
        provider = self.subscription.provider if self.subscription.provider_id else None
        return (provider.name or provider.code) if provider is not None else ""

    @property
    def is_direct(self) -> bool:
        """True when the carrier itself supplies the data."""
        return self.carrier_known and self.carrier_code == self.provider_code

    @property
    def provider_label(self) -> str:
        """How to describe the data source in one phrase.

        "Direct API" for a carrier watching its own box, and the provider's name
        otherwise. Never the carrier's name for an aggregator watch, which is the whole
        reason this is derived in one place.
        """
        from django.utils.translation import gettext

        if self.is_direct:
            return gettext("Direct API")
        return self.provider_name

    @property
    def provider_reference(self) -> str:
        return self.subscription.provider_reference

    def __str__(self) -> str:
        return f"{self.carrier_name or 'unknown carrier'} via {self.provider_label}"


def get_container_tracking_provenance(team: Team, container) -> list[TrackingProvenance]:
    """Return provenance for every verified source this container has, oldest first.

    One entry per source, because a container legitimately has several — an ocean carrier
    for the sea leg, an aggregator for the onward move — and each answers the three
    questions differently. Collapsing them to one would have to pick a winner, and there
    is not always one to pick.
    """
    return [TrackingProvenance(subscription) for subscription in get_verified_container_subscriptions(team, container)]


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


# The *actual* events that answer an arrival forecast. One tuple, because "has this
# journey arrived" has to mean the same thing to the polling cadence, the container ETA
# derivation and the ETA observation intake.
ARRIVAL_ACTUAL_EVENT_TYPES = (TrackingEvent.EventType.VESSEL_ARRIVED, TrackingEvent.EventType.DISCHARGED)


def has_journey_arrived(team: Team, *, shipment=None, container=None) -> bool:
    """True when arrival has actually been reported for this journey.

    The shipment's own milestone is the cheaper and more authoritative answer where
    there is a shipment. A container tracked on its own has no shipment to carry that
    milestone, so its events are asked directly — otherwise a standalone container
    would look permanently in transit.
    """
    if shipment is not None:
        return shipment.actual_arrival_at is not None
    if container is None:
        return False
    return TrackingEvent.objects.filter(
        team=team,
        container=container,
        event_time_type=TrackingEvent.EventTimeType.ACTUAL,
        event_type__in=ARRIVAL_ACTUAL_EVENT_TYPES,
    ).exists()


def get_container_tracking_eta_event(team: Team, container, *, provider=None) -> TrackingEvent | None:
    """Return the carrier's current arrival forecast for a container, or None.

    The latest ESTIMATED or PLANNED arrival event — but only while it is still a
    forecast. Once the carrier reports an *actual* arrival at or after it, the
    forecast has been answered and showing it as an ETA would contradict what
    happened, which is the same rule the shipment ETA already follows.

    ``provider`` narrows both halves of that rule to one source, answering "what would
    this container's ETA be if only this provider existed". Left None — as every
    production caller does — every provider's events count, because a container tracked
    by several sources has one arrival, not one per source.
    """
    events = TrackingEvent.objects.filter(team=team, container=container).exclude(event_datetime__isnull=True)
    if provider is not None:
        events = events.filter(provider=provider)

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
        event_type__in=ARRIVAL_ACTUAL_EVENT_TYPES,
    ).exists()
    return None if has_arrived else forecast


# A subscription left in SYNCING for longer than this is assumed to belong to a
# worker that died; the sync lock — not this status — prevents double runs.
STALE_SYNCING_MINUTES = 60


def get_due_tracking_subscriptions(team: Team | None = None):
    """Return subscriptions that are due for syncing.

    A subscription is due when:
    - its provider is one the scheduled sync can actually fetch, and
    - status is ACTIVE or FAILED, or it has been stuck in SYNCING long enough that
      the worker holding it is presumed dead (otherwise a crashed sync would
      starve the subscription forever), and
    - next_sync_at is in the past or null.

    The provider exclusion is about *fetchability*, not about being a carrier: Traqo is
    outside the carrier registry and is polled here like any other source, because a watch
    that recorded its sealine can be asked the same question again. Vizion is excluded —
    see :mod:`apps.scm.tracking.sources` — and excluded here rather than skipped later,
    because a skip per cycle forever is noise: the run would be correct and useless.
    Calling ``sync_tracking_subscription`` for one directly still skips safely.

    Concurrency is prevented by the sync lock, not by the SYNCING status.
    """
    now = timezone.now()
    stale_cutoff = now - timedelta(minutes=STALE_SYNCING_MINUTES)
    runnable = models.Q(status__in=[TrackingSubscription.Status.ACTIVE, TrackingSubscription.Status.FAILED]) | models.Q(
        status=TrackingSubscription.Status.SYNCING, updated_at__lte=stale_cutoff
    )

    qs = (
        TrackingSubscription.objects.filter(runnable)
        .exclude(provider__code__in=unfetchable_provider_codes())
        .filter(models.Q(next_sync_at__isnull=True) | models.Q(next_sync_at__lte=now))
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
