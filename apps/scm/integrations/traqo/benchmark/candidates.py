"""Choosing which container to spend a live provider request on.

A live Traqo fetch may register a shipment in the account, so it is a consumable. It
must not be spent on a journey that is already over: a completed box has no arrival
left to forecast, so it cannot answer the question Phase 2.2 asks — what a provider's
ETA means *during* a journey and how it moves. Phase 2 already proved that the hard way
on a delivered container.

So the candidate is chosen from data Container SCM already has, before any request, and
the bar is deliberately high enough to fail:

*In transit, by the canonical rule.* ``journey_state_from_observed`` — the same
derivation the visibility read models use — not the subscription status. A subscription
says ACTIVE because nobody cancelled the watch; it says nothing about where the box is.
An ARRIVED or DELIVERED journey is rejected even if its subscription looks healthy.

*A live forecast.* ``get_container_tracking_eta_event`` must return something, which
also means the canonical rule has not already retired the forecast against an actual
arrival. A journey with no forecast gives nothing to compare a provider's ETA against.

*Reference data to compare with.* Enough of the reference provider's own events for the
comparison to measure a difference rather than an absence.

Nothing here fetches, writes or ranks providers. It reads canonical rows and reports a
verdict per container, with the reason — because "no candidate exists" is a real and
useful answer, and one that has to be legible rather than asserted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from apps.scm.tracking.models import TrackingEvent, TrackingSubscription
from apps.scm.visibility.read_models import JourneyState, journey_state_from_observed

# Journey states where the box still has an arrival ahead of it. NOT_DEPARTED is
# included: a container that has not sailed yet has an ETA that will move, which is
# exactly what the experiment measures. UNKNOWN is not — nothing is known about it.
IN_TRANSIT_STATES = (JourneyState.NOT_DEPARTED, JourneyState.IN_TRANSIT)

# Below this, the reference provider has not described enough of the journey for a
# comparison to mean anything. One event is a data point, not a reference timeline.
MIN_REFERENCE_EVENTS = 3

# Sorts a container that has never moved behind every container that has, without
# special-casing None at each comparison.
_NO_MOVEMENT = datetime(1, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class CandidateAssessment:
    """One container weighed as a live-benchmark candidate, and why it did or did not qualify."""

    container_number: str
    container_id: int
    journey_state: str
    latest_actual_milestone: str = ""
    latest_actual_event_at: datetime | None = None
    latest_actual_provider: str = ""
    current_eta_at: datetime | None = None
    eta_event_type: str = ""
    eta_location_name: str = ""
    eta_source: str = ""
    has_future_forecast: bool = False
    subscription_id: int | None = None
    subscription_status: str = ""
    tracking_status: str = ""
    last_synced_at: datetime | None = None
    reference_event_count: int = 0
    has_arrived: bool = False
    rejections: tuple[str, ...] = field(default_factory=tuple)

    @property
    def qualifies(self) -> bool:
        return not self.rejections

    @property
    def journey_state_label(self) -> str:
        return str(JourneyState(self.journey_state).label)

    def as_dict(self) -> dict:
        return {
            "container": self.container_number,
            "journey_state": self.journey_state,
            "latest_actual_milestone": self.latest_actual_milestone,
            "latest_actual_event_at": _iso(self.latest_actual_event_at),
            "latest_actual_provider": self.latest_actual_provider,
            "current_eta_at": _iso(self.current_eta_at),
            "eta_event_type": self.eta_event_type,
            "eta_location_name": self.eta_location_name,
            "eta_source": self.eta_source,
            "has_future_forecast": self.has_future_forecast,
            "subscription_status": self.subscription_status,
            "tracking_status": self.tracking_status,
            "last_synced_at": _iso(self.last_synced_at),
            "reference_event_count": self.reference_event_count,
            "has_arrived": self.has_arrived,
            "qualifies": self.qualifies,
            "rejections": list(self.rejections),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def assess_candidate(
    *,
    team,
    container,
    reference_provider_code: str,
    now: datetime | None = None,
) -> CandidateAssessment:
    """Weigh one container as a live-benchmark candidate against one reference provider."""
    from django.utils import timezone

    from apps.scm.tracking.selectors import (
        get_container_tracking_eta_event,
        get_latest_meaningful_actual_event,
        has_journey_arrived,
    )

    now = now or timezone.now()

    observed = set(
        TrackingEvent.objects.filter(
            team=team,
            container=container,
            event_time_type=TrackingEvent.EventTimeType.ACTUAL,
        )
        .exclude(event_type=TrackingEvent.EventType.UNKNOWN)
        .values_list("event_type", flat=True)
    )
    state = journey_state_from_observed(observed)

    latest_actual = get_latest_meaningful_actual_event(team, container)
    forecast = get_container_tracking_eta_event(team, container)
    arrived = has_journey_arrived(team, container=container)
    subscription = _reference_subscription(team, container, reference_provider_code)
    reference_events = TrackingEvent.objects.filter(
        team=team, container=container, provider__code=reference_provider_code
    ).count()
    # A forecast whose date has passed is not a forecast anyone can measure drift
    # against; it is a stale row the carrier stopped updating.
    future_forecast = forecast is not None and forecast.event_datetime is not None and forecast.event_datetime > now

    rejections: list[str] = []
    if state not in IN_TRANSIT_STATES:
        rejections.append(f"journey state is {state} — the box is not in transit")
    if arrived:
        rejections.append("an actual arrival has been reported for this journey")
    if forecast is None:
        rejections.append("no canonical arrival forecast — nothing to compare an ETA against")
    elif not future_forecast:
        rejections.append("the only arrival forecast is already in the past")
    if subscription is None:
        rejections.append(f"no {reference_provider_code} subscription")
    elif subscription.status == TrackingSubscription.Status.CANCELLED:
        rejections.append(f"the {reference_provider_code} watch was cancelled")
    if reference_events < MIN_REFERENCE_EVENTS:
        rejections.append(
            f"only {reference_events} {reference_provider_code} event(s) stored — "
            f"fewer than the {MIN_REFERENCE_EVENTS} a reference timeline needs"
        )

    return CandidateAssessment(
        container_number=container.container_id,
        container_id=container.pk,
        journey_state=state,
        latest_actual_milestone=latest_actual.event_type if latest_actual else "",
        latest_actual_event_at=latest_actual.event_datetime if latest_actual else None,
        latest_actual_provider=latest_actual.provider.code if latest_actual else "",
        current_eta_at=forecast.event_datetime if forecast else None,
        eta_event_type=forecast.event_type if forecast else "",
        eta_location_name=(forecast.location_name if forecast else ""),
        eta_source=(forecast.provider.code if forecast else ""),
        has_future_forecast=future_forecast,
        subscription_id=subscription.pk if subscription else None,
        subscription_status=subscription.status if subscription else "",
        tracking_status=subscription.tracking_status if subscription else "",
        last_synced_at=subscription.last_synced_at if subscription else None,
        reference_event_count=reference_events,
        has_arrived=arrived,
        rejections=tuple(rejections),
    )


def assess_reference_candidates(
    *,
    team,
    reference_provider_code: str,
    now: datetime | None = None,
) -> list[CandidateAssessment]:
    """Weigh every container the reference provider watches, most recent movement first.

    Ordered by the newest observed movement so the report reads with the liveliest
    journeys at the top, which is where a qualifying candidate would be if one existed.
    """
    containers = _reference_containers(team, reference_provider_code)
    assessments = [
        assess_candidate(
            team=team,
            container=container,
            reference_provider_code=reference_provider_code,
            now=now,
        )
        for container in containers
    ]
    return sorted(
        assessments,
        key=lambda assessment: (
            assessment.qualifies,
            assessment.latest_actual_event_at or _NO_MOVEMENT,
        ),
        reverse=True,
    )


def choose_candidate(assessments: list[CandidateAssessment]) -> CandidateAssessment | None:
    """Return the strongest qualifying candidate, or None when there is none.

    None is a valid and expected outcome. The caller must not fall back to "the least
    bad container": spending a provider request on a finished journey produces evidence
    about nothing, which is worse than producing none.
    """
    qualifying = [assessment for assessment in assessments if assessment.qualifies]
    if not qualifying:
        return None
    return max(qualifying, key=lambda assessment: assessment.latest_actual_event_at or _NO_MOVEMENT)


def _reference_containers(team, reference_provider_code: str) -> list:
    """Return the distinct containers this provider has a subscription for."""
    from apps.scm.containers.models import Container

    container_ids = (
        TrackingSubscription.objects.filter(team=team, provider__code=reference_provider_code)
        .exclude(container__isnull=True)
        .values_list("container_id", flat=True)
    )
    return list(
        Container.objects.filter(team=team, pk__in=set(container_ids)).order_by(
            "owner_code", "category_id", "serial_number"
        )
    )


def _reference_subscription(team, container, reference_provider_code: str):
    """Return the provider's live watch for this container, preferring an uncancelled one."""
    subscriptions = list(
        TrackingSubscription.objects.filter(
            team=team, container=container, provider__code=reference_provider_code
        ).order_by("-created_at")
    )
    for subscription in subscriptions:
        if subscription.status != TrackingSubscription.Status.CANCELLED:
            return subscription
    return subscriptions[0] if subscriptions else None
