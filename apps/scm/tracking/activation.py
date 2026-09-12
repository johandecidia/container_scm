"""Making a chosen tracking route real, through the write path that already exists.

:mod:`apps.scm.integrations.carriers.carrier_resolution` decides who the carrier is,
:mod:`.provider_routing` decides who to ask about it, and this module does the asking and
the storing. Splitting it out is what keeps the two decisions pure: neither of them can
create a subscription or spend a call, so neither can be reasoned about incorrectly by
reading its return value.

Every write here is an existing tracking service, in the order every other source uses::

    get_or_create_container_subscription   the watch, on the unchanged natural key
    create_sync_run                        the attempt, so it appears in sync history
    store_verified_carrier_result          raw payload first, then normalised events
    apply_sync_outcome                     close the run and move the subscription

So an activated route's events land in ``TrackingEvent`` through the same fingerprinting
and upsert as Maersk's, and the journey, timeline, position and ETA derivations read them
without knowing which provider produced them. There is no second ingestion path and no
provider-specific event model.

Two ordering rules matter.

**Fetch before write.** A provider outage, a rejected key or an account problem leaves the
container exactly as it was — no subscription, no state change, and above all no effect on
events another source already produced.

**Re-use a payload already in hand.** A step that proves a carrier does it by *fetching*
its events; activating that carrier must not immediately ask the same question again. When
the resolution carries a payload, :func:`activate_tracking_route` stores that instead of
refetching, which is the difference between one provider call per refresh and two. This
holds for both steps that can prove a carrier — a direct sweep and a Traqo candidate
probe — and the resolution says which provider produced the payload rather than each
branch guessing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from apps.scm.integrations.carriers.exceptions import CarrierError

from . import provider_routing
from .manual_refresh import get_or_create_container_subscription, store_discovered_carrier_source
from .models import TrackingSubscription, TrackingSyncRun

if TYPE_CHECKING:
    from apps.scm.containers.models import Container
    from apps.scm.integrations.carriers.carrier_resolution import CarrierResolution
    from apps.teams.models import Team

    from .provider_routing import TrackingRoute

logger = logging.getLogger(__name__)

# What activation achieved. Values, so the caller decides what to say.
ACTIVATED = "activated"
NO_DATA = "no_data"
NOT_CONFIGURED = "not_configured"
UNAVAILABLE = "unavailable"
CARRIER_UNKNOWN = "carrier_unknown"


@dataclass
class ActivationResult:
    """What one attempt to start tracking through a route achieved."""

    state: str
    route: TrackingRoute | None = None
    subscription: TrackingSubscription | None = None
    sync_run: TrackingSyncRun | None = None
    events_created: int = 0
    events_updated: int = 0
    # For the log. May echo a provider response body, so it is never rendered.
    detail: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def activated(self) -> bool:
        return self.state == ACTIVATED

    @property
    def events_seen(self) -> int:
        return self.events_created + self.events_updated

    @property
    def carrier_code(self) -> str:
        return self.route.carrier_code if self.route is not None else ""

    @property
    def carrier_name(self) -> str:
        return self.route.carrier_name if self.route is not None else ""

    @property
    def provider_code(self) -> str:
        return self.route.provider_code if self.route is not None else ""


def activate_tracking_route(
    *,
    team: Team,
    container: Container,
    resolution: CarrierResolution,
    route: TrackingRoute | None = None,
    traqo_client=None,
    traqo_sandbox: bool = False,
) -> ActivationResult:
    """Start tracking ``container`` through the best provider for its resolved carrier.

    ``route`` may be supplied by a caller that has already routed; otherwise it is
    resolved here. ``traqo_client`` and ``traqo_sandbox`` are passed through to the Traqo
    ingest for testing and for sandbox runs.

    Never raises: a provider failure becomes an ``UNAVAILABLE`` result. The container is
    left untouched by anything short of a successful fetch.
    """
    if not resolution.resolved:
        return ActivationResult(state=CARRIER_UNKNOWN, detail="No carrier was resolved for this container.")

    route = route or provider_routing.resolve_tracking_route(
        team=team,
        carrier_code=resolution.carrier_code,
        container=container,
    )

    if not route.available:
        logger.info(
            "Activation for %s: carrier %s resolved, no provider available (%s).",
            container.container_id,
            route.carrier_code,
            route.reason,
        )
        return ActivationResult(state=NOT_CONFIGURED, route=route, detail=route.reason)

    if route.is_direct:
        return _activate_direct(team=team, container=container, resolution=resolution, route=route)

    from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE

    if route.provider_code == TRAQO_PROVIDER_CODE:
        return _activate_traqo(
            team=team,
            container=container,
            resolution=resolution,
            route=route,
            client=traqo_client,
            sandbox=traqo_sandbox,
        )

    # Routing only ever chooses providers it knows how to reach; a new one arriving here
    # without an activation branch is a bug worth reporting as a configuration gap rather
    # than a crash in a web request.
    logger.error("No activation path for provider %s (carrier %s).", route.provider_code, route.carrier_code)
    return ActivationResult(state=NOT_CONFIGURED, route=route, detail=f"No activation path for {route.provider_code}.")


# ---------------------------------------------------------------------------
# Direct carriers
# ---------------------------------------------------------------------------


def _activate_direct(*, team, container, resolution: CarrierResolution, route: TrackingRoute) -> ActivationResult:
    """Record a direct carrier as this container's tracking source.

    The fast path is the important one. When the resolution came from direct discovery it
    already holds the events and the raw payload that proved the carrier, so this stores
    them through ``store_discovered_carrier_source`` — the same function continuation
    discovery uses — rather than calling the carrier a second time for a payload it just
    returned.

    Without a payload in hand — a carrier resolved by Traqo's lookup that also happens to
    have a working direct adapter — the ordinary sync cycle does the fetch, which is the
    same code the scheduled poller runs.
    """
    # ``discovery is not None`` is what makes this the sweep's own payload rather than
    # any payload: a Traqo probe also carries events, and they are not this carrier's.
    if resolution.has_tracking_payload and resolution.discovery is not None:
        subscription, sync_run = store_discovered_carrier_source(
            team=team,
            container=container,
            outcome=resolution.discovery,
        )
        if subscription is None or sync_run is None:
            return ActivationResult(state=NOT_CONFIGURED, route=route, detail="Provider row could not be resolved.")
        return _from_sync_run(route=route, subscription=subscription, sync_run=sync_run)

    from .models import CarrierSource
    from .sync import sync_tracking_subscription

    subscription = get_or_create_container_subscription(
        team=team,
        container=container,
        provider_code=route.provider_code,
        provider_name=route.provider_name,
        carrier_code=route.carrier_code,
        carrier_name=route.carrier_name,
        carrier_source=resolution.source or CarrierSource.DIRECT_API,
    )
    if subscription is None:
        return ActivationResult(state=NOT_CONFIGURED, route=route, detail="Provider row could not be resolved.")

    sync_run = sync_tracking_subscription(subscription)
    if sync_run is None:
        return ActivationResult(
            state=UNAVAILABLE,
            route=route,
            subscription=subscription,
            detail="A sync for this subscription is already running.",
        )
    return _from_sync_run(route=route, subscription=subscription, sync_run=sync_run)


# ---------------------------------------------------------------------------
# Traqo
# ---------------------------------------------------------------------------


def _activate_traqo(
    *,
    team,
    container,
    resolution: CarrierResolution,
    route: TrackingRoute,
    client,
    sandbox: bool,
) -> ActivationResult:
    """Start tracking through Traqo, telling it which carrier to ask about.

    The sealine is the point. Traqo requires one, routing has already worked out which
    one this carrier maps to, and sending it means Traqo is asked about ONE rather than
    guessing — the carrier stays Container SCM's own answer even though the data is
    Traqo's.

    When the resolution already holds a Traqo payload — a candidate probe established the
    carrier *by* fetching it — that payload is stored and no request is made. Anything
    else would spend a second shipment call to be told what is already in hand.

    Idempotent by construction: both paths go through the same ``get_or_create`` natural
    key as every other source, so a double click produces one Traqo watch and one further
    sync run rather than two watches.
    """
    from apps.scm.integrations.traqo.service import ingest_traqo_container

    from .models import CarrierSource

    if _carries_traqo_payload(resolution):
        return _activate_traqo_from_payload(
            team=team,
            container=container,
            resolution=resolution,
            route=route,
            sandbox=sandbox,
        )

    sealine = route.provider_reference
    if not sealine:  # pragma: no cover — routing only returns Traqo with a sealine
        return ActivationResult(state=NOT_CONFIGURED, route=route, detail="Traqo needs a sealine and none was routed.")

    try:
        ingest = ingest_traqo_container(
            team=team,
            container=container,
            sealine=sealine,
            sandbox=sandbox,
            client=client,
            carrier_code=route.carrier_code,
            carrier_name=route.carrier_name,
            carrier_source=resolution.source or CarrierSource.TRAQO_LOOKUP,
        )
    except CarrierError as exc:
        # Nothing was written: the fetch happens before any persistence. The container is
        # exactly as it was and can be routed here again later.
        logger.warning(
            "Traqo activation for %s (carrier %s, sealine %s) failed: %s (%s).",
            container.container_id,
            route.carrier_code,
            sealine,
            type(exc).__name__,
            exc,
        )
        return ActivationResult(
            state=_state_for_carrier_error(exc),
            route=route,
            detail=f"{type(exc).__name__}: {exc}",
        )

    result = _from_sync_run(route=route, subscription=ingest.subscription, sync_run=ingest.sync_run)
    result.metadata = {"sealine": sealine, "events_mapped": ingest.events_mapped}
    return result


def _carries_traqo_payload(resolution: CarrierResolution) -> bool:
    """Whether this resolution already holds a Traqo container payload to store.

    All three conditions matter. There must be events; they must have come from Traqo
    rather than from a direct sweep; and Traqo must have said which sealine answered, so
    the watch records the handle a later fetch needs. Anything less and the ordinary
    fetch-then-store path is the right one.
    """
    from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE

    return bool(
        resolution.has_tracking_payload
        and resolution.tracking_provider_code == TRAQO_PROVIDER_CODE
        and resolution.provider_reference
    )


def _activate_traqo_from_payload(
    *,
    team,
    container,
    resolution: CarrierResolution,
    route: TrackingRoute,
    sandbox: bool,
) -> ActivationResult:
    """Record a Traqo answer that has already been fetched, without fetching it again.

    The same writes the ordinary Traqo path makes, because it is literally the same
    function: ``store_traqo_container_result`` is the second half of
    ``ingest_traqo_container``, reached here with the response the probe already got.
    Subscription, sync run, raw payload, events and the ETA observation all land exactly
    as they would have — the only difference is that no request is sent.

    The sealine stored is the one the probe *answered* under, not the one routing would
    choose. They agree today; if they ever disagree, the one that returned this
    container's data is the one a scheduled refresh must ask with.
    """
    from apps.scm.integrations.traqo.service import TraqoContainerResponse, store_traqo_container_result

    from .models import CarrierSource

    sealine = resolution.provider_reference
    response = TraqoContainerResponse(
        container_number=container.container_id,
        sealine=sealine,
        payload=resolution.raw_payload,
        events=tuple(resolution.events),
        sandbox=sandbox,
    )
    ingest = store_traqo_container_result(
        team=team,
        container=container,
        response=response,
        carrier_code=route.carrier_code,
        carrier_name=route.carrier_name,
        carrier_source=resolution.source or CarrierSource.TRAQO_PROBE,
    )

    logger.info(
        "Traqo activation for %s (carrier %s, sealine %s) re-used the payload the probe fetched.",
        container.container_id,
        route.carrier_code,
        sealine,
    )
    result = _from_sync_run(route=route, subscription=ingest.subscription, sync_run=ingest.sync_run)
    result.metadata = {"sealine": sealine, "events_mapped": ingest.events_mapped, "payload_reused": True}
    return result


def _state_for_carrier_error(exc: CarrierError) -> str:
    """A configuration problem and an outage need different advice, so keep them apart."""
    from apps.scm.integrations.carriers.exceptions import (
        CarrierConfigurationError,
        CarrierNoDataError,
        CarrierNotImplementedError,
        CarrierUnsupportedReferenceError,
    )

    if isinstance(exc, CarrierNoDataError):
        # A real answer: Traqo has no shipment for this container under this sealine.
        return NO_DATA
    if isinstance(exc, (CarrierConfigurationError, CarrierNotImplementedError, CarrierUnsupportedReferenceError)):
        return NOT_CONFIGURED
    return UNAVAILABLE


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def _from_sync_run(*, route, subscription, sync_run) -> ActivationResult:
    """Read a finished run into an activation result.

    Zero events is not a failure. The provider answered and has nothing for this
    reference yet, which is a valid answer and leaves the watch in place — a source that
    goes quiet for one call has not stopped being this container's source.
    """
    if sync_run is None:  # pragma: no cover — ingest always creates one
        return ActivationResult(state=UNAVAILABLE, route=route, subscription=subscription)

    statuses = TrackingSyncRun.Status
    total = sync_run.events_created + sync_run.events_updated
    common = {
        "route": route,
        "subscription": subscription,
        "sync_run": sync_run,
        "events_created": sync_run.events_created,
        "events_updated": sync_run.events_updated,
        "detail": sync_run.error_message or "",
    }

    if sync_run.status == statuses.SKIPPED:
        return ActivationResult(state=NOT_CONFIGURED, **common)
    if sync_run.status == statuses.FAILED:
        return ActivationResult(state=UNAVAILABLE, **common)
    if not total:
        return ActivationResult(state=NO_DATA, **common)
    return ActivationResult(state=ACTIVATED, **common)
