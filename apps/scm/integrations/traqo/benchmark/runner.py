"""Running one container's provider comparison end to end.

The order of operations is the experiment's design:

1. read what the reference provider already has — no new carrier call, because the
   stored events *are* what Container SCM knows;
2. optionally refresh the reference through its own existing sync service, only when
   asked for explicitly;
3. fetch and ingest the candidate **once**, through the Phase 1 pipeline and nothing
   else, so the candidate's data arrives exactly as production would receive it;
4. re-read both providers' canonical rows and compare them locally.

Step 3 is the only write, and it is the candidate's ordinary ingestion. No event of
either provider is altered, merged or enriched from the other — see
``benchmark/matching.py`` for why that matters.

A live candidate fetch can register the shipment in the provider's account, so it
happens once per comparison and never as a side effect of reading.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from django.utils import timezone

from apps.scm.tracking.models import TrackingEvent

from .. import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from .matching import DEFAULT_TOLERANCE_HOURS, EventComparison, match_events
from .metrics import (
    EtaMetrics,
    FreshnessMetrics,
    LocationMetrics,
    MatchSummary,
    ProviderEventSummary,
    VesselMetrics,
    eta_for_provider,
    freshness_metrics,
    location_metrics,
    summarise_matches,
    summarise_provider_events,
    vessel_metrics,
)
from .snapshot import build_snapshot

logger = logging.getLogger(__name__)

REFERENCE_PROVIDER_CODE = "maersk"


@dataclass
class ComparisonResult:
    """Everything one benchmark run measured, ready to render or serialise."""

    container_number: str
    team_slug: str
    run_at: datetime
    reference_provider_code: str
    candidate_provider_code: str
    mode: str
    sealine: str

    reference_summary: ProviderEventSummary
    candidate_summary: ProviderEventSummary
    match_summary: MatchSummary
    reference_location: LocationMetrics
    candidate_location: LocationMetrics
    reference_vessel: VesselMetrics
    candidate_vessel: VesselMetrics
    eta: EtaMetrics
    freshness: FreshnessMetrics
    comparisons: list[EventComparison] = field(default_factory=list)
    candidate_ingest_created: int = 0
    candidate_ingest_updated: int = 0
    notes: list[str] = field(default_factory=list)
    # The Phase 2.2 T0 observation: everything a later run needs to be subtracted from
    # this one. Empty when the snapshot could not be built — see ``build_snapshot``.
    snapshot: dict = field(default_factory=dict)

    @property
    def is_sandbox(self) -> bool:
        return self.mode == "sandbox"


def compare_providers(
    *,
    team,
    container,
    sealine: str,
    reference_provider_code: str = REFERENCE_PROVIDER_CODE,
    candidate_provider_code: str = TRAQO_PROVIDER_CODE,
    sandbox: bool = False,
    ingest_candidate: bool = True,
    refresh_reference: bool = False,
    tolerance_hours: int = DEFAULT_TOLERANCE_HOURS,
    client=None,
) -> ComparisonResult:
    """Compare two providers' canonical tracking for one container.

    ``ingest_candidate`` performs exactly one candidate fetch through the Phase 1
    service. Set it False to re-compare what is already stored without spending another
    provider request.

    ``refresh_reference`` runs the reference provider's *own* existing sync service
    first. Off by default: the stored events are what Container SCM knows, and a refresh
    is an extra carrier call that changes what the comparison is measuring.

    Designed to be called once per container, so a caller can loop over several.
    """
    notes: list[str] = []

    if refresh_reference:
        notes.extend(_refresh_reference(team=team, container=container, provider_code=reference_provider_code))

    candidate_before = _provider_event_count(team, container, candidate_provider_code)
    ingest_created = 0
    ingest_updated = 0
    payload_last_updated_at = ""
    candidate_payload: dict | None = None

    if ingest_candidate:
        result = _ingest_candidate(team=team, container=container, sealine=sealine, sandbox=sandbox, client=client)
        ingest_created = result.events_created
        ingest_updated = result.events_updated
        candidate_payload = result.payload
        payload_last_updated_at = str((result.payload.get("data") or {}).get("last_updated_at") or "")
    else:
        notes.append("Candidate was not refetched; comparing what is already stored.")
        # The last response actually received, so a --no-fetch run can still report the
        # provider's ETA framing. It is labelled with its own received_at in the
        # snapshot; nothing here pretends it is current.
        candidate_payload = _stored_candidate_payload(team, container, candidate_provider_code)
        if candidate_payload is None:
            notes.append(
                f"No stored {candidate_provider_code} payload for this container, so the provider's own "
                "ETA framing cannot be reported."
            )

    reference_events, candidate_events = _read_events(
        team=team,
        container=container,
        reference_provider_code=reference_provider_code,
        candidate_provider_code=candidate_provider_code,
    )
    comparisons = match_events(reference_events, candidate_events, tolerance_hours=tolerance_hours)

    reference_provider = _provider(reference_provider_code)
    candidate_provider = _provider(candidate_provider_code)

    result = ComparisonResult(
        container_number=container.container_id,
        team_slug=team.slug,
        run_at=timezone.now(),
        reference_provider_code=reference_provider_code,
        candidate_provider_code=candidate_provider_code,
        mode="sandbox" if sandbox else "production",
        sealine=sealine,
        reference_summary=summarise_provider_events(reference_provider_code, reference_events),
        candidate_summary=summarise_provider_events(candidate_provider_code, candidate_events),
        match_summary=summarise_matches(
            comparisons, reference_events=reference_events, tolerance_hours=tolerance_hours
        ),
        reference_location=location_metrics(reference_provider_code, reference_events),
        candidate_location=location_metrics(candidate_provider_code, candidate_events),
        reference_vessel=vessel_metrics(reference_provider_code, reference_events),
        candidate_vessel=vessel_metrics(candidate_provider_code, candidate_events),
        eta=EtaMetrics(
            reference=(
                eta_for_provider(team=team, container=container, provider=reference_provider)
                if reference_provider
                else _absent_eta(reference_provider_code)
            ),
            candidate=(
                eta_for_provider(team=team, container=container, provider=candidate_provider)
                if candidate_provider
                else _absent_eta(candidate_provider_code)
            ),
            eta_history=_eta_history(team, container),
        ),
        freshness=freshness_metrics(
            comparisons,
            reference_provider=reference_provider_code,
            candidate_provider=candidate_provider_code,
            reference_events=reference_events,
            candidate_events=candidate_events,
            # A candidate with no stored events before this run is being seen for the
            # first time, so its timing figures describe the experiment, not the feed.
            first_observation=candidate_before == 0 and bool(candidate_events),
            candidate_payload_last_updated_at=payload_last_updated_at,
        ),
        comparisons=comparisons,
        candidate_ingest_created=ingest_created,
        candidate_ingest_updated=ingest_updated,
        notes=notes,
    )

    # Built last, from the finished result: the snapshot's ETA and its ETA target must
    # describe the same event the rest of the report describes, and the only way to
    # guarantee that is to derive both from the same computed values.
    result.snapshot = build_snapshot(
        result=result,
        candidate_payload=candidate_payload,
        reference_events=reference_events,
        candidate_events=candidate_events,
        journey_state=_journey_state(team, container),
        reference_subscription=_subscription(team, container, reference_provider_code),
        candidate_subscription=_subscription(team, container, candidate_provider_code),
        eta_history_rows=result.eta.eta_history,
    )
    return result


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _refresh_reference(*, team, container, provider_code: str) -> list[str]:
    """Refresh the reference provider through its own existing sync service.

    Reuses :func:`apps.scm.tracking.sync.sync_tracking_subscription` — the same cycle a
    scheduled poll runs. No carrier transport, parsing or persistence is reimplemented
    here, and a failure is reported rather than raised: the stored events are still
    comparable.
    """
    from apps.scm.tracking.selectors import get_verified_container_subscriptions
    from apps.scm.tracking.sync import sync_tracking_subscription

    notes: list[str] = []
    subscriptions = [
        subscription
        for subscription in get_verified_container_subscriptions(team, container)
        if subscription.provider.code == provider_code
    ]
    if not subscriptions:
        return [f"No {provider_code} subscription to refresh for this container."]

    for subscription in subscriptions:
        try:
            run = sync_tracking_subscription(subscription)
        except Exception as exc:  # noqa: BLE001 — a failed refresh must not lose the comparison
            logger.warning("Benchmark refresh of %s subscription %s failed.", provider_code, subscription.pk)
            notes.append(f"{provider_code} refresh failed ({type(exc).__name__}); compared stored events instead.")
            continue
        if run is None:
            notes.append(f"{provider_code} refresh skipped — a sync was already running.")
        else:
            notes.append(
                f"{provider_code} refreshed: {run.status}, {run.events_created} new, {run.events_updated} updated."
            )
    return notes


def _ingest_candidate(*, team, container, sealine: str, sandbox: bool, client):
    """Fetch and store the candidate once, through the Phase 1 pipeline only."""
    from ..service import ingest_traqo_container

    return ingest_traqo_container(
        team=team,
        container=container,
        sealine=sealine,
        sandbox=sandbox,
        client=client,
    )


def _read_events(
    *, team, container, reference_provider_code: str, candidate_provider_code: str
) -> tuple[list[TrackingEvent], list[TrackingEvent]]:
    """Read both providers' canonical events through the ordinary container selector.

    One query for the container's whole timeline, split by provider in memory — the same
    rows the timeline and position logic read, so the benchmark cannot measure a
    different set of events than the product shows.
    """
    from apps.scm.tracking.selectors import get_tracking_events_for_container

    events = list(get_tracking_events_for_container(team, container))
    reference = [event for event in events if event.provider.code == reference_provider_code]
    candidate = [event for event in events if event.provider.code == candidate_provider_code]
    return reference, candidate


def _provider_event_count(team, container, provider_code: str) -> int:
    return TrackingEvent.objects.filter(team=team, container=container, provider__code=provider_code).count()


def _stored_candidate_payload(team, container, provider_code: str) -> dict | None:
    """Return the last response this provider actually sent for this container, if any.

    Read through the subscription rather than the container, because a raw payload is
    stored against the watch that fetched it. Used only by ``--no-fetch``; a run that
    fetches has the live response in hand and never consults this.
    """
    from apps.scm.tracking.models import TrackingRawPayload

    payload = (
        TrackingRawPayload.objects.filter(
            team=team,
            provider__code=provider_code,
            subscription__container=container,
        )
        .order_by("-received_at", "-pk")
        .first()
    )
    return payload.payload_json if payload and isinstance(payload.payload_json, dict) else None


def _journey_state(team, container) -> str:
    """Return the canonical journey state, so the snapshot cannot invent its own."""
    from apps.scm.visibility.read_models import journey_state_from_observed

    observed = set(
        TrackingEvent.objects.filter(
            team=team,
            container=container,
            event_time_type=TrackingEvent.EventTimeType.ACTUAL,
        )
        .exclude(event_type=TrackingEvent.EventType.UNKNOWN)
        .values_list("event_type", flat=True)
    )
    return journey_state_from_observed(observed)


def _subscription(team, container, provider_code: str):
    from apps.scm.tracking.models import TrackingSubscription

    return (
        TrackingSubscription.objects.filter(team=team, container=container, provider__code=provider_code)
        .order_by("-created_at")
        .first()
    )


def _provider(provider_code: str):
    from apps.scm.tracking.models import TrackingProvider

    return TrackingProvider.objects.filter(code=provider_code).first()


def _absent_eta(provider_code: str):
    from .metrics import ProviderEta

    return ProviderEta(provider_code=provider_code)


def _eta_history(team, container) -> list[dict]:
    """Return the canonical ETA history rows for this container, for drift analysis.

    Read from the existing ETAHistory model; nothing is written. Empty when the container
    is not on a shipment, since that is where the canonical history hangs.
    """
    from apps.scm.tracking.models import ETAHistory

    rows = (
        ETAHistory.objects.filter(team=team, container=container)
        .select_related("tracking_event__provider")
        .order_by("changed_at")[:50]
    )
    history = []
    for row in rows:
        provider = ""
        event = row.tracking_event
        if event is not None and event.provider_id:
            provider = event.provider.code
        history.append(
            {
                "changed_at": row.changed_at.isoformat() if row.changed_at else None,
                "previous_eta_at": row.previous_eta_at.isoformat() if row.previous_eta_at else None,
                "new_eta_at": row.new_eta_at.isoformat() if row.new_eta_at else None,
                "delta_minutes": row.delta_minutes,
                "source": row.source,
                "provider": provider,
            }
        )
    return history
