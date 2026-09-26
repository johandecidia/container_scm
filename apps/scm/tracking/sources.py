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
* Does stopping a watch have to be reported to it? (``release_provider_subscription``)
* How much of our account with it is left? (``get_provider_usage``)

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

**Stopping is per source for exactly the same reason, and the two aggregators differ
again.** When :mod:`apps.scm.tracking.lifecycle` stops watching a container, whether
anything has to be said to the provider is a fact about that provider's contract:

    Vizion   a reference is the billable unit and stays active until it is released, so
             stopping locally while leaving it subscribed goes on costing money.
             ``DELETE /references/{id}`` is published for this and the client already
             implements it — see ``integrations/vizion/README.md``.
    Traqo    publishes ``DELETE /shipments/{id}``, which takes the shipment id *or*
             the container number the shipment is tracked by. It stops the shipment
             being updated; it does **not** return the billing slot, because the
             allowance counts references added during the cycle. So untracking ends the
             tracking, not the charge — see ``integrations/traqo/client.py``.

Direct carriers are the ones with nothing to withdraw. ``CarrierCapability`` has a
``supports_subscriptions`` flag, but it describes what a carrier's API offers on paper
and no adapter implements a subscribe or unsubscribe call — each one is a pull against
a container number. So stopping a direct watch is local by nature rather than by
omission.

It does **not** decide which provider should track a container. That is
:mod:`apps.scm.tracking.provider_routing`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from apps.scm.integrations.carriers.dcsa.schemas import NormalisedTrackingEvent
from apps.scm.integrations.carriers.exceptions import CarrierError
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.integrations.traqo import PROVIDER_NAME as TRAQO_PROVIDER_NAME
from apps.scm.integrations.traqo.mapper import map_traqo_container_payload
from apps.scm.integrations.vizion import PROVIDER_CODE as VIZION_PROVIDER_CODE
from apps.scm.integrations.vizion import PROVIDER_NAME as VIZION_PROVIDER_NAME
from apps.scm.integrations.vizion.mapper import read_stored_payload as read_vizion_payload

if TYPE_CHECKING:
    from .models import TrackingSubscription
    from .sync import SyncOutcome

logger = logging.getLogger(__name__)


# What asking a provider to stop watching a container achieved. Four values, because the
# lifecycle has to treat them differently and collapsing any two would lose money or
# lose the ability to retry.
#
# ``STOP_RELEASED``      the provider was told and confirmed. Nothing is still charged.
# ``STOP_NOT_REQUIRED``  there is nothing to release — the provider keeps no subscription
#                        of its own, or this watch never recorded a handle for one.
# ``STOP_NOT_CONFIGURED``the provider cannot be reached at all, so it was never asked.
#                        Distinct from a failure because no retry can fix it: nobody can
#                        release a reference without a credential, and refusing to record
#                        the stop would leave the container permanently unstoppable.
# ``STOP_FAILED``        we asked and the call failed. The only retryable one.
STOP_RELEASED = "released"
STOP_NOT_REQUIRED = "not_required"
STOP_NOT_CONFIGURED = "not_configured"
STOP_FAILED = "failed"


@dataclass(frozen=True)
class ProviderStopOutcome:
    """What one attempt to release a provider's own subscription achieved."""

    state: str
    # For the log only. Providers' own messages: free of credentials, but not of
    # everything else — Traqo's can carry our account's plan, allowance and billing.
    detail: str = ""
    # What may be stored on the watch's ``last_error_message``, which team-facing pages
    # render. The failing error's own ``safe_message`` — the same boundary the sync
    # engine persists through — and never derived from ``detail``.
    safe_message: str = CarrierError.safe_message_template

    @property
    def released(self) -> bool:
        return self.state == STOP_RELEASED


# How much of an account is left, as a status rather than a number. Ours, for a page:
# it is not a state Traqo reports and nothing decides anything from it.
USAGE_OK = "ok"
USAGE_WARNING = "warning"
USAGE_EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class ProviderUsage:
    """How much of our account with one provider is spent this billing cycle.

    Platform information, not tenant information. Every field here describes the
    installation's own commercial relationship with a provider — the plan we bought, the
    allowance on it, what it has cost so far — and none of it belongs to any team. It is
    read by one superuser-only view and must reach nothing else; see
    ``tracking/platform_views.py``.

    ``used`` and ``active`` are kept as the two different numbers they are. See
    ``integrations/traqo/usage.py`` for why ``used 25, active 10, remaining 0`` is an
    ordinary state and why treating ``active`` as the spend would overstate capacity.
    """

    provider_code: str
    provider_name: str
    plan: str = ""
    period_days: int = 0
    cycle_start: datetime | None = None
    cycle_end: datetime | None = None
    limit: int = 0
    addon_slots: int = 0
    carried_slots: int = 0
    effective_limit: int = 0
    used: int = 0
    active: int = 0
    remaining: int = 0
    # True when these are the provider's fixed demo numbers rather than our account's.
    sandbox: bool = False

    @property
    def status(self) -> str:
        """``USAGE_OK`` / ``USAGE_WARNING`` / ``USAGE_EXHAUSTED``.

        A presentation grade over the numbers, decided here so the template holds no
        thresholds. Exhausted is exactly zero remaining rather than a near-zero band,
        because the provider's own answer to an activation is what actually decides
        whether tracking can start — this only decides how loud the page is.
        """
        from apps.scm.integrations.traqo.usage import WARNING_REMAINING_FRACTION

        if self.remaining <= 0:
            return USAGE_EXHAUSTED
        if self.effective_limit > 0 and self.remaining <= self.effective_limit * WARNING_REMAINING_FRACTION:
            return USAGE_WARNING
        return USAGE_OK

    @property
    def is_exhausted(self) -> bool:
        return self.status == USAGE_EXHAUSTED

    def __str__(self) -> str:
        return f"{self.provider_name}: {self.used}/{self.effective_limit} used, {self.remaining} remaining"


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
    # How a watch on this provider is withdrawn *at the provider*, or None when there is
    # nothing to withdraw. None is the honest answer for a provider whose contract has no
    # such call — see this module's docstring.
    stop_tracking: Callable[[TrackingSubscription], ProviderStopOutcome] | None = None
    # How much of our account with this provider is left, or None when it publishes no
    # such endpoint. Installation-wide, so it takes no team: these aggregators are one
    # account for the whole deployment, which is exactly why the answer is superuser-only.
    account_usage: Callable[[], object] | None = None

    @property
    def supports_scheduled_tracking(self) -> bool:
        return self.scheduled_sync is not None

    @property
    def requires_external_stop(self) -> bool:
        """Whether stopping a watch here has to be reported to the provider."""
        return self.stop_tracking is not None

    @property
    def reports_usage(self) -> bool:
        """Whether this provider can say how much of our account is left."""
        return self.account_usage is not None

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


def _stop_vizion(subscription: TrackingSubscription) -> ProviderStopOutcome:
    """Release one Vizion reference. Imported lazily, like every other provider call."""
    from apps.scm.integrations.vizion.service import release_vizion_reference

    return release_vizion_reference(subscription)


def _stop_traqo(subscription: TrackingSubscription) -> ProviderStopOutcome:
    """Untrack one Traqo shipment. Imported lazily, like every other provider call."""
    from apps.scm.integrations.traqo.service import release_traqo_shipment

    return release_traqo_shipment(subscription)


def _usage_traqo():
    """Read our Traqo account's allowance. Imported lazily to keep this module a leaf."""
    from apps.scm.integrations.traqo.usage import fetch_traqo_account_usage

    return fetch_traqo_account_usage()


_NON_CARRIER_SOURCES: dict[str, NonCarrierSource] = {
    TRAQO_PROVIDER_CODE: NonCarrierSource(
        code=TRAQO_PROVIDER_CODE,
        name=TRAQO_PROVIDER_NAME,
        read_payload=_read_traqo_payload,
        refresh_hint="refresh the container's tracking",
        scheduled_sync=_sync_traqo,
        # ``DELETE /shipments/{id}``, addressed by the container number the shipment is
        # tracked by. It ends the tracking, not the charge — the allowance counts
        # references added during the cycle, so the slot does not come back.
        stop_tracking=_stop_traqo,
        # ``GET /account/usage``, which is free at Traqo's end. Advisory only: it never
        # gates a Start, because the activation response is what actually decides and a
        # cached read cannot survive two concurrent starts against one remaining slot.
        account_usage=_usage_traqo,
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
        # The mirror image of the missing ``scheduled_sync``. Vizion is never polled, so
        # it costs nothing per cycle — but the reference it bills for stays subscribed
        # until it is released, so stopping a Vizion watch without telling Vizion is the
        # one stop that would keep charging.
        stop_tracking=_stop_vizion,
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


def get_provider_usage(provider_code: str):
    """Return what this provider says about our account with it, or None.

    None for a carrier — those are per-team integrations with no installation-wide
    allowance — and None for a non-carrier source that publishes no usage endpoint.

    The result describes the installation's own plan and spend, so every caller is
    responsible for keeping it out of tenant-facing responses. There is exactly one
    caller, and it is superuser-only: ``tracking/platform_views.py``.
    """
    source = get_non_carrier_source(provider_code)
    if source is None or source.account_usage is None:
        return None
    return source.account_usage()


def usage_reporting_provider_codes() -> tuple[str, ...]:
    """Provider codes that can report an account allowance, for the platform status page."""
    return tuple(code for code, source in _NON_CARRIER_SOURCES.items() if source.reports_usage)


def holds_provider_subscription(provider_code: str) -> bool:
    """Whether a watch on this provider stands for a resource held *at* the provider.

    True exactly where :func:`release_provider_subscription` has something to release, so
    a cancelled watch on such a provider is one whose external resource Stop has given up.
    Resuming that row alone would restore nothing at the provider; the lifecycle asks this
    to know when a restart has to go through activation instead.
    """
    source = get_non_carrier_source(provider_code)
    return source is not None and source.requires_external_stop


def release_provider_subscription(subscription: TrackingSubscription) -> ProviderStopOutcome:
    """Tell ``subscription``'s provider to stop watching, where it has to be told.

    The one place :mod:`apps.scm.tracking.lifecycle` asks "does stopping this cost a
    provider call", so the answer is a property of the source rather than an ``if
    provider.code == "vizion"`` in the lifecycle.

    ``STOP_NOT_REQUIRED`` for every provider that keeps no subscription of its own —
    which is every direct carrier and, today, Traqo. Never raises: a provider error
    becomes a ``STOP_FAILED`` outcome, because a stop that crashed the request would
    leave the watch in whatever state the exception interrupted.
    """
    source = get_non_carrier_source(subscription.provider.code if subscription.provider_id else "")
    if source is None or source.stop_tracking is None:
        return ProviderStopOutcome(state=STOP_NOT_REQUIRED)

    try:
        return source.stop_tracking(subscription)
    except Exception as exc:  # noqa: BLE001 — a stop must not fail as an exception
        logger.exception("Releasing the %s subscription for watch %s failed.", source.code, subscription.pk)
        return ProviderStopOutcome(state=STOP_FAILED, detail=f"{type(exc).__name__}: {exc}")
