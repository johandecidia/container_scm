"""Tracking providers that are not carriers, and what can still be done with them.

Most `TrackingProvider` rows name a shipping line that Container SCM calls directly, and
everything about them is resolved through the carrier registry: which client fetches, which
parser reads the response, whether the scheduled poller drives it. Traqo is not one of
those — it is an aggregator, deliberately absent from that registry (see
``integrations/traqo/README.md``) — and code that only knows "the carrier registry has
never heard of this" cannot tell it apart from a provider that is simply misconfigured.

Those are opposite situations. A misconfigured carrier is a problem to surface; a
non-carrier provider is working exactly as designed and must not be marked untrackable for
it. So this module answers the two questions that difference actually forces, and nothing
more:

* Is this provider driven by the carrier poller? (``is_polled_by_carrier_sync``)
* Which mapper reads a payload it already gave us? (``get_non_carrier_source``)

It does **not** fetch, schedule, or choose a provider for a container. Those are the
decisions a real provider-routing layer would make, and none of them is needed yet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.integrations.traqo import PROVIDER_NAME as TRAQO_PROVIDER_NAME
from apps.scm.integrations.traqo.mapper import map_traqo_container_payload


@dataclass(frozen=True)
class NonCarrierSource:
    """A tracking provider that feeds the canonical pipeline from outside the registry."""

    code: str
    name: str
    read_payload: Callable[[dict, str], list[NormalisedTrackingEvent]]
    refresh_hint: str

    def __str__(self) -> str:
        return self.name


def _read_traqo_payload(payload_json: dict, reference: str) -> list[NormalisedTrackingEvent]:
    return map_traqo_container_payload(payload_json, container_number=reference)


_NON_CARRIER_SOURCES: dict[str, NonCarrierSource] = {
    TRAQO_PROVIDER_CODE: NonCarrierSource(
        code=TRAQO_PROVIDER_CODE,
        name=TRAQO_PROVIDER_NAME,
        read_payload=_read_traqo_payload,
        refresh_hint="run the traqo_test management command",
    ),
}


def get_non_carrier_source(provider_code: str) -> NonCarrierSource | None:
    """Return the known non-carrier source for ``provider_code``, or None."""
    return _NON_CARRIER_SOURCES.get((provider_code or "").strip().lower())


def is_polled_by_carrier_sync(provider_code: str) -> bool:
    """Whether the scheduled carrier poller is the thing that refreshes this provider.

    False does not mean broken. It means the provider's data arrives some other way, so a
    poller that cannot fetch it should step aside rather than record a fault.
    """
    return get_non_carrier_source(provider_code) is None


def non_carrier_provider_codes() -> tuple[str, ...]:
    """Provider codes the carrier poller does not drive, for excluding from its queue."""
    return tuple(_NON_CARRIER_SOURCES)
