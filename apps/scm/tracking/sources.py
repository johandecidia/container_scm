"""Tracking providers that are not carriers, and what can still be done with them.

Most `TrackingProvider` rows name a shipping line that Container SCM calls directly, and
everything about them is resolved through the carrier registry: which client fetches, which
parser reads the response, whether the scheduled poller drives it. Traqo and Vizion are not
among them — they are aggregators, deliberately absent from that registry (see
``integrations/traqo/README.md``) — and code that only knows "the carrier registry has
never heard of this" cannot tell them apart from a provider that is simply misconfigured.

Those are opposite situations. A misconfigured carrier is a problem to surface; an
aggregator is working exactly as designed and must not be marked untrackable for it. So
this module answers the questions that difference actually forces, and nothing more:

* Which mapper reads a payload it already gave us? (``get_non_carrier_source``)
* Is it resolved through the carrier registry? (``is_polled_by_carrier_sync``)
* How does the scheduled poller fetch it, if it can at all? (``get_scheduled_provider_sync``)
* Which ones can it not fetch? (``unfetchable_provider_codes``)

The last three answer two different questions that look like one. "Resolved through the
carrier registry" is about *how* a provider is reached — it is what
:mod:`apps.scm.tracking.repair` asks, because the bug it corrects was the carrier
poller's. "Can be fetched on a schedule" is about *whether* the poller should queue it.
Those were the same fact until a subscription could record a ``provider_reference``, and
separating them is what made Traqo schedulable.

**Not in the carrier registry is not the same as not schedulable.** Those were one fact
until a subscription could record ``provider_reference``; now they are two, and the two
aggregators answer them differently:

    Traqo    discovery **and** ongoing tracking — its container endpoint answers about
             the same box again for the cost of a request, and the sealine a watch
             already recorded is all a later fetch needs.
    Vizion   carrier identification only — a reference is its billable unit, so polling
             one on a schedule would turn a cadence into a purchase. Its events are
             stored and correct; the one thing the poller must not do is fetch them.

That is why the capability is per source rather than per "is it an aggregator": a blanket
rule in either direction would either strand Traqo or start buying Vizion references.

It does **not** decide which provider should track a container. That is
:mod:`apps.scm.tracking.provider_routing`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.integrations.traqo import PROVIDER_NAME as TRAQO_PROVIDER_NAME
from apps.scm.integrations.traqo.mapper import map_traqo_container_payload
from apps.scm.integrations.vizion import PROVIDER_CODE as VIZION_PROVIDER_CODE
from apps.scm.integrations.vizion import PROVIDER_NAME as VIZION_PROVIDER_NAME
from apps.scm.integrations.vizion.mapper import read_stored_payload as read_vizion_payload

if TYPE_CHECKING:
    from .models import TrackingSubscription
    from .sync import SyncOutcome


@dataclass(frozen=True)
class NonCarrierSource:
    """A tracking provider that feeds the canonical pipeline from outside the registry."""

    code: str
    name: str
    read_payload: Callable[[dict, str], list[NormalisedTrackingEvent]]
    refresh_hint: str
    # How the scheduled poller refreshes an existing watch on this provider, or None when
    # it must not be polled at all. Takes the subscription and returns the outcome of one
    # cycle — the same contract the carrier branch of ``sync._fetch_normalise_and_store``
    # fulfils, so the engine's run, state and cadence handling is shared rather than
    # reimplemented.
    scheduled_sync: Callable[[TrackingSubscription], SyncOutcome] | None = None

    @property
    def supports_scheduled_tracking(self) -> bool:
        return self.scheduled_sync is not None

    def __str__(self) -> str:
        return self.name


def _read_traqo_payload(payload_json: dict, reference: str) -> list[NormalisedTrackingEvent]:
    return map_traqo_container_payload(payload_json, container_number=reference)


def _read_vizion_payload(payload_json: dict, reference: str) -> list[NormalisedTrackingEvent]:
    return read_vizion_payload(payload_json, reference)


def _sync_traqo(subscription: TrackingSubscription) -> SyncOutcome:
    """Refresh one established Traqo watch. Imported lazily to keep this module a leaf.

    The implementation lives in :mod:`apps.scm.integrations.traqo.scheduled` because it is
    Traqo's, and it imports the tracking sync engine that imports this module.
    """
    from apps.scm.integrations.traqo.scheduled import sync_traqo_subscription

    return sync_traqo_subscription(subscription)


_NON_CARRIER_SOURCES: dict[str, NonCarrierSource] = {
    TRAQO_PROVIDER_CODE: NonCarrierSource(
        code=TRAQO_PROVIDER_CODE,
        name=TRAQO_PROVIDER_NAME,
        read_payload=_read_traqo_payload,
        refresh_hint="refresh the container's tracking",
        scheduled_sync=_sync_traqo,
    ),
    # No ``scheduled_sync``, and that absence is the decision: registering Vizion here is
    # what stops the scheduled sync queueing a Vizion subscription and then marking the
    # container NOT_CONFIGURED — its events are stored and correct, and the only thing the
    # poller cannot do is fetch them. Giving it one would spend a reference per cycle.
    VIZION_PROVIDER_CODE: NonCarrierSource(
        code=VIZION_PROVIDER_CODE,
        name=VIZION_PROVIDER_NAME,
        read_payload=_read_vizion_payload,
        refresh_hint="run the vizion_test management command",
    ),
}


def get_non_carrier_source(provider_code: str) -> NonCarrierSource | None:
    """Return the known non-carrier source for ``provider_code``, or None."""
    return _NON_CARRIER_SOURCES.get((provider_code or "").strip().lower())


def get_scheduled_provider_sync(provider_code: str) -> Callable[[TrackingSubscription], SyncOutcome] | None:
    """Return the non-carrier sync for ``provider_code``, or None.

    None for a carrier — the registry drives those — and None for a non-carrier source
    that must not be polled. Callers distinguish the two with
    :func:`get_non_carrier_source`.
    """
    source = get_non_carrier_source(provider_code)
    return source.scheduled_sync if source is not None else None


def is_polled_by_carrier_sync(provider_code: str) -> bool:
    """Whether this provider is reached through the carrier registry's client and parser.

    False for both aggregators, including the one that *is* now scheduled: Traqo brings its
    own fetch rather than a registry adapter. Deliberately not the same question as
    "can the poller fetch it" (:func:`get_scheduled_provider_sync`): "could the retired
    carrier poller have written this row's status" is about the registry path specifically
    — see :mod:`apps.scm.tracking.repair`, which is this function's only caller.
    """
    return get_non_carrier_source(provider_code) is None


def non_carrier_provider_codes() -> tuple[str, ...]:
    """Provider codes outside the carrier registry, whether or not they are schedulable."""
    return tuple(_NON_CARRIER_SOURCES)


def unfetchable_provider_codes() -> tuple[str, ...]:
    """Provider codes the scheduled sync cannot fetch, for excluding from its queue."""
    return tuple(code for code, source in _NON_CARRIER_SOURCES.items() if not source.supports_scheduled_tracking)
